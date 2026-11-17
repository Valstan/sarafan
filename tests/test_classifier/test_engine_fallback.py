"""Политика выбора движка и фолбэка: MiniMax — основной, Дипсик — страховка.

**Что здесь стережётся.** Решение владельца 16.11: «Дипсику оставалось только те
кусочки, когда с MiniMax случаются какие-то сбои случайные, которые должны быть
условно пару раз в неделю, а не половина трафика в течение дня».

До правки любой отказ MiniMax закрывался Дипсиком, и это стоило половины
трафика: замер 16.11 — 372 вызова Дипсика на классификацию против ~414
успешных вызовов MiniMax (ровно 47 % на платном движке). Теперь фолбэк
включается **только** по решению самого движка (``pool_dead``), а обычный отказ
оставляет чанк без вердикта (fail-open).
"""

from __future__ import annotations

from modules.classifier import headless


def _install(monkeypatch, *, minimax_result, deepseek_result=None):
    """Подменить оба движка; вернуть список вызовов Дипсика."""
    calls = []

    def _minimax(**kwargs):
        return dict(minimax_result)

    def _deepseek(**kwargs):
        calls.append(kwargs)
        return dict(deepseek_result or {"ok": True, "content": "{}", "model": "deepseek-flash"})

    import modules.minimax_engine as mx

    monkeypatch.setattr(mx, "chat", _minimax)
    monkeypatch.setattr(headless, "_deepseek_chat", _deepseek)
    return calls


def test_minimax_failure_does_not_fall_back_to_deepseek(monkeypatch):
    """Обычный отказ движка — чанк без вердикта, платный Дипсик не трогаем."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "karman_unavailable", "detail": "пауза"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is False
    assert result["reason"] == "karman_unavailable"
    assert calls == []


def test_pool_dead_goes_to_deepseek(monkeypatch):
    """Пул мёртв (десять пустых подряд) — волна уходит платной страховке."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "pool_dead", "detail": "10 аккаунтов подряд"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is True
    assert result["model"] == "deepseek-flash"
    assert len(calls) == 1


def test_paid_fallback_can_be_switched_off(monkeypatch):
    """``CLASSIFIER_PAID_FALLBACK=0`` — даже мёртвый пул не тратит деньги."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "0")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "pool_dead"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is False
    assert result["reason"] == "pool_dead"
    assert calls == []


def test_explicit_engine_override_wins(monkeypatch):
    """Путь со своим движком (переписывание постов) не тащит общий переключатель."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "pool_dead"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    result = headless.chat(system="S", user="U", engine="deepseek")

    assert result["ok"] is True
    assert len(calls) == 1


def test_posts_argument_does_not_reach_deepseek(monkeypatch):
    """``posts`` — только для журнала расхода MiniMax; у Дипсика такой
    сигнатуры нет, и лишний аргумент сломал бы его вызов."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "pool_dead"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    headless.chat(system="S", user="U", posts=5)

    assert len(calls) == 1
    assert "posts" not in calls[0]


def test_chunk_size_is_per_engine(monkeypatch):
    """У MiniMax и Дипсика свой размер чанка: агент-CLI против HTTP-запроса."""
    from config import classifier

    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.delenv("MINIMAX_CHUNK_SIZE", raising=False)
    monkeypatch.setenv("CLASSIFIER_HEADLESS_CHUNK", "40")
    assert classifier.get_chunk_size_for_engine() == 20

    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    assert classifier.get_chunk_size_for_engine() == 40

    monkeypatch.setenv("MINIMAX_CHUNK_SIZE", "30")
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    assert classifier.get_chunk_size_for_engine() == 30


def test_publish_timeout_is_per_engine(monkeypatch):
    """Потолок волны — тоже по движку: 40 с у Дипсика, 90 с у MiniMax.

    Вызов MiniMax — запуск агента-CLI (десятки секунд); общий потолок 40 с
    обрывал его **прямо на волне**, то есть на главном пути.
    """
    from config.opencode import get_publish_timeout_for_engine

    monkeypatch.delenv("OPENCODE_PUBLISH_TIMEOUT", raising=False)
    monkeypatch.delenv("MINIMAX_PUBLISH_TIMEOUT", raising=False)

    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    assert get_publish_timeout_for_engine() == 40.0

    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    assert get_publish_timeout_for_engine() == 180.0

    monkeypatch.setenv("MINIMAX_PUBLISH_TIMEOUT", "150")
    assert get_publish_timeout_for_engine() == 150.0
