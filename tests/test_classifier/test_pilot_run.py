"""Тесты отметки о прогоне классификатора (modules/classifier/pilot_run).

Держатся три вещи, на которых держится смысл отметки: «прошло с прошлого»
считается верно, повторный submit того же батча НЕ отмечается второй раз
(иначе хронология врёт), а флаж выключен — не отмечается вовсе.
"""

from __future__ import annotations

import pytest

from modules.classifier import pilot_run


class FakeRedis:
    def __init__(self, initial=None):
        self.store = {} if initial is None else dict(initial)
        self.writes = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value


@pytest.fixture(autouse=True)
def _notify_on(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", "1")


def _v(region, action="publish"):
    return {"lip": "1_2", "region_code": region, "action": action}


# --- формат времени ----------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "прошлого прогона не было"),
        (-5, "прошлого прогона не было"),
        (0, "0с"),
        (45, "45с"),
        (2700, "45м"),
        (3600, "1ч"),
        (8100, "2ч15м"),
        (86400, "1д"),
        (97200, "1д3ч"),
    ],
)
def test_format_elapsed_russian(seconds, expected):
    assert pilot_run.format_elapsed(seconds) == expected


def test_format_actions_only_nonzero():
    assert (
        pilot_run.format_actions({"publish": 40, "delete": 38, "hold": 0})
        == "publish 40 / delete 38"
    )
    assert pilot_run.format_actions({}) == "нет"


# --- сборка сообщения ---------------------------------------------------------------


def test_build_message_has_all_four_lines():
    msg = pilot_run.build_message(
        recorded=79,
        regions=["mi", "ur", "klz"],
        actions={"publish": 28, "delete": 49, "hold": 2},
        elapsed_seconds=8100,
        stamp="05.10 18:42",
    )
    lines = msg.splitlines()
    assert lines[0] == "ПРОГОН 05.10 18:42 · 79 вердиктов"
    assert lines[1] == "Районы: mi, ur, klz"
    assert lines[2] == "Решения: publish 28 / delete 49 / hold 2"
    assert lines[3] == "С прошлого: 2ч15м"


def test_first_run_says_so_honestly():
    msg = pilot_run.build_message(
        recorded=1,
        regions=["mi"],
        actions={"publish": 1},
        elapsed_seconds=None,
        stamp="05.10 18:42",
    )
    assert "прошлого прогона не было" in msg


def test_regions_dedup_preserves_first_seen_order():
    assert pilot_run.regions_from([_v("ur"), _v("mi"), _v("ur")]) == ["ur", "mi"]
    assert pilot_run.regions_from([{"lip": "1"}]) == []


# --- note_run -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_run_sends_and_writes_timestamp(monkeypatch):
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = FakeRedis()
    out = await pilot_run.note_run(
        recorded=79,
        verdicts=[_v("mi"), _v("ur", "delete")],
        redis_client=redis,
        now_ts=1000.0,
        stamp="05.10 18:42",
    )
    assert out == "note-sent"
    assert len(sent) == 1
    assert "ПРОГОН" in sent[0]
    assert redis.writes == [(pilot_run.LAST_RUN_KEY, "1000.0")]


@pytest.mark.asyncio
async def test_repeat_submit_is_not_a_second_note(monkeypatch):
    """Повторный submit того же батча даёт recorded=0 — это не новый прогон.

    Живой случай 05.10: первый POST ушёл в таймаут чтения, повтор показал
    ``skipped_existing: 79``. Без этой проверки один прогон отметил бы время
    дважды.
    """
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = FakeRedis()
    out = await pilot_run.note_run(
        recorded=0, verdicts=[_v("mi")], redis_client=redis, now_ts=1000.0
    )
    assert out == "skipped:no-new-verdicts"
    assert sent == []
    assert redis.writes == []


@pytest.mark.asyncio
async def test_elapsed_computed_from_previous_run(monkeypatch):
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = FakeRedis({pilot_run.LAST_RUN_KEY: "700.0"})
    await pilot_run.note_run(recorded=1, verdicts=[_v("mi")], redis_client=redis, now_ts=1000.0)
    assert "С прошлого: 5м" in sent[0]


@pytest.mark.asyncio
async def test_corrupt_previous_value_degrades_to_first_run(monkeypatch):
    """Мусор в ключе не должен ронять отметку — читаем как «прогона не было»."""
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = FakeRedis({pilot_run.LAST_RUN_KEY: "не-число"})
    out = await pilot_run.note_run(
        recorded=1, verdicts=[_v("mi")], redis_client=redis, now_ts=1000.0
    )
    assert out == "note-sent"
    assert "прошлого прогона не было" in sent[0]


