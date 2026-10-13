"""Тесты движка OpenCode-классификации (пилот 14.10).

Мокаем транспорт ``urllib`` — сеть не нужна. Проверяем три вещи, которые
дороже всего ломаются молча: сбор текста из потока json-lines, фолбэк по
цепочке при geo-отказе (музейный RegionError) и граница «все модели молчат»
(контракт ``engine_all_failed``, чтобы классификатор получил тихий отказ,
а не исключение).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from modules import opencode_engine
from modules.opencode_engine import chat, parse_stream


class _FakeResponse:
    """Файл-подобный ответ urlopen: read() для JSON и итерация для потока."""

    def __init__(self, status: int, payload: Any, lines: Optional[List[bytes]] = None):
        self._status = status
        self._payload = payload
        self._lines = lines or []

    @property
    def status(self) -> int:
        return self._status

    def read(self) -> bytes:
        if isinstance(self._payload, (dict, list)):
            return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")
        return str(self._payload or "").encode("utf-8")

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _stream_lines(*events: Dict[str, Any]) -> List[bytes]:
    return [json.dumps(e, ensure_ascii=False).encode("utf-8") for e in events]


def _text_event(text: str) -> Dict[str, Any]:
    return {
        "info": {"tokens": {"input": 10, "output": 5, "reasoning": 0}},
        "parts": [{"type": "text", "text": text}],
    }


def _error_event(model: str, status_code: int, message: str) -> Dict[str, Any]:
    return {
        "info": {
            "error": {
                "name": "APIError",
                "data": {"message": message, "statusCode": status_code},
            }
        },
        "parts": [],
    }


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_ENGINE", "opencode")
    monkeypatch.setenv("OPENCODE_BASE_URL", "http://127.0.0.1:4099")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "")


def _patch_transport(monkeypatch, responses: List[_FakeResponse]) -> None:
    """urlopen по очереди отдаёт подготовленные ответы."""
    calls: List[_FakeResponse] = list(responses)

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return calls.pop(0)

    monkeypatch.setattr(opencode_engine.urllib.request, "urlopen", fake_urlopen)


def test_parse_stream_collects_text_and_usage():
    text, usage, error = parse_stream([_text_event("первый "), _text_event("второй")])
    assert text == "первый второй"
    assert usage == {"input": 10, "output": 5, "reasoning": 0}
    assert error is None


def test_normalize_content_unwraps_markdown_and_array():
    """longcat отдаёт ```json + топ-level массив — движок приводит к контракту."""
    from modules.opencode_engine import _normalize_content

    assert json.loads(_normalize_content('```json\n[{"a": 1}]\n```')) == {"verdicts": [{"a": 1}]}
    # Объект с нужным ключом проходит как есть.
    raw = '{"verdicts": [{"a": 1}]}'
    assert _normalize_content(raw) == raw
    # Не-JSON не трогаем: пусть классификатор получит llm_unparseable.
    assert _normalize_content(" boltox ") == " boltox "


def test_normalize_usage_maps_serve_counters():
    from modules.opencode_engine import _normalize_usage

    usage = _normalize_usage({"input": 4254, "output": 181, "reasoning": 251, "total": 4435})
    assert usage == {"total_tokens": 4435, "prompt_tokens": 4254, "completion_tokens": 181}
    assert _normalize_usage(None) is None


def test_parse_stream_extracts_error():
    text, usage, error = parse_stream([_error_event("muse", 403, "RegionError")])
    assert text == ""
    assert error is not None and "403" in error and "RegionError" in error


def test_chat_first_model_answers(monkeypatch, engine_on):
    _patch_transport(
        monkeypatch,
        [
            _FakeResponse(200, {"id": "ses_1"}),
            _FakeResponse(200, None, lines=_stream_lines(_text_event('{"verdicts": []}'))),
        ],
    )
    result = chat(user="посты", system="постулаты", label="test")
    assert result["ok"] is True
    assert result["content"] == '{"verdicts": []}'
    assert result["model"].startswith("opencode/")


def test_chat_falls_back_when_first_model_geo_blocked(monkeypatch, engine_on):
    """muse → 403 RegionError → следующая в цепочке отвечает — вызов зелёный."""
    _patch_transport(
        monkeypatch,
        [
            _FakeResponse(200, {"id": "ses_1"}),
            _FakeResponse(
                200,
                None,
                lines=_stream_lines(
                    _error_event("muse", 403, "This model is not available in your country")
                ),
            ),
            _FakeResponse(200, {"id": "ses_2"}),
            _FakeResponse(200, None, lines=_stream_lines(_text_event("OK"))),
        ],
    )
    monkeypatch.setenv(
        "OPENCODE_MODEL_CHAIN",
        "muse-spark-1.3-contributor-free,longcat-2.5-preview-free",
    )
    result = chat(user="посты", label="test")
    assert result["ok"] is True
    assert result["model"] == "opencode/longcat-2.5-preview-free"


def test_chat_all_models_fail_gives_engine_all_failed(monkeypatch, engine_on):
    """Транспорт мёртв (serve не поднят) — тихий отказ, не исключение."""
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "longcat-2.5-preview-free,space-bunny-free")

    def boom(req, timeout=None):  # noqa: ARG001
        raise OSError("Connection refused")

    monkeypatch.setattr(opencode_engine.urllib.request, "urlopen", boom)
    result = chat(user="посты", label="test")
    assert result["ok"] is False
    assert result["reason"] == "engine_all_failed"
    assert result["detail"].count("network") == 2


def test_chat_skips_retry_when_reason_not_retryable(monkeypatch, engine_on):
    """no_base_url не лечится следующей моделью — выходим сразу."""
    monkeypatch.setenv("OPENCODE_BASE_URL", "")
    result = chat(user="посты", label="test")
    assert result["ok"] is False
    assert result["reason"] == "engine_all_failed"


def test_chat_engine_disabled(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    result = chat(user="посты", label="test")
    assert result["ok"] is False
    assert result["reason"] == "engine_disabled"


def test_chat_empty_prompt(monkeypatch, engine_on):
    result = chat(user="   ", label="test")
    assert result == {"ok": False, "reason": "empty_prompt"}


def test_headless_router_follows_engine_flag(monkeypatch, engine_on):
    """Флаг CLASSIFIER_ENGINE реально переводят вызов на opencode и обратно."""
    from modules import opencode_engine
    from modules.classifier import headless

    monkeypatch.setattr(
        opencode_engine,
        "chat",
        lambda **kw: {"ok": True, "content": "x", "model": "opencode/y", "usage": None},
    )
    sentinel = {"ok": False, "reason": "deepseek-path"}
    monkeypatch.setattr(headless, "_deepseek_chat", lambda **kw: sentinel)

    assert headless.chat(user="u") == {
        "ok": True,
        "content": "x",
        "model": "opencode/y",
        "usage": None,
    }

    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")
    assert headless.chat(user="u") is sentinel
