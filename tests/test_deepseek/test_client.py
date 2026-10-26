"""Общий клиент DeepSeek — единственная точка входа к движку (D-024).

Тесты держат контракт, на который опираются три потребителя: конвейер,
категоризатор discovery и черновики ответов. Форма отказа тут важнее удачного
пути — все три вызывающих ветвятся именно по ``reason``.
"""

from __future__ import annotations

import pytest

from modules import deepseek_client as dc


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BALANCE_COOLDOWN", raising=False)
    dc.reset_balance_breaker()


def _api(status, payload):
    # `label` появился вместе с учётом префикс-кэша (мандат brain 2026-08-30):
    # chat() прокидывает его в call_api, чтобы расход разных потребителей не
    # сливался в одно среднее. Заглушка обязана повторять реальную сигнатуру —
    # иначе она проверяет вызов, которого в проде не существует.
    def fake(body, *, api_key, base_url, timeout, label="deepseek"):
        fake.body = body
        fake.api_key = api_key
        fake.label = label
        return status, payload

    return fake


def _completion(content, usage=None):
    return {"choices": [{"message": {"content": content}}], "usage": usage or {"total_tokens": 42}}


def test_happy_path_returns_content_model_and_usage(monkeypatch):
    monkeypatch.setattr(dc, "call_api", _api(200, _completion("привет")))
    out = dc.chat(user="вопрос")
    assert out == {
        "ok": True,
        "content": "привет",
        "model": "deepseek-chat",
        "usage": {"total_tokens": 42},
    }


