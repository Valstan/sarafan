"""Отметка о прохождении пилота: время прогона владельцу в Telegram.

**Что это.** Пока DeepSeek отвечает 402 (P177), очередь классификатора
разбирают сессии владельца — вручную, по промпту. Раньше время последнего
разбора нигде не фиксировалось, и «пора ли уже новый» отвечал только
перечитыванием истории: это ровно класс «дата, которую читает лишь человек»
(pool #152).

**Где вешается.** На ``POST /api/classifier/verdicts`` — точку, которую
проходит ЛЮБОЙ разбор (сессия владельца, облачная рутина когда её
оживут). Ответ уходит сразу, отметка — фоновой задачей после ответа:
сетевой сбой Telegram не должен задерживать приём вердиктов.

**Почему только при ``recorded > 0``.** Ответ на повторный submit того же
батча даёт ``recorded=0, skipped_existing=N`` — это не новый прогон. На
живом прогоне 05.10 первый POST ушёл в таймаут чтения, а повтор показал
``skipped_existing: 79``: без этого условия один прогон отметил бы время
дважды, и хронология врёт.

**Флаж.** ``CLASSIFIER_PILOT_NOTIFY`` — 1 включает отметку. Читается на
каждом вызове через :func:`notify_enabled`, а не на импорте: переключение
не требует рестарта сервисов. По умолчанию выключено — чтобы фича молчала
до явного решения владельца.

Env:
  CLASSIFIER_PILOT_NOTIFY=0     # 1/true/yes/on — слать отметку в Telegram
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Iterable, List, Optional, Sequence, Set

logger = logging.getLogger(__name__)

# Имя прогона в сообщении. Одно слово — чтобы читалось с телефона одной строкой.
RUN_LABEL = "ПРОГОН"
# Отметка последнего прогона в Redis. Без TTL: хронология не должна
# протухать, потеря ключа означает лишь «прошлого прогона не было».
LAST_RUN_KEY = "setka:pilot_classifier:last_run"

_TRUE_VALUES = ("1", "true", "yes", "on")


def notify_enabled() -> bool:
    """Включены ли отметки о прогонах (env ``CLASSIFIER_PILOT_NOTIFY``)."""
    return os.getenv("CLASSIFIER_PILOT_NOTIFY", "0").strip().lower() in _TRUE_VALUES


def regions_from(verdicts: Iterable[Any]) -> List[str]:
    """Регионы батча в порядке первого появления — для строки в сообщении."""
    seen: Set[str] = set()
    out: List[str] = []
    for v in verdicts or []:
        code = str((v or {}).get("region_code") or "").strip() if isinstance(v, dict) else ""
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def actions_histogram(verdicts: Iterable[Any]) -> dict:
    """Счётчик действий батча: ``{"publish": n, "delete": n, "hold": n}``.

    Нулевые действия не печатаем — построим строку через :func:`format_actions`.
    """
    hist: dict = {}
    for v in verdicts or []:
        if not isinstance(v, dict):
            continue
        action = str(v.get("action") or "").strip()
        if action:
            hist[action] = hist.get(action, 0) + 1
    return hist


def format_elapsed(seconds: Optional[float]) -> str:
    """Сколько прошло с прошлого прогона по-русски: ``2ч15м``, ``45м``, ``1д3ч``.

    ``None``/отрицательное значение → «прошлого прогона не было»: так читается
    честнее, чем ври��ка про ноль минут.
    """
    if seconds is None or seconds < 0:
        return "прошлого прогона не было"
    total = int(seconds)
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}д{hours}ч" if hours else f"{days}д"
    if hours:
        return f"{hours}ч{minutes}м" if minutes else f"{hours}ч"
    if minutes:
        return f"{minutes}м"
    return f"{sec}с"


def format_actions(hist: dict) -> str:
    """``publish 40 / delete 38 / hold 1`` — только ненулевые."""
    parts = [f"{a} {n}" for a, n in hist.items() if n]
    return " / ".join(parts) if parts else "нет"


def build_message(
    *,
    recorded: int,
    regions: Sequence[str],
    actions: dict,
    elapsed_seconds: Optional[float],
    stamp: str,
) -> str:
    """Строка отметки. ``stamp`` — уже отформатированное локальное время."""
    lines = [
        f"{RUN_LABEL} {stamp} · {recorded} вердиктов",
        f"Районы: {', '.join(regions) if regions else '—'}",
        f"Решения: {format_actions(actions)}",
        f"С прошлого: {format_elapsed(elapsed_seconds)}",
    ]
    return "\n".join(lines)


async def send_to_owner(text: str) -> bool:
    """Отправить сообщение владельцу в Telegram. True — ушло.

    Повтор на сетевой сбой — через общий :mod:`modules.telegram_http`, чтобы
    не плодить вторую копию правил G307. Любая ошибка глушится: отметка о
    прогоне не имеет права ронять приём вердиктов.
    """
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules import telegram_http as tg_http

        tokens = [t for t in (TELEGRAM_TOKENS or {}).values() if t]
        bot_token = tokens[0] if tokens else None
        if not bot_token or not TELEGRAM_ALERT_CHAT_ID:
            logger.info("pilot run note skipped: telegram not configured")
            return False
        resp = await asyncio.to_thread(
            tg_http.post,
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": TELEGRAM_ALERT_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        if not resp.ok:
            logger.warning("pilot run note: telegram answered %s", resp.status_code)
            return False
        logger.info("pilot run note sent: %s", text.splitlines()[0])
        return True
    except Exception:  # noqa: BLE001 — отметка не роняет приём вердиктов
        logger.warning("pilot run note failed", exc_info=True)
        return False


async def note_run(
    *,
    recorded: int,
    verdicts: Iterable[Any],
    redis_client: Any = None,
    now_ts: float = 0.0,
    stamp: Optional[str] = None,
) -> str:
    """Отметить прогон: прочитать прошлый, посчитать прошлое, отправить, записать.

    ``recorded`` — только что записанные вердикты. Ноль означает повторный
    submit того же батча (см. докстринг модуля) — тогда не отмечаемся вовсе,
    но ключ всё равно дочитываем, чтобы не плодить запись.

    ``stamp``/``now_ts`` — внедряются в тестах, чтобы не зависеть от часов.
    """
    if not notify_enabled():
        return "skipped:disabled"
    if recorded <= 0:
        return "skipped:no-new-verdicts"

    regions = regions_from(verdicts)
    actions = actions_histogram(verdicts)
    ts = now_ts or time.time()
    stamp = stamp or time.strftime("%d.%m %H:%M", time.localtime(ts))

    elapsed: Optional[float] = None
    previous = None
    if redis_client is not None:
        try:
            previous = redis_client.get(LAST_RUN_KEY)
        except Exception:  # noqa: BLE001 — метка не обязана ломать прогон
            logger.warning("pilot run: reading last run failed", exc_info=True)
    if previous:
        try:
            prev_ts = float(previous)
        except (TypeError, ValueError):
            prev_ts = None
        if prev_ts is not None:
            elapsed = max(0.0, ts - prev_ts)

    message = build_message(
        recorded=recorded,
        regions=regions,
        actions=actions,
        elapsed_seconds=elapsed,
        stamp=stamp,
    )
    sent = await send_to_owner(message)

    if redis_client is not None:
        try:
            redis_client.set(LAST_RUN_KEY, str(ts))
        except Exception:  # noqa: BLE001
            logger.warning("pilot run: writing last run failed", exc_info=True)
    return "note-sent" if sent else "note-failed"


__all__ = [
    "LAST_RUN_KEY",
    "RUN_LABEL",
    "actions_histogram",
    "build_message",
    "format_actions",
    "format_elapsed",
    "note_run",
    "notify_enabled",
    "regions_from",
    "send_to_owner",
]