@pytest.mark.asyncio
async def test_disabled_flag_is_silent(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", "0")
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = FakeRedis()
    out = await pilot_run.note_run(recorded=79, verdicts=[_v("mi")], redis_client=redis)
    assert out == "skipped:disabled"
    assert sent == []
    assert redis.writes == []


@pytest.mark.asyncio
async def test_telegram_failure_does_not_raise_and_still_records(monkeypatch):
    """Отметка не имеет права ронять приём вердиктов."""
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture([], text, result=False))
    redis = FakeRedis()
    out = await pilot_run.note_run(recorded=1, verdicts=[_v("mi")], redis_client=redis, now_ts=5.0)
    assert out == "note-failed"
    assert redis.writes == [(pilot_run.LAST_RUN_KEY, "5.0")]


@pytest.mark.asyncio
async def test_redis_failure_does_not_break_note(monkeypatch):
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))

    class BrokenRedis:
        def get(self, key):
            raise RuntimeError("redis down")

        def set(self, key, value):
            raise RuntimeError("redis down")

    out = await pilot_run.note_run(
        recorded=1, verdicts=[_v("mi")], redis_client=BrokenRedis(), now_ts=9.0
    )
    assert out == "note-sent"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_works_without_redis_at_all(monkeypatch):
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    out = await pilot_run.note_run(recorded=1, verdicts=[_v("mi")], redis_client=None)
    assert out == "note-sent"
    assert "прошлого прогона не было" in sent[0]


# --- асинхронный клиент Redis: регрессия живого бага 06.10 ------------------------


class AsyncFakeRedis:
    """Клиент веб-приложения: ``get``/``set`` — корутины.

    Именно он стоит в проде (``utils.cache.get_cache().get_client()``), и
    именно на нём первый вызов отметки сломал «сколько прошло с прошлого»:
    синхронный вызов возвращал непрочитанную корутину (она же truthy),
    ``float()`` на ней падал, маркер не писался — а сообщение в Telegram
    уходило как будто всё в порядке.
    """

    def __init__(self, initial=None):
        self.store = {} if initial is None else dict(initial)
        self.writes = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value


@pytest.mark.asyncio
async def test_async_redis_client_marker_is_actually_written(monkeypatch):
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = AsyncFakeRedis()
    out = await pilot_run.note_run(
        recorded=3, verdicts=[_v("mi")], redis_client=redis, now_ts=2000.0
    )
    assert out == "note-sent"
    assert redis.writes == [(pilot_run.LAST_RUN_KEY, "2000.0")]


@pytest.mark.asyncio
async def test_async_redis_client_computes_elapsed(monkeypatch):
    """Главный симптом бага: прошлое есть, а сообщение врало «не было»."""
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = AsyncFakeRedis({pilot_run.LAST_RUN_KEY: b"1000.0"})
    await pilot_run.note_run(recorded=3, verdicts=[_v("mi")], redis_client=redis, now_ts=4600.0)
    assert "С прошлого: 1ч" in sent[0]


@pytest.mark.asyncio
async def test_sync_client_still_supported(monkeypatch):
    """Синхронный клиент из Celery (selection.py) — тоже рабочий, не ломаем."""
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", lambda text: _capture(sent, text, result=True))
    redis = FakeRedis({pilot_run.LAST_RUN_KEY: "700.0"})
    out = await pilot_run.note_run(
        recorded=1, verdicts=[_v("mi")], redis_client=redis, now_ts=1000.0
    )
    assert out == "note-sent"
    assert "С прошлого: 5м" in sent[0]
    assert redis.writes == [(pilot_run.LAST_RUN_KEY, "1000.0")]


def test_notify_flag_accepts_common_spellings(monkeypatch):
    for raw in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", raw)
        assert pilot_run.notify_enabled() is True
    for raw in ("0", "false", "", "no"):
        monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", raw)
        assert pilot_run.notify_enabled() is False
    monkeypatch.delenv("CLASSIFIER_PILOT_NOTIFY", raising=False)
    assert pilot_run.notify_enabled() is False


# --- помощник: send_to_owner подменяется синхронной заглушкой -----------------------


def _capture(sink, text, *, result):
    async def _inner():
        sink.append(text)
        return result

    return _inner()
