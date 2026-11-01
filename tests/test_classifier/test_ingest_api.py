"""Tests ingest-API классификатора (web/api/classifier_ingest.py) — auth, gate, эндпоинты.

Мини-FastAPI + TestClient; service замокан (без БД).
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import classifier_ingest as ing

KEY = "routine-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_INGEST_KEY", KEY)
    monkeypatch.delenv("CLASSIFIER_DISABLED", raising=False)
    monkeypatch.delenv("CLASSIFIER_REGION_CODES", raising=False)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ing.router, prefix="/api/classifier")
    return TestClient(app)


def test_pending_requires_key(client):
    assert client.get("/api/classifier/pending").status_code == 401
    assert client.get("/api/classifier/pending", headers={"X-API-Key": "nope"}).status_code == 401


def test_pending_ok_with_key(client):
    fake = AsyncMock(return_value=[{"lip": "1_10", "text": "a"}])
    with patch.object(ing.service, "fetch_pending", fake):
        r = client.get("/api/classifier/pending?limit=5", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1 and body["posts"][0]["lip"] == "1_10"


def test_pending_uses_region_query_over_allowlist(client, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_REGION_CODES", "vp")
    fake = AsyncMock(return_value=[])
    with patch.object(ing.service, "fetch_pending", fake):
        client.get("/api/classifier/pending?region=mi", headers={"X-API-Key": KEY})
    # region-query перекрывает allowlist
    assert fake.await_args.kwargs["region_codes"] == ["mi"]


def test_pending_passes_source_days_default(client):
    # По умолчанию окно свежести — сутки: «жить только свежим» (решение владельца
    # 2026-08-19). Старое всё равно не пойдёт в сводку по правилу 72 часов.
    fake = AsyncMock(return_value=[])
    with patch.object(ing.service, "fetch_pending", fake):
        client.get("/api/classifier/pending", headers={"X-API-Key": KEY})
    assert fake.await_args.kwargs["days"] == 1


def test_pending_source_days_env_override(client, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_SOURCE_DAYS", "5")
    fake = AsyncMock(return_value=[])
    with patch.object(ing.service, "fetch_pending", fake):
        client.get("/api/classifier/pending", headers={"X-API-Key": KEY})
    assert fake.await_args.kwargs["days"] == 5


def test_verdicts_requires_key(client):
    r = client.post("/api/classifier/verdicts", json={"verdicts": []})
    assert r.status_code == 401


def test_verdicts_records(client):
    fake = AsyncMock(return_value={"recorded": 1, "skipped_existing": 0, "skipped_missing": 0})
    payload = {
        "verdicts": [{"lip": "1_10", "theme": "novost", "action": "publish", "confidence": 80}]
    }
    with patch.object(ing.service, "record_verdicts", fake):
        r = client.post("/api/classifier/verdicts", json=payload, headers={"X-API-Key": KEY})
    assert r.status_code == 200
    assert r.json()["recorded"] == 1
    # source='routine' проставлен
    assert fake.await_args.kwargs["source"] == "routine"


def test_verdicts_lenient_repairs_instead_of_422(client):
    # Толерантный разбор (2026-07-16): чинимые перелимиты нормализуются,
    # батч НЕ отвергается — прогон рутины стоит токенов.
    fake = AsyncMock(return_value={"recorded": 1, "skipped_existing": 0, "skipped_missing": 0})
    payload = {"verdicts": [{"lip": "1_10", "theme": "t", "confidence": 500, "text": "x" * 20000}]}
    with patch.object(ing.service, "record_verdicts", fake):
        r = client.post("/api/classifier/verdicts", json=payload, headers={"X-API-Key": KEY})
    assert r.status_code == 200
    assert r.json()["skipped_invalid"] == 0
    verdicts = fake.await_args.args[1]
    assert verdicts[0].confidence == 100  # clamp, не 422
    assert len(verdicts[0].text) == 10000  # обрезано до лимита схемы


def test_verdicts_unfixable_item_skipped_not_fatal(client):
    # Нечинимый (без lip) пропускается со счётчиком, остальные записываются.
    fake = AsyncMock(return_value={"recorded": 1, "skipped_existing": 0, "skipped_missing": 0})
    payload = {
        "verdicts": [
            {"theme": "t"},  # нет lip — нечинимый
            {"lip": "1_11", "theme": "t2"},
        ]
    }
    with patch.object(ing.service, "record_verdicts", fake):
        r = client.post("/api/classifier/verdicts", json=payload, headers={"X-API-Key": KEY})
    assert r.status_code == 200
    assert r.json()["skipped_invalid"] == 1
    verdicts = fake.await_args.args[1]
    assert [v.lip for v in verdicts] == ["1_11"]


def test_verdicts_malformed_body_still_422(client):
    r = client.post("/api/classifier/verdicts", json={"nope": 1}, headers={"X-API-Key": KEY})
    assert r.status_code == 422


def test_kill_switch_503(client, monkeypatch):
    monkeypatch.setenv("CLASSIFIER_DISABLED", "1")
    r = client.get("/api/classifier/pending", headers={"X-API-Key": KEY})
    assert r.status_code == 503


def test_postulates_requires_key(client):
    # D-096/#015 Ф1 (01.11): текст правил — корпус проекта, а не публичная страница.
    # До правки эндпоинт был единственным в роутере без require_ingest_key, и
    # ~41 КБ постулатов отдавались интернету без ключа.
    assert client.get("/api/classifier/postulates").status_code == 401
    assert (
        client.get("/api/classifier/postulates", headers={"X-API-Key": "nope"}).status_code == 401
    )


def test_postulates_served_effective(client):
    # /postulates отдаёт эффективные постулаты (база + утверждённые правила, ADR-0005).
    fake = AsyncMock(return_value="Классификационные постулаты\n\n## Выученные правила\n1. x")
    with patch.object(ing.rules, "render_effective_postulates", fake):
        r = client.get("/api/classifier/postulates", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    assert "Классификационные постулаты" in r.text
    assert "Выученные правила" in r.text


def test_corrections_requires_key(client):
    assert client.get("/api/classifier/corrections").status_code == 401


def test_corrections_ok_with_key(client):
    fake = AsyncMock(return_value=[{"lip": "1_10", "verdict_type": "action"}])
    with patch.object(ing.rules, "fetch_corrections_for_distill", fake):
        r = client.get("/api/classifier/corrections?limit=50&days=14", headers={"X-API-Key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert fake.await_args.kwargs["days"] == 14


def test_rule_proposals_requires_key(client):
    r = client.post("/api/classifier/rule-proposals", json={"proposals": []})
    assert r.status_code == 401


def test_rule_proposals_records(client):
    fake = AsyncMock(return_value={"recorded": 1, "skipped_existing": 0, "skipped_invalid": 0})
    payload = {"proposals": [{"rule_text": "Новое обобщённое правило", "rationale": "3 коррекции"}]}
    with patch.object(ing.rules, "record_rule_proposals", fake):
        r = client.post("/api/classifier/rule-proposals", json=payload, headers={"X-API-Key": KEY})
    assert r.status_code == 200
    assert r.json()["recorded"] == 1
    assert fake.await_args.kwargs["source"] == "routine"


def test_rule_proposals_too_short_rejected_by_schema(client):
    # rule_text < 3 символов → 422 (pydantic), до записи не доходит
    payload = {"proposals": [{"rule_text": "x"}]}
    r = client.post("/api/classifier/rule-proposals", json=payload, headers={"X-API-Key": KEY})
    assert r.status_code == 422
