"""Тесты intake-бота радара «Карман» (форвард канала → радар).

Чистая логика (extract_forwarded_channel/build_reply/handle_message) и тик
poll_radar_bot_once с инъекцией getUpdates/sendMessage/offset — без сети/БД.
"""

from modules.radar.bot_intake import (
    build_reply,
    extract_forwarded_channel,
    handle_message,
    poll_radar_bot_once,
)

# --------------------------------------------------------------------------- #
# extract_forwarded_channel
# --------------------------------------------------------------------------- #


def test_extract_forward_origin_channel():
    msg = {
        "forward_origin": {
            "type": "channel",
            "chat": {"username": "scitopus", "title": "SciTopus"},
        }
    }
    assert extract_forwarded_channel(msg) == ("scitopus", "SciTopus")


def test_extract_legacy_forward_from_chat():
    msg = {"forward_from_chat": {"type": "channel", "username": "nplus1", "title": "N+1"}}
    assert extract_forwarded_channel(msg) == ("nplus1", "N+1")


def test_extract_private_channel_no_username():
    msg = {"forward_from_chat": {"type": "channel", "title": "Закрытый"}}
    assert extract_forwarded_channel(msg) == (None, "Закрытый")


def test_extract_not_a_channel_forward():
    assert extract_forwarded_channel({"text": "просто текст"}) == (None, None)
    assert extract_forwarded_channel({"forward_from": {"id": 1}}) == (
        None,
        None,
    )  # форвард от юзера
    assert extract_forwarded_channel("not a dict") == (None, None)


# --------------------------------------------------------------------------- #
# build_reply
# --------------------------------------------------------------------------- #


def test_build_reply_variants():
    assert build_reply("added", username="x", title="T").startswith("✅")
    assert build_reply("exists", username="x").startswith("ℹ️")
    assert "приватный" in build_reply("private", title="Y")
    assert "Перешлите" in build_reply("not_forward")
    assert build_reply("error", detail="boom").startswith("❌")


# --------------------------------------------------------------------------- #
# handle_message
# --------------------------------------------------------------------------- #


async def _added(username):
    return {"status": "added", "title": "Канал"}


async def _exists(username):
    return {"status": "exists", "title": "Канал"}


async def test_handle_unauthorized_silent():
    # Чужому/рандому — молчим (None), чтобы не спамить общий/публичный бот.
    msg = {
        "chat": {"id": 99},
        "from": {"id": 777},
        "forward_from_chat": {"type": "channel", "username": "x"},
    }
    assert await handle_message(msg, allowed_users={1}, add_channel=_added) is None
    assert await handle_message(msg, allowed_users=set(), add_channel=_added) is None


async def test_handle_authorized_adds_channel():
    msg = {
        "chat": {"id": 5},
        "from": {"id": 1},
        "forward_from_chat": {
            "type": "channel",
            "username": "scitopus",
            "title": "SciTopus",
        },
    }
    out = await handle_message(msg, allowed_users={1}, add_channel=_added)
    assert out[0] == 5
    assert out[1].startswith("✅") and "scitopus" in out[1]


async def test_handle_already_exists():
    msg = {
        "chat": {"id": 5},
        "from": {"id": 1},
        "forward_origin": {"type": "channel", "chat": {"username": "nplus1"}},
    }
    out = await handle_message(msg, allowed_users={1}, add_channel=_exists)
    assert out[1].startswith("ℹ️")


async def test_handle_not_forward():
    msg = {"chat": {"id": 5}, "from": {"id": 1}, "text": "привет"}
    out = await handle_message(msg, allowed_users={1}, add_channel=_added)
    assert "Перешлите" in out[1]


async def test_handle_private_channel():
    msg = {
        "chat": {"id": 5},
        "from": {"id": 1},
        "forward_from_chat": {"type": "channel", "title": "Закрытый"},
    }
    out = await handle_message(msg, allowed_users={1}, add_channel=_added)
    assert "приватный" in out[1]


async def test_handle_no_chat_id_none():
    assert await handle_message({"from": {"id": 1}}, allowed_users={1}, add_channel=_added) is None


# --------------------------------------------------------------------------- #
# Привязка личного аккаунта /start <код> (Радиоточка)
# --------------------------------------------------------------------------- #


async def _link(status):
    async def _fn(code, chat_id, display_name):
        return {"status": status}

    return _fn


async def test_handle_start_code_links_account():
    # /start <код> от НЕ-allowlisted юзера всё равно обрабатывается (код авторизует).
    msg = {"chat": {"id": 42}, "from": {"id": 999}, "text": "/start ABC123"}
    out = await handle_message(
        msg, allowed_users={1}, add_channel=_added, link_account=await _link("linked")
    )
    assert out[0] == 42
    assert out[1].startswith("✅") and "Подключено" in out[1]


