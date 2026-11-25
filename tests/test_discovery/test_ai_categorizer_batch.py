"""Партия сообществ: один вызов движка на N карточек вместо вызова на каждое.

**Зачем этот файл.** Ночной срез discovery звал движок **по одному разу на
сообщество**: 129 вызовов за ночь 25.11, из них 7 успешных, 450 минут слота —
и при медиане успешного вызова ~2 минуты срез не влезал в собственную
трёхчасовую рамку (``time_limit=3*3600``) и умирал без отчёта. Партия кладёт N
карточек в один вызов.

Читаемость ответа — главный риск партии, поэтому проверяем именно его:

- вердикты приходят не по порядку — сопоставляем по ``id``;
- часть вердиктов потерялась — помечаем потерянные (их доберут одиночными
  вызовами), остальные не теряем;
- движок не ответил — отказ одинаков для всех карточек партии;
- отказ по ёмкости бокса — платной страховки нет (то же правило, что у
  переписывания и одиночного вызова).
"""

from __future__ import annotations

import json

import pytest

import modules.discovery.ai_categorizer as ac
from modules import minimax_engine as mx


def _card(index: int) -> dict:
    return {
        "name": "Сообщество %d" % index,
        "description": None,
        "members_count": 100,
        "recent_posts": ["новость района"],
    }


def _batch_answer(items: list[dict]) -> str:
    return json.dumps({"results": items}, ensure_ascii=False)


def _verdict(index: int, category: str = "novost") -> dict:
    return {
        "id": index,
        "category": category,
        "confidence": 80,
        "is_info_page": False,
        "reasoning": "районный паблик",
    }


@pytest.fixture
def calls(monkeypatch):
    """Подмены обоих движков + список вызовов."""
    seen: list[tuple[str, dict]] = []

    def fake_minimax(prompt, **kwargs):
        seen.append(("minimax", {"prompt": prompt, **kwargs}))
        return {"ok": True, "content": _batch_answer([_verdict(1), _verdict(2)]), "model": "m"}

    def fake_deepseek(prompt, **kwargs):
        seen.append(("deepseek", {"prompt": prompt, **kwargs}))
        return {"ok": True, "content": _batch_answer([_verdict(1), _verdict(2)]), "model": "d"}

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    monkeypatch.setattr(ac, "_call_deepseek", fake_deepseek)
    return seen


@pytest.mark.asyncio
async def test_batch_makes_one_call_for_many_communities(monkeypatch, calls):
    """N сообществ — **один** вызов движка, а не N."""
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)
    res = await ac.categorize_candidates_bulk([_card(1), _card(2), _card(3)])
    assert [name for name, _ in calls] == ["minimax"]
    assert len(res) == 3


@pytest.mark.asyncio
async def test_batch_result_length_matches_cards(monkeypatch, calls):
    """Список вердиктов — той же длины, что и список карточек."""
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)
    cards = [_card(i) for i in range(1, 6)]
    res = await ac.categorize_candidates_bulk(cards)
    assert len(res) == len(cards)


@pytest.mark.asyncio
async def test_batch_maps_verdicts_by_id_not_by_position(monkeypatch):
    """Вердикты сопоставляются по ``id``, даже если модель их переставила.

    Порядок элементов в ответе модель не гарантирует — сопоставка по позиции
    молча перевела бы вердикт соседнему сообществу.
    """
    seen: list[str] = []

    def fake_minimax(prompt, **kwargs):
        seen.append("minimax")
        return {
            "ok": True,
            "content": _batch_answer([_verdict(2, "sport"), _verdict(1, "kultura")]),
            "model": "m",
        }

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    res = await ac.categorize_candidates_bulk([_card(1), _card(2)])
    assert res[0]["category"] == "kultura"
    assert res[1]["category"] == "sport"


@pytest.mark.asyncio
async def test_batch_marks_only_missing_verdict(monkeypatch):
    """Потерянный вердикт помечается — остальные не теряются."""
    monkeypatch.delenv(ac.ENV_ENGINE, raising=False)

    def fake_minimax(prompt, **kwargs):
        return {"ok": True, "content": _batch_answer([_verdict(1)]), "model": "m"}

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    res = await ac.categorize_candidates_bulk([_card(1), _card(2)])
    assert res[0]["success"] is True
    assert res[1]["success"] is False
    assert res[1]["error"] == ac.BATCH_MISSING_VERDICT_ERROR


@pytest.mark.asyncio
async def test_batch_engine_failure_marks_every_card(monkeypatch):
    """Движок не ответил — отказ одинаков для всех карточек партии."""
    seen: list[str] = []

    def fake_minimax(prompt, **kwargs):
        seen.append("minimax")
        return {"ok": False, "reason": "accounts_exhausted", "detail": "нет очков"}

    def fake_deepseek(prompt, **kwargs):
        seen.append("deepseek")
        return {
            "ok": True,
            "content": _batch_answer([_verdict(1), _verdict(2)]),
            "model": "d",
        }

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    monkeypatch.setattr(ac, "_call_deepseek", fake_deepseek)

    res = await ac.categorize_candidates_bulk([_card(1), _card(2)])
    assert [item["success"] for item in res] == [True, True]
    assert seen == ["minimax", "deepseek"]


