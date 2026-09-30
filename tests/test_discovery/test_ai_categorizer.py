"""Unit tests for modules/discovery/ai_categorizer.py.

Движок — DeepSeek через общий ``modules.deepseek_client`` (D-024, 2026-08-12).
Подменяется ровно одна точка — ``ac.chat``, — потому что модуль импортирует её
в своё пространство имён. Раньше подменялся целый фальшивый пакет ``groq`` в
``sys.modules``: SDK импортировался лениво внутри функции, и дотянуться до него
можно было только так. После перехода на прямой HTTP протез не нужен.
"""

from __future__ import annotations

import json

import pytest

import modules.discovery.ai_categorizer as ac


def _install_chat(monkeypatch, result):
    """Подменить вызов модели. ``result`` — то, что вернёт deepseek_client.chat.

    Возвращает словарь, в который попадут переданные kwargs — так проверяется
    не только исход, но и форма запроса.
    """
    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr(ac, "chat", fake_chat)
    return captured


def _ok(content: str, model: str = "deepseek-chat"):
    return {"ok": True, "content": content, "model": model, "usage": None}


# ───────── pure helpers ─────────


def test_strip_json_fences_unwraps_json_block():
    assert ac._strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strip_json_fences_passthrough_plain():
    assert ac._strip_json_fences('{"a": 1}') == '{"a": 1}'


def test_parse_response_handles_plain_json():
    parsed = ac._parse_response('{"category": "novost", "confidence": 80}')
    assert parsed == {"category": "novost", "confidence": 80}


def test_parse_response_extracts_json_after_garbage_text():
    parsed = ac._parse_response('Хорошо, вот ответ:\n{"category": "sport"}\nспасибо')
    assert parsed == {"category": "sport"}


def test_parse_response_returns_none_on_unrecoverable():
    assert ac._parse_response("nope, just text") is None


def test_normalise_clamps_confidence_and_validates_category():
    out = ac._normalise({"category": "FAKE_CAT", "confidence": 150})
    assert out["category"] == "other"
    assert out["confidence"] == 100


def test_normalise_negative_confidence_clamps_to_zero():
    out = ac._normalise({"category": "sport", "confidence": -5})
    assert out["confidence"] == 0


def test_normalise_keeps_known_category():
    out = ac._normalise({"category": "novost", "confidence": 73, "is_info_page": True})
    assert out["category"] == "novost"
    assert out["confidence"] == 73
    assert out["is_info_page"] is True


# ───────── categorize_candidate (end-to-end с подменённым DeepSeek) ─────────


@pytest.mark.asyncio
async def test_categorize_empty_name_short_circuits():
    res = await ac.categorize_candidate(name="")
    assert res == {"success": False, "error": "name is empty"}


@pytest.mark.asyncio
async def test_categorize_no_api_key(monkeypatch):
    _install_chat(monkeypatch, {"ok": False, "reason": "no_api_key"})
    res = await ac.categorize_candidate(name="Test")
    assert res["success"] is False
    assert "DEEPSEEK_API_KEY" in res["error"]


@pytest.mark.asyncio
async def test_categorize_asks_for_json_mode(monkeypatch):
    """Строгий JSON включается явно: без него модель охотно оборачивает ответ в
    markdown, и разбор держится на одной терпимости ``_parse_response``."""
    captured = _install_chat(monkeypatch, _ok('{"category": "novost"}'))
    await ac.categorize_candidate(name="X")
    assert captured["json_object"] is True
    assert captured["temperature"] == ac._TEMPERATURE
    assert captured["max_tokens"] == ac._MAX_TOKENS


@pytest.mark.asyncio
async def test_categorize_happy_path(monkeypatch):
    _install_chat(
        monkeypatch,
        _ok(
            json.dumps(
                {
                    "category": "novost",
                    "confidence": 88,
                    "is_info_page": True,
                    "reasoning": "Постит ежедневные новости района",
                }
            )
        ),
    )
    res = await ac.categorize_candidate(
        name="МАЛМЫЖ-ИНФО",
        description="Главные новости района",
        members_count=12000,
        recent_posts=["Сегодня в Малмыже..."],
        region_name="Малмыж",
    )
    assert res["success"] is True
    assert res["category"] == "novost"
    assert res["confidence"] == 88
    assert res["is_info_page"] is True
    assert "район" in res["reasoning"]
    # Имя модели — фактическое из ответа, а не константа модуля: оно ложится в
    # БД рядом с вердиктом, и «чем размечено» должно быть правдой даже при
    # переопределённом DEEPSEEK_MODEL.
    assert res["model"] == "deepseek-chat"


