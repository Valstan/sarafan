"""Redis-heartbeat «последняя успешная публикация сводки» + watchdog-алёрт.

**Зачем не Prometheus:** gauge ``setka_digest_last_published_timestamp`` на
проде ненадёжен (multiproc-mmap пуст несмотря на реальные публикации — давняя
боль вокруг PR #75). Для алёрта «давно нет сводок» нужен простой надёжный
сигнал: пишем unix-ts в Redis из единой точки
``monitoring.metrics.track_digest_published`` (она вызывается на ВСЕХ путях
публикации — theme-волны и каскад), а beat-watchdog читает и при протухании
шлёт Telegram-алёрт.

Ключ ``setka:digest_last_published:<topic>`` (Redis db=1, как у
``NotificationsStorage`` — переиспользуем его клиент, чтобы не плодить
коннект-параметры).

**Второй ключ — по региону** (P169, 2026-09-21):
``setka:digest_last_published:region:<region>:<topic>``. Тематический ключ
слеп к региону: пока хоть один район публикует ``novost``, сторож рапортует
``fresh`` — 20–21.08 Арбаж и Подосиновец просидели сутки без единой сводки
из шести слотов, и сторож не покраснел ни разу. Порог по региону —
``DEFAULT_REGION_MAX_AGE_HOURS``: все районы ходят по одним шести слотам
``novost`` в сутки, так что «пропущены все слоты подряд» = сутки с запасом.
Регион без ключа не алёртит (тот же принцип, что у темы: тонкий район или
свежий регион ≠ поломка) — это записанный компромисс, и его цена измерена
2026-09-22: район, чей поток умер до выката ключа, невидим, пока не
опубликует хоть раз. Отключённые регионы отсекает **только** фильтр
``active_regions``, который вызывающий обязан передать; без него
``maybe_alert_stale_regions`` молчит (fail-closed), а не кричит нефильтрованным
списком.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "setka:digest_last_published"

# TTL ключа — санитар для ИСЧЕЗНУВШИХ кодов регионов (район удалён или
# переименован), а не защита от выключенных: тех отсекает фильтр
# ``active_regions``, и держать вторую защиту от того же — платить дважды.
#
# Прежние 14 суток при пороге 26 ч давали амнезию: район, который опубликовал
# и умер, кричал две недели, потом ключ истекал, регион выпадал из скана — и
# сторож возвращался к ``fresh`` по тому самому району, о котором кричал.
# Сигнал гас тем вернее, чем дольше длился отказ, а со стороны это выглядело
# самопочинкой (замер 2026-09-22: Подосиновец, novost мёртв с 16.09).
# На порог и на состав алёрта TTL не влияет — протухание считается по возрасту
# значения; TTL решает только, существует ли ключ вообще.
_HEARTBEAT_TTL_SECONDS = 90 * 24 * 3600

# Cooldown алёрта — не спамить, пока простой длится.
ALERT_COOLDOWN_SECONDS = 6 * 3600

# Порог простоя по умолчанию (часы). novost-волны идут 6×/сутки
# (6:40/10:40/13:40/16:40/18:40/20:40 MSK), макс. дневной зазор ~4ч
# (6:40→10:40) + разнос регионов на 50 мин → 6ч с запасом не даёт ложных
# срабатываний. Ночной зазор (20:40→6:40) сторож не видит: он ходит
# с 10:00 до 22:00.
DEFAULT_MAX_AGE_HOURS = 6

# Порог простоя ОДНОГО региона (часы). У всех районов одни и те же 6 слотов
# novost в сутки; последняя удача в 6:40 + пропуск всех шести следующих слотов
# = ~27.4 ч к утренней проверке. 26 ч = «все слоты подряд пропущены», и это
# же число — порог свежести дашборда.
DEFAULT_REGION_MAX_AGE_HOURS = 26

_REGION_SEGMENT = "region"

_redis_client = None
_redis_pid: Optional[int] = None


def _redis():
    """Лениво-кэшированный, **fork-safe** Redis-клиент (db=1, decode_responses).

    Переиспользует параметры подключения ``NotificationsStorage`` — единый
    источник настроек Redis для app-level ключей.

    **PID-guard:** клиент кэшируется вместе с PID процесса и пересоздаётся,
    если PID сменился (целевой процесс — форк, как Celery prefork-worker:
    redis-py connection pool не fork-safe). Сбой инициализации логируется на
    **WARNING** (раньше debug → невидимо при прод LOG_LEVEL=INFO, из-за чего
    «heartbeat молчит» пряталось — инцидент 2026-06-05).
    """
    global _redis_client, _redis_pid
    pid = os.getpid()
    if _redis_client is None or _redis_pid != pid:
        try:
            from modules.notifications.storage import NotificationsStorage

            _redis_client = NotificationsStorage().redis_client
            _redis_pid = pid
        except Exception:  # pragma: no cover - инфраструктурный сбой
            logger.warning("redis init failed for bulletin heartbeat", exc_info=True)
            _redis_client = None
            return None
    return _redis_client


def _region_key(region: str, topic: str) -> str:
    return f"{KEY_PREFIX}:{_REGION_SEGMENT}:{region}:{topic}"


def mark_published(topic: str, *, region: Optional[str] = None, ts: Optional[float] = None) -> None:
    """Отметить успешную публикацию сводки темы (best-effort, не падает).

    Вызывается из ``track_digest_published`` при ``result == "success"``.
    С ``region`` пишется и второй ключ — по паре регион/тема (P169); без него
    только тематический, как раньше.
    """
    if not topic:
        return
    try:
        client = _redis()
        if client is None:
            # Громко (WARNING): немой `return` здесь скрывал, что heartbeat не
            # пишется, хотя публикация прошла (инцидент 2026-06-05).
            logger.warning("bulletin heartbeat skipped: redis unavailable (topic=%s)", topic)
            return
        value = str(int(ts if ts is not None else time.time()))
        client.setex(f"{KEY_PREFIX}:{topic}", _HEARTBEAT_TTL_SECONDS, value)
        if region:
            client.setex(_region_key(region, topic), _HEARTBEAT_TTL_SECONDS, value)
    except Exception:  # pragma: no cover - наблюдаемость не должна ломать публикацию
        # WARNING (не debug): на проде LOG_LEVEL=INFO глушил debug, из-за чего
        # «heartbeat не пишется» оставалось незамеченным (инцидент 2026-06-05).
        logger.warning("bulletin heartbeat write failed (%s)", topic, exc_info=True)


def last_published_ts(topic: str) -> Optional[int]:
    """Unix-ts последней успешной публикации темы, либо ``None``."""
    try:
        client = _redis()
        if client is None:
            return None
        val = client.get(f"{KEY_PREFIX}:{topic}")
        return int(val) if val else None
    except Exception:
        logger.debug("bulletin heartbeat read failed (%s)", topic, exc_info=True)
        return None


def all_heartbeats() -> dict[str, int]:
    """Все ``topic → unix-ts`` из Redis (best-effort, не падает).

    Сканирует ключи ``setka:digest_last_published:*``, исключая служебные
    cooldown-ключи (``…:stale_alert_cooldown:<topic>``). Возвращает ``{}`` при
    любом инфраструктурном сбое — наблюдаемость не должна валить вызывающего.
    Используется дашбордом (``/api/monitoring/heartbeat``) для показа свежести
    публикаций по всем темам разом.
    """
    out: dict[str, int] = {}
    try:
        client = _redis()
        if client is None:
            return out
        prefix = f"{KEY_PREFIX}:"
        scan_iter = getattr(client, "scan_iter", None)
        keys = scan_iter(match=f"{prefix}*") if callable(scan_iter) else client.keys(f"{prefix}*")
        for raw in keys:
            key = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            topic = key[len(prefix) :]
            # cooldown-ключи начинаются с "stale_alert_cooldown:", региональные —
            # с "region:" — не темы
            if (
                not topic
                or topic.startswith("stale_alert_cooldown")
                or topic.startswith(_REGION_SEGMENT + ":")
            ):
                continue
            val = client.get(key)
            if val is None:
                continue
            try:
                out[topic] = int(val)
            except (TypeError, ValueError):
                continue
    except Exception:  # pragma: no cover - инфраструктурный сбой
        logger.debug("bulletin heartbeat scan failed", exc_info=True)
    return out


def all_region_heartbeats(topic: str) -> dict[str, int]:
    """``region → unix-ts`` последней успешной публикации темы по регионам (P169).

    Best-effort: ``{}`` при любом сбое. Регион без ключа в словарь не попадает —
    «никогда не публиковал» и «свежий деплой» здесь неразличимы и не считаются
    отказом (тот же принцип, что у тематического watchdog'а).
    """
    out: dict[str, int] = {}
    if not topic:
        return out
    try:
        client = _redis()
        if client is None:
            return out
        prefix = f"{KEY_PREFIX}:{_REGION_SEGMENT}:"
        suffix = f":{topic}"
        scan_iter = getattr(client, "scan_iter", None)
        keys = (
            scan_iter(match=f"{prefix}*{suffix}")
            if callable(scan_iter)
            else client.keys(f"{prefix}*")
        )
        for raw in keys:
            key = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
            if not key.endswith(suffix):
                continue
            region = key[len(prefix) : -len(suffix)]
            if not region or ":" in region:
                continue
            val = client.get(key)
            if val is None:
                continue
            try:
                out[region] = int(val)
            except (TypeError, ValueError):
                continue
    except Exception:  # pragma: no cover - инфраструктурный сбой
        logger.debug("bulletin region heartbeat scan failed", exc_info=True)
    return out


def stale_regions(
    topic: str = "novost",
    *,
    max_age_hours: float = DEFAULT_REGION_MAX_AGE_HOURS,
    active_regions: Optional[set[str]] = None,
    now: Optional[float] = None,
) -> list[tuple[str, int]]:
    """Регионы, у которых последняя удача темы старше порога: ``[(region, age_s)]``.

    ``active_regions`` — фильтр по живым регионам: ключ живёт 14 дней и без
    фильтра выключенный район кричал бы две недели. ``None`` = не фильтровать.
    Сортировка — самые давние сверху.
    """
    current = now if now is not None else time.time()
    out: list[tuple[str, int]] = []
    for region, ts in all_region_heartbeats(topic).items():
        if active_regions is not None and region not in active_regions:
            continue
        age = current - ts
        if age >= max_age_hours * 3600:
            out.append((region, int(age)))
    out.sort(key=lambda item: -item[1])
    return out


def maybe_alert_stale_regions(
    *,
    topic: str = "novost",
    max_age_hours: float = DEFAULT_REGION_MAX_AGE_HOURS,
    active_regions: Optional[set[str]] = None,
    telegram_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
    now: Optional[float] = None,
) -> str:
    """Один Telegram-алёрт со списком районов, где тема протухла дольше порога.

    Статусы: ``fresh`` (протухших нет) | ``skipped:no-active-regions`` |
    ``skipped:no-telegram-config`` | ``skipped:cooldown`` | ``alert-sent:<N>`` |
    ``error:…``. Cooldown общий на список
    (``…:stale_alert_cooldown:regions:<topic>``), тот же 6 ч.

    **Fail-closed при ``active_regions=None``.** Здесь, в отличие от
    ``stale_regions``, «не знаю списка активных» — не то же самое, что «не
    фильтровать»: без списка в алёрт уехали бы давно погашенные районы, и с
    TTL в 90 суток — надолго. Молчание дешевле ложного крика: список не
    приходит только когда БД недоступна, а тогда волна ``novost`` всё равно не
    идёт и тематический сторож (порог 6 ч, свой алёрт) покраснеет сам.
    ``stale_regions`` семантику ``None = не фильтровать`` сохраняет — на ней
    стоят дашборд и тесты.
    """
    if active_regions is None:
        return "skipped:no-active-regions"

    stale = stale_regions(
        topic, max_age_hours=max_age_hours, active_regions=active_regions, now=now
    )
    if not stale:
        return "fresh"

    if not telegram_token or not chat_id:
        return "skipped:no-telegram-config"

    client = _redis()
    cooldown_key = f"{KEY_PREFIX}:stale_alert_cooldown:{_REGION_SEGMENT}s:{topic}"
    try:
        if client is not None and client.get(cooldown_key):
            return "skipped:cooldown"
    except Exception:
        pass

    lines = [f"• <b>{region}</b> — {age / 3600.0:.1f} ч" for region, age in stale]
    parts = [
        "⚠️ <b>SETKA: районы без сводок</b>\n",
        f"Тема <b>{topic}</b> не выходила дольше <b>{max_age_hours:g} ч</b> "
        f"(все слоты подряд) в {len(stale)} рег.:",
        "\n".join(lines),
        "\nОбщий поток жив — смотреть сам район: пул источников, "
        "кандидаты после отбора, ключ COMM_&lt;id&gt;.",
    ]
    if dashboard_url:
        parts.append(f"\n🔗 <a href='{dashboard_url}'>Открыть SETKA</a>")
    message = "\n".join(parts)

    try:
        from modules import telegram_http as tg_http

        resp = tg_http.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            logger.warning("stale-regions alert failed: %s %s", resp.status_code, resp.text[:200])
            return "error:http-" + str(resp.status_code)
        if client is not None:
            client.setex(cooldown_key, ALERT_COOLDOWN_SECONDS, "1")
        logger.info(
            "Sent stale-regions alert for topic=%s regions=%s",
            topic,
            ",".join(region for region, _ in stale),
        )
        return f"alert-sent:{len(stale)}"
    except Exception as exc:
        logger.error("Failed to send stale-regions alert: %s", exc)
        return "error:" + str(exc)


def maybe_alert_stale_bulletin(
    *,
    topic: str = "novost",
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    telegram_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
    now: Optional[float] = None,
) -> str:
    """Если heartbeat темы старше порога — Telegram-алёрт (с cooldown).

    Возвращает статус-строку: ``fresh`` | ``unknown:no-heartbeat`` |
    ``skipped:no-telegram-config`` | ``skipped:cooldown`` | ``alert-sent`` |
    ``error:…``.

    **None heartbeat НЕ алёртит**: нельзя отличить «свежий деплой, ещё не было
    волны» от «сломано навсегда». Алёртим только на ПРОТУХШИЙ существующий
    heartbeat (был сигнал → пропал = что-то сломалось в beat/worker). novost
    пишется ≥6×/сутки, так что heartbeat появляется в первые часы после деплоя.
    """
    current = now if now is not None else time.time()
    ts = last_published_ts(topic)
    if ts is None:
        return "unknown:no-heartbeat"

    age = current - ts
    if age < max_age_hours * 3600:
        return "fresh"

    if not telegram_token or not chat_id:
        return "skipped:no-telegram-config"

    client = _redis()
    cooldown_key = f"{KEY_PREFIX}:stale_alert_cooldown:{topic}"
    try:
        if client is not None and client.get(cooldown_key):
            return "skipped:cooldown"
    except Exception:
        pass

    hours = age / 3600.0
    parts = [
        "⚠️ <b>SETKA: давно нет сводок</b>\n",
        f"Тема <b>{topic}</b> не публиковалась <b>{hours:.1f} ч</b> " f"(порог {max_age_hours} ч).",
        "\nВероятно, упал beat/worker или все публикации падают. Проверь: "
        "<code>systemctl status setka-celery-beat setka-celery-worker</code>.",
    ]
    if dashboard_url:
        parts.append(f"\n🔗 <a href='{dashboard_url}'>Открыть SETKA</a>")
    message = "\n".join(parts)

    try:
        from modules import telegram_http as tg_http

        resp = tg_http.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            logger.warning("stale-bulletin alert failed: %s %s", resp.status_code, resp.text[:200])
            return "error:http-" + str(resp.status_code)
        if client is not None:
            client.setex(cooldown_key, ALERT_COOLDOWN_SECONDS, "1")
        logger.info("Sent stale-bulletin alert for topic=%s age=%.1fh", topic, hours)
        return "alert-sent"
    except Exception as exc:
        logger.error("Failed to send stale-bulletin alert: %s", exc)
        return "error:" + str(exc)
