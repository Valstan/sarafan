"""load_photo_upload_token: токен аккаунта-владельца для заливки фото (P214).

Фото на стену сообщества заливает **владелец группы**, а не публикатор:
community-токен залить не может вовсе ([27]), а фото администратора
community-токен в записи не принимает — молча. Разкладку сняла живая проба
09.11: ``docs/ops/photo-upload-path.md``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules import vk_token_router as router


@pytest.mark.asyncio
async def test_returns_owner_account_token(monkeypatch):
    monkeypatch.setenv("VK_PHOTO_UPLOAD_TOKEN_NAMES", "VALSTAN")
    asked = []

    async def fake_token_for_account(self, name):
        asked.append(name)
        return SimpleNamespace(name=name, token="tok-owner", source="user")

    monkeypatch.setattr(router.TokenPolicy, "token_for_account", fake_token_for_account)

    token = await router.load_photo_upload_token(AsyncMock())

    assert token == "tok-owner"
    assert asked == ["VALSTAN"]


@pytest.mark.asyncio
async def test_none_when_account_missing(monkeypatch):
    """Нет аккаунта-владельца — заливать нечем: токен не выдумывается."""
    monkeypatch.setenv("VK_PHOTO_UPLOAD_TOKEN_NAMES", "VALSTAN")

    async def fake_token_for_account(self, name):
        return None

    monkeypatch.setattr(router.TokenPolicy, "token_for_account", fake_token_for_account)

    assert await router.load_photo_upload_token(AsyncMock()) is None


@pytest.mark.asyncio
async def test_first_available_name_wins(monkeypatch):
    """Имена из env — по очереди: берём первый аккаунт, у которого есть токен."""
    monkeypatch.setenv("VK_PHOTO_UPLOAD_TOKEN_NAMES", "MAMA,VALSTAN")
    asked = []

    async def fake_token_for_account(self, name):
        asked.append(name)
        if name == "VALSTAN":
            return SimpleNamespace(name=name, token="tok-owner", source="user")
        return None

    monkeypatch.setattr(router.TokenPolicy, "token_for_account", fake_token_for_account)

    assert await router.load_photo_upload_token(AsyncMock()) == "tok-owner"
    assert asked == ["MAMA", "VALSTAN"]
