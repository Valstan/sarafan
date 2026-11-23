"""Движок категоризации discovery: MiniMax по умолчанию, Дипсик — страховка.

Контекст (23.11): еженедельный recheck платил 2 701 вызов и 2,29 млн
prompt-токенов на платном Дипсике, пока классификация и переписывание уже
живут на бесплатном free-тире MiniMax. Переключение сдвинуло и **время**
прогона: вызов через CLI MiniMax занимает ~10 с против ~1,7 с у Дипсика,
поэтому обход разложен по ночам (см. ``tasks/discovery_tasks.py``).

Здесь проверяется выбор движка, платный фолбэк и учёт токенов — в том числе
написание полей у MiniMax (``inputTokens``/``outputTokens``), иначе смена
движка обнулила бы отчёт о цене прогона.
"""

from __future__ import annotations

import json

import pytest

import modules.discovery.ai_categorizer as ac
from modules import minimax_engine as mx


def _ok(engine: str = "minimax", usage: dict | None = None) -> dict:
    return {
        "ok": True,
        "content": json.dumps(
            {
                "category": "novost",
                "confidence": 88,
                "is_info_page": True,
                "reasoning": "районный паблик",
            }
        ),
        "model": "minimax/MiniMax-M3.1-Flash",
        "usage": usage,
    }


@pytest.fixture
def calls(monkeypatch):
    """Подмены обоих движков + список вызовов."""
    seen: list[tuple[str, dict]] = []

    def fake_minimax(prompt):
        seen.append(("minimax", {"prompt": prompt}))
        return _ok("minimax")

    def fake_deepseek(prompt):
        seen.append(("deepseek", {"prompt": prompt}))
        return _ok("deepseek")

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    monkeypatch.setattr(ac, "_call_deepseek", fake_deepseek)
    return seen


def test_default_engine_is_minimax(monkeypatch):
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)
    assert ac._engine_name() == "minimax"


def test_engine_env_override(monkeypatch):
    monkeypatch.setenv(ac.ENV_ENGINE, "DeepSeek")
    assert ac._engine_name() == "deepseek"


@pytest.mark.asyncio
async def test_minimax_is_used_by_default(monkeypatch, calls):
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)
    res = await ac.categorize_candidate(name="Районный паблик")
    assert res["success"] is True
    assert res["engine"] == "minimax"
    assert [name for name, _ in calls] == ["minimax"]


@pytest.mark.asyncio
async def test_paid_fallback_when_minimax_fails(monkeypatch):
    """Мини-движок не ответил — закрываем платным Дипсиком, и это видно."""
    seen: list[str] = []

    def fake_minimax(prompt):
        seen.append("minimax")
        return {"ok": False, "reason": "accounts_exhausted", "detail": "нет очков"}

    def fake_deepseek(prompt):
        seen.append("deepseek")
        return _ok("deepseek")

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    monkeypatch.setattr(ac, "_call_deepseek", fake_deepseek)

    res = await ac.categorize_candidate(name="X")
    assert res["success"] is True
    assert res["engine"] == "deepseek"
    assert seen == ["minimax", "deepseek"]


@pytest.mark.asyncio
async def test_no_fallback_when_paid_fallback_disabled(monkeypatch):
    """Страховка выключена — платного вызова не будет, отказ честный."""
    seen: list[str] = []
    monkeypatch.setenv(ac.ENV_PAID_FALLBACK, "0")

    def fake_minimax(prompt):
        seen.append("minimax")
        return {"ok": False, "reason": "accounts_exhausted"}

    def fake_deepseek(prompt):
        seen.append("deepseek")
        return _ok("deepseek")

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    monkeypatch.setattr(ac, "_call_deepseek", fake_deepseek)

    res = await ac.categorize_candidate(name="X")
    assert res["success"] is False
    assert res["engine"] == "minimax"
    assert seen == ["minimax"]


@pytest.mark.asyncio
async def test_minimax_usage_keys_are_counted(monkeypatch):
    """У MiniMax имена полей другие — смена движка не должна обнулять цену."""
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)
    monkeypatch.setattr(
        ac,
        "_call_minimax",
        lambda prompt: _ok("minimax", usage={"inputTokens": 840, "outputTokens": 53}),
    )
    res = await ac.categorize_candidate(name="X")
    assert res["tokens_prompt"] == 840
    assert res["tokens_completion"] == 53


def test_discovery_has_its_own_task_text():
    """У discovery своё задание: с классификаторским модель ответит вердиктами.

    Ровно тот случай, который 18.11 стоил 14 отказов в сутки у переписывания
    (ADR-0017).
    """
    text = mx.TASK_BY_LABEL["discovery"]
    assert text
    assert text != mx.TASK_TEXT
    assert "JSON" in text
