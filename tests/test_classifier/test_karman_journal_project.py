"""Журнальный проект аренды у Кармана — под ним аккаунт принадлежит комнате.

**Почему это отдельный тест.** Ручка ``renew`` продлевает аккаунт только той
комнате, чей журнальный проект совпал с проектом выдачи; при несовпадении —
409, молча. Поэтому фактическое значение заголовка должно быть видно: по
умолчанию — имя комнаты (``ROOM_PROJECT``), а переопределение переменной
окружения обязано быть **видно в логе**, а не всплывать отказом на проде.
"""

from __future__ import annotations

import logging

from modules import karman_minimax as km
from modules import minimax_engine as mx


def test_default_project_is_the_room(monkeypatch) -> None:
    monkeypatch.delenv("MINIMAX_PROJECT", raising=False)
    headers = km.consumer_headers()
    assert headers["X-MiniMax-Project"] == km.ROOM_PROJECT == "SARAFAN"


def test_env_override_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_PROJECT", "other-room")
    assert km.consumer_headers()["X-MiniMax-Project"] == "other-room"


def test_mismatched_project_is_logged_as_warning(monkeypatch, caplog) -> None:
    """Переопределение не совпало с комнатой — это предупреждение, а не INFO."""
    monkeypatch.setenv("MINIMAX_PROJECT", "setka")
    with caplog.at_level(logging.INFO):
        mx._log_journal_project("work")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "расхождение проекта обязано быть заметно в логе"
    assert "setka" in warnings[0].getMessage()
    assert "SARAFAN" in warnings[0].getMessage()


def test_matching_project_is_logged_without_warning(monkeypatch, caplog) -> None:
    monkeypatch.delenv("MINIMAX_PROJECT", raising=False)
    with caplog.at_level(logging.INFO):
        mx._log_journal_project("work")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings
    assert any("SARAFAN" in r.getMessage() for r in caplog.records)
