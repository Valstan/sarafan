"""Предохранитель вызовов движка: под лимитом free-тира волна не ждёт впустую.

**Что измерено (21.10.2026, лог воркера за 48 часов).** Цепочка моделей
``deepseek-v4.1-flash-free`` → ``glm-5.3-flash-free`` → ``mimo-v2.6-flash-free``
получила 371 отказ ``http_429`` и 156 ``http_403``. Пересказ одного поста
занимал 30–120 секунд не потому, что модель думает: вызывающий по очереди
пробует три модели, при отказе по IP поворачивает выход ВПН и повторяет всю
цепочку заново. Фаза пересказа — 37–61 % всего времени воркера, а слот у него
один: пока он ждёт заведомо неудачный ответ, волны других районов стоят в
очереди, и посты выходят позже расписания.

**Класс отказа тихий.** Публикация не падает — везде fail-open, в ленту уходит
исходный текст. Поэтому «движок лимитит» выглядит как «всё работает, только
медленно», и заметить это можно лишь по длине очереди.

**Что делает предохранитель.** Считает подряд идущие отказы по лимиту/сети.
После ``ENGINE_BREAKER_FAILS`` (дефолт 3) объявляет движок недоступным на
``ENGINE_BREAKER_COOLDOWN`` секунд (дефолт 300): вызовы сразу возвращают отказ,
не трогая сеть, — волны идут по обычным фильтрам. Первый успешный вызов
закрывает предохранитель досрочно, поэтому после снятия лимита пересказ
возвращается сам, без рестарта.

**Почему в процессе, а не в Redis.** Воркер — один процесс на слот (prefork),
состояние нужно ровно этому процессу; разделяемое состояние потребовало бы
сетевого вызова на каждый чек — цена больше пользы. Плата: у web-процесса свой
предохранитель, и это нормально (там движок зовут кнопки, а не конвейер).

Env:
  ENGINE_BREAKER_ENABLED=1     # 0 — выключить (аварийный откат без деплоя)
  ENGINE_BREAKER_FAILS=3       # сколько отказов подряд открывают предохранитель
  ENGINE_BREAKER_COOLDOWN=300  # на сколько секунд он открывается
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Отказы, которые означают «движок сейчас недоступен», а не «ответ не по форме».
# ``llm_unparseable``/``shape:*``/``empty_response`` сюда НЕ входят: модель
# ответила, просто ответ негоден — это не повод глушить её на пять минут.
_THROTTLE_REASONS = frozenset(
    {
        "http_429",
        "http_403",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "network",
        "timeout",
        "engine_all_failed",
        "engine_disabled",
    }
)

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "failures": 0,
    "open_until": 0.0,
    "opens": 0,
    "skipped": 0,
    "last_reason": "",
}


def _enabled() -> bool:
    raw = (os.getenv("ENGINE_BREAKER_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _threshold() -> int:
    try:
        return max(1, int(os.getenv("ENGINE_BREAKER_FAILS", "3")))
    except ValueError:
        return 3


def _cooldown() -> float:
    try:
        return max(0.0, float(os.getenv("ENGINE_BREAKER_COOLDOWN", "300")))
    except ValueError:
        return 300.0


def _is_throttle(reason: str) -> bool:
    return (reason or "").strip() in _THROTTLE_REASONS


def allow() -> bool:
    """Можно ли сейчас звать движок. ``False`` — предохранитель открыт."""
    if not _enabled():
        return True
    now = time.monotonic()
    with _lock:
        if _state["open_until"] > now:
            _state["skipped"] += 1
            return False
        if _state["open_until"]:
            # Истёк — закрываем и начинаем считать заново.
            _state["open_until"] = 0.0
            _state["failures"] = 0
        return True


def note_result(ok: bool, reason: str = "") -> None:
    """Учесть исход вызова: успех закрывает, лимит — считает к открытию."""
    if not _enabled():
        return
    now = time.monotonic()
    with _lock:
        if ok:
            if _state["failures"] or _state["open_until"]:
                logger.info(
                    "engine breaker: движок ответил — предохранитель закрыт"
                    " (отказов подряд было %d)",
                    _state["failures"],
                )
            _state["failures"] = 0
            _state["open_until"] = 0.0
            return

        if not _is_throttle(reason):
            return

        _state["failures"] += 1
        _state["last_reason"] = reason
        if _state["failures"] < _threshold():
            return

        cooldown = _cooldown()
        if cooldown <= 0:
            return
        _state["open_until"] = now + cooldown
        _state["opens"] += 1
        _state["failures"] = 0
        logger.warning(
            "engine breaker: движок лимитит (%s) — на %.0f с вызовы пропускаются,"
            " волны идут по обычным фильтрам",
            reason,
            cooldown,
        )


def snapshot() -> Dict[str, Any]:
    """Состояние для страницы «Карта конвейера» и диагностики."""
    now = time.monotonic()
    with _lock:
        remaining = max(0.0, _state["open_until"] - now)
        return {
            "enabled": _enabled(),
            "open": _state["open_until"] > now,
            "remaining_seconds": round(remaining, 1),
            "failures_in_row": _state["failures"],
            "opens": _state["opens"],
            "skipped": _state["skipped"],
            "last_reason": _state["last_reason"],
            "threshold": _threshold(),
            "cooldown": _cooldown(),
        }


def reset() -> None:
    """Сбросить состояние (тесты и ручная диагностика)."""
    with _lock:
        _state.update(
            {"failures": 0, "open_until": 0.0, "opens": 0, "skipped": 0, "last_reason": ""}
        )


__all__ = ["allow", "note_result", "snapshot", "reset"]
