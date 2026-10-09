"""Tests карты ingest-контракта (modules/conveyor/contract.py + /api/ingest/contract).

Пилот варианта B (мозг 07.10, pool #367). Центральное свойство, которое здесь
закреплено: **карта — генерация из живого конфига, а не вторная копия**. Ручописная
копия состава разделов расходится с ``SITES`` молча, и узнать об этом неоткуда —
поэтому тесты сверяют выход с конфигом по полям и проверяют, что секреты в ответ
не утекают ни при каких настройках.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.content_conveyor import SITES
from modules.conveyor.contract import CONTRACT_VERSION, SOURCE_LABEL, build_contract
from web.api import ingest_contract


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ingest_contract.router, prefix="/api/ingest")
    return TestClient(app)


@pytest.fixture
def one_site(monkeypatch):
    """Ровно один активный сайт — остальное окружение выключено."""
    monkeypatch.setenv("CONVEYOR_SITES", "vmalmyzhe")
    monkeypatch.delenv("CONVEYOR_DISABLED", raising=False)
    return "vmalmyzhe"


def _sites_by_key(contract):
    return {s["key"]: s for s in contract["sites"]}


def test_contract_contains_only_active_sites(one_site, monkeypatch):
    # kazanskaya описана в SITES, но не включена — контракт про неё молчит.
    contract = build_contract()
    assert set(_sites_by_key(contract)) == {one_site}


def test_contract_matches_config_for_active_site(one_site):
    # Правда ответа === правда конфига, по которой работает доставка.
    site = next(s for s in SITES if s["key"] == one_site)
    entry = _sites_by_key(build_contract())[one_site]
    assert entry["sections"] == list(site["sections"])
    assert entry["skip_themes"] == list(site["skip_themes"])
    assert entry["source_region"] == site["source_region"]
    assert entry["title"] == site["title"]
    assert entry["unknown_section"] == "draft-with-warning"


def test_contract_keywords_and_owners_only_when_configured(monkeypatch):
    # vmalmyzhe без тематической ловли — полей source_keywords/owner_ids нет вовсе.
    monkeypatch.setenv("CONVEYOR_SITES", "vmalmyzhe")
    monkeypatch.delenv("CONVEYOR_DISABLED", raising=False)
    entry = _sites_by_key(build_contract())["vmalmyzhe"]
    assert "source_keywords" not in entry
    assert "source_owner_ids" not in entry
    # kazanskaya ловит по словам — поле есть и совпадает с конфигом.
    monkeypatch.setenv("CONVEYOR_SITES", "kazanskaya")
    site = next(s for s in SITES if s["key"] == "kazanskaya")
    entry = _sites_by_key(build_contract())["kazanskaya"]
    assert entry["source_keywords"] == list(site["source_keywords"])


def test_contract_publishes_directly_follows_key_presence(one_site, monkeypatch):
    monkeypatch.delenv("VMALMYZHE_PUBLISH_KEY", raising=False)
    entry = _sites_by_key(build_contract())[one_site]
    assert entry["publishes_directly"] is False
    monkeypatch.setenv("VMALMYZHE_PUBLISH_KEY", "any-value")
    entry = _sites_by_key(build_contract())[one_site]
    assert entry["publishes_directly"] is True


def test_contract_has_no_secrets_or_key_names(one_site, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "ingest-super-secret")
    monkeypatch.setenv("VMALMYZHE_PUBLISH_KEY", "publish-super-secret")
    text = str(build_contract())
    assert "ingest-super-secret" not in text
    assert "publish-super-secret" not in text
    # Имена ключей тоже не нужны приёмнику: право выражается булевым флагом.
    assert "key_env" not in text
    assert "INGEST_KEY" not in text
    assert "PUBLISH_KEY" not in text


def test_contract_kill_switch_empties_sites(monkeypatch):
    monkeypatch.setenv("CONVEYOR_SITES", "vmalmyzhe,kazanskaya")
    monkeypatch.setenv("CONVEYOR_DISABLED", "1")
    assert build_contract()["sites"] == []


def test_contract_limits_come_from_config(one_site, monkeypatch):
    monkeypatch.setenv("CONVEYOR_SOURCE_DAYS", "7")
    monkeypatch.setenv("CONVEYOR_BATCH_MAX", "5")
    limits = build_contract()["limits"]
    assert limits == {"source_days": 7, "batch_max": 5}


def test_contract_vkpostid_rule_states_sign_preserved():
    # Идемпотентность приёмника строится на vkPostId; инцидент Культуры (P186)
    # родился из потери знака — правило зафиксировано в контракте явно.
    fields = build_contract()["delivery"]["fields"]["vkPostId"]
    assert "знак" in fields


def test_endpoint_returns_contract(client, one_site):
    r = client.get("/api/ingest/contract")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == CONTRACT_VERSION
    assert body["source"] == SOURCE_LABEL
    assert one_site in _sites_by_key(body)
    assert body["delivery"]["method"] == "POST"
    assert body["limits"]["source_days"] >= 1


def test_endpoint_needs_no_auth(client, one_site):
    # Публичная дверь: у приёмника нет cookie нашей сессии. Сессию не подсунуть —
    # достаточно что без каких-либо заголовков ответ 200, а не 302/401.
    r = client.get("/api/ingest/contract")
    assert r.status_code == 200


def test_endpoint_same_truth_as_module(client, one_site):
    # Дверь не переписывает карту своими руками: ответ === build_contract().
    assert client.get("/api/ingest/contract").json() == build_contract()
