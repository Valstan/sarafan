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


class _FakeChild:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):  # noqa: ARG002
        return 0

    def kill(self):
        self.killed = True


def test_port_of_parses_base_url(monkeypatch):
    from modules.opencode_engine import _port_of

    assert _port_of("http://127.0.0.1:4096") == 4096
    assert _port_of("http://127.0.0.1:4099/base") == 4099
    assert _port_of("мусор") == 4096


def test_harness_server_reuses_live_serve(monkeypatch):
    """Порт занят живым serve — своего ребёнка не плодим."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: True)

    def boom(*a, **kw):  # pragma: no cover — Popen не должен вызываться
        raise AssertionError("Popen вызван при живом serve")

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", boom)
    with opencode_engine.harness_server() as alive:
        assert alive is True


def test_harness_server_spawns_and_terminates(monkeypatch):
    """Порт свободен — serve поднят ребёнком и убит в finally."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")

    seen = {}
    child = _FakeChild()

    def fake_popen(cmd, env=None, **kwargs):  # noqa: ARG001
        seen["cmd"] = cmd
        seen["start_new_session"] = kwargs.get("start_new_session")
        return child

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", fake_popen)
    with opencode_engine.harness_server() as alive:
        assert alive is True
    assert "serve" in seen["cmd"]
    assert seen["start_new_session"] is True
    assert child.terminated is True


def test_harness_server_terminates_on_exception(monkeypatch):
    """Падение прогона внутри блока не оставляет serve-сироты."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")
    child = _FakeChild()
    monkeypatch.setattr(opencode_engine.subprocess, "Popen", lambda *a, **kw: child)

    with pytest.raises(RuntimeError):
        with opencode_engine.harness_server():
            raise RuntimeError("прогон упал")
    assert child.terminated is True


def test_default_chain_excludes_geo_dead_muse():
    """muse geo-мертва с IP бокса (P195) — в дефолтной цепочке её нет,
    иначе каждый чанк съедает таймаут впустую."""
    from config.opencode import DEFAULT_MODEL_CHAIN

    assert "muse-spark-1.3-contributor-free" not in DEFAULT_MODEL_CHAIN
    assert DEFAULT_MODEL_CHAIN[0] == "longcat-2.5-preview-free"


def test_serve_limits_from_env(monkeypatch):
    from config import opencode as oc

    assert oc.get_serve_rss_kill_mb() == 450
    assert oc.get_serve_max_lifetime() == 15 * 60
    monkeypatch.setenv("OPENCODE_SERVE_RSS_KILL_MB", "300")
    monkeypatch.setenv("OPENCODE_SERVE_MAX_LIFETIME", "600")
    assert oc.get_serve_rss_kill_mb() == 300
    assert oc.get_serve_max_lifetime() == 600.0


def test_harness_server_starts_rss_monitor(monkeypatch):
    """Ребёнок без preexec_fn (RLIMIT_AS ломает старт bun); RSS-монитор
    запускается и останавливается при выходе."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")

    seen = {}
    child = _FakeChild()

    def fake_popen(cmd, env=None, **kwargs):  # noqa: ARG001
        seen["preexec_fn"] = kwargs.get("preexec_fn")
        seen["start_new_session"] = kwargs.get("start_new_session")
        return child

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(opencode_engine.threading, "Timer", _FakeTimer)

    with opencode_engine.harness_server():
        pass
    assert seen["preexec_fn"] is None
    assert seen["start_new_session"] is True
    assert _FakeTimer.started is True
    assert _FakeTimer.cancelled is True
    assert child.terminated is True


