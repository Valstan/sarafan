"""Тесты отметки о прогоне классификатора (modules/classifier/pilot_run).

Держатся три вещи, на которых держится смысл отметки: время — московское и
явное, повторный submit того же батча НЕ отмечается второй раз (иначе
хронология врёт), а флаж выключен — не отмечается вовсе.

Строки «сколько прошло с прошлого» здесь НЕТ намеренно (решение владельца
06.10): интервал считается в голове, Telegram — сам журнал. Поэтому Redis
в отметке не участвует вообще, и целый класс «синхронный вызов на
асинхронном клиенте» (живой баг 06.10) здесь больше нечему ловить.
"""

from __future__ import annotations

import pytest

from modules.classifier import pilot_run


@pytest.fixture(autouse=True)
def _notify_on(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", "1")


def _v(region, action="publish"):
    return {"lip": "1_2", "region_code": region, "action": action}


# --- московское время --------------------------------------------------------------


def test_moscow_stamp_has_explicit_suffix():
    """06.10 08:19:11 МСК — epoch из живого прогона №3, не с потолка."""
    assert pilot_run.moscow_stamp(1791263951.0) == "06.10 08:19 МСК"


def test_moscow_stamp_is_not_server_local(monkeypatch):
    """Поехал бокс в другой пояс — отметка осталась московской.

    Подменяем ``TZ`` сервера на UTC+0: отметка обязана не двинуться, потому
    что зона зашита в код, а не взята из окружения.
    """
    monkeypatch.setenv("TZ", "UTC0")
    assert pilot_run.moscow_stamp(1791263951.0) == "06.10 08:19 МСК"


# --- сборка сообщения ---------------------------------------------------------------


def test_build_message_is_three_lines_with_moscow_time():
    msg = pilot_run.build_message(
        recorded=79,
        regions=["mi", "ur", "klz"],
        actions={"publish": 28, "delete": 49, "hold": 2},
        stamp="06.10 08:19 МСК",
    )
    lines = msg.splitlines()
    assert lines[0] == "ПРОГОН 06.10 08:19 МСК · 79 вердиктов"
    assert lines[1] == "Районы: mi, ur, klz"
    assert lines[2] == "Решения: publish 28 / delete 49 / hold 2"
    assert len(lines) == 3


def test_format_actions_only_nonzero():
    assert (
        pilot_run.format_actions({"publish": 40, "delete": 38, "hold": 0})
        == "publish 40 / delete 38"
    )
    assert pilot_run.format_actions({}) == "нет"


def test_regions_dedup_preserves_first_seen_order():
    assert pilot_run.regions_from([_v("ur"), _v("mi"), _v("ur")]) == ["ur", "mi"]
    assert pilot_run.regions_from([{"lip": "1"}]) == []


# --- note_run -----------------------------------------------------------------------


def _capture(sink, *, result=True):
    async def sender(text):
        sink.append(text)
        return result

    return sender


@pytest.mark.asyncio
async def test_note_run_sends_moscow_time(monkeypatch):
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", _capture(sent))
    out = await pilot_run.note_run(
        recorded=79, verdicts=[_v("mi"), _v("ur", "delete")], now_ts=1791263951.0
    )
    assert out == "note-sent"
    assert len(sent) == 1
    assert sent[0].splitlines()[0] == "ПРОГОН 06.10 08:19 МСК · 79 вердиктов"


@pytest.mark.asyncio
async def test_repeat_submit_is_not_a_second_note(monkeypatch):
    """Повторный submit того же батча даёт recorded=0 — это не новый прогон.

    Живой случай 05.10: первый POST ушёл в таймаут чтения, повтор показал
    ``skipped_existing: 79``. Без этой проверки один прогон отметил бы время
    дважды.
    """
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", _capture(sent))
    out = await pilot_run.note_run(recorded=0, verdicts=[_v("mi")])
    assert out == "skipped:no-new-verdicts"
    assert sent == []


@pytest.mark.asyncio
async def test_disabled_flag_is_silent(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", "0")
    sent = []
    monkeypatch.setattr(pilot_run, "send_to_owner", _capture(sent))
    out = await pilot_run.note_run(recorded=79, verdicts=[_v("mi")])
    assert out == "skipped:disabled"
    assert sent == []


@pytest.mark.asyncio
async def test_telegram_failure_does_not_raise(monkeypatch):
    """Отметка не имеет права ронять приём вердиктов."""
    monkeypatch.setattr(pilot_run, "send_to_owner", _capture([], result=False))
    out = await pilot_run.note_run(recorded=1, verdicts=[_v("mi")])
    assert out == "note-failed"


def test_notify_flag_accepts_common_spellings(monkeypatch):
    for raw in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", raw)
        assert pilot_run.notify_enabled() is True
    for raw in ("0", "false", "", "no"):
        monkeypatch.setenv("CLASSIFIER_PILOT_NOTIFY", raw)
        assert pilot_run.notify_enabled() is False
    monkeypatch.delenv("CLASSIFIER_PILOT_NOTIFY", raising=False)
    assert pilot_run.notify_enabled() is False