# ───────── учёт токенов (P001: цена прогона в отчёте, а не в логе) ─────────
#
# Число токенов нужно для решения «включать ли авто-подбор», и оно должно
# быть в результате задачи. Раньше оно жило только строкой в ротированном логе,
# то есть вопрос четыре месяца висел без ответа не из-за отсутствия данных, а
# из-за того, что данные надо было сначала добыть grep'ом по трём архивам.


def _ok_with_usage(content: str, prompt: int, completion: int):
    return {
        "ok": True,
        "content": content,
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


@pytest.mark.asyncio
async def test_categorize_carries_tokens_from_usage(monkeypatch):
    _install_chat(monkeypatch, _ok_with_usage('{"category": "novost"}', 840, 53))
    res = await ac.categorize_candidate(name="X")
    assert res["tokens_prompt"] == 840
    assert res["tokens_completion"] == 53


@pytest.mark.asyncio
async def test_categorize_without_usage_gives_none_not_zero(monkeypatch):
    """Нет счётчиков — это ``None`` («нечем померить»), а не ``0`` («стоит ноль»).

    Разница видна в сумме: ноль занижает цену прогона, и решение принимается по
    неверной цифре. Именно та ошибка, ради которой заведён P001.
    """
    _install_chat(monkeypatch, _ok('{"category": "novost"}'))  # usage=None
    res = await ac.categorize_candidate(name="X")
    assert res["tokens_prompt"] is None
    assert res["tokens_completion"] is None


def test_sum_ai_tokens_totals_and_marks_complete():
    got = ac.sum_ai_tokens(
        [
            {"tokens_prompt": 100, "tokens_completion": 10},
            {"tokens_prompt": 250, "tokens_completion": 25},
        ]
    )
    assert got["tokens_prompt"] == 350
    assert got["tokens_completion"] == 35
    assert got["tokens_measured"] is True
    assert got["tokens_unmeasured_calls"] == 0


def test_sum_ai_tokens_flags_partial_measurement():
    """Частичный замер — третье состояние, а не «полная сумма поменьше»."""
    got = ac.sum_ai_tokens(
        [
            {"tokens_prompt": 100, "tokens_completion": 10},
            {"tokens_prompt": None, "tokens_completion": None},
        ]
    )
    assert got["tokens_prompt"] == 100
    assert got["tokens_measured"] is False
    assert got["tokens_unmeasured_calls"] == 1


def test_sum_ai_tokens_of_empty_is_measured_zero():
    got = ac.sum_ai_tokens([])
    assert got == {
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "tokens_measured": True,
        "tokens_unmeasured_calls": 0,
    }


@pytest.mark.asyncio
async def test_categorize_unknown_category_falls_back_to_other(monkeypatch):
    _install_chat(monkeypatch, _ok('{"category": "unknown_xyz", "confidence": 50}'))
    res = await ac.categorize_candidate(name="X")
    assert res["success"] is True
    assert res["category"] == "other"


@pytest.mark.asyncio
async def test_categorize_network_failure_returns_reason(monkeypatch):
    _install_chat(monkeypatch, {"ok": False, "reason": "network", "detail": "timeout"})
    res = await ac.categorize_candidate(name="X")
    assert res["success"] is False
    assert res["error"] == "network"


@pytest.mark.asyncio
async def test_categorize_http_error_keeps_status_code(monkeypatch):
    """401 и 429 — разные решения: первое чинит человек, второе проходит само,
    поэтому код статуса обязан доезжать до вызывающего, а не схлопываться."""
    _install_chat(monkeypatch, {"ok": False, "reason": "http_429"})
    res = await ac.categorize_candidate(name="X")
    assert res["error"] == "http_429"


@pytest.mark.asyncio
async def test_categorize_empty_response_failure(monkeypatch):
    _install_chat(monkeypatch, {"ok": False, "reason": "empty_response"})
    res = await ac.categorize_candidate(name="X")
    assert res["success"] is False
    assert "empty" in res["error"].lower()


@pytest.mark.asyncio
async def test_categorize_garbage_json_failure_with_raw(monkeypatch):
    _install_chat(monkeypatch, _ok("this is not json at all"))
    res = await ac.categorize_candidate(name="X")
    assert res["success"] is False
    assert "valid JSON" in res["error"]
    assert "raw" in res
