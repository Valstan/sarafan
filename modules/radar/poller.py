"""Fan-out поллер контент-радара (Ф0.2) + heartbeat/watchdog (#018).

Каждый активный источник с ≥1 подпиской поллится РОВНО один раз за прогон,
независимо от числа подписчиков (требование директивы brain 2026-06-11).
Новые элементы вставляются в общий seen-стор ``radar_items`` через
ON CONFLICT DO NOTHING по uniq (source_id, external_id) — повторный фетч
того же поста = no-op, курсоры не нужны.

**Источники поллятся параллельно, пачкой.** Замер 21.10: прогон стоил 78 с
при пяти источниках и занимал единственный слот воркера 144 раза в сутки
(187 мин — больше любой другой периодики). Причина была не в трафике, а в
последовательном обходе: телеграм-фетчер идёт через релей с таймаутом 45 с и
двумя попытками, то есть одна медленная лента держала остальные. Теперь фетчи
уходят в ``asyncio.gather`` под семафором, а записи остаются последовательными
в одной сессии (параллельные ``execute`` в одной сессии запрещены).

**Крутит его демон, а не beat.** Ждать сеть в очереди задач нельзя — это тот же
класс отказа, что интейки: ожидание занимает слот, а под переполненной очередью
тик истекает по ``expires`` и не выполняется вовсе. Поллер живёт третьим циклом
демона ``setka-radar-intake`` (``scripts/radar_intake_daemon.py``); Celery-таска
``poll_radar_sources`` осталась для ручного прогона и диагностики.

Liveness — по паттерну ``modules/bulletin_heartbeat.py`` (pool #018,
retired≠dead R6): успешный прогон пишет unix-ts в Redis
``setka:radar_last_polled``; watchdog алёртит ТОЛЬКО если есть активные
подписанные источники, а heartbeat протух — «нечего поллить» не считается
смертью.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "setka:radar_last_polled"
_HEARTBEAT_TTL_SECONDS = 14 * 24 * 3600

# Сколько источников опрашивать одновременно. Пять — по числу активных
# источников на проде 21.10; больше незачем, меньше смысла.
POLL_CONCURRENCY = max(1, int(os.getenv("RADAR_POLL_CONCURRENCY", "5")))

# Поллер идёт каждые 10 минут; 40 мин = 4 пропущенных прогона подряд.
DEFAULT_MAX_AGE_MINUTES = 40
ALERT_COOLDOWN_SECONDS = 6 * 3600
_ALERT_COOLDOWN_KEY = "setka:radar_poll_alert_cooldown"

_redis_client = None
_redis_pid: Optional[int] = None


def _redis():
    """Fork-safe Redis-клиент (PID-guard, как в bulletin_heartbeat — инцидент 2026-06-05)."""
    global _redis_client, _redis_pid
    pid = os.getpid()
    if _redis_client is None or _redis_pid != pid:
        try:
            from modules.notifications.storage import NotificationsStorage

            _redis_client = NotificationsStorage().redis_client
            _redis_pid = pid
        except Exception as e:  # noqa: BLE001 - heartbeat best-effort
            logger.warning("radar poller: redis init failed: %s", e)
            _redis_client = None
    return _redis_client


def touch_heartbeat() -> None:
    client = _redis()
    if client is None:
        return
    try:
        client.setex(HEARTBEAT_KEY, _HEARTBEAT_TTL_SECONDS, int(time.time()))
    except Exception as e:  # noqa: BLE001
        logger.warning("radar poller: heartbeat write failed: %s", e)


async def _fetch_source(source, fetcher, semaphore: asyncio.Semaphore):
    """Один источник: только сеть, без БД. Возвращает ``(source, items, error)``.

    Ошибку не бросаем: упавший источник не должен стоить остальных (прежняя
    семантика последовательного цикла), но и ждать его никто не обязан.
    """
    async with semaphore:
        try:
            return source, await fetcher(source), None
        except Exception as e:  # noqa: BLE001 - источник упал, прогон живёт
            return source, None, e


def _rows_for_source(source_id, items, *, now=None) -> tuple:
    """Элементы источника → строки для одного INSERT. Возвращает ``(rows, newest)``.

    Дубли внутри пачки снимаем здесь: ``ON CONFLICT`` разбирает конфликты с уже
    лежащими строками, а не с соседями по VALUES.
    """
    rows = []
    seen: set = set()
    newest: Optional[datetime] = None
    stamp = now or datetime.utcnow()
    for item in items:
        external_id = item.external_id
        if external_id in seen:
            continue
        seen.add(external_id)
        rows.append(
            {
                "source_id": source_id,
                "external_id": external_id,
                "url": item.url,
                "title": item.title,
                "text": item.text,
                "media": item.media or [],
                "published_at": item.published_at,
                "fetched_at": stamp,
            }
        )
        if item.published_at and (newest is None or item.published_at > newest):
            newest = item.published_at
    return rows, newest


async def _insert_items(session, source_id, items) -> tuple:
    """Вставить элементы источника одной пачкой. Возвращает ``(сколько новых, newest)``.

    Одна вставка на источник вместо запроса на элемент: при полном прогоне это
    десятки элементов, и каждый отдельный ``execute`` — круг до Postgres посреди
    той же транзакции.
    """
    rows, newest = _rows_for_source(source_id, items)
    if not rows:
        return 0, newest

    from database.models_extended import RadarItem

    result = await session.execute(
        pg_insert(RadarItem)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["source_id", "external_id"])
    )
    return max(int(getattr(result, "rowcount", 0) or 0), 0), newest


async def poll_all_sources(*, poll_concurrency: Optional[int] = None) -> dict:
    """Один прогон поллера: фетч всех активных подписанных источников.

    Возвращает сводку {sources, new_items, failed} для лога/таски.
    Падение одного источника не валит прогон: fail_count++/last_error на
    источнике, остальные поллятся дальше.
    """
    from database import models  # noqa: F401 - конфигурация мапперов (PR #189)
    from database.connection import AsyncSessionLocal
    from database.models_extended import RadarSource, RadarSubscription
    from modules.radar.sources import get_fetcher

    summary = {"sources": 0, "new_items": 0, "failed": 0}
    new_by_source: dict = {}  # source_id -> новых элементов (для web-push Ф0.5)

    async with AsyncSessionLocal() as session:
        sources = (
            (
                await session.execute(
                    select(RadarSource).where(
                        RadarSource.is_active.is_(True),
                        exists(
                            select(RadarSubscription.id).where(
                                RadarSubscription.source_id == RadarSource.id
                            )
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

        # Сеть — параллельно, БД — последовательно.
        #
        # Замер 21.10: прогон стоил 78 с при ПЯТИ активных источниках, и эти
        # 78 с занимали единственный слот воркера 144 раза в сутки (187 мин,
        # больше любой другой периодики). Причина не в объёме, а в том, что
        # источники поллились по очереди: у телеграм-релейного фетчера таймаут
        # 45 с и две попытки, то есть одна медленная лента держала остальные.
        # Сессия БД между фетчами не нужна вовсе — источник читается один раз
        # выше, а записи идут после. Поэтому фетчи отпускаем в gather, а записи
        # остаются в одной сессии (одна сессия = одно соединение, параллельные
        # execute в ней запрещены).
        pairs = []
        for source in sources:
            fetcher = get_fetcher(source.type)
            if fetcher is None:  # tg до Ф0.3 — пропускаем молча, это не ошибка
                continue
            pairs.append((source, fetcher))
        summary["sources"] = len(pairs)

        concurrency = poll_concurrency if poll_concurrency is not None else POLL_CONCURRENCY
        semaphore = asyncio.Semaphore(max(1, concurrency))
        fetched = await asyncio.gather(
            *(_fetch_source(source, fetcher, semaphore) for source, fetcher in pairs)
        )

        for source, items, error in fetched:
            if error is not None:
                source.fail_count = (source.fail_count or 0) + 1
                source.last_error = str(error)[:512]
                summary["failed"] += 1
                logger.warning(
                    "radar poll failed for %s:%s (fail #%d): %s",
                    source.type,
                    source.key,
                    source.fail_count,
                    error,
                )
                continue

            new_count, newest = await _insert_items(session, source.id, items)
            source.last_polled_at = datetime.utcnow()
            source.fail_count = 0
            source.last_error = None
            if newest and (source.last_item_at is None or newest > source.last_item_at):
                source.last_item_at = newest
            summary["new_items"] += new_count
            if new_count:
                new_by_source[source.id] = new_count

        await session.commit()

    # Web-push (Ф0.5) — после коммита, best-effort: сбой пуша не ломает прогон.
    if new_by_source:
        try:
            from modules.radar.push import notify_new_items

            push_summary = await notify_new_items(new_by_source)
            if push_summary.get("sent") or push_summary.get("dropped"):
                logger.info("radar push: %s", push_summary)
        except Exception as e:  # noqa: BLE001
            logger.warning("radar push hook failed: %s", e)

        # Доставка во внешние выводы (кабинет, миграция 045) — тоже после коммита,
        # best-effort и под аварийным kill-switch. Курсор at-most-once независим
        # от new_by_source: модуль сам выбирает новые элементы по per-output курсору.
        try:
            from config.runtime import radar_delivery_disabled

            if not radar_delivery_disabled():
                from modules.radar.delivery import deliver_new_items

                delivery_summary = await deliver_new_items()
                if delivery_summary.get("delivered") or delivery_summary.get("failed"):
                    logger.info("radar delivery: %s", delivery_summary)
        except Exception as e:  # noqa: BLE001
            logger.warning("radar delivery hook failed: %s", e)

    # Heartbeat пишем и при 0 источников: «поллер жив» ≠ «есть что поллить».
    touch_heartbeat()
    logger.info("radar poll done: %s", summary)
    return summary


async def cleanup_old_items(retention_days: Optional[int] = None) -> dict:
    """Удалить элементы ленты старше порога (ретенция, план Ф0: 30 дней).

    Сохранёнки переживают чистку by design: radar_saved — снимок контента,
    FK item_id имеет ON DELETE SET NULL.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    from database import models  # noqa: F401
    from database.connection import AsyncSessionLocal
    from database.models_extended import RadarItem

    days = retention_days or int(os.getenv("RADAR_ITEMS_RETENTION_DAYS", "30"))
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with AsyncSessionLocal() as session:
        result = await session.execute(delete(RadarItem).where(RadarItem.fetched_at < cutoff))
        await session.commit()
    return {"deleted": result.rowcount or 0, "retention_days": days}