async def test_handle_start_code_already_exists():
    msg = {"chat": {"id": 42}, "from": {"id": 1}, "text": "/start ABC123"}
    out = await handle_message(
        msg, allowed_users={1}, add_channel=_added, link_account=await _link("exists")
    )
    assert out[1].startswith("ℹ️")


async def test_handle_start_code_invalid():
    msg = {"chat": {"id": 42}, "from": {"id": 1}, "text": "/start BADCODE"}
    out = await handle_message(
        msg, allowed_users={1}, add_channel=_added, link_account=await _link("invalid")
    )
    assert out[1].startswith("❌") and "Код не найден" in out[1]


async def test_handle_bare_start_silent():
    # Голый /start без кода на общем боте — молчим (не тревожим чужих).
    msg = {"chat": {"id": 42}, "from": {"id": 999}, "text": "/start"}
    out = await handle_message(
        msg, allowed_users={1}, add_channel=_added, link_account=await _link("linked")
    )
    assert out is None


async def test_handle_start_without_link_account_silent():
    msg = {"chat": {"id": 42}, "from": {"id": 999}, "text": "/start ABC123"}
    out = await handle_message(msg, allowed_users={1}, add_channel=_added)
    assert out is None


# --------------------------------------------------------------------------- #
# poll_radar_bot_once
# --------------------------------------------------------------------------- #


async def test_poll_processes_and_advances_offset():
    sent = []
    state = {"offset": None}

    def fake_call(token, method, params):
        if method == "getUpdates":
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 10,
                        "message": {
                            "chat": {"id": 5},
                            "from": {"id": 1},
                            "forward_from_chat": {
                                "type": "channel",
                                "username": "scitopus",
                                "title": "S",
                            },
                        },
                    },
                    {
                        "update_id": 11,
                        "message": {"chat": {"id": 5}, "from": {"id": 1}, "text": "hi"},
                    },
                ],
            }
        if method == "sendMessage":
            sent.append(params)
            return {"ok": True}
        return {}

    res = await poll_radar_bot_once(
        token="T",
        allowed_users={1},
        add_channel=_added,
        offset_get=lambda: state["offset"],
        offset_set=lambda v: state.__setitem__("offset", v),
        call=fake_call,
    )
    assert res["ok"] is True
    assert res["processed"] == 2
    assert res["added"] == 1  # один канал-форвард
    assert res["replied"] == 2  # ответили на оба (второй — «перешлите»)
    assert state["offset"] == 12  # max update_id (11) + 1
    assert len(sent) == 2


async def test_poll_getupdates_not_ok():
    res = await poll_radar_bot_once(
        token="T",
        allowed_users={1},
        add_channel=_added,
        offset_get=lambda: None,
        offset_set=lambda v: None,
        call=lambda t, m, p: {"ok": False, "description": "Unauthorized"},
    )
    assert res["ok"] is False
    assert res["processed"] == 0


# --------------------------------------------------------------------------- #
# Конфиг + регистрация beat
# --------------------------------------------------------------------------- #


def test_radar_bot_config(monkeypatch):
    monkeypatch.setenv("RADAR_BOT_NAME", "karman")
    monkeypatch.setenv("RADAR_BOT_ALLOWED_USERS", "777, 888, junk")
    monkeypatch.delenv("RADAR_BOT_RADAR_USER_ID", raising=False)
    from config.runtime import (
        get_radar_bot_allowed_users,
        get_radar_bot_name,
        get_radar_bot_radar_user_id,
    )

    assert get_radar_bot_name() == "KARMAN"
    assert get_radar_bot_allowed_users() == {777, 888}
    assert get_radar_bot_radar_user_id() == 1


def test_radar_bot_runs_in_daemon_not_in_beat():
    """Поллинги интейков живут в демоне, а не в расписании beat.

    Причина — не организация, а слот воркера: ожидание события (getUpdates,
    a_check) занимало единственный слот и днём выбрасывалось по `expires`, то
    есть радар не принимал сообщения. Демон крутит тот же тик
    (`scripts/radar_intake_daemon.py` → `modules.radar.intake_runners`).
    """
    from tasks import radar_tasks as tasks_radar
    from tasks.celery_app import app

    assert "radar-intake-bot" not in app.conf.beat_schedule
    assert "radar-vk-intake" not in app.conf.beat_schedule
    # Задачи остаются живыми: ручной тик и диагностика.
    assert callable(tasks_radar.poll_radar_bot)
    assert callable(tasks_radar.poll_radar_vk_intake)


def test_redis_helper_symbol_exists():
    # Guard: _run_radar_bot лениво импортирует redis-хелпер из bulletin_heartbeat.
    # Ловит переименование/опечатку (этот импорт не виден import-time CI).
    from modules.bulletin_heartbeat import _redis  # noqa: F401

    assert callable(_redis)
