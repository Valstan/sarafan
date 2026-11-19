"""Карточка рекламной заявки в MAX — что видит владелец и что он нажимает.

Карточка — это уведомление **и** кнопка ответа. Значит, её форма обязана
нести всё, что нужно для решения за секунду: фото (о чём речь), текст
(что предлагают), ссылку (открыть целиком) и две кнопки — «услуги» (что мы
предлагаем) и «ответить автору» (что делать).

И главное — **цитата обязательна**: без ссылки на источник карточка ничем не
лучше уведомления «новых заявок: 3».
"""

from __future__ import annotations

from modules import max_notifier


class _Request:
    """Минимальная заявка (как строка БД)."""

    def __init__(self, **kw):
        self.id = kw.get("id", 42)
        self.origin = kw.get("origin", "suggested")
        self.community_vk_id = kw.get("community_vk_id", -123456)
        self.community_name = kw.get("community_name", "Малмыж ИНФО")
        self.vk_post_id = kw.get("vk_post_id", 777)
        self.author_name = kw.get("author_name", "Иван Петров")
        self.text_snapshot = kw.get("text_snapshot", "Размещу рекламу у себя, цена 5000")
        self.photo_urls_json = kw.get("photo_urls_json", None)


class TestPayload:
    def test_round_trip(self):
        payload = max_notifier.callback_payload(99)
        assert max_notifier.ad_request_id_from_payload(payload) == 99

    def test_foreign_payload_is_ignored(self):
        assert max_notifier.ad_request_id_from_payload("something-else") is None
        assert max_notifier.ad_request_id_from_payload("ad:not-a-number") is None
        assert max_notifier.ad_request_id_from_payload("") is None


class TestCard:
    def test_text_carries_who_where_and_what(self):
        card = max_notifier.build_card(
            ad_request_id=42,
            community_name="Малмыж ИНФО",
            author_name="Иван Петров",
            text_snapshot="Размещу рекламу у себя, цена 5000",
            source_url="https://vk.com/wall-123456_777",
        )
        assert "Иван Петров" in card["text"]
        assert "Малмыж ИНФО" in card["text"]
        assert "Размещу рекламу" in card["text"]
        assert "https://vk.com/wall-123456_777" in card["text"]

    def test_keyboard_has_services_link_and_reply_button(self):
        card = max_notifier.build_card(
            ad_request_id=42,
            community_name="Малмыж ИНФО",
            author_name="Иван",
            text_snapshot="реклама",
        )
        keyboard = next(a for a in card["attachments"] if a["type"] == "inline_keyboard")
        rows = keyboard["payload"]["buttons"]
        links = [b for row in rows for b in row if b["type"] == "link"]
        callbacks = [b for row in rows for b in row if b["type"] == "callback"]
        assert links and links[0]["url"] == max_notifier.services_url()
        assert callbacks and callbacks[0]["payload"] == "ad:42"

    def test_photo_is_attached_when_known(self):
        card = max_notifier.build_card(
            ad_request_id=42,
            community_name="М",
            author_name="И",
            text_snapshot="реклама",
            photo_url="https://cdn/photo.jpg",
        )
        image = next(a for a in card["attachments"] if a["type"] == "image")
        assert image["payload"]["url"] == "https://cdn/photo.jpg"

    def test_no_photo_no_image_attachment(self):
        card = max_notifier.build_card(
            ad_request_id=42,
            community_name="М",
            author_name="И",
            text_snapshot="реклама",
        )
        assert not [a for a in card["attachments"] if a["type"] == "image"]

    def test_long_text_is_truncated(self):
        long_text = "а" * (max_notifier.SNIPPET_LIMIT + 200)
        card = max_notifier.build_card(
            ad_request_id=42,
            community_name="М",
            author_name="И",
            text_snapshot=long_text,
        )
        assert len(card["text"]) < len(long_text)
        assert card["text"].rstrip().endswith("…")


class TestCardFromRequest:
    def test_suggestion_gets_post_link(self):
        card = max_notifier.card_for_request(_Request(origin="suggested"))
        assert "https://vk.com/wall123456_777" in card["text"]
        assert "(предложке)" in card["text"]

    def test_inbound_dm_falls_back_to_cabinet_link(self):
        card = max_notifier.card_for_request(
            _Request(origin="inbound_dm", vk_post_id=None), cabinet_url="https://sarafan/ad"
        )
        assert "https://sarafan/ad" in card["text"]
        assert "входящему ЛС" in card["text"]

    def test_photo_taken_from_request(self):
        card = max_notifier.card_for_request(
            _Request(photo_urls_json=["https://cdn/1.jpg", "https://cdn/2.jpg"])
        )
        image = next(a for a in card["attachments"] if a["type"] == "image")
        assert image["payload"]["url"] == "https://cdn/1.jpg"


class TestSendCard:
    def test_without_chat_id_card_is_not_sent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(max_notifier.max_http, "send_message", lambda *a, **k: sent.append(a))
        monkeypatch.setenv("MAX_CHAT_ID", "")
        monkeypatch.setattr(max_notifier.max_http, "chat_id", lambda: "")
        result = max_notifier.send_card({"text": "t", "attachments": []})
        assert result["ok"] is False
        assert result["reason"] == "no_chat_id"
        assert not sent

    def test_learned_chat_id_is_used_when_env_is_empty(self, monkeypatch):
        """Чат выучен из события bot_started — копировать руками не надо."""
        sent = []
        monkeypatch.setattr(max_notifier.max_http, "send_message", lambda *a, **k: sent.append(a))
        monkeypatch.setenv("MAX_TOKEN", "tok")
        monkeypatch.setattr(max_notifier.max_http, "chat_id", lambda: "424242")
        result = max_notifier.send_card({"text": "t", "attachments": []})
        assert result["ok"] is True
        assert sent and sent[0][0] == "424242"

    def test_without_token_card_is_not_sent(self, monkeypatch):
        sent = []
        monkeypatch.setattr(max_notifier.max_http, "send_message", lambda *a, **k: sent.append(a))
        monkeypatch.setenv("MAX_CHAT_ID", "42")
        monkeypatch.setenv("MAX_TOKEN", "")
        result = max_notifier.send_card({"text": "t", "attachments": []})
        assert result["ok"] is False
        assert result["reason"] == "no_token"
        assert not sent

    def test_send_failure_does_not_raise(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("сеть упала")

        monkeypatch.setattr(max_notifier.max_http, "send_message", boom)
        monkeypatch.setenv("MAX_CHAT_ID", "42")
        monkeypatch.setenv("MAX_TOKEN", "tok")
        result = max_notifier.send_card({"text": "t", "attachments": []})
        assert result["ok"] is False
        assert "сеть упала" in result["reason"]