@pytest.mark.asyncio
async def test_batch_capacity_refusal_is_not_paid(monkeypatch):
    """Отказ по ёмкости бокса платной страховкой не закрывается — как и у одиночного вызова."""
    seen: list[str] = []

    def fake_minimax(prompt, **kwargs):
        seen.append("minimax")
        return {"ok": False, "reason": "insufficient_memory"}

    def fake_deepseek(prompt, **kwargs):
        seen.append("deepseek")
        return {"ok": True, "content": _batch_answer([_verdict(1)]), "model": "d"}

    monkeypatch.delenv(ac.ENV_PAID_FALLBACK, raising=False)
    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    monkeypatch.setattr(ac, "_call_deepseek", fake_deepseek)

    res = await ac.categorize_candidates_bulk([_card(1), _card(2)])
    assert seen == ["minimax"]
    assert [item["success"] for item in res] == [False, False]
    assert res[0]["error"] == "insufficient_memory"


@pytest.mark.asyncio
async def test_batch_uses_its_own_task_text(monkeypatch):
    """У партии — своё задание для CLI.

    Агент, получивший «одну карточку», ответит вердиктом на первую карточку
    вместо массива по всем (ровно тот случай, что 18.11 стоил 14 отказов в
    сутки у переписывания — ADR-0017).
    """
    seen: dict = {}

    def fake_chat(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "content": _batch_answer([_verdict(1)]), "model": "m"}

    monkeypatch.setattr(mx, "chat", fake_chat)
    await ac.categorize_candidates_bulk([_card(1)])
    assert seen["task"] == mx.TASK_TEXT_DISCOVERY_BATCH
    assert seen["task"] != mx.TASK_TEXT_DISCOVERY


@pytest.mark.asyncio
async def test_batch_sends_all_cards_in_one_attachment(monkeypatch):
    """Все карточки едут в одном вложении — иначе это не партия, а поштучность."""
    seen: dict = {}

    def fake_chat(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "content": _batch_answer([_verdict(1), _verdict(2)]), "model": "m"}

    monkeypatch.setattr(mx, "chat", fake_chat)
    await ac.categorize_candidates_bulk([_card(1), _card(2)])
    assert "Сообщество #1" in seen["user"]
    assert "Сообщество #2" in seen["user"]


@pytest.mark.asyncio
async def test_batch_tokens_are_shared_not_multiplied(monkeypatch):
    """Токены партии **делятся** между вердиктами.

    Иначе отчёт сложил бы весь расход партии в каждый вердикт и показал
    расход в N раз больше правды (8 карточек на вызов ⇒ отчёт «стоил» в 8 раз
    дороже). Сумма по региону должна равняться расходу партии.
    """
    seen: list[str] = []

    def fake_minimax(prompt, **kwargs):
        seen.append("minimax")
        return {
            "ok": True,
            "content": _batch_answer([_verdict(1), _verdict(2)]),
            "model": "m",
            "usage": {"inputTokens": 800, "outputTokens": 200},
        }

    monkeypatch.setattr(ac, "_call_minimax", fake_minimax)
    res = await ac.categorize_candidates_bulk([_card(1), _card(2)])
    assert res[0]["tokens_prompt"] == 400
    assert res[1]["tokens_prompt"] == 400
    assert res[0]["tokens_prompt"] + res[1]["tokens_prompt"] == 800
    assert res[0]["tokens_completion"] == 100


def test_batch_size_default_is_eight(monkeypatch):
    """Дефолт — 8 карточек на вызов: ночь 25.11 — 129 вызовов, здесь будет ~16."""
    monkeypatch.delenv(ac.ENV_AI_BATCH_SIZE, raising=False)
    assert ac.ai_batch_size() == 8


def test_batch_size_env_override(monkeypatch):
    monkeypatch.setenv(ac.ENV_AI_BATCH_SIZE, "3")
    assert ac.ai_batch_size() == 3


def test_batch_size_one_returns_to_single_calls(monkeypatch):
    """``1`` — откат на прежнее поведение (один вызов на сообщество) без деплоя."""
    monkeypatch.setenv(ac.ENV_AI_BATCH_SIZE, "1")
    assert ac.ai_batch_size() == 1


def test_batch_size_is_clamped(monkeypatch):
    monkeypatch.setenv(ac.ENV_AI_BATCH_SIZE, "999")
    assert ac.ai_batch_size() == ac.MAX_AI_BATCH_SIZE
    monkeypatch.setenv(ac.ENV_AI_BATCH_SIZE, "0")
    assert ac.ai_batch_size() == ac.MIN_AI_BATCH_SIZE


def test_batch_answer_tolerates_fences_and_text_around(monkeypatch):
    """Модель может обернуть JSON в ```` ```json ```` и добавить текст — разбор терпимый."""
    content = "Вот вердикты:\n```json\n%s\n```\nГотово." % _batch_answer([_verdict(1)])
    parsed = ac._parse_batch_response(content, 1)
    assert parsed[0] is not None
    assert parsed[0]["id"] == 1


def test_batch_answer_accepts_bare_array(monkeypatch):
    """Массив без обёртки — тоже валидный ответ."""
    content = json.dumps([_verdict(1)], ensure_ascii=False)
    parsed = ac._parse_batch_response(content, 1)
    assert parsed[0] is not None
