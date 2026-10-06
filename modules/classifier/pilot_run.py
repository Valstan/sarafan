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

**Формат.** Только время ТЕКУЩЕГО прогона, явно по Москве — Telegram и есть
журнал, владелец смотрит последнее сообщение и сам решает, пора ли новый:

    ПРОГОН 06.10 08:19 МСК · 72 вердикта
    Районы: mi, ur, klz
    Решения: publish 24 / delete 48 / hold 0

Строки «сколько прошло с прошлого» здесь НЕТ намеренно: интервал владелец
считает в голове, а лишний расчёт — лишний повод соврать. Первая версия
отметки его считала через Redis-ключ и на живом прогоне 06.10 соврала
(синхронный вызов на асинхронном клиенте, ``RuntimeWarning: coroutine
'Redis.execute_command' was never awaited``): ради одной строки тащить
хранилище оказалось дороже самой строки. Redis из отметки убран полностью.

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
import datetime
import logging
import os
import time
import zoneinfo
from typing import Any, Iterable, List, Sequence

logger = logging.getLogger(__name__)

# Имя прогона в сообщении. Одно слово — чтобы читалось с телефона одной строкой.
RUN_LABEL = "ПРОГОН"
# Часовой пояс отметки. Явно, а не «локальное время сервера»: сервер сегодня
# в Москве, а отметка должна пережить любой переезд бокса.
MOSCOW_TZ = zoneinfo.ZoneInfo("Europe/Moscow")

_TRUE_VALUES = ("1", "true", "yes", "on")


def notify_enabled() -> bool:
    """Включены ли отметки о прогонах (env ``CLASSIFIER_PILOT_NOTIFY``)."""
    return os.getenv("CLASSIFIER_PILOT_NOTIFY", "0").strip().lower() in _TRUE_VALUES


def regions_from(verdicts: Iterable[Any]) -> List[str]:
    """Регионы батча в порядке первого появления — для строки в сообщении."""
    seen = set()
    out: List[str] = []
    for v in verdicts or []:
        code = str((v or {}).get("region_code") or "").strip() if isinstance(v, dict) else ""
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def actions_histogram(verdicts: Iterable[Any]) -> dict:
    """Счётчик действий батча: ``{"publish": n, "delete": n, "hold": n}``."""
    hist: dict = {}
    for v in verdicts or []:
        if not isinstance(v, dict):
            continue
        action = str(v.get("action") or "").strip()
        if action:
            hist[action] = hist.get(action, 0) + 1
    return hist


def moscow_stamp(ts: float) -> str:
    """Время прогона по Москве: ``06.10 08:19 МСК``. Суффикс — часть формата:
    без него через полгода никто не вспомнит, в чьём поясе отметка."""
    return datetime.datetime.fromtimestamp(ts, MOSCOW_TZ).strftime("%d.%m %H:%M МСК")


def format_actions(hist: dict) -> str:
    """``publish 40 / delete 38 / hold 1`` — только ненулевые."""
    parts = [f"{a} {n}" for a, n in hist.items() if n]
    return " / ".join(parts) if parts else "нет"


def build_message(
    *,
    recorded: int,
    regions: Sequence[str],
    actions: dict,
    stamp: str,
) -> str:
    """Строка отметки. ``stamp`` — уже отформатированное московское время."""
    lines = [
        f"{RUN_LABEL} {stamp} · {recorded} вердиктов",
        f"Районы: {', '.join(regions) if regions else '—'}",
        f"Решения: {format_actions(actions)}",
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
    now_ts: float = 0.0,
) -> str:
    """Отметить прогон: собрать сообщение с московским временем и отправить.

    ``recorded`` — только что записанные вердикты. Ноль означает повторный
    submit того же батча (см. докстринг модуля) — тогда не отмечаемся вовсе.

    ``now_ts`` — внедряется в тестах, чтобы не зависеть от часов.
    """
    if not notify_enabled():
        return "skipped:disabled"
    if recorded <= 0:
        return "skipped:no-new-verdicts"

    message = build_message(
        recorded=recorded,
        regions=regions_from(verdicts),
        actions=actions_histogram(verdicts),
        stamp=moscow_stamp(now_ts or time.time()),
    )
    sent = await send_to_owner(message)
    return "note-sent" if sent else "note-failed"


__all__ = [
    "MOSCOW_TZ",
    "RUN_LABEL",
    "actions_histogram",
    "build_message",
    "format_actions",
    "moscow_stamp",
    "note_run",
    "notify_enabled",
    "regions_from",
    "send_to_owner",
]
