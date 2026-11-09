"""Клиент аккаунта-владельца для заливки фото в стену сообщества (P214).

Правило снято живой пробой 09.11 (полигон + 6 групп сети):

* community-токен залить фото в стену **не может** — ``[27] method is
  unavailable with group auth``;
* фото, залитое администратором, community-токен в записи **не принимает**:
  пост уходит, вложения нет — молча;
* фото аккаунта-владельца (создателя группы) принимается и становится
  собственностью сообщества.

Значит, заливает **владелец группы**, а не тот, кто публикует.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.publisher.vk_publisher_extended import VKPublisher


class _FakePolicy:
    """Политика, отдающая токен заданного аккаунта (или None)."""

    def __init__(self, tokens: dict):
        self._tokens = {k.upper(): v for k, v in tokens.items()}
        self.asked = []

    async def token_for_account(self, name):
        self.asked.append(name)
        tok = self._tokens.get((name or "").upper())
        if not tok:
            return None
        return SimpleNamespace(name=name.upper(), token=tok, source="user")


@pytest.fixture
def _no_env_tokens(monkeypatch):
    """Окружение без токенов VK — иначе тест Depends от машины к машине."""
    import config.runtime as runtime

    monkeypatch.setattr(runtime, "VK_TOKENS", {})
    monkeypatch.setenv("VK_PHOTO_UPLOAD_TOKEN_NAMES", "VALSTAN")


@pytest.fixture
def _fake_client(monkeypatch):
    """VKClient не создаём: он тянет сессию — в тесте достаточно факта."""
    created = []

    class _Stub:
        def __init__(self, token):
            self.token = token
            created.append(token)

    monkeypatch.setattr("modules.vk_monitor.vk_client.VKClient", _Stub)
    return created


@pytest.mark.asyncio
async def test_owner_client_taken_from_policy(_no_env_tokens, _fake_client):
    publisher = VKPublisher(vk_client=object())
    publisher._policy = _FakePolicy({"VALSTAN": "tok-owner"})

    client = await publisher._photo_upload_client()

    assert client.token == "tok-owner"
    assert publisher._policy.asked == ["VALSTAN"]


@pytest.mark.asyncio
async def test_owner_client_is_cached(_no_env_tokens, _fake_client):
    """Клиент создаётся один раз: второй фото не плодит сессии."""
    publisher = VKPublisher(vk_client=object())
    publisher._policy = _FakePolicy({"VALSTAN": "tok-owner"})

    first = await publisher._photo_upload_client()
    second = await publisher._photo_upload_client()

    assert first is second
    assert _fake_client == ["tok-owner"]


@pytest.mark.asyncio
async def test_owner_client_falls_back_to_env(_no_env_tokens, _fake_client, monkeypatch):
    """Без политики (не из фабрики) — берём env-токен с тем же именем."""
    import config.runtime as runtime

    monkeypatch.setattr(runtime, "VK_TOKENS", {"VALSTAN": "env-owner"})
    publisher = VKPublisher(vk_client=object())

    client = await publisher._photo_upload_client()

    assert client.token == "env-owner"


@pytest.mark.asyncio
async def test_no_owner_account_means_no_client(_no_env_tokens, _fake_client):
    """Нет аккаунта-владельца — заливать нечем, клиент не выдумывается."""
    publisher = VKPublisher(vk_client=object())
    publisher._policy = _FakePolicy({})

    assert await publisher._photo_upload_client() is None
    assert _fake_client == []


@pytest.mark.asyncio
async def test_env_does_not_revive_account_disabled_in_db(
    _no_env_tokens, _fake_client, monkeypatch
):
    """Аккаунт, выключенный в БД, env-токеном не воскрешается.

    Правило TokenPolicy: имя, выключенное в БД, env не возвращает. Иначе
    снятый рубильник обходился бы одной переменной окружения.
    """
    import config.runtime as runtime

    monkeypatch.setattr(runtime, "VK_TOKENS", {"VALSTAN": "env-owner"})
    publisher = VKPublisher(vk_client=object())
    publisher._policy = _FakePolicy({})

    assert await publisher._photo_upload_client() is None
    assert _fake_client == []


@pytest.mark.asyncio
async def test_owner_account_name_is_configurable(_no_env_tokens, _fake_client, monkeypatch):
    """Имя аккаунта-владельца задаётся переменной (по умолчанию VALSTAN)."""
    monkeypatch.setenv("VK_PHOTO_UPLOAD_TOKEN_NAMES", "MAMA, VALSTAN")
    publisher = VKPublisher(vk_client=object())
    publisher._policy = _FakePolicy({"VALSTAN": "tok-owner"})

    client = await publisher._photo_upload_client()

    assert client.token == "tok-owner"
    assert publisher._policy.asked == ["MAMA", "VALSTAN"]
