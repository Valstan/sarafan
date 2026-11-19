"""Вебхук MAX: нажатие «Ответить автору» → ответ автору заявки.

Проверяем то, что ломается тихо:

- **неавторизованный запрос не проходит** (иначе любой, кто знает URL, мог бы
  нажимать кнопки от имени владельца);
- **MAX всегда получает 200** — иначе после трёх попыток (60 с, 150 с, 375 с)
  платформа **отключит подписку**, и канал молча умрёт;
- **нажатие действительно отправляет ответ автору**, а не просто гасится;
- **чужой payload игнорируется** (кнопки с другим действием — не наши).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import max_webhook as api


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    return TestClient(app)


class _FakeRequest:
    def __init__(self, **kw):
        self.id = kw.get("id", 42)
        self.origin = kw.get("origin", "suggested")
        self.community_vk_id = kw.get("community_vk_id", -123456)
        self.community_name = kw.get("community_name", "Малмыж ИНФО")
        self.vk_post_id = kw.get("vk_post_id", 777)
        self.author_name = kw.get("author_name", "Иван Петров")
        self.text_snapshot = kw.get("text_snapshot", "Размещу рекламу")
        self.photo_urls_json = kw.get("photo_urls_json", None)
        self.peer_id = kw.get("peer_id", 555)
        self.author_is_group = False
        self.prepared_message = kw.get("prepared_message", None)
        self.client_id = None
        self.status = "new"
        self.max_notified_at = None


class _FakeSession:
    def __init__(self, request):
        self._request = request
        self.committed = False

    async def get(self, model, pk):
        return self._request if self._request and self._request.id == pk else None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


def _patch_db(monkeypatch, request):
    import database.connection as db_conn

    @asynccontextmanager
    async def _factory():
        yield _FakeSession(request)

    monkeypatch.setattr(db_conn, "AsyncSessionLocal", _factory)


def _callback_update(payload: str = "ad:42", callback_id: str = "cb-1") -> dict:
    return {
        "type": "message_callback",
        "callback": {
            "timestamp": 1732000000000,
            "callback_id": callback_id,
            "payload": payload,
            "user": {"user_id": 1, "is_bot": False},
        },
        "message": None,
    }


class TestAuth:
    def test_without_secret_is_401(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        resp = client.post("/api/max/webhook", json=[_callback_update()])
        assert resp.status_code == 401

    def test_wrong_secret_is_401(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        resp = client.post(
            "/api/max/webhook",
            json=[_callback_update()],
            headers={"X-Max-Bot-Api-Secret": "ne-tak"},
        )
        assert resp.status_code == 401

    def test_unconfigured_webhook_is_503(self, client, monkeypatch):
        monkeypatch.delenv("MAX_WEBHOOK_SECRET", raising=False)
        resp = client.post("/api/max/webhook", json=[])
        assert resp.status_code == 503


class TestCallback:
    def test_press_sends_reply_and_answers_callback(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        request = _FakeRequest()
        _patch_db(monkeypatch, request)

        sent = {}

        async def fake_build_text(session, ar):
            return "Здравствуйте! Услуги: https://сарафан.вмалмыже.рф/regions/links"

        async def fake_send(session, ar, **kw):
            sent["message"] = kw.get("message")
            return {"success": True, "vk_message_id": 99, "via": "community-token"}

        import modules.ad_cabinet.send_reply as sr

        monkeypatch.setattr(sr, "build_reply_text", fake_build_text)
        monkeypatch.setattr(sr, "send_reply_to_author", fake_send)

        answered = []
        monkeypatch.setattr(
            api.max_http, "answer_callback", lambda cb, text=None: answered.append((cb, text))
        )

        resp = client.post(
            "/api/max/webhook",
            json=[_callback_update()],
            headers={"X-Max-Bot-Api-Secret": "sekret"},
        )
        assert resp.status_code == 200
        assert resp.json()["handled"] == 1
        assert "Услуги" in sent["message"]
        assert answered and answered[0][0] == "cb-1"

    def test_reply_blocked_gives_owner_a_deeplink(self, client, monkeypatch):
        """VK не даёт сообществу писать первым → владельцу возвращается ссылка
        на его личный диалог, а не «ничего не вышло»."""
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        _patch_db(monkeypatch, _FakeRequest())

        async def fake_build_text(session, ar):
            return "текст"

        async def fake_send(session, ar, **kw):
            return {
                "allowed": False,
                "personal_deeplink": "https://vk.com/im?sel=555",
                "prepared_message": "текст",
            }

        import modules.ad_cabinet.send_reply as sr

        monkeypatch.setattr(sr, "build_reply_text", fake_build_text)
        monkeypatch.setattr(sr, "send_reply_to_author", fake_send)

        answered = []
        monkeypatch.setattr(
            api.max_http, "answer_callback", lambda cb, text=None: answered.append((cb, text))
        )

        resp = client.post(
            "/api/max/webhook",
            json=[_callback_update()],
            headers={"X-Max-Bot-Api-Secret": "sekret"},
        )
        assert resp.status_code == 200
        assert answered
        assert "vk.com/im" in answered[0][1]

    def test_foreign_payload_is_ignored(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        _patch_db(monkeypatch, _FakeRequest())
        resp = client.post(
            "/api/max/webhook",
            json=[_callback_update(payload="что-то другое")],
            headers={"X-Max-Bot-Api-Secret": "sekret"},
        )
        assert resp.status_code == 200
        assert resp.json()["handled"] == 0

    def test_unknown_request_does_not_break_the_batch(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        _patch_db(monkeypatch, _FakeRequest(id=999))
        resp = client.post(
            "/api/max/webhook",
            json=[_callback_update()],
            headers={"X-Max-Bot-Api-Secret": "sekret"},
        )
        assert resp.status_code == 200
        assert resp.json()["handled"] == 0

    def test_internal_error_still_returns_200(self, client, monkeypatch):
        """Подписка MAX отключается после трёх неудачных попыток — значит, даже
        наша ошибка обязана отвечать 200, иначе канал молча умрёт."""
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        _patch_db(monkeypatch, _FakeRequest())

        import modules.ad_cabinet.send_reply as sr

        async def boom(session, ar, **kw):
            raise RuntimeError("сломалось")

        monkeypatch.setattr(sr, "build_reply_text", lambda *a, **k: None)
        monkeypatch.setattr(sr, "send_reply_to_author", boom)

        resp = client.post(
            "/api/max/webhook",
            json=[_callback_update()],
            headers={"X-Max-Bot-Api-Secret": "sekret"},
        )
        assert resp.status_code == 200


class TestPayloadShapes:
    def test_single_update_object_is_accepted(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        resp = client.post(
            "/api/max/webhook",
            json=_callback_update(payload="foreign"),
            headers={"X-Max-Bot-Api-Secret": "sekret"},
        )
        assert resp.status_code == 200

    def test_unparsable_body_is_200(self, client, monkeypatch):
        monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sekret")
        resp = client.post(
            "/api/max/webhook",
            content=b"not json",
            headers={
                "X-Max-Bot-Api-Secret": "sekret",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
