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
- The loop is a **process-wide resource**: it is taken under a lock, so when two
  threads want it (e.g. a worker with two slots, or the ETA-timer thread and
  the main thread), the second one **waits** instead of crashing the task
  (P253, live measurement 27.11 06:47 — the holder was celery's ETA-timer
  thread ``Timer-1``).
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

#: Замок на петлю: крутить её можно только по очереди. asyncio сам этого не
#: проверяет — ``run_forever`` смотрит на thread-local «текущую петлю», а не
#: на петлю под рукой, поэтому второй поток, позвавший ``run_until_complete``,
#: получил бы «This event loop is already running» (P253).
_loop_lock = threading.Lock()

#: Сколько ждать освобождения петли, прежде чем отказать. По умолчанию
#: полчаса: держать петлю дольше — это уже висящая задача, а не «занято
#: на минуту». Перекрывается env'ом ``SETKA_RUN_CORO_WAIT_TIMEOUT_SEC``.
_BUSY_WAIT_TIMEOUT_DEFAULT_SEC = 1800.0


def _busy_wait_timeout() -> float:
    """Таймаут ожидания петли (секунды) — с перекрытием через env."""
    raw = os.environ.get("SETKA_RUN_CORO_WAIT_TIMEOUT_SEC", "")
    if raw.strip():
        try:
            return max(1.0, float(raw))
        except ValueError:
            logger.warning("SETKA_RUN_CORO_WAIT_TIMEOUT_SEC=%r — не число, беру дефолт", raw)
    return _BUSY_WAIT_TIMEOUT_DEFAULT_SEC


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

    **Петля — ресурс на процесс.** Она крутится под замком, поэтому второй
    поток, позвавший ``run_coro``, **ждёт** её освобождения, а не падает.
    Живой замер 27.11 06:47 (P253): держателем петли был поток ETA-таймера
    celery (``Timer-1``), а звал из главного потока; раньше второй получал
    отказ, и задача падала целиком — для мамы волны это «волна не пошла
    вообще». Отказ остаётся в двух случаях: петлю держат дольше таймаута
    ожидания или её крутит кто-то вне нашего замка. Тогда — свой текст +
    диагностика держателя.
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

    # ⚠️ **Петля — ресурс на процесс, и брать её надо по очереди.**
    #
    # Живой замер 27.11 06:47 (P253): держателем петли был **поток ETA-таймера
    # celery** (`Timer-1`), а звал из главного потока. Причина — пул `solo`:
    # ETA-задачу (у нас это веер волны и веер сводки, у обоих `countdown=`)
    # исполняет поток таймера, а обычные задачи — главный поток; оба зовут
    # `run_coro`, а петля у процесса одна.
    #
    # Раньше второй поток получал отказ («цикл уже занят») — и задача падала
    # целиком. Для мамы волны это значит «волна не пошла вообще»: 26.11 так
    # умерли 5 мам-задач, а 27.11 в 06:47 — лента хаба и задача региона.
    #
    # Теперь вместо отказа — **ожидание**: петля крутится по очереди, кто
    # пришёл раньше, тот и катает. Отказ остаётся только на два случая:
    # петлю держат дольше таймаута, либо её крутит кто-то вне нашего замка
    # (чужой петли/чужой поток — тогда нужен отказ с диагностикой).
    acquired = _loop_lock.acquire(timeout=_busy_wait_timeout())
    if not acquired:
        info = _loop_holder_info(_loop)
        logger.error(
            "run_coro: общий event-loop занят другим потоком дольше %s с (%s)",
            _busy_wait_timeout(),
            info,
        )
        raise RuntimeError(
            "run_coro(): общий event-loop занят другим потоком дольше "
            f"{_busy_wait_timeout()} с ({info}) — вызывающий код должен "
            "выполнять корутину в своём потоке"
        )
    try:
        # Петля создаётся под замком: иначе два потока, впервые позвавшие
        # `run_coro`, создали бы по своей петле и потеряли бы общий пул
        # asyncpg (а он привязан к петле, см. докстринг модуля).
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            asyncio.set_event_loop(_loop)
        # Замок мы взяли — но петлю может крутить кто-то, кто наш замок не
        # держит (чужой поток, тест). Это уже не «занято на минуту», а
        # неисправимая коллизия: отказываем своим сообщением.
        if _loop.is_running():
            info = _loop_holder_info(_loop)
            logger.error("run_coro: общий event-loop занят другим потоком (%s)", info)
            raise RuntimeError(
                "run_coro(): общий event-loop уже занят другим потоком "
                f"({info}) — вызывающий код должен выполнять корутину в своём потоке"
            )
        return _loop.run_until_complete(coro)
    finally:
        _loop_lock.release()
