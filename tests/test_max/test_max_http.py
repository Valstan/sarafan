"""Транспорт Bot API MAX — auth, повторы, ошибки.

Держим три вещи, каждая закрывает свой класс отказа:

- **Токен только в заголовке.** Платформа больше не принимает query-параметры, а
  токен в URL утекает в лог (урок G239 мозга — тот же, что мы сделали для
  Telegram-релея).
- **Повтор только на сетевой отказ.** HTTP-ответ (4xx/5xx, 429) доказывает, что
  соединение было, а повтор ``sendMessage`` задвоил бы сообщение.
- **Ошибка MAX не прячется в «пустой ответ».** ``MaxError`` несёт код и текст:
  «сеть не ответила» и «MAX сказал 403» лечатся по-разному.
"""

from __future__ import annotations

import pytest
import requests

from modules import max_http


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


@pytest.fixture(autouse=True)
def _max_env(monkeypatch):
    monkeypatch.setenv("MAX_TOKEN", "tok-123")
    monkeypatch.delenv("MAX_API_BASE", raising=False)


class TestAuth:
    def test_token_goes_into_header_not_url(self, monkeypatch):
        seen = {}

        def fake_request(method, url, **kw):
            seen["url"] = url
            seen["headers"] = kw.get("headers") or {}
            seen["params"] = kw.get("params")
            return _Resp()

        monkeypatch.setattr(requests, "request", fake_request)
        max_http.post("/messages", json={"text": "hi"})

        assert seen["headers"]["Authorization"] == "tok-123"
        assert "tok-123" not in seen["url"]
        assert "tok-123" not in (seen["params"] or {})

    def test_enabled_requires_token_and_chat(self, monkeypatch):
        monkeypatch.setenv("MAX_TOKEN", "tok")
        monkeypatch.setenv("MAX_CHAT_ID", "42")
        assert max_http.enabled() is True
        monkeypatch.setenv("MAX_CHAT_ID", "")
        assert max_http.enabled() is False
        monkeypatch.setenv("MAX_TOKEN", "")
        monkeypatch.setenv("MAX_CHAT_ID", "42")
        assert max_http.enabled() is False

    def test_without_token_request_fails_fast(self, monkeypatch):
        monkeypatch.setenv("MAX_TOKEN", "")
        with pytest.raises(max_http.MaxError) as exc:
            max_http.post("/messages", json={})
        assert "MAX_TOKEN" in str(exc.value)


class TestRetries:
    def test_network_error_is_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_request(method, url, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ConnectTimeout("syn lost")
            return _Resp()

        monkeypatch.setattr(requests, "request", fake_request)
        result = max_http.post("/messages", json={"text": "hi"})
        assert result["ok"] is True
        assert calls["n"] == 3

    def test_http_error_is_not_retried(self, monkeypatch):
        calls = {"n": 0}

        def fake_request(method, url, **kw):
            calls["n"] += 1
            return _Resp(403, {"error_code": 403, "description": "forbidden"})

        monkeypatch.setattr(requests, "request", fake_request)
        with pytest.raises(max_http.MaxError) as exc:
            max_http.post("/messages", json={"text": "hi"})
        assert exc.value.code == 403
        assert exc.value.description == "forbidden"
        assert calls["n"] == 1, "HTTP-ответ повторять нельзя — соединение было"


class TestErrorShape:
    def test_error_code_and_description_are_kept(self, monkeypatch):
        def fake_request(method, url, **kw):
            return _Resp(429, {"error_code": 429, "description": "flood control"})

        monkeypatch.setattr(requests, "request", fake_request)
        with pytest.raises(max_http.MaxError) as exc:
            max_http.post("/messages", json={})
        assert exc.value.code == 429
        assert "flood control" in exc.value.description

    def test_non_json_error_body_is_readable(self, monkeypatch):
        def fake_request(method, url, **kw):
            return _Resp(500, None)

        monkeypatch.setattr(requests, "request", fake_request)
        with pytest.raises(max_http.MaxError) as exc:
            max_http.post("/messages", json={})
        assert exc.value.status_code == 500


class TestSendMessage:
    def test_chat_id_goes_to_query_and_body_has_text(self, monkeypatch):
        seen = {}

        def fake_request(method, url, **kw):
            seen.update(url=url, json=kw.get("json"), params=kw.get("params"))
            return _Resp()

        monkeypatch.setattr(requests, "request", fake_request)
        max_http.send_message("777", "привет")

        assert seen["params"] == {"chat_id": "777"}
        assert seen["json"]["text"] == "привет"

    def test_attachments_are_passed_through(self, monkeypatch):
        seen = {}

        def fake_request(method, url, **kw):
            seen.update(json=kw.get("json"))
            return _Resp()

        monkeypatch.setattr(requests, "request", fake_request)
        keyboard = [{"type": "inline_keyboard", "payload": {"buttons": [[{"type": "link"}]]}}]
        max_http.send_message("777", "привет", attachments=keyboard)
        assert seen["json"]["attachments"] == keyboard


class TestAnswerCallback:
    def test_body_is_wrapped_into_message(self, monkeypatch):
        """Схема CallbackAnswer — ``{message: {text}}``, не плоский объект.

        Плоский ``{"text": ...}`` MAX тихо проигнорирует, и нажатие будет
        выглядеть как «часик» без всякой реакции.
        """
        seen = {}

        def fake_request(method, url, **kw):
            seen.update(url=url, json=kw.get("json"), params=kw.get("params"))
            return _Resp()

        monkeypatch.setattr(requests, "request", fake_request)
        max_http.answer_callback("cb-1", "готово")

        assert seen["url"].endswith("/answers")
        assert seen["params"] == {"callback_id": "cb-1"}
        assert seen["json"] == {"message": {"text": "готово"}}


class TestBaseUrl:
    def test_default_is_platform_api2(self, monkeypatch):
        monkeypatch.delenv("MAX_API_BASE", raising=False)
        assert max_http.api_base() == "https://platform-api2.max.ru"

    def test_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("MAX_API_BASE", "https://proxy.example/max/")
        assert max_http.api_base() == "https://proxy.example/max"


class TestChatId:
    def test_env_wins_over_learned(self, monkeypatch):
        """Явный env важнее выученного: он поставлен руками и осознанно."""
        monkeypatch.setenv("MAX_CHAT_ID", "111")
        monkeypatch.setattr(max_http, "learned_chat_id", lambda: "222")
        assert max_http.chat_id() == "111"

    def test_learned_is_used_when_env_is_empty(self, monkeypatch):
        """Chat_id приходит из события bot_started — копировать руками не надо."""
        monkeypatch.setenv("MAX_CHAT_ID", "")
        monkeypatch.setattr(max_http, "learned_chat_id", lambda: "222")
        assert max_http.chat_id() == "222"

    def test_enabled_needs_token_and_chat(self, monkeypatch):
        monkeypatch.setenv("MAX_TOKEN", "tok")
        monkeypatch.setenv("MAX_CHAT_ID", "111")
        monkeypatch.setattr(max_http, "learned_chat_id", lambda: "")
        assert max_http.enabled() is True
        monkeypatch.setenv("MAX_CHAT_ID", "")
        assert max_http.enabled() is False
