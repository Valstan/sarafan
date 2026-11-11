"""Тесты публичной двери слоя условий услуг (``/api/service-conditions``).

Пилот #386, шаг 1. Проверяем ровно то, что должно быть правдой снаружи:

- карточка услуги отдаёт условия, документы, владельца **и ссылку на
  источник** (иначе цитата непроверяема);
- неизвестная услуга — 404, а не пустой 200: молчание должно отличаться от
  «условий нет»;
- вопрос без условия даёт «не знаю» + кто отвечает (жёлтый пример приёмки),
  а не правдоподобную выдумку;
- пути открыты публично (их читают серверы соседних проектов без cookie) и
  при этом остаются узкими.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import service_conditions as api


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    return TestClient(app)


class TestCatalogEndpoint:
    def test_returns_all_services_with_conditions(self, client):
        resp = client.get("/api/service-conditions")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["version"]
        assert payload["source"] == "setka"
        titles = {s["title"] for s in payload["services"]}
        assert "Культура Малмыжского района" in titles
        assert len(payload["services"]) == 11

    def test_every_condition_carries_its_source(self, client):
        payload = client.get("/api/service-conditions").json()
        seen = 0
        for service in payload["services"]:
            for condition in service["conditions"]:
                assert condition["source_ref"].startswith("https://")
                assert condition["owner"].strip()
                assert condition["question"].strip()
                assert condition["then"].strip()
                seen += 1
        assert seen >= 10, "условий слишком мало — слой пуст"

    def test_service_without_conditions_explains_itself(self, client):
        payload = client.get("/api/service-conditions").json()
        by_title = {s["title"]: s for s in payload["services"]}
        radar = by_title["Радар"]
        assert radar["conditions"] == []
        assert radar["conditions_note"], "условий нет, но не сказано почему"

    def test_response_has_no_secrets(self, client):
        """Ответ читают чужие серверы — в нём не должно быть ни ключей, ни
        внутренних адресов."""
        raw = client.get("/api/service-conditions").text
        for secret in ("API_KEY", "SECRET", "password", "SECRETS_TOKEN"):
            assert secret not in raw


class TestSingleServiceEndpoint:
    def test_returns_conditions_for_known_service(self, client):
        resp = client.get("/api/service-conditions/Культура%20Малмыжского%20района")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["service"] == "Культура Малмыжского района"
        assert payload["conditions"]
        assert all(c["source_ref"] for c in payload["conditions"])

    def test_unknown_service_is_404_not_empty_200(self, client):
        """Молчание и «услуги нет» — разные вещи. Пустой 200 выглядел бы как
        «условий нет» (то есть как выдумка)."""
        resp = client.get("/api/service-conditions/Нет такой услуги")
        assert resp.status_code == 404


class TestAnswerEndpoint:
    def test_known_question_answers_with_source(self, client):
        resp = client.get(
            "/api/service-answer",
            params={
                "service": "Культура Малмыжского района",
                "question": "Как записаться в кружок или студию?",
            },
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["answer"]
        stmt = payload["answer"][0]
        assert stmt["source"]["kind"] == "service_condition"
        assert stmt["source"]["ref"].startswith("https://")
        assert stmt["source"]["verified"] is True

    def test_unknown_question_answers_dont_know(self, client):
        """Жёлтый пример приёмки: вопрос вне каталога ⇒ «не знаю» + кто
        отвечает, а не выдумка."""
        resp = client.get(
            "/api/service-answer",
            params={"service": "Культура Малмыжского района", "question": "Как получить субсидию?"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["answer"] == []
        assert payload["missing"]
        assert payload["owner"].strip()

    def test_unknown_service_is_404(self, client):
        resp = client.get("/api/service-answer", params={"service": "Нет такой", "question": "x"})
        assert resp.status_code == 404

    def test_missing_service_param_is_422(self, client):
        resp = client.get("/api/service-answer", params={"question": "x"})
        assert resp.status_code == 422


class TestAuthGate:
    def test_paths_are_public_and_narrow(self):
        from middleware.auth_gate import PUBLIC_EXACT, PUBLIC_PREFIXES

        for path in ("/api/service-conditions", "/api/service-answer"):
            assert path in PUBLIC_EXACT
            assert not [p for p in PUBLIC_PREFIXES if path.startswith(p)]

    def test_operator_pages_stay_closed(self):
        from middleware.auth_gate import PUBLIC_EXACT

        for path in ("/api/advertiser/me", "/api/ad-cabinet/requests", "/api/regions"):
            assert path not in PUBLIC_EXACT
