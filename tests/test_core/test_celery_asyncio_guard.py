"""Сторож на защиту ``run_coro`` от вызова из работающего event-loop.

Почему этот файл существует. Защита была написана так:

    try:
        asyncio.get_running_loop()
        raise RuntimeError("run_coro() cannot be called from within ...")
    except RuntimeError:
        pass

— собственный ``raise`` попадал в собственный ``except`` строкой ниже, и
сторож глушил сам себя. Отказ всё равно наступал, но ниже и с ЧУЖИМ текстом
(«This event loop is already running» из ``run_until_complete``), поэтому
вызывающий код читал его как «непонятная ошибка» и уходил в свой fallback.
Именно так ``tasks/discovery_tasks`` годами брал VK-токены из env мимо
маршрутизатора (расход уезжал в отчёт `/tokens` строкой ``UNKNOWN:<fp>``,
найдено 2026-08-31).

Отсюда форма проверки: мало убедиться, что вызов падает — он падал и до
починки. Проверяем ИМЕННО ТЕКСТ, потому что разница между двумя отказами и
была ценой дефекта.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from utils.celery_asyncio import run_coro

_GUARD_MESSAGE = "cannot be called from within a running event loop"
_BUSY_MESSAGE = "общий event-loop уже занят другим потоком"


def test_run_coro_runs_coroutine_outside_loop() -> None:
    """Штатный путь (синхронная Celery-таска) не сломан починкой."""

    async def _answer() -> int:
        return 42

    assert run_coro(_answer()) == 42


def test_run_coro_reuses_one_loop() -> None:
    """Петля переиспользуется — ради этого модуль и написан."""

    async def _loop_id() -> int:
        return id(asyncio.get_running_loop())

    assert run_coro(_loop_id()) == run_coro(_loop_id())


async def test_run_coro_refuses_inside_running_loop() -> None:
    """Из async-контекста — отказ СВОИМ сообщением, а не чужим.

    До починки здесь поднималось ``RuntimeError`` из ``run_until_complete``
    («This event loop is already running» / «Cannot run the event loop while
    another loop is running»), то есть тест на факт исключения был бы зелёным
    и на сломанном сторо́же. Регресс ловит только сверка текста.
    """

    async def _noop() -> None:
        return None

    coro = _noop()
    try:
        with pytest.raises(RuntimeError, match=_GUARD_MESSAGE):
            run_coro(coro)
    finally:
        coro.close()


def test_run_coro_refuses_when_loop_busy_in_another_thread() -> None:
    """Цикл занят ДРУГИМ потоком — отказ СВОИМ сообщением (P253).

    Живой дефект боевого контура: общий ``_loop`` держал другой поток, и
    ``run_until_complete`` падал с ЧУЖИМ текстом «This event loop is already
    running» — вызывающий код читал это как «непонятная ошибка» и уходил в
    fallback. Лента хаба падала 100 % попыток.

    Регресс ловит сверку ТЕКСТА: чужое сообщение asyncio означает, что
    полного сторожа нет.
    """

    async def _noop() -> None:
        return None

    # Занимаем петлю из отдельного потока — ровно та ситуация, что давала
    # «This event loop is already running» на боевом контуре.
    holder_ready = threading.Event()
    release = threading.Event()

    def _hold_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.call_soon(holder_ready.set)
        try:
            loop.run_forever()
        finally:
            loop.close()

    thread = threading.Thread(target=_hold_loop, daemon=True)
    thread.start()
    assert holder_ready.wait(timeout=10), "поток-держатель не поднялся"

    import utils.celery_asyncio as celery_asyncio

    original = celery_asyncio._loop
    busy = asyncio.new_event_loop()
    celery_asyncio._loop = busy
    try:
        # Эмулируем «петлю занял другой поток»: запускаем петлю в фоне.
        busy_thread = threading.Thread(target=busy.run_forever, daemon=True)
        busy_thread.start()
        try:
            # Ждём, пока петля реально станет «занятой».
            for _ in range(100):
                if busy.is_running():
                    break
                threading.Event().wait(0.01)
            assert busy.is_running(), "петля не оказалась занятой"

            coro = _noop()
            try:
                with pytest.raises(RuntimeError, match=_BUSY_MESSAGE):
                    run_coro(coro)
            finally:
                coro.close()
        finally:
            busy.call_soon_threadsafe(busy.stop)
            busy_thread.join(timeout=5)
    finally:
        celery_asyncio._loop = original
        release.set()
        thread.join(timeout=5)
