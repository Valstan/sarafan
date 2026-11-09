# -*- coding: utf-8 -*-
"""Клиент раздела промтов КАРМАНА и скрипт его чтения (D-190).

Проверяем контракт без сети: подменяем HTTP-вызов.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from modules import prompts_section as ps

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "prompts_fetch.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("prompts_fetch", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_call(status, payload, recorder=None):
    def _call(method, url, token, body=None):
        if recorder is not None:
            recorder.append({"method": method, "url": url, "token": token, "body": body})
        return status, payload

    return _call


def test_token_name_is_canonical():
    assert ps.token_name("SARAFAN") == "PROMPTS_TOKEN_SARAFAN"
    assert ps.token_name("setka") == "PROMPTS_TOKEN_SETKA"
    with pytest.raises(ValueError):
        ps.token_name("")


def test_section_url_carries_project_and_archived_flag():
    url = ps.section_url("SARAFAN", vault_url="https://vault.example/api/secrets")
    assert url.startswith("https://vault.example/api/prompts/section?")
    assert "project=SARAFAN" in url
    assert "archived" not in url

    url_archived = ps.section_url(
        "SARAFAN", archived=True, vault_url="https://vault.example/api/secrets"
    )
    assert "archived=1" in url_archived


def test_fetch_section_returns_prompts():
    recorder = []
    payload = {"prompts": [{"id": 7, "title": "Починить вечерний сборник"}]}
    status, prompts = ps.fetch_section(
        "tok",
        "SARAFAN",
        vault_url="https://vault.example/api/secrets",
        call=_fake_call(200, payload, recorder),
    )
    assert status == 200
    assert [p["id"] for p in prompts] == [7]
    assert recorder[0]["method"] == "GET"


def test_fetch_section_503_is_not_an_empty_success():
    """503 = сервис не видит токен раздела: это НЕ «раздел пуст»."""
    status, prompts = ps.fetch_section(
        "tok",
        "SARAFAN",
        vault_url="https://vault.example/api/secrets",
        call=_fake_call(503, {"error": "канал промтов раздела не настроен"}),
    )
    assert status == 503
    assert prompts == []


def test_archive_prompt_sends_archived_flag():
    recorder = []
    status, _ = ps.archive_prompt(
        "tok",
        "SARAFAN",
        7,
        vault_url="https://vault.example/api/secrets",
        call=_fake_call(200, {"ok": True}, recorder),
    )
    assert status == 200
    body = recorder[0]["body"]
    assert body == {"project": "SARAFAN", "id": 7, "archived": True}


def test_restore_prompt_keeps_archived_false():
    recorder = []
    ps.archive_prompt(
        "tok",
        "SARAFAN",
        7,
        archived=False,
        vault_url="https://vault.example/api/secrets",
        call=_fake_call(200, {"ok": True}, recorder),
    )
    assert recorder[0]["body"]["archived"] is False


def test_resolve_section_token_prefers_env():
    token = ps.resolve_section_token("SARAFAN", env={"PROMPTS_TOKEN_SARAFAN": "from-env"})
    assert token == "from-env"


def test_resolve_section_token_falls_back_to_room():
    payload = {"secrets": {"PROMPTS_TOKEN_SARAFAN": "from-room"}}
    token = ps.resolve_section_token(
        "SARAFAN",
        env={},
        room_token="room-tok",
        vault_url="https://vault.example/api/secrets",
        call=_fake_call(200, payload),
    )
    assert token == "from-room"


def test_resolve_section_token_none_without_room():
    assert ps.resolve_section_token("SARAFAN", env={}, room_token="") is None


def test_prompt_text_and_plan_flag():
    assert ps.prompt_text({"title": "План: собрать сводку"}) == "План: собрать сводку"
    assert ps.is_plan({"title": "План: собрать сводку"}) is True
    assert ps.is_plan({"title": "Починить вечерний сборник"}) is False
    assert ps.prompt_text({"id": 1}) == ""


def test_script_lists_prompts(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr(
        ps,
        "fetch_section",
        lambda *a, **k: (200, [{"id": 7, "title": "Починить вечерний сборник"}]),
    )
    monkeypatch.setattr(ps, "resolve_section_token", lambda *a, **k: "tok")

    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert "id=7" in out
    assert "Починить вечерний сборник" in out


def test_script_reports_503_as_channel_problem(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr(ps, "fetch_section", lambda *a, **k: (503, []))
    monkeypatch.setattr(ps, "resolve_section_token", lambda *a, **k: "tok")

    assert script.main([]) == 1
    assert "не настроен" in capsys.readouterr().err


def test_script_without_token_fails_loudly(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr(ps, "resolve_section_token", lambda *a, **k: None)

    assert script.main([]) == 1
    assert "PROMPTS_TOKEN_SARAFAN" in capsys.readouterr().err


def test_script_archives_prompt(monkeypatch, capsys):
    script = _load_script()
    seen = {}

    def _archive(token, project, prompt_id, *, archived=True, vault_url=None):
        seen["id"] = prompt_id
        seen["archived"] = archived
        return 200, {"ok": True}

    monkeypatch.setattr(ps, "resolve_section_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ps, "archive_prompt", _archive)

    assert script.main(["--archive", "7"]) == 0
    assert seen == {"id": 7, "archived": True}
    assert "в архиве" in capsys.readouterr().out
