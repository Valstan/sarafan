"""modules.telegram_http — повторы только на сетевой отказ (G307).

Держим: ConnectTimeout/ConnectionError повторяются до ATTEMPTS с коротким таймаутом
на попытку; ReadTimeout и любой HTTP-ответ (включая 5xx/429) — НЕ повторяются;
после всех попыток исключение поднимается наружу, а не глотается.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from modules import telegram_http as th


def _resp(status=200):
    return SimpleNamespace(status_code=status, ok=status == 200, text="", json=lambda: {"ok": True})


def test_retries_connect_timeout_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["timeout"])
        if len(calls) < 3:
            raise requests.exceptions.ConnectTimeout("syn lost")
        return _resp()

    monkeypatch.setattr(requests, "post", fake_post)
    r = th.post("https://api.telegram.org/botX/sendMessage", json={}, sleep=None)
    assert r.status_code == 200
    assert calls == [th.ATTEMPT_TIMEOUT] * 3


def test_gives_up_after_attempts_and_raises(monkeypatch):
    n = {"c": 0}

    def fake_post(url, **kw):
        n["c"] += 1
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(requests.exceptions.ConnectionError):
        th.post("https://api.telegram.org/botX/sendMessage", json={}, sleep=None)
    assert n["c"] == th.ATTEMPTS


def test_http_error_response_is_not_retried(monkeypatch):
    n = {"c": 0}

    def fake_post(url, **kw):
        n["c"] += 1
        return _resp(429)

    monkeypatch.setattr(requests, "post", fake_post)
    r = th.post("https://api.telegram.org/botX/sendMessage", json={}, sleep=None)
    assert r.status_code == 429 and n["c"] == 1


def test_read_timeout_is_not_retried(monkeypatch):
    """Соединение было — сообщение могло уйти; повтор задвоил бы его."""
    n = {"c": 0}

    def fake_post(url, **kw):
        n["c"] += 1
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(requests.exceptions.ReadTimeout):
        th.post("https://api.telegram.org/botX/sendMessage", json={}, sleep=None)
    assert n["c"] == 1


def test_get_retries_too_and_sleeps_between(monkeypatch):
    slept = []
    calls = {"c": 0}

    def fake_get(url, **kw):
        calls["c"] += 1
        if calls["c"] == 1:
            raise requests.exceptions.ConnectTimeout("syn lost")
        return _resp()

    monkeypatch.setattr(requests, "get", fake_get)
    r = th.get("https://api.telegram.org/botX/getMe", sleep=slept.append)
    assert r.status_code == 200 and slept == [0.5]


def test_defaults_are_short_and_many():
    assert th.ATTEMPT_TIMEOUT <= 8 and th.ATTEMPTS >= 6


# --- База API: relay вместо прямого api.telegram.org (G63 / #041) -----------
#
# С 06.11.2026 api.telegram.org с бокса не отвечает (РКН-блок IP Telegram):
# 391–655 отказов в сутки по ~40 с. Выход — relay (Cloudflare Worker). Здесь
# держим контракт переписывания: адрес уходит на базу relay'а, токен бота едет
# ЗАГОЛОВКОМ (секрет в пути утекает в лог — G239), гейт relay'а — X-Relay-Secret.


def test_without_base_url_is_untouched(monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    monkeypatch.delenv("TG_RELAY_SECRET", raising=False)
    url = "https://api.telegram.org/bot123:ABC/sendMessage"
    headers = {}
    assert th.rewrite_url(url, headers) == url
    assert headers == {}


def test_with_base_url_token_goes_to_header(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example.workers.dev/bot")
    monkeypatch.setenv("TG_RELAY_SECRET", "s3cr3t")
    headers = {}
    url = th.rewrite_url("https://api.telegram.org/bot123:ABC/sendMessage", headers)
    assert url == "https://relay.example.workers.dev/bot/sendMessage"
    assert headers["X-TG-Token"] == "123:ABC"
    assert headers["X-Relay-Secret"] == "s3cr3t"


def test_query_string_is_kept(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example.workers.dev/bot")
    headers = {}
    url = th.rewrite_url("https://api.telegram.org/botT/getUpdates?timeout=25", headers)
    assert url == "https://relay.example.workers.dev/bot/getUpdates?timeout=25"


def test_foreign_url_is_not_rewritten(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example.workers.dev/bot")
    headers = {}
    url = "https://example.com/anything"
    assert th.rewrite_url(url, headers) == url
    assert headers == {}


def test_post_goes_to_relay_with_headers(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example.workers.dev/bot")
    monkeypatch.setenv("TG_RELAY_SECRET", "s3cr3t")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["headers"] = kw.get("headers")
        return _resp()

    monkeypatch.setattr(requests, "post", fake_post)
    r = th.post("https://api.telegram.org/bot123:ABC/sendMessage", json={"a": 1}, sleep=None)
    assert r.status_code == 200
    assert seen["url"] == "https://relay.example.workers.dev/bot/sendMessage"
    assert seen["headers"]["X-TG-Token"] == "123:ABC"


def test_caller_headers_are_not_lost(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example.workers.dev/bot")
    monkeypatch.delenv("TG_RELAY_SECRET", raising=False)
    seen = {}

    def fake_post(url, **kw):
        seen["headers"] = kw.get("headers")
        return _resp()

    monkeypatch.setattr(requests, "post", fake_post)
    th.post(
        "https://api.telegram.org/botT/sendMessage",
        headers={"X-Custom": "keep"},
        sleep=None,
    )
    assert seen["headers"]["X-Custom"] == "keep"
    assert seen["headers"]["X-TG-Token"] == "T"
