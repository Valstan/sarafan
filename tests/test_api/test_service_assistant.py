"""Тесты ассистента единого профиля жителя (``/api/service-assistant``).

Пилот #386, шаг 3. Проверяем ровно то, что должно быть правдой:

- **цитата обязательна**: у каждого утверждения есть ``source`` (вид источника
  и ссылка), а у условия — ссылка на публичную страницу владельца;
- **жёлтый пример приёмки**: вопрос вне каталога ⇒ пустой ответ + что
  спросить + кто отвечает, а не правдоподобная выдумка;
- **факты не копируются**: факт приходит одним запросом к сервису-владельцу,
  живёт только в ответе и не сохраняется;
- **«не знаю» — валидный ответ**: факта нет ⇒ утверждения нет, а ``missing``
  говорит, что спросить у жителя;
- **факты закрыты ключом**: ответ с ``esa_sub`` без ключа — 401.

Плюс дверь: путь публичный и узкий, в ответе нет секретов.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules import service_assistant as assistant
from modules.profile_facts import FactOutcome
from web.api import service_assistant as api


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    return TestClient(app)


def _fact_ok(key: str = "benefit_category", value: str = "veteran", verified: bool = False):
    """Подмена сервиса-владельца: факт найден.

    Подтверждённым факт может быть только у источника, который его
    подтвердил (документ, ведомство) — сам житель факт не подтверждает, поэтому
    у подтверждённого факта ``source`` не ``self`` (как и в Казанской:
    ``factIsVerified``).
    """

    def _fetch(*, sub, key, question):
        return FactOutcome(
            status="ok",
            key=key,
            value=value,
            source="doc:passport" if verified else "self",
            verified_at="2026-11-19T10:00:00" if verified else None,
        )

    return _fetch


def _fact_missing(*, sub, key, question):
    """Подмена сервиса-владельца: факта нет."""
    return FactOutcome(status="not_found", key=key)


class TestCatalogAnswers:
    def test_known_question_answers_with_citation(self, client):
        resp = client.post(
            "/api/service-assistant",
            json={"question": "Как записаться в кружок или студию?"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["answer"]
        stmt = payload["answer"][0]
        assert stmt["source"]["kind"] == "service_condition"
        assert stmt["source"]["ref"].startswith("https://")
        assert stmt["source"]["verified"] is True
        assert stmt["statement"].strip()
        assert payload["owner"].strip()

    def test_every_statement_carries_a_source(self, client):
        """Правило 1 контракта: нет источника — нет утверждения."""
        resp = client.post(
            "/api/service-assistant",
            json={"question": "Хочу вступить в кружок, студию или секцию"},
        )
        payload = resp.json()
        assert payload["answer"]
        for stmt in payload["answer"]:
            assert stmt["source"]["kind"] in ("service_condition", "profile_fact", "canon")
            assert stmt["source"]["ref"].strip()

    def test_unknown_question_answers_dont_know(self, client):
        """Жёлтый пример приёмки: вопрос вне каталога ⇒ «не знаю» + кто
        отвечает, а не выдумка."""
        resp = client.post(
            "/api/service-assistant",
            json={"question": "Как получить субсидию на лечение зубов?"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["answer"] == []
        assert payload["missing"]
        assert payload["owner"].strip()

    def test_one_word_question_is_not_guessed(self, client):
        """Одно слово ничего не различает — «не знаю» честнее угадывания."""
        resp = client.post("/api/service-assistant", json={"question": "скидка"})
        payload = resp.json()
        assert payload["answer"] == []
        assert payload["missing"]

    def test_empty_question_is_422(self, client):
        assert client.post("/api/service-assistant", json={"question": ""}).status_code == 422
        assert client.post("/api/service-assistant", json={}).status_code == 422


class TestFacts:
    def test_fact_is_requested_for_matching_question(self, monkeypatch):
        calls = []

        def _fetch(*, sub, key, question):
            calls.append((sub, key, question))
            return _fact_ok()(sub=sub, key=key, question=question)

        monkeypatch.setattr(assistant, "fetch_fact", _fetch)
        payload = assistant.ask("какая у меня льготная категория?", esa_sub="sub-123")
        assert calls, "за фактом не пошли, а вопрос именно о факте"
        assert calls[0][0] == "sub-123"
        assert calls[0][1] == "benefit_category"
        facts = [s for s in payload["answer"] if s["source"]["kind"] == "profile_fact"]
        assert facts, "факт найден, но в ответе его нет"
        assert facts[0]["source"]["ref"] == "kazanskaya/benefit_category"

    def test_self_declared_fact_is_marked_as_user_data(self, monkeypatch):
        """Правило 2 контракта: самопровозглашённый факт помечается «по вашим
        данным» и прав не обещает."""
        monkeypatch.setattr(assistant, "fetch_fact", _fact_ok())
        payload = assistant.ask("какая у меня льготная категория?", esa_sub="sub-123")
        fact = next(s for s in payload["answer"] if s["source"]["kind"] == "profile_fact")
        assert fact["note"] == "по вашим данным"
        assert fact["source"]["verified"] is False

    def test_verified_fact_is_marked_verified(self, monkeypatch):
        monkeypatch.setattr(assistant, "fetch_fact", _fact_ok(verified=True))
        payload = assistant.ask("какая у меня льготная категория?", esa_sub="sub-123")
        fact = next(s for s in payload["answer"] if s["source"]["kind"] == "profile_fact")
        assert fact["source"]["verified"] is True
        assert fact["note"] is None

    def test_missing_fact_gives_question_not_invention(self, monkeypatch):
        """Факта нет ⇒ утверждения нет, а missing говорит, что спросить."""
        monkeypatch.setattr(assistant, "fetch_fact", _fact_missing)
        payload = assistant.ask("какая у меня льготная категория?", esa_sub="sub-123")
        assert payload["answer"] == []
        assert payload["missing"]
        assert any("льготн" in item.lower() for item in payload["missing"])
        assert payload["owner"].strip()

    def test_no_fact_request_without_resident(self, monkeypatch):
        """Без жителя спрашивать некого — и ходить к сервису-владельцу незачем."""
        calls = []
        monkeypatch.setattr(
            assistant, "fetch_fact", lambda **kw: calls.append(kw) or _fact_missing(**kw)
        )
        payload = assistant.ask("какая у меня льготная категория?")
        assert not calls, "пошли за фактом, не имея жителя"
        assert payload["answer"] == []
        assert payload["missing"]

    def test_question_about_rights_is_not_answered_by_a_fact_alone(self, monkeypatch):
        """Факт — ответ на вопрос О НЁМ. На вопрос «положена ли субсидия» факт
        «льготная категория» ответом не является: там нужен владелец услуги."""
        calls = []
        monkeypatch.setattr(
            assistant, "fetch_fact", lambda **kw: calls.append(kw) or _fact_ok()(**kw)
        )
        payload = assistant.ask("положена ли мне субсидия на лечение?", esa_sub="sub-123")
        assert not calls, "пошли за фактом, хотя вопрос не о факте"
        assert payload["answer"] == []
        assert payload["missing"]
        assert payload["owner"].strip()

    def test_facts_are_not_copied_anywhere(self):
        """Правило плана «факты не копируются» — проверяем по коду: в модулях
        ассистента и клиента фактов нет ни хранилища, ни кэша, ни импорта БД."""
        import inspect

        for module in (assistant, __import__("modules.profile_facts", fromlist=["x"])):
            source = inspect.getsource(module)
            assert "database" not in source.lower(), "в модуле фактов появилась БД"
            for marker in ("INSERT", "UPDATE ", "session.add", "cache"):
                assert marker not in source, f"в модуле фактов появился след: {marker}"


class TestFactDoorProtection:
    def test_fact_path_requires_key(self, client, monkeypatch):
        monkeypatch.setenv("KAZANSKAYA_INGEST_KEY", "secret-key")
        monkeypatch.setattr(assistant, "fetch_fact", _fact_ok())
        resp = client.post(
            "/api/service-assistant",
            json={"question": "какая у меня льготная категория?", "esa_sub": "sub-123"},
        )
        assert resp.status_code == 401
        assert "answer" not in resp.json()

    def test_fact_path_works_with_key(self, client, monkeypatch):
        monkeypatch.setenv("KAZANSKAYA_INGEST_KEY", "secret-key")
        monkeypatch.setattr(assistant, "fetch_fact", _fact_ok())
        resp = client.post(
            "/api/service-assistant",
            json={"question": "какая у меня льготная категория?", "esa_sub": "sub-123"},
            headers={"X-API-Key": "secret-key"},
        )
        assert resp.status_code == 200
        assert resp.json()["answer"]

    def test_unconfigured_fact_path_is_503_not_401(self, client, monkeypatch):
        """«Нет ключа» и «не тот ключ» — разные вещи."""
        monkeypatch.delenv("KAZANSKAYA_INGEST_KEY", raising=False)
        resp = client.post(
            "/api/service-assistant",
            json={"question": "какая у меня льготная категория?", "esa_sub": "sub-123"},
            headers={"X-API-Key": "whatever"},
        )
        assert resp.status_code == 503


class TestDoor:
    def test_path_is_public_and_narrow(self):
        from middleware.auth_gate import PUBLIC_EXACT, PUBLIC_PREFIXES

        assert "/api/service-assistant" in PUBLIC_EXACT
        assert not [p for p in PUBLIC_PREFIXES if "/api/service-assistant".startswith(p)]

    def test_response_has_no_secrets(self, client):
        raw = client.post(
            "/api/service-assistant", json={"question": "Как записаться в кружок или студию?"}
        ).text
        for secret in ("API_KEY", "SECRET", "password", "SECRETS_TOKEN", "X-API-Key"):
            assert secret not in raw

    def test_operator_pages_stay_closed(self):
        from middleware.auth_gate import PUBLIC_EXACT

        for path in ("/api/advertiser/me", "/api/regions"):
            assert path not in PUBLIC_EXACT