def test_missing_key_is_refusal_not_crash(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert dc.chat(user="вопрос") == {"ok": False, "reason": "no_api_key"}


def test_empty_prompt_short_circuits_before_network(monkeypatch):
    called = {"n": 0}

    def fake(*a, **kw):
        called["n"] += 1
        return 200, _completion("x")

    monkeypatch.setattr(dc, "call_api", fake)
    assert dc.chat(user="   ")["reason"] == "empty_prompt"
    assert called["n"] == 0


def test_network_failure_is_reported(monkeypatch):
    monkeypatch.setattr(dc, "call_api", _api(0, {"error": "timeout"}))
    out = dc.chat(user="вопрос")
    assert out["ok"] is False
    assert out["reason"] == "network"
    assert out["detail"] == "timeout"


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_status_survives_in_reason(monkeypatch, status):
    """401 (чинит человек) и 429 (пройдёт само) — разные решения у вызывающего,
    поэтому код не схлопывается в общий 'http_error'."""
    monkeypatch.setattr(dc, "call_api", _api(status, {"error": "nope"}))
    assert dc.chat(user="вопрос")["reason"] == f"http_{status}"


def test_blank_content_is_empty_response(monkeypatch):
    monkeypatch.setattr(dc, "call_api", _api(200, _completion("   ")))
    assert dc.chat(user="вопрос")["reason"] == "empty_response"


# ─── Предохранитель «нет денег»: 402 не повторяем (замер 26.10) ─────────────


def test_402_opens_breaker_and_stops_calling_network(monkeypatch):
    """Первый 402 — один вызов; следующие отвечают локально.

    Живой прогон recheck 26.10: 2448 вызовов подряд, все 402, 27 минут слота.
    Предохранитель превращает это в один настоящий вызов и 2447 локальных.
    """
    calls = {"n": 0}

    def fake(body, *, api_key, base_url, timeout, label="deepseek"):
        calls["n"] += 1
        return 402, {"error": "Insufficient Balance"}

    monkeypatch.setattr(dc, "call_api", fake)

    first = dc.chat(user="вопрос")
    second = dc.chat(user="вопрос")

    assert first["reason"] == "http_402"
    assert second["reason"] == "http_402"
    assert "no network call" in second["detail"]
    assert calls["n"] == 1, "второй вызов обязан не ходить в сеть"


def test_breaker_does_not_touch_other_refusals(monkeypatch):
    """429/500 по-прежнему пробуются: это временные отказы, их лечит повтор."""
    monkeypatch.setattr(dc, "call_api", _api(429, {"error": "rate"}))

    assert dc.chat(user="вопрос")["reason"] == "http_429"
    assert dc.balance_blocked_for() == 0.0


def test_breaker_expires_so_topped_up_account_recovers(monkeypatch):
    """Кулдаун конечный: пополнили счёт — вызов после окна снова настоящий."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(dc.time, "monotonic", lambda: clock["t"])
    monkeypatch.setenv("DEEPSEEK_BALANCE_COOLDOWN", "60")

    statuses = [402, 200]
    calls = {"n": 0}

    def fake(body, *, api_key, base_url, timeout, label="deepseek"):
        calls["n"] += 1
        return statuses[min(calls["n"] - 1, 1)], _completion("ок")

    monkeypatch.setattr(dc, "call_api", fake)

    assert dc.chat(user="вопрос")["reason"] == "http_402"
    assert dc.balance_blocked_for() == 60.0

    clock["t"] += 61
    out = dc.chat(user="вопрос")

    assert out["ok"] is True, "после окна предохранитель обязан сняться сам"
    assert calls["n"] == 2


def test_missing_choices_is_empty_response(monkeypatch):
    monkeypatch.setattr(dc, "call_api", _api(200, {}))
    assert dc.chat(user="вопрос")["reason"] == "empty_response"


# ─── Бюджет вывода: обрезание по max_tokens (письмо brain 2026-09-10, G334) ──


def _finished(content, finish, usage=None):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage or {"total_tokens": 42},
    }


def test_cut_json_at_length_is_truncated_not_success(monkeypatch):
    """Наш случай, снят живой пробой на проде 10.09: нерассуждающая модель при
    тесном лимите отдаёт НЕПУСТОЙ оборванный JSON с ``finish_reason: length``."""
    cut = '{"lip": "x1", "theme": "инфраструктура", "action": "publish", "reasoning": "Позит'
    monkeypatch.setattr(dc, "call_api", _api(200, _finished(cut, "length")))
    out = dc.chat(user="вопрос", max_tokens=40)
    assert out["ok"] is False
    assert out["reason"] == "truncated"
    assert out["detail"] == "max_tokens=40"
    assert out["usage"] == {"total_tokens": 42}, "оплаченный вызов выпал из учёта"


def test_blank_content_at_length_is_truncated_not_empty(monkeypatch):
    """Случай соседа: раздумья съели весь бюджет, наружу пустой content."""
    monkeypatch.setattr(dc, "call_api", _api(200, _finished("", "length")))
    assert dc.chat(user="вопрос")["reason"] == "truncated"


def test_blank_content_without_length_is_still_empty_response(monkeypatch):
    """Контроль-негатив: без него «развели причины» неотличимо от «переименовали»."""
    monkeypatch.setattr(dc, "call_api", _api(200, _finished("   ", "stop")))
    assert dc.chat(user="вопрос")["reason"] == "empty_response"


def test_normal_stop_is_success(monkeypatch):
    monkeypatch.setattr(dc, "call_api", _api(200, _finished("ответ", "stop")))
    out = dc.chat(user="вопрос")
    assert out["ok"] is True and out["content"] == "ответ"


def test_system_prompt_is_sent_first(monkeypatch):
    fake = _api(200, _completion("ok"))
    monkeypatch.setattr(dc, "call_api", fake)
    dc.chat(user="u", system="s")
    assert [m["role"] for m in fake.body["messages"]] == ["system", "user"]


def test_blank_system_prompt_is_omitted(monkeypatch):
    fake = _api(200, _completion("ok"))
    monkeypatch.setattr(dc, "call_api", fake)
    dc.chat(user="u", system="   ")
    assert [m["role"] for m in fake.body["messages"]] == ["user"]


def test_json_mode_is_opt_in(monkeypatch):
    fake = _api(200, _completion("{}"))
    monkeypatch.setattr(dc, "call_api", fake)
    dc.chat(user="u")
    assert "response_format" not in fake.body
    dc.chat(user="u", json_object=True)
    assert fake.body["response_format"] == {"type": "json_object"}


def test_model_override_wins_over_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "from-env")
    fake = _api(200, _completion("ok"))
    monkeypatch.setattr(dc, "call_api", fake)
    out = dc.chat(user="u", model="explicit")
    assert fake.body["model"] == "explicit"
    assert out["model"] == "explicit"


def test_explicit_api_key_wins_over_env(monkeypatch):
    fake = _api(200, _completion("ok"))
    monkeypatch.setattr(dc, "call_api", fake)
    dc.chat(user="u", api_key="explicit-key")
    assert fake.api_key == "explicit-key"