async def _has_pollable_sources() -> bool:
    from database import models  # noqa: F401
    from database.connection import AsyncSessionLocal
    from database.models_extended import RadarSource, RadarSubscription

    async with AsyncSessionLocal() as session:
        row = await session.execute(
            select(RadarSource.id)
            .where(
                RadarSource.is_active.is_(True),
                RadarSource.type.in_(("vk", "rss", "tg")),
                exists(
                    select(RadarSubscription.id).where(
                        RadarSubscription.source_id == RadarSource.id
                    )
                ),
            )
            .limit(1)
        )
        return row.first() is not None


async def maybe_alert_stale_radar_poll(
    *,
    telegram_token: Optional[str],
    chat_id: Optional[str],
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
) -> str:
    """Watchdog: алёрт, если есть что поллить, а heartbeat протух.

    Возвращает статус-строку для лога: ok|no-sources|stale-alerted|
    stale-cooldown|no-heartbeat-alerted|skipped.
    """
    client = _redis()
    if client is None:
        return "skipped"

    if not await _has_pollable_sources():
        return "no-sources"  # retired≠dead (R6): пустой радар — не инцидент

    raw = client.get(HEARTBEAT_KEY)
    age_seconds = None if raw is None else int(time.time()) - int(raw)
    if age_seconds is not None and age_seconds < max_age_minutes * 60:
        return "ok"

    if client.get(_ALERT_COOLDOWN_KEY):
        return "stale-cooldown"

    if not telegram_token or not chat_id:
        return "stale-no-telegram"

    age_text = "никогда" if age_seconds is None else f"{age_seconds // 60} мин назад"
    text = (
        "📡⚠️ <b>Контент-радар: поллер молчит</b>\n"
        f"Последний успешный прогон: {age_text} (порог {max_age_minutes} мин).\n"
        "Проверь celery-worker/beat и логи radar poll."
    )
    try:
        from modules import telegram_http as tg_http

        tg_http.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
        client.setex(_ALERT_COOLDOWN_KEY, ALERT_COOLDOWN_SECONDS, 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("radar watchdog: telegram alert failed: %s", e)
        return "alert-failed"
    return "stale-alerted" if age_seconds is not None else "no-heartbeat-alerted"
