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
import time

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


def test_run_coro_waits_for_busy_loop_instead_of_failing() -> None:
    """Петля занята ДРУГИМ вызовом ``run_coro`` — второй ЖДЁТ и получает результат.

    Живой дефект боевого контура (P253, замер 27.11 06:47): держателем петли был
    поток ETA-таймера celery (``Timer-1``), а звал из главного потока. Раньше
    второй получал отказ, и задача падала целиком — а для мамы волны это «волна
    не пошла вообще» (26.11 так умерли 5 мам-задач).

    Теперь петля — ресурс на процесс: её крутят по очереди, второй ждёт.
    """
    import utils.celery_asyncio as celery_asyncio

    async def _slow() -> str:
        await asyncio.sleep(1.0)
        return "первый"

    async def _quick() -> str:
        return "второй"

    holder_done = threading.Event()
    results: list[str] = []

    def _holder() -> None:
        results.append(run_coro(_slow()))
        holder_done.set()

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    # Даём держателю взять петлю: он держит её ~1 с.
    time.sleep(0.2)

    waited = run_coro(_quick())
    results.append(waited)
    thread.join(timeout=10)

    assert holder_done.is_set(), "держатель петли не закончил"
    assert set(results) == {"первый", "второй"}, f"результаты: {results}"
    # Петля та же самая — пул asyncpg не должен рваться.
    assert celery_asyncio._loop is not None


def test_run_coro_two_threads_share_one_loop_without_errors() -> None:
    """Два потока зовут ``run_coro`` одновременно — оба получают результат.

    Это и есть режим пула ``threads`` с двумя слотами: тела задач исполняют
    два потока пула, а петля у процесса одна. Раньше второй из них получал
    «This event loop is already running» и задача падала.
    """
    import utils.celery_asyncio as celery_asyncio

    async def _answer(value: int) -> int:
        await asyncio.sleep(0.05)
        return value

    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _worker(value: int) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(run_coro(_answer(value)))
        except BaseException as exc:  # noqa: BLE001 — тест ловит отказ
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, f"отказы недопустимы: {errors}"
    assert sorted(results) == [1, 2], f"результаты: {results}"
    assert celery_asyncio._loop is not None
