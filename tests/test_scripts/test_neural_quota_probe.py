"""Unit-тесты для ``scripts/neural_quota_probe.py`` — проба выхода (P210).

Скрипт — CLI-утилита вне пакета, грузим через importlib (как
``tests/test_scripts/test_smoke_test.py``). На бокс здесь ничего не ходит:
``run_cycle`` принимает шаги и ``wait`` инжектируемыми, поэтому проверяется
порядок действий и решения, а не systemctl/curl.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "neural_quota_probe", REPO_ROOT / "scripts" / "neural_quota_probe.py"
)
probe = importlib.util.module_from_spec(_spec)
sys.modules["neural_quota_probe"] = probe
_spec.loader.exec_module(probe)


class FakeSteps:
    """Заглушки шагов с журналом вызовов."""

    def __init__(
        self,
        *,
        rotate_ok: bool = True,
        start_ok: bool = True,
        probes: List[Dict[str, Any]] | None = None,
        exits: List[str | None] | None = None,
    ) -> None:
        self.calls: List[str] = []
        self.rotate_ok = rotate_ok
        self.start_ok = start_ok
        self.probes = list(probes or [{"ok": True, "reason": "ok"}])
        self.exits = list(exits or ["1.1.1.1|DE"])
        self._probe_i = 0
        self._exit_i = 0

    def rotate(self):
        self.calls.append("rotate")
        return (self.rotate_ok, "" if self.rotate_ok else "нет выхода")

    def exit_addr(self):
        value = self.exits[self._exit_i % len(self.exits)]
        self._exit_i += 1
        self.calls.append(f"exit:{value}")
        return value

    def engine_stop(self):
        self.calls.append("stop")
        return (True, "")

    def engine_start(self):
        self.calls.append("start")
        return (self.start_ok, "" if self.start_ok else "юнит не поднялся")

    def engine_reset(self):
        self.calls.append("reset")
        return (True, "")

    def probe(self):
        value = self.probes[self._probe_i % len(self.probes)]
        self._probe_i += 1
        self.calls.append("probe")
        return value

    def as_steps(self):
        return probe.Steps(
            engine_stop=self.engine_stop,
            engine_start=self.engine_start,
            engine_reset=self.engine_reset,
            rotate=self.rotate,
            exit_addr=self.exit_addr,
            probe=self.probe,
        )


def _run(fake: FakeSteps, **kwargs):
    kwargs.setdefault("max_hops", 3)
    kwargs.setdefault("pause", 0.0)
    return probe.run_cycle(
        fake.as_steps(),
        wait=lambda *a, **k: True,
        sleep=lambda _s: None,
        **kwargs,
    )


# --- чистая часть ---------------------------------------------------------- #


def test_parse_env_file_skips_noise():
    text = """
    # комментарий
    OPENCODE_PASSWORD="secret value"
    BROKEN LINE
    EMPTY=
    """
    parsed = probe.parse_env_file(text)
    assert parsed["OPENCODE_PASSWORD"] == "secret value"
    assert parsed["EMPTY"] == ""
    assert "BROKEN LINE" not in parsed


def test_pick_password_prefers_server_name():
    assert probe.pick_password({"OPENCODE_PASSWORD": "a", "OPENCODE_SERVER_PASSWORD": "b"}) == "b"
    assert probe.pick_password({"OPENCODE_PASSWORD": "a"}) == "a"
    assert probe.pick_password({}) == ""


def test_wanted_env_key_is_narrow_allowlist():
    assert probe.wanted_env_key("CLASSIFIER_ENGINE")
    assert probe.wanted_env_key("OPENCODE_MODEL_CHAIN")
    assert probe.wanted_env_key("NEURAL_EGRESS_ROTATE_COOLDOWN")
    assert probe.wanted_env_key("ENGINE_BREAKER_QUOTA_COOLDOWN")
    # Секреты и прочий env приложения в пробу не едут.
    assert not probe.wanted_env_key("DATABASE_URL")
    assert not probe.wanted_env_key("VK_TOKEN_VALSTAN")
    assert not probe.wanted_env_key("CLASSIFIER_INGEST_KEY")


def test_load_app_env_sets_missing_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / "setka.env"
    env.write_text(
        "CLASSIFIER_ENGINE=opencode\n"
        'OPENCODE_MODEL_CHAIN="deepseek-v4.1-flash-free,glm-5.3-flash-free"\n'
        "OPENCODE_MODEL_VARIANTS=\n"
        "DATABASE_URL=postgresql://secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_MODEL_CHAIN", "mine")
    monkeypatch.delenv("OPENCODE_MODEL_VARIANTS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CLASSIFIER_ENGINE", raising=False)

    found = probe.load_app_env(str(env))

    assert found["CLASSIFIER_ENGINE"] == "opencode"
    assert os.environ["CLASSIFIER_ENGINE"] == "opencode"
    # Заданное извне не перетирается.
    assert os.environ["OPENCODE_MODEL_CHAIN"] == "mine"
    # Пустое значение не подставляется: иначе перебило бы дефолт.
    assert "OPENCODE_MODEL_VARIANTS" not in os.environ
    # Чужой ключ не просочился ни в окружение, ни в отчёт.
    assert "DATABASE_URL" not in os.environ
    assert "DATABASE_URL" not in found


def test_load_app_env_missing_file_is_silent():
    assert probe.load_app_env("C:/definitely/not/here.env") == {}


def test_prepare_env_defaults_engine_to_opencode(tmp_path, monkeypatch):
    missing = str(tmp_path / "missing.env")
    monkeypatch.setattr(probe, "APP_ENV", missing)
    monkeypatch.setattr(probe, "ANON_ENV", missing)
    monkeypatch.delenv("CLASSIFIER_ENGINE", raising=False)
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_CMD", "sudo -n rotate")

    probe.prepare_env()

    assert os.environ["CLASSIFIER_ENGINE"] == "opencode"
    assert os.environ["OPENCODE_BASE_URL"] == probe.DEFAULT_BASE_URL
    # Автоповорот выхода на время пробы выключен.
    assert os.environ["NEURAL_EGRESS_ROTATE_CMD"] == ""
    assert probe.engine_ready() is True


def test_app_env_engine_key_reaches_the_engine(tmp_path, monkeypatch):
    """Ровно тот отказ 25.10: без env движок выключен, проба мерит пустоту."""
    env = tmp_path / "setka.env"
    env.write_text("CLASSIFIER_ENGINE=opencode\n", encoding="utf-8")
    monkeypatch.setattr(probe, "APP_ENV", str(env))
    monkeypatch.setattr(probe, "ANON_ENV", str(tmp_path / "missing.env"))
    monkeypatch.delenv("CLASSIFIER_ENGINE", raising=False)

    probe.prepare_env()

    assert probe.engine_ready() is True


def test_prepare_env_keeps_explicit_engine(tmp_path, monkeypatch):
    missing = str(tmp_path / "missing.env")
    monkeypatch.setattr(probe, "APP_ENV", missing)
    monkeypatch.setattr(probe, "ANON_ENV", missing)
    monkeypatch.setenv("CLASSIFIER_ENGINE", "deepseek")

    probe.prepare_env()

    assert os.environ["CLASSIFIER_ENGINE"] == "deepseek"
    assert probe.engine_ready() is False


def test_decide_classifies_vendor_failures():
    assert probe.decide({"ok": True, "reason": "ok"}) == "success"
    # отказы вендора: пробуем другой выход
    for reason in ("quota_exhausted", "http_429", "network_gate"):
        assert probe.decide({"ok": False, "reason": reason}) == "rotate", reason
    # локальные причины: смена адреса не лечит
    for reason in ("network", "empty_response", "engine_disabled", "breaker_open"):
        assert probe.decide({"ok": False, "reason": reason}) == "stop", reason


# --- цикл ------------------------------------------------------------------ #


def test_first_hop_success_stops_engine():
    fake = FakeSteps()
    result = _run(fake)

    assert result["success"] is True
    assert result["stop_reason"] == ""
    assert len(result["hops"]) == 1
    # порядок шага: поворот -> адрес -> стоп -> старт -> проба
    assert fake.calls[:5] == ["rotate", "exit:1.1.1.1|DE", "stop", "start", "probe"]
    # движок погашен в конце (владелец просил не держать его включённым),
    # и с него снято состояние failed после штатной остановки
    assert fake.calls[-2:] == ["stop", "reset"]
    assert result["engine_left_running"] is False


def test_quota_on_first_hop_rotates_and_retries():
    fake = FakeSteps(
        probes=[
            {"ok": False, "reason": "quota_exhausted", "detail": "Free usage limit reached"},
            {"ok": True, "reason": "ok"},
        ],
        exits=["1.1.1.1|DE", "2.2.2.2|PL"],
    )
    result = _run(fake)

    assert result["success"] is True
    assert [h["exit"] for h in result["hops"]] == ["1.1.1.1|DE", "2.2.2.2|PL"]
    assert result["hops"][0]["verdict"] == "rotate"
    assert result["hops"][1]["verdict"] == "success"
    assert fake.calls.count("rotate") == 2


def test_local_failure_stops_without_burning_other_exits():
    fake = FakeSteps(probes=[{"ok": False, "reason": "network"}])
    result = _run(fake)

    assert result["success"] is False
    assert result["stop_reason"] == "network"
    assert fake.calls.count("probe") == 1
    assert fake.calls.count("rotate") == 1


def test_rotate_failure_ends_cycle_before_probe():
    fake = FakeSteps(rotate_ok=False)
    result = _run(fake)

    assert result["success"] is False
    assert result["stop_reason"] == "egress_rotate_failed"
    assert "probe" not in fake.calls


def test_engine_start_failure_is_reported():
    fake = FakeSteps(start_ok=False)
    result = _run(fake)

    assert result["success"] is False
    assert result["stop_reason"] == "engine_start_failed"


def test_hop_budget_is_enforced():
    fake = FakeSteps(probes=[{"ok": False, "reason": "quota_exhausted"}] * 10)
    result = _run(fake, max_hops=2)

    assert result["success"] is False
    assert result["stop_reason"] == "hops_exhausted"
    assert fake.calls.count("probe") == 2


def test_leave_running_keeps_engine_up():
    fake = FakeSteps()
    result = _run(fake, leave_running=True)

    assert result["success"] is True
    assert result["engine_left_running"] is True
    assert fake.calls[-1] == "probe"  # финального stop нет


def test_direct_mode_does_not_touch_vpn():
    fake = FakeSteps()
    result = _run(fake, direct=True)

    assert result["success"] is True
    assert "rotate" not in fake.calls
    assert not any(c.startswith("exit:") for c in fake.calls)
    assert result["hops"][0]["exit"] is None


def test_log_receives_every_step():
    entries: List[Dict[str, Any]] = []
    fake = FakeSteps(probes=[{"ok": False, "reason": "quota_exhausted"}, {"ok": True}])
    _run(fake, write_log=entries.append)

    steps = [e["step"] for e in entries]
    assert steps == ["probe", "probe"]
    assert entries[0]["verdict"] == "rotate"