def test_harness_server_systemd_oneshot(monkeypatch):
    """Автоматический путь: systemctl start/stop вместо Popen."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_SYSTEMD_RUN", "1")

    calls = []

    def fake_systemctl(action, timeout=30.0):  # noqa: ARG002
        calls.append(action)
        return True

    monkeypatch.setattr(opencode_engine, "_systemctl_run", fake_systemctl)

    def boom(*a, **kw):  # pragma: no cover — Popen не должен вызываться
        raise AssertionError("Popen вызван в systemd-режиме")

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", boom)
    with opencode_engine.harness_server() as alive:
        assert alive is True
    assert calls == ["start", "stop"]


def test_harness_server_systemd_start_failure(monkeypatch):
    """systemctl start не прошёл — yield False, stop не зовём."""
    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setenv("OPENCODE_SYSTEMD_RUN", "1")
    monkeypatch.setattr(
        opencode_engine, "_systemctl_run", lambda action, timeout=30.0: action == "stop"
    )

    with opencode_engine.harness_server() as alive:
        assert alive is False


def test_child_rss_mb_reads_proc():
    import os

    from modules.opencode_engine import _child_rss_mb

    if not os.path.exists("/proc/self/status"):  # pragma: no cover — Windows
        pytest.skip("/proc доступен только на Linux")
    rss = _child_rss_mb(os.getpid())
    assert rss is not None and rss > 0
    assert _child_rss_mb(999999) is None


class _FakeTimer:
    started = False
    cancelled = False

    def __init__(self, delay, fn):  # noqa: ARG002
        type(self).started = True

    def start(self):
        pass

    def cancel(self):
        type(self).cancelled = True


def test_classify_posts_wraps_harness_for_opencode_engine(monkeypatch):
    """Батч под opencode идёт внутри harness_server — воркеру не надо ничего знать."""
    from modules import opencode_engine
    from modules.classifier import headless

    posts = [{"lip": "-1_1", "region_code": "mi", "text": "тест"}]

    class _Ctx:
        entered = False

        def __enter__(self):
            type(self).entered = True
            return True

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(opencode_engine, "harness_server", lambda: _Ctx())
    verdict_json = (
        '{"verdicts": [{"lip": "-1_1", "theme": "новости", '
        '"action": "publish", "confidence": 90}]}'
    )
    monkeypatch.setattr(
        opencode_engine,
        "chat",
        lambda **kw: {
            "ok": True,
            "content": verdict_json,
            "model": "opencode/x",
            "usage": {"total_tokens": 10, "prompt_tokens": 6, "completion_tokens": 4},
        },
    )
    monkeypatch.setattr(headless, "engine_enabled", lambda: True)

    run = headless.classify_posts(posts, postulates="p")
    assert _Ctx.entered is True
    assert len(run["verdicts"]) == 1
    assert run["failures"] == []
    assert run["tokens"] == 10


def test_sweep_opencode_spill_removes_only_old_matching(tmp_path):
    """P197: старый `.bcdf*.so` убран, свежий и посторонние файлы — нет."""
    import os
    import time

    from modules.opencode_engine import sweep_opencode_spill

    old = tmp_path / ".bcdf1234abcd-00000000.so"
    old.write_bytes(b"x" * 1024)
    fresh = tmp_path / ".bcdf5678efgh-00000000.so"
    fresh.write_bytes(b"y" * 1024)
    other = tmp_path / "keep.txt"
    other.write_bytes(b"z")
    now = time.time()
    os.utime(old, (now - 3600, now - 3600))
    os.utime(other, (now - 3600, now - 3600))

    stats = sweep_opencode_spill(str(tmp_path), max_age_sec=1800, now=now)

    assert not old.exists()
    assert fresh.exists()
    assert other.exists()
    assert stats["deleted"] == 1
    assert stats["freed_bytes"] == 1024
    assert stats["failed"] == 0


def test_sweep_opencode_spill_removes_stale_run_dir(tmp_path):
    """Каталог запуска, прерванного до finally, не живёт вечно."""
    import os
    import time

    from modules.opencode_engine import sweep_opencode_spill

    stale = tmp_path / "setka-opencode-stale"
    stale.mkdir()
    (stale / ".bcdf0000-00000000.so").write_bytes(b"x" * 10)
    live = tmp_path / "setka-opencode-live"
    live.mkdir()
    now = time.time()
    os.utime(stale, (now - 3600, now - 3600))

    stats = sweep_opencode_spill(str(tmp_path), max_age_sec=1800, now=now)

    assert not stale.exists()
    assert live.exists()
    assert stats["removed_dirs"] == 1


def test_sweep_opencode_spill_missing_dir_is_silent(tmp_path):
    """Свипер не бросает: падающий на каждом тике свипер — молчащий свипер."""
    from modules.opencode_engine import sweep_opencode_spill

    stats = sweep_opencode_spill(str(tmp_path / "нет-каталога"))

    assert stats["deleted"] == 0
    assert stats["failed"] == 0


def test_harness_server_popen_gets_private_tmpdir(monkeypatch):
    """P197: ребёнок получает приватный TMPDIR, каталог исчезает после блока."""
    import os
    from pathlib import Path

    from modules import opencode_engine

    monkeypatch.setattr(opencode_engine, "server_alive", lambda: False)
    monkeypatch.setattr(opencode_engine, "_wait_port", lambda *a, **kw: True)
    monkeypatch.setenv("OPENCODE_BIN", "/tmp/fake-opencode")

    seen = {}
    child = _FakeChild()

    def fake_popen(cmd, env=None, **kwargs):  # noqa: ARG001
        seen["env"] = env
        return child

    monkeypatch.setattr(opencode_engine.subprocess, "Popen", fake_popen)
    with opencode_engine.harness_server():
        tmpdir = seen["env"]["TMPDIR"]
        assert os.path.isdir(tmpdir)
        # Мусор запуска оседает внутри приватного каталога...
        Path(tmpdir, ".bcdf0000-00000000.so").write_bytes(b"x")
    # ...и уходит вместе с ним.
    assert not Path(tmpdir).exists()


def test_beat_schedule_has_opencode_tmp_sweep():
    """Страховка P197 зарегистрирована в beat — иначе она молчит."""
    from tasks.celery_app import app

    entry = app.conf.beat_schedule.get("sweep-opencode-tmp")
    assert entry is not None
    assert entry["task"] == "tasks.celery_app.sweep_opencode_tmp"
    assert entry["options"]["catchup"] is False
