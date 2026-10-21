"""Демон интейков радара: цикл не встаёт, конфиг-пауза не спамит, тик крутится.

Почему тесты. Демон держит приём сообщений радара в единственном экземпляре: если
он молча встаёт (исключение в цикле) или крутит вхолостую, интейк пропадает —
ровно так это и случилось в воркере 21.10 (тики выбрасывались по expires, днём
радар не принимал сообщения). Тесты держат три свойства: цикл переживает
исключение, останавливается по событию, и «выключен конфигом» не превращается в
тысячу строк лога.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scripts import radar_intake_daemon as daemon


def test_daemon_pulls_vault_secrets():
    """Секреты (TELEGRAM_TOKEN_*, VK) живут в комнате КАРМАНа, а не в env-файле.

    Живой повод 21.10: демон стартовал с «no TELEGRAM_TOKEN for AFONYA» при
    живом боте — bootstrap секретов не вызывался, в отличие от воркера
    (`tasks/celery_app.py`). Сторож на уровне текста модуля: импорт-тест такую
    дыру не ловит, потому что в тестах env уже заполнен.
    """
    src = (Path(__file__).resolve().parents[2] / "scripts" / "radar_intake_daemon.py").read_text(
        encoding="utf-8"
    )

    assert "bootstrap_secrets()" in src


@pytest.mark.asyncio
async def test_loop_stops_on_event(monkeypatch):
    monkeypatch.setattr(daemon, "SKIPPED_SLEEP", 0.01)
    stop = asyncio.Event()
    calls = {"n": 0}

    async def runner():
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()
        return {"skipped": "нет конфига"}

    await asyncio.wait_for(daemon._loop("t", runner, stop), timeout=2)

    assert calls["n"] >= 3


@pytest.mark.asyncio
async def test_loop_survives_exception(monkeypatch):
    """Один упавший тик не должен ронять демон: иначе приём сообщений умирает."""
    monkeypatch.setattr(daemon, "ERROR_SLEEP", 0.01)
    stop = asyncio.Event()
    calls = {"n": 0}

    async def runner():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("сеть мигнула")
        stop.set()
        return {"ok": True, "processed": 0}

    await asyncio.wait_for(daemon._loop("t", runner, stop), timeout=2)

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_loop_repeats_success_without_sleep(monkeypatch):
    """Успешный тик идёт сразу следующим: для long poll это и есть ожидание."""
    stop = asyncio.Event()
    calls = {"n": 0}

    async def runner():
        calls["n"] += 1
        if calls["n"] >= 5:
            stop.set()
        return {"ok": True}

    await asyncio.wait_for(daemon._loop("t", runner, stop), timeout=2)

    assert calls["n"] == 5


@pytest.mark.asyncio
async def test_vk_runner_skips_without_community_id(monkeypatch):
    from modules.radar.intake_runners import vk_intake_once

    monkeypatch.setattr("config.runtime.get_radar_vk_community_id", lambda: None)

    out = await vk_intake_once(lp_wait=25)

    assert out == {"skipped": "RADAR_VK_COMMUNITY_ID not set"}


@pytest.mark.asyncio
async def test_telegram_runner_skips_without_bot_name(monkeypatch):
    from modules.radar.intake_runners import telegram_intake_once

    monkeypatch.setattr("config.runtime.get_radar_bot_name", lambda: "")
    monkeypatch.setattr("config.runtime.TELEGRAM_TOKENS", {})

    out = await telegram_intake_once(long_poll=25)

    assert out == {"skipped": "RADAR_BOT_NAME not set"}


@pytest.mark.asyncio
async def test_telegram_runner_skips_with_empty_allowlist(monkeypatch):
    from modules.radar.intake_runners import telegram_intake_once

    monkeypatch.setattr("config.runtime.get_radar_bot_name", lambda: "KARMAN")
    monkeypatch.setattr("config.runtime.TELEGRAM_TOKENS", {"KARMAN": "123:abc"})
    monkeypatch.setattr("config.runtime.get_radar_bot_allowed_users", lambda: set())

    out = await telegram_intake_once(long_poll=25)

    assert out == {"skipped": "RADAR_BOT_ALLOWED_USERS empty"}


@pytest.mark.asyncio
async def test_intake_tick_passes_long_poll_to_getupdates():
    """Демон ждёт событие на стороне Telegram (25 с), а не крутит короткий poll.

    Иначе тот же 25-секундный бюджет тратится на десятки пустых запросов —
    и разговор с Bot API идёт чаще, чем нужно.
    """
    from modules.radar.bot_intake import poll_radar_bot_once

    seen = {}

    def fake_call(token, method, params):
        seen[method] = dict(params)
        return {"ok": True, "result": []}

    await poll_radar_bot_once(
        token="123:abc",
        allowed_users={1},
        add_channel=lambda u: None,
        offset_get=lambda: None,
        offset_set=lambda v: None,
        call=fake_call,
        long_poll=25,
    )

    assert seen["getUpdates"]["timeout"] == 25
