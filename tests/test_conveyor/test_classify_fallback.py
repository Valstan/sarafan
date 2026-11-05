"""Фолбэк на free-движок, когда прямой DeepSeek отвечает «Insufficient Balance» (402).

Замер 05.11.2026: прямой API без денег на счёте → конвейер молчал, 150 постов
лежали без вердикта, сайты не кормились с 17.10. Free-движок отдаёт тот же
контракт chat(), поэтому подмена не должна менять разбор ответа.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from modules.conveyor import classify as classify_mod

POST: Dict[str, Any] = {
    "lip": "1_2",
    "text": "В нашем ДК состоялся праздничный концерт, на котором выступили коллективы района.",
    "region_code": "mi",
    "theme": "novost",
}

OK_JSON = (
    '{"action": "accept", "section": "novosti", "title": "Концерт в нашем ДК", '
    '"text": "В нашем ДК состоялся праздничный концерт, на котором выступили коллективы района."}'
)


def _balance_402() -> Tuple[int, Dict[str, Any]]:
    return 402, {
        "error": {
            "message": "Insufficient Balance (request_id: abc)",
            "type": "insufficient_balance_error",
        }
    }


def test_balance_402_goes_to_opencode_and_parses(monkeypatch):
    """402 от DeepSeek → free-движок, ответ разбирается как обычно."""
    calls: List[str] = []

    def fake_call_api(body, **kwargs):
        calls.append("deepseek")
        return _balance_402()

    def fake_call_opencode(body):
        calls.append("opencode")
        return 200, {"choices": [{"message": {"content": OK_JSON}}]}

    monkeypatch.setattr(classify_mod, "_call_api", fake_call_api)
    monkeypatch.setattr(classify_mod, "_call_opencode", fake_call_opencode)

    res = classify_mod.classify(POST, sections=("novosti",), rules="", api_key="k")

    assert calls == ["deepseek", "opencode"]
    assert res["ok"] is True
    assert res["verdict"]["section"] == "novosti"


def test_fallback_failure_keeps_the_truth(monkeypatch):
    """Если free-движок тоже не ответил — в журнал ложится настоящая причина 402."""
    monkeypatch.setattr(classify_mod, "_call_api", lambda body, **kwargs: _balance_402())
    monkeypatch.setattr(
        classify_mod,
        "_call_opencode",
        lambda body: (0, {"error": {"message": "engine_disabled"}}),
    )

    res = classify_mod.classify(POST, sections=("novosti",), rules="", api_key="k")

    assert res["ok"] is False
    assert res["reason"] == "http_402"


def test_is_balance_error_recognises_payload():
    assert classify_mod._is_balance_error({"error": {"message": "Insufficient Balance"}}) is True
    assert classify_mod._is_balance_error({"error": {"message": "Insufficient Balance"}}) is True
    assert classify_mod._is_balance_error({"error": {"message": "bad request"}}) is False
    assert classify_mod._is_balance_error({}) is False
