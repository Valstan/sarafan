"""Платная страховка: решение на уровне вызова, а не только общей ручкой.

**Что тут стережётся.** Общая ручка ``CLASSIFIER_PAID_FALLBACK`` одна на весь
проект, а потоки разные: вердикт классификатора решает, что попадёт в ленту
(платить за него надо), а переписывание — украшение (пост и так уйдёт с
исходным текстом). Поэтому у вызова появился рубильник: поток вправе сказать
«на такой отказ платить не буду» — иначе «бокс не дал место» превращается в
платный вызов за косметику (замер 25.11: 52 платных вызова в сутки только на
переписывании, 33 — отказ по ёмкости).

Общая ручка при этом остаётся сильнее ручки потока: «платить нельзя вообще»
значит «платить нельзя вообще».
"""

from __future__ import annotations

from modules.classifier import headless


def _minimax_failing(monkeypatch, reason: str):
    """Подменить движок MiniMax отказом с указанной причиной."""
    from modules import minimax_engine

    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "reason": reason, "detail": ""}

    monkeypatch.setattr(minimax_engine, "chat", fake)
    return calls


def _paid_spy(monkeypatch):
    """Подменить платный Дипсик и вернуть список его вызовов."""
    paid = []

    def fake(**kwargs):
        paid.append(kwargs)
        return {"ok": True, "content": "{}", "model": "deepseek-flash"}

    monkeypatch.setattr(headless, "_deepseek_chat", fake)
    return paid


def test_paid_fallback_skipped_for_capacity_reason(monkeypatch):
    """Отказ по ёмкости бокса → платный Дипсик НЕ зовётся.

    Переписывание — косметика: пост и так уйдёт с исходным текстом (fail-open),
    а платить за «бокс не дал место» смысла нет (решение владельца 25.11).
    """
    monkeypatch.delenv("CLASSIFIER_PAID_FALLBACK", raising=False)
    _minimax_failing(monkeypatch, "insufficient_memory")
    paid = _paid_spy(monkeypatch)

    out = headless.chat(
        user="u",
        system="s",
        engine="minimax",
        skip_paid_reasons=("insufficient_memory",),
    )

    assert out["ok"] is False
    assert paid == [], "на отказ по ёмкости платить нельзя"


def test_paid_fallback_skipped_when_paid_fallback_is_false(monkeypatch):
    """Поток, которому платить нельзя никогда, — не платит и на «сломанном» движке."""
    monkeypatch.delenv("CLASSIFIER_PAID_FALLBACK", raising=False)
    _minimax_failing(monkeypatch, "engine_failed")
    paid = _paid_spy(monkeypatch)

    out = headless.chat(user="u", system="s", engine="minimax", paid_fallback=False)

    assert out["ok"] is False
    assert paid == [], "поток без платной страховки не должен платить"


def test_paid_fallback_still_works_for_other_reasons(monkeypatch):
    """Прежнее поведение сохранено: отказ не по ёмкости — страховка платит."""
    monkeypatch.delenv("CLASSIFIER_PAID_FALLBACK", raising=False)
    _minimax_failing(monkeypatch, "engine_failed")
    paid = _paid_spy(monkeypatch)

    out = headless.chat(
        user="u",
        system="s",
        engine="minimax",
        skip_paid_reasons=("insufficient_memory",),
    )

    assert out["ok"] is True
    assert len(paid) == 1, "на «сломанный движок» страховка обязана закрыть чанк"


def test_global_switch_still_wins_over_per_call(monkeypatch):
    """Общая ручка ``CLASSIFIER_PAID_FALLBACK=0`` сильнее ручки потока."""
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "0")
    _minimax_failing(monkeypatch, "engine_failed")
    paid = _paid_spy(monkeypatch)

    out = headless.chat(user="u", system="s", engine="minimax", paid_fallback=True)

    assert out["ok"] is False
    assert paid == [], "общая ручка «платить нельзя» — последнее слово"
