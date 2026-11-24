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

    def fake_minimax(prompt, **kwargs):
        seen.append(("minimax", {"prompt": prompt, **kwargs}))
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

    def fake_minimax(prompt, **kwargs):
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

    def fake_minimax(prompt, **kwargs):
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
        lambda prompt, **kwargs: _ok("minimax", usage={"inputTokens": 840, "outputTokens": 53}),
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


# ───────── системный промт: без него CLI MiniMax не работает ─────────


@pytest.mark.asyncio
async def test_minimax_gets_non_empty_system_prompt(monkeypatch):
    """MiniMax получает **непустой** системный промт.

    CLI отказывается работать с пустым файлом промта («--system-prompt-file ...
    is empty»). 24.11 discovery звал MiniMax без системного промта: 385 отказов
    за сутки и весь ночной срез ушёл на платный Дипсик — при том, что «перевод
    discovery на MiniMax» считали сделанным ещё 23.11.
    """
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)
    seen: dict = {}

    def fake_chat(**kwargs):
        seen.update(kwargs)
        return _ok("minimax")

    monkeypatch.setattr(mx, "chat", fake_chat)

    res = await ac.categorize_candidate(
        name="Подслушано Малмыж",
        description="паблик района",
        members_count=1000,
        recent_posts=["новость"],
        region_name="Малмыжский район",
    )

    assert res["success"] is True
    assert seen["system"].strip(), "системный промт MiniMax не должен быть пустым"
    assert "admin" in seen["system"]  # список категорий — в системном промте
    assert "Подслушано Малмыж" in seen["user"]  # карточка — во вложении
    assert seen["label"] == "discovery"


def test_deepseek_prompt_keeps_original_shape():
    """Промт Дипсика не меняется: роль → карточка → инструкция, как раньше."""
    prompt = ac._build_prompt(
        name="Подслушано Малмыж",
        description="паблик района",
        members_count=1000,
        recent_posts=["новость"],
        region_name="Малмыжский район",
    )
    assert prompt.index("Ты — модератор") < prompt.index("Сообщество:")
    assert prompt.index("Сообщество:") < prompt.index("Выбери одну категорию")
    assert "Подслушано Малмыж" in prompt
    assert "Малмыжский район" in prompt
    assert prompt.endswith("}")


def test_run_classification_refuses_empty_system_prompt():
    """Пустой системный промт — наша ошибка, а не отказ аккаунта.

    Ответ должен называть причину (``empty_system``), а не прятать её за
    сообщением CLI: иначе отказ выглядит как поломка движка и стоит вызова.
    """
    ok, content, details = mx.run_classification(None, system="", user="пост")
    assert ok is False
    assert content == ""
    assert details["reason"] == "empty_system"
