"""DISCOVERY_AI_BATCH_MODE flag (P043, вариант А): auto|manual|off.

- Парсинг флага: дефолт auto, неизвестное → auto.
- off → 404 на всех трёх ai-batch endpoints (гвард до обращения к БД).
- auto/manual → endpoints работают, /status отдаёт mode.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from config import runtime as runtime_config
from web.api import discovery as discovery_api


def _region():
    region = SimpleNamespace(code="x", name="X", config={})
    region.id = 1
    return region


class _Session:
    """Минимальный fake: region есть, кандидатов нет."""

    def __init__(self, *, region=True):
        self._region = _region() if region else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, _stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._region
        scalars = MagicMock()
        scalars.all.return_value = []
        result.scalars.return_value = scalars
        return result

    async def commit(self):
        pass


# ─── Парсинг флага ───


def test_mode_default_is_auto(monkeypatch):
    monkeypatch.delenv("DISCOVERY_AI_BATCH_MODE", raising=False)
    assert runtime_config.discovery_ai_batch_mode() == "auto"


def test_mode_manual(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "manual")
    assert runtime_config.discovery_ai_batch_mode() == "manual"


def test_mode_off_case_insensitive_and_stripped(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", " Off ")
    assert runtime_config.discovery_ai_batch_mode() == "off"


def test_mode_unknown_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "sometimes")
    assert runtime_config.discovery_ai_batch_mode() == "auto"


def test_mode_empty_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "")
    assert runtime_config.discovery_ai_batch_mode() == "auto"


# ─── off → 404 ───


async def test_off_get_returns_404(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "off")
    session = _Session()
    with patch.object(discovery_api, "AsyncSessionLocal", return_value=session):
        with pytest.raises(HTTPException) as exc:
            await discovery_api.get_ai_batch(code="x", chunk=0, size=30)
    assert exc.value.status_code == 404


async def test_off_apply_returns_404_even_empty(monkeypatch):
    """Гвард стоит до разбора тела: off → 404, а не нулевая сводка."""
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "off")
    body = discovery_api._AiBatchApply(items=[])
    with patch.object(discovery_api, "AsyncSessionLocal", return_value=None):
        with pytest.raises(HTTPException) as exc:
            await discovery_api.apply_ai_batch(code="x", body=body)
    assert exc.value.status_code == 404


async def test_off_status_returns_404(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "off")
    session = _Session()
    with patch.object(discovery_api, "AsyncSessionLocal", return_value=session):
        with pytest.raises(HTTPException) as exc:
            await discovery_api.ai_batch_status(code="x")
    assert exc.value.status_code == 404


# ─── auto/manual → работают, status несёт mode ───


async def test_auto_status_carries_mode(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "auto")
    session = _Session()
    with patch.object(discovery_api, "AsyncSessionLocal", return_value=session):
        out = await discovery_api.ai_batch_status(code="x")
    assert out["mode"] == "auto"
    assert out["total"] == 0


async def test_manual_get_still_works(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "manual")
    session = _Session()
    with patch.object(discovery_api, "AsyncSessionLocal", return_value=session):
        out = await discovery_api.get_ai_batch(code="x", chunk=0, size=30)
    assert out["items"] == []
    assert out["prompt"] == ""


async def test_manual_apply_still_works(monkeypatch):
    monkeypatch.setenv("DISCOVERY_AI_BATCH_MODE", "manual")
    session = _Session()
    body = discovery_api._AiBatchApply(items=[{"id": 1, "category": "novost", "is_relevant": True}])
    with patch.object(discovery_api, "AsyncSessionLocal", return_value=session):
        out = await discovery_api.apply_ai_batch(code="x", body=body)
    assert out["updated"] == 0
    assert out["missing_ids"] == [1]
