"""Политика выбора движка и фолбэка: MiniMax — основной, Дипсик — страховка.

**Что здесь стережётся.** Решение владельца 17.11: «Дипсик вернуть на
подстраховку, если Минимакс проморгал или не смог — то отдавать пропущенное
Дипсику». То есть **любой** отказ MiniMax закрывает чанк платным Дипсиком:
контент не должен теряться из-за того, что у бесплатного движка кончились
очки или Карман не ответил.

История этой кнопки — важная, потому что тот же фолбэк уже один раз стоил
половины трафика: замер 16.11 — 372 вызова Дипсика против ~414 успешных
вызовов MiniMax (ровно 47 % на платном движке). Тогда причина была не в
«сбоях», а в паузе на полчаса после трёх подряд пустых аккаунтов. С тех пор
пауза — 10 с, мёртвые аккаунты уходят в ``exclude=``, пул помечается мёртвым
после десяти пустых подряд.

Поэтому рядом с фолбэком стоит **потолок платных вызовов на сутки**
(``CLASSIFIER_PAID_DAILY_CALLS``, по умолчанию выключен) и весь расход виден
в панели: решение «сколько платить за страховку» — за владельцем, но оно не
должно быть невидимым.
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


def test_minimax_failure_falls_back_to_deepseek(monkeypatch):
    """Карман не ответил — пропущенное уходит платному Дипсику, а не теряется."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "karman_unavailable", "detail": "пауза"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is True
    assert result["model"] == "deepseek-flash"
    assert len(calls) == 1


def test_every_minimax_failure_reason_falls_back(monkeypatch):
    """Причина отказа MiniMax не важна: важно, что вердикта нет.

    Проверяем весь класс отказов движка, а не один счастливый случай: раньше
    фолбэк срабатывал только на ``pool_dead``, и остальные причины молча
    оставляли чанк без вердикта (fail-open) — контент уходил в ленту мимо
    фильтра.
    """
    reasons = (
        "karman_unavailable",
        "pool_dead",
        "accounts_exhausted",
        "quota_exhausted",
        "engine_failed",
        "timeout",
        "exec_failed",
    )
    for reason in reasons:
        monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
        monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")
        calls = _install(
            monkeypatch,
            minimax_result={"ok": False, "reason": reason},
            deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
        )

        result = headless.chat(system="S", user="U")

        assert result["ok"] is True, reason
        assert len(calls) == 1, reason


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


def test_posts_argument_reaches_journal_only(monkeypatch):
    """``posts`` — объём для журнала расхода, а не ключ в теле запроса.

    Раньше аргумент забирался «только для MiniMax», потому что у Дипсика такой
    сигнатуры не было. Теперь у клиента Дипсика есть ``posts`` — он уходит в
    журнал («на сколько постов потратили вызов»), но в HTTP-тело не попадает
    (проверяется в ``tests/test_deepseek/test_client.py``).
    """
    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "pool_dead"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    headless.chat(system="S", user="U", posts=5)

    assert len(calls) == 1
    assert calls[0]["posts"] == 5


def test_paid_budget_is_unlimited_by_default(monkeypatch):
    """По умолчанию потолка нет: решение владельца 17.11 — страховка без лимита."""
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")
    monkeypatch.delenv("CLASSIFIER_PAID_DAILY_CALLS", raising=False)
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "engine_failed"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is True
    assert len(calls) == 1


def test_paid_budget_stops_paid_calls_when_limit_reached(monkeypatch):
    """Потолок исчерпан — чанк без вердикта, деньги не текут дальше."""
    from modules import neural_usage

    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")
    monkeypatch.setenv("CLASSIFIER_PAID_DAILY_CALLS", "5")
    monkeypatch.setattr(neural_usage, "count_paid_calls", lambda **kw: 5)
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "engine_failed"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is False
    assert result["reason"] == "engine_failed"
    assert calls == []


def test_paid_budget_allows_calls_below_limit(monkeypatch):
    """До потолка страховка работает как обычно."""
    from modules import neural_usage

    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")
    monkeypatch.setenv("CLASSIFIER_PAID_DAILY_CALLS", "5")
    monkeypatch.setattr(neural_usage, "count_paid_calls", lambda **kw: 4)
    calls = _install(
        monkeypatch,
        minimax_result={"ok": False, "reason": "engine_failed"},
        deepseek_result={"ok": True, "content": "{}", "model": "deepseek-flash"},
    )

    result = headless.chat(system="S", user="U")

    assert result["ok"] is True
    assert len(calls) == 1


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


def test_task_reaches_minimax_but_never_deepseek(monkeypatch):
    """Задание агенту MiniMax доходит до движка, а платный Дипсик его не видит.

    Задание — часть контракта вызова MiniMax (по потоку: классификация,
    переписывание, конвейер). Платный Дипсик такого аргумента не знает: если
    задание протечёт в его запрос, он либо сломается, либо (что хуже) уйдёт в
    тело запроса.
    """
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")
    monkeypatch.setenv("CLASSIFIER_PAID_FALLBACK", "1")

    minimax_calls = []
    deepseek_calls = []

    import modules.minimax_engine as mx

    def _minimax(**kwargs):
        minimax_calls.append(kwargs)
        return {"ok": False, "reason": "engine_failed", "detail": "упал"}

    def _deepseek(**kwargs):
        deepseek_calls.append(kwargs)
        return {"ok": True, "content": "{}", "model": "deepseek-flash"}

    monkeypatch.setattr(mx, "chat", _minimax)
    monkeypatch.setattr(headless, "_deepseek_chat", _deepseek)

    headless.chat(system="S", user="U", label="post_rewrite", task="своё задание")

    assert minimax_calls and minimax_calls[0]["task"] == "своё задание"
    assert deepseek_calls, "отказ MiniMax должен уйти в платную страховку"
    assert "task" not in deepseek_calls[0]
