"""
Async runner helpers for Celery workers.

Why this exists:
- Many Celery tasks in SETKA are synchronous but call async code
  (SQLAlchemy async, VK async clients, etc.).
- Using `asyncio.run()` inside Celery tasks creates a NEW event loop every call.
- SQLAlchemy asyncpg connections (and the async engine pool) are bound to the
  loop they were created in. Reusing pooled connections across different loops
  leads to errors like: "got Future attached to a different loop" /
  "Event loop is closed".

Solution:
- Keep ONE event loop per worker process and reuse it for all coroutine executions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import traceback
from typing import Any, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_loop: Optional[asyncio.AbstractEventLoop] = None

#: asyncio выставляет ``loop._thread_id`` на время ``run_until_complete`` —
#: по нему видно, ЧЕЙ поток занял петлю.
_BUSY_HOLDER_THREAD_ATTR = "_thread_id"


def _thread_name(ident: Optional[int]) -> str:
    """Имя потока по идентификатору (``unknown`` — поток уже умер)."""
    if ident is None:
        return "unknown"
    for thread in threading.enumerate():
        if thread.ident == ident:
            return thread.name
    return "unknown"


def _holder_stack(ident: Optional[int], limit: int = 6) -> str:
    """Короткий стек потока-держателя — где именно он висит.

    ``sys._current_frames()`` — единственный способ посмотреть стек ЧУЖОГО
    потока из Python без отладчика: держатель может быть не нами (другой
    воркер, executor-поток), и без стека запись «кто держит цикл» бесполезна.
    """
    if ident is None:
        return "нет данных"
    frame = sys._current_frames().get(ident)
    if frame is None:
        return "поток не найден"
    stack = traceback.extract_stack(frame)
    tail = stack[-limit:]
    return " | ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in tail)


def _loop_holder_info(loop: asyncio.AbstractEventLoop) -> str:
    """Кто держит общий цикл — для диагностики отказа.

    Отдаёт идентификатор и имя потока-держателя, поток вызова и стек
    держателя: разница между holder и caller и есть «цикл занят ДРУГИМ
    потоком».
    """
    holder = getattr(loop, _BUSY_HOLDER_THREAD_ATTR, None)
    caller = threading.get_ident()
    return (
        f"holder_thread={holder} ({_thread_name(holder)}) "
        f"caller_thread={caller} ({threading.current_thread().name}) "
        f"holder_stack={_holder_stack(holder)}"
    )


def run_coro(coro: Coroutine[Any, Any, T]) -> T:
    """
    Run a coroutine on a persistent event loop (per process).

    NOTE:
    - This function is intended to be used from synchronous Celery tasks.
    - Celery prefork workers execute tasks sequentially per process, so loop reuse is safe here.

    **Сторож занятого цикла.** Петля в модуле — общая на процесс, и asyncio
    выставляет ``loop._thread_id``, пока петля занята. Если ``run_coro`` зовут
    из потока, который УЖЕ держит эту петлю (например из ``asyncio.to_thread``
    внутри корутины, или из второго потока Celery-воркера), то
    ``run_until_complete`` падает с ЧУЖИМ текстом «This event loop is already
    running» — и вызывающий код читает это как «непонятная ошибка».
    Поэтому ниже стоит полный сторож: отказ СВОИМ сообщением + диагностика
    держателя (P253).
    """
    global _loop

    # If we're already inside an event loop, we cannot "sync-wait" safely.
    #
    # ⚠️ `raise` обязан жить в `else`, а не в теле `try`. Раньше он стоял внутри
    # `try` и попадал в собственный `except RuntimeError` строкой ниже — то есть
    # сторож глушил сам себя и никогда не срабатывал. Отказ всё равно наступал,
    # но ниже и с чужим текстом («This event loop is already running» из
    # `run_until_complete`), поэтому вызывающий код ловил его как «непонятную
    # ошибку БД» и уходил в свой fallback. Именно так discovery годами брал
    # VK-токены из env мимо маршрутизатора (расход уезжал в отчёт `/tokens`
    # строкой `UNKNOWN:<fp>`, найдено 2026-08-31).
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread => OK.
        pass
    else:
        raise RuntimeError("run_coro() cannot be called from within a running event loop")

    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)

    # Полный сторож занятого цикла (P253).
    #
    # asyncio выставляет `loop._thread_id`, пока петля занята `run_until_complete`.
    # Если петлю держит ДРУГОЙ поток (вызов из `asyncio.to_thread`, из второго
    # потока воркера, из фонового демона), то `run_until_complete` ниже упал бы
    # с чужим текстом «This event loop is already running» — вызывающий код
    # читал его как «непонятная ошибка» и уходил в fallback. Поэтому отказываем
    # ЗДЕСЬ: своим сообщением и с указанием, кто держит петлю.
    if _loop.is_running():
        info = _loop_holder_info(_loop)
        logger.error("run_coro: общий event-loop занят другим потоком (%s)", info)
        raise RuntimeError(
            "run_coro(): общий event-loop уже занят другим потоком "
            f"({info}) — вызывающий код должен выполнять корутину в своём потоке"
        )

    return _loop.run_until_complete(coro)
