"""Ёмкость бокса под вызовы MiniMax Code: слот, память и честный отказ.

Замер 25.11 (прод): один вызов MiniMax — процесс **~315 МБ RSS**, бокс — 1536 МБ
без свопа, база ~1000 МБ, запас под CLI ~536 МБ. Четыре CLI разом (ночной срез
discovery) = 1,26 ГБ поверх базы: ядро сняло CLI **125 раз за ночь**, каждый отказ
ушёл в **платный Дипсик** (122 платных вызова). Утром то же самое без среза —
MiniMax с 0 % успеха, очередь ``celery`` выросла 15 → 72.

Здесь проверяется то, что это чинит: **до запуска** процесса решается, есть ли на
боксе место; слот не даёт двум вызовам пойти разом; убитый по памяти CLI **не
повторяется** на голодном боксе; а отказ называется своим именем, а не «движок не
ответил».
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from modules import minimax_capacity as capacity
from modules import minimax_engine as mx


class FakeRedis:
    """Redis-заглушка: слоты выдаются по одному, как настоящие."""

    def __init__(self, *, busy: bool = False, fail: bool = False):
        self.busy = busy
        self.fail = fail
        self.keys: dict[str, str] = {}
        self.released: list[str] = []

    def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise ConnectionError("redis лежит")
        if self.busy:
            return None
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def eval(self, script, numkeys, key, token):
        if self.fail:
            raise ConnectionError("redis лежит")
        if self.keys.get(key) == token:
            del self.keys[key]
            self.released.append(key)
            return 1
        return 0


@pytest.fixture(autouse=True)
def _capacity_env(monkeypatch):
    """Чистое окружение механизма: слот включён, ждать недолго."""
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(capacity.ENV_SLOT_WAIT_SECONDS, "0")
    monkeypatch.setenv(capacity.ENV_MIN_AVAILABLE_MB, "384")
    monkeypatch.delenv(capacity.ENV_MAX_CONCURRENT, raising=False)
    monkeypatch.setattr(capacity, "_client", None, raising=False)
    yield
    capacity._client = None


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    class _Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    return _Proc()


def _area(tmp_path: Path) -> mx.MinimaxAccount:
    root = tmp_path / "work"
    auth = root / "data" / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"schemaVersion": 1, "records": {}}), encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    return mx.MinimaxAccount(name="work", root=root, email="a@example.com")


# ── память ────────────────────────────────────────────────────────────────


def test_memory_decision_allows_when_box_has_room(monkeypatch):
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)

    ok, details = capacity.memory_decision()

    assert ok is True
    assert details["mem_available_mb"] == 664
    assert details["mem_required_mb"] == 384


def test_memory_decision_refuses_when_box_has_no_room(monkeypatch):
    """63 МБ — это замер 25.11 в момент волны: вызов там обречён."""
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 63)

    ok, details = capacity.memory_decision()

    assert ok is False
    assert details["mem_available_mb"] == 63


def test_memory_decision_is_skipped_without_meminfo(monkeypatch):
    """Не Linux (или файл не прочитан) — проверки нет, вызов идёт как раньше."""
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: None)

    ok, details = capacity.memory_decision()

    assert ok is True
    assert details["mem_available_mb"] is None


# ── слот ──────────────────────────────────────────────────────────────────


def test_lease_takes_slot_and_releases_it(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(capacity, "_client", fake)
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)

    lease = capacity.lease()

    assert lease.ok is True
    assert len(fake.keys) == 1, "слот должен быть занят"
    lease.release()
    assert not fake.keys, "слот отпущен"
    assert fake.released


def test_lease_refuses_when_all_slots_busy(monkeypatch):
    """Слот занят другим вызовом — свой не запускаем (иначе два CLI на боксе)."""
    monkeypatch.setattr(capacity, "_client", FakeRedis(busy=True))
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)

    lease = capacity.lease()

    assert lease.ok is False
    assert lease.reason == capacity.REASON_BUSY


def test_lease_refuses_when_box_has_no_room_and_frees_slot(monkeypatch):
    """Места нет — вызов не запускаем, а занятый слот отпускаем сразу."""
    fake = FakeRedis()
    monkeypatch.setattr(capacity, "_client", fake)
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 63)

    lease = capacity.lease()

    assert lease.ok is False
    assert lease.reason == capacity.REASON_NO_MEMORY
    assert not fake.keys, "слот отпущен — иначе бокс ждал бы впустую"


def test_lease_fails_open_when_redis_is_down(monkeypatch):
    """Redis лежит — вызов идёт (доступность важнее), страхует проверка памяти."""
    monkeypatch.setattr(capacity, "_client", FakeRedis(fail=True))
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)

    lease = capacity.lease()

    assert lease.ok is True


def test_disabled_capacity_lets_everything_through(monkeypatch):
    """Аварийный откат: флаг выключает механизм целиком."""
    monkeypatch.setenv(capacity.ENV_ENABLED, "0")
    monkeypatch.setattr(capacity, "_client", FakeRedis(busy=True))

    lease = capacity.lease()

    assert lease.ok is True
    assert lease.details.get("capacity_check") == "disabled"


# ── движок: вызов не запускается, когда бокс не тянет ─────────────────────


def test_engine_does_not_start_cli_when_box_is_starved(tmp_path, monkeypatch):
    """Нет места — CLI не поднимаем: отказ уходит в платный Дипсик сразу."""
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    monkeypatch.setattr(capacity, "_client", FakeRedis())
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 63)
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_proc(0)

    monkeypatch.setattr(subprocess, "run", _run)

    ok, _content, details = mx.run_classification(_area(tmp_path), system="S", user="U", task="T")

    assert ok is False
    assert details.get("reason") == capacity.REASON_NO_MEMORY
    assert not calls, "процесс CLI поднимать нельзя"


def test_engine_reports_memory_context_with_the_call(tmp_path, monkeypatch):
    """В деталях — сколько памяти было на боксе, когда вызов начался."""
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    monkeypatch.setattr(capacity, "_client", FakeRedis())
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 512)
    payload = json.dumps(
        {"type": "exec.result", "status": "succeeded", "output": '{"verdicts": []}'}
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _fake_proc(0, stdout=payload),
    )

    ok, _content, details = mx.run_classification(_area(tmp_path), system="S", user="U", task="T")

    assert ok is True
    assert details.get("mem_available_mb") == 512


def test_engine_does_not_repeat_killed_call_on_starved_box(tmp_path, monkeypatch):
    """Ядро сняло CLI, а бокс голоден — повтор не делаем.

    Повтор на голодном боксе — это ещё одно ядерное убийство и ещё 167 с
    занятости слота воркера (25.11: убийство → повтор → убийство → платный
    Дипсик).

    Бокс «портится» уже после старта вызова: на входе места хватало, а к
    моменту убийства — нет (так и бывает, когда CLI растёт и снимает
    ядро).
    """
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    monkeypatch.setattr(capacity, "_client", FakeRedis())
    readings = [664, 63]

    def _avail():
        return readings.pop(0) if len(readings) > 1 else readings[0]

    monkeypatch.setattr(capacity, "mem_available_mb", _avail)
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _fake_proc(-9, stderr="Killed")

    monkeypatch.setattr(subprocess, "run", _run)

    ok, _content, details = mx.run_classification(_area(tmp_path), system="S", user="U", task="T")

    assert ok is False
    assert len(calls) == 1, "повтор на голодном боксе — лишнее ядерное убийство"
    assert details.get("reason") == "oom_killed"
    assert details.get("killed_by_kernel") is True


def test_engine_repeats_killed_call_when_box_has_room(tmp_path, monkeypatch):
    """Бокс тянет — сброс состояния и один повтор (старое поведение)."""
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    monkeypatch.setattr(capacity, "_client", FakeRedis())
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            return _fake_proc(-9, stderr="Killed")
        payload = json.dumps(
            {"type": "exec.result", "status": "succeeded", "output": '{"verdicts": []}'}
        )
        return _fake_proc(0, stdout=payload)

    monkeypatch.setattr(subprocess, "run", _run)

    ok, _content, _details = mx.run_classification(_area(tmp_path), system="S", user="U", task="T")

    assert ok is True
    assert len(calls) == 2, "повтор ровно один"


def test_engine_keeps_v8_heap_refusal_as_engine_oom(tmp_path, monkeypatch):
    """Отказ V8 по своей куче — это про движок, а не про бокс.

    Различать надо: ``engine_oom`` лечится потолком кучи, ``oom_killed`` — тем,
    что бокс не дал памяти. Смешались бы — и лечение, и счёт в журнале.
    """
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    monkeypatch.setattr(capacity, "_client", FakeRedis())
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _fake_proc(
            1, stderr="FATAL ERROR: Reached heap limit - JavaScript heap out of memory"
        ),
    )

    ok, _content, details = mx.run_classification(_area(tmp_path), system="S", user="U", task="T")

    assert ok is False
    assert details.get("reason") == "engine_oom"
    assert "killed_by_kernel" not in details


def test_engine_releases_slot_after_the_call(tmp_path, monkeypatch):
    """Слот отпускается сразу после вызова, а не по TTL."""
    monkeypatch.setenv(capacity.ENV_ENABLED, "1")
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    fake = FakeRedis()
    monkeypatch.setattr(capacity, "_client", fake)
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)
    payload = json.dumps(
        {"type": "exec.result", "status": "succeeded", "output": '{"verdicts": []}'}
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _fake_proc(0, stdout=payload),
    )

    mx.run_classification(_area(tmp_path), system="S", user="U", task="T")

    assert not fake.keys, "слот должен быть отпущен"
