"""Тесты скрипта MAX-канала (`scripts/max_channel.py`).

Сеть не трогаем: проверяются чистые части — тело подписки, разбор кодов двери
и проверка «задана ли переменная окружения» без печати значения.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "max_channel.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("max_channel", _SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_subscription_payload_has_url_types_and_secret(mod):
    """Тело подписки: url + update_types + secret."""
    payload = json.loads(
        mod.subscription_payload("https://x.example/api/max/webhook", "sekret").decode()
    )
    assert payload["url"] == "https://x.example/api/max/webhook"
    assert payload["update_types"] == ["message_callback", "bot_started"]
    assert payload["secret"] == "sekret"


def test_subscription_payload_is_bytes(mod):
    """Подписка уходит POST-ом — тело должно быть байтами."""
    assert isinstance(mod.subscription_payload("https://x/y", "s"), bytes)


def test_explain_code_200_is_alive(mod):
    assert "живой" in mod.explain_webhook_code(200)


def test_explain_code_503_means_secret_not_set(mod):
    """503 — секрет на сервере не задан (не «маршрута нет»)."""
    text = mod.explain_webhook_code(503)
    assert "503" in text
    assert "секрет" in text.lower()


def test_explain_code_401_means_secret_mismatch(mod):
    assert "401" in mod.explain_webhook_code(401)


def test_explain_code_404_means_stale_web_service(mod):
    """404 — веб поднят на старом коде: маршрута ещё нет."""
    assert "рестарт" in mod.explain_webhook_code(404)


def test_has_reports_presence_without_value(mod, monkeypatch):
    """Проверка env говорит только «есть / нет» — значение не уходит наружу."""
    monkeypatch.setenv("MAX_TOKEN", "секретное-значение")
    assert mod._has("MAX_TOKEN") is True
    monkeypatch.delenv("MAX_TOKEN", raising=False)
    assert mod._has("MAX_TOKEN") is False
    monkeypatch.setenv("MAX_TOKEN", "   ")
    assert mod._has("MAX_TOKEN") is False
