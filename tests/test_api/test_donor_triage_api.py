"""API страницы разбора доноров (P236 / P235): контракт эндпоинтов.

**Что проверяем.** Страница ``/donor-triage`` — это «что куда» мышкой: список
общих групп и молчащих доноров с рекомендацией, выбор в дропдауне, кнопка
«Применить». Проверяется не сам разбор (он в ``tests/test_donor_triage.py``), а
**граница между страницей и модулем**:

* ``GET /candidates`` отдаёт обе секции разбора и честно говорит, если VK не
  ответил (тогда часть правил не сработала — владелец должен это видеть);
* ``POST /apply`` переводит pydantic-модели в тот же формат, который ждёт
  ``modules.donor_triage.apply_decisions`` — иначе браузерный путь и headless
  разойдутся (а это ровно тот класс тихого расхождения, который канон запрещает);
* пустое решение — 400, а не «применили и ничего не сделали».

БД и VK в тестах не трогаются: сбор и запись подменены заглушками.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api import donor_triage as api

# ---------------------------------------------------------------------------
# заглушки
# ---------------------------------------------------------------------------


def _report(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "shared": [
            {
                "vk_id": 100,
                "name": "Объявления Свеча",
                "publications": 3,
                "group_city": "Свеча",
                "description": "",
                "regs": [
                    {
                        "row_id": 1,
                        "region_id": 10,
                        "region_code": "darovskoy",
                        "region_name": "ДАРОВСКОЙ",
                        "category": "novost",
                        "posts_count": 5,
                        "members": 100,
                        "created_at": "2026-10-02",
                        "last_post_at": "2026-11-20",
                        "is_own_main": False,
                    }
                ],
                "recommendation": {
                    "action": "keep_region",
                    "keep_region_ids": [10],
                    "drop_row_ids": [],
                    "reason": "город группы совпал с городом региона darovskoy",
                    "confidence": "high",
                },
            }
        ],
        "donors": [
            {
                "row_id": 77,
                "vk_id": -200,
                "region_id": 11,
                "region_code": "kiknur",
                "region_name": "КИКНУР",
                "name": "Молодёжь Ютазинского",
                "category": "molodezh",
                "suggested_category": "",
                "health": "active",
                "members": 1467,
                "posts_count": 0,
                "last_post_at": "",
                "created_at": "2026-10-02",
                "publications": 0,
                "description": "",
                "recommendation": {
                    "action": "set_theme",
                    "theme": "novost",
                    "reason": "в названии — наш НП → переносим в тему со слотом",
                    "confidence": "medium",
                },
            }
        ],
        "slotted_themes": ["novost", "reklama", "kultura"],
        "no_slot_themes": ["nauka", "sosed"],
        "vk_error": "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/donor-triage")

    async def _fake_session():
        yield object()

    app.dependency_overrides[api.get_db_session] = _fake_session
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /candidates
# ---------------------------------------------------------------------------


def test_candidates_returns_both_sections(client, monkeypatch):
    async def _fake(_session):
        return _report()

    monkeypatch.setattr(api.donor_triage, "collect_with_vk", _fake)
    response = client.get("/api/donor-triage/candidates")
    assert response.status_code == 200
    data = response.json()
    assert len(data["shared"]) == 1
    assert len(data["donors"]) == 1
    assert data["shared"][0]["vk_id"] == 100
    assert data["donors"][0]["row_id"] == 77
    assert "novost" in data["slotted_themes"]


def test_candidates_reports_vk_failure_instead_of_hiding_it(client, monkeypatch):
    """VK не ответил — часть правил не сработала. Владелец обязан это видеть."""

    async def _fake(_session):
        return _report(vk_error="TimeoutError: vk down")

    monkeypatch.setattr(api.donor_triage, "collect_with_vk", _fake)
    data = client.get("/api/donor-triage/candidates").json()
    assert "TimeoutError" in data["vk_error"]


def test_candidates_survives_broken_collect(client, monkeypatch):
    """Разбор не должен ронять страницу: решение остаётся за человеком."""

    async def _fake(_session):
        raise RuntimeError("boom")

    monkeypatch.setattr(api.donor_triage, "collect_with_vk", _fake)
    response = client.get("/api/donor-triage/candidates")
    assert response.status_code == 200
    data = response.json()
    assert data["shared"] == [] and data["donors"] == []
    assert "boom" in data["error"]


# ---------------------------------------------------------------------------
# POST /apply
# ---------------------------------------------------------------------------


def test_apply_without_decisions_is_rejected(client):
    response = client.post("/api/donor-triage/apply", json={})
    assert response.status_code == 400


def test_apply_passes_decisions_to_the_module_in_its_own_format(client, monkeypatch):
    """Страница и CLI обязаны говорить с модулем на одном языке."""
    captured: Dict[str, Any] = {}

    async def _fake(_session, decisions):
        captured.update(decisions)
        return {"shared": [{"vk_id": 100}], "donors": [{"row_id": 77}], "skipped": []}

    monkeypatch.setattr(api.donor_triage, "apply_decisions", _fake)
    response = client.post(
        "/api/donor-triage/apply",
        json={
            "shared": {"100": {"action": "keep_region", "keep_region_id": 10}},
            "donors": {"77": {"action": "set_theme", "theme": "novost"}},
        },
    )
    assert response.status_code == 200
    assert captured == {
        "shared": {"100": {"action": "keep_region", "keep_region_id": 10}},
        "donors": {"77": {"action": "set_theme", "theme": "novost"}},
    }
    data = response.json()
    assert data["shared_changed"] == 1
    assert data["donors_changed"] == 1
    assert data["skipped"] == 0


def test_apply_reports_skipped_decisions(client, monkeypatch):
    """Тема без слота / наша главная группа — решение пропущено, а не «применено»."""

    async def _fake(_session, _decisions):
        return {
            "shared": [],
            "donors": [],
            "skipped": [{"scope": "donors", "row_id": 77, "why": "тема nauka без слота"}],
        }

    monkeypatch.setattr(api.donor_triage, "apply_decisions", _fake)
    data = client.post(
        "/api/donor-triage/apply",
        json={"donors": {"77": {"action": "set_theme", "theme": "nauka"}}},
    ).json()
    assert data["skipped"] == 1
    assert "nauka" in data["detail"]["skipped"][0]["why"]
