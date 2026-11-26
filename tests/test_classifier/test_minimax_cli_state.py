"""Состояние CLI MiniMax: накопление → OOM → весь трафик в платный Дипсик.

Замер 22.11 (живой факт, две сутки отказа): рабочая область CLI разрослась до
**897 МБ** (31 112 файлов), вызов поднимал RSS до **554 МБ**, ядро снимало
процесс (``OOM killed process 683639 (minimax-code) … anon-rss:554124kB``), и
**каждый** вызов уходил в платный Дипсик: 945 отказов и 945 вызовов Дипсика
20.11, 953 и 954 — 21.11, ни одного успешного вызова MiniMax.

Здесь проверяется то, что чинит это: потолок состояния, сброс с сохранением
входа, повтор вызова после убийства по памяти, потолок кучи V8 и сохранение
**начала** stderr (у краша V8 смысл в начале, а хвост — только C++-стек).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from modules import minimax_engine as mx


def _area(tmp_path: Path, *, junk_bytes: int = 0) -> mx.MinimaxAccount:
    """Рабочая область с токен-стором и (по желанию) мусором в состоянии."""
    root = tmp_path / "work"
    auth = root / "data" / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"schemaVersion": 1, "records": {}}), encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    if junk_bytes:
        junk = root / "data" / "v2" / "sessions" / "s1"
        junk.mkdir(parents=True, exist_ok=True)
        (junk / "session.log").write_bytes(b"x" * junk_bytes)
    return mx.MinimaxAccount(name="work", root=root, email="a@example.com")


def test_state_over_limit_is_reset_and_login_is_kept(tmp_path, monkeypatch):
    """Состояние переросло — сбрасываем, но **вход** оставляем.

    Ключ записи токена выводится из пути (``token_store_key``), поэтому чистить
    можно только содержимое: снести ``auth/`` — значит потерять вход.
    """
    monkeypatch.setenv(mx.ENV_STATE_MAX_MB, "1")
    account = _area(tmp_path, junk_bytes=3 * 1024 * 1024)
    junk = account.data_dir / "v2" / "sessions" / "s1" / "session.log"
    auth = account.data_dir / "auth" / "prod" / "en" / "mcode-public" / "auth.json"

    mx._ensure_cli_state_bounded(account)

    assert not junk.exists(), "накопленное состояние должно быть сброшено"
    assert auth.exists(), "вход сносить нельзя — ключ токена выводится из пути"


def test_state_under_limit_is_kept(tmp_path, monkeypatch):
    """Не трогаем то, что ещё не выросло: лишний сброс — лишний форк CLI."""
    monkeypatch.setenv(mx.ENV_STATE_MAX_MB, "64")
    account = _area(tmp_path, junk_bytes=1024)
    junk = account.data_dir / "v2" / "sessions" / "s1" / "session.log"

    mx._ensure_cli_state_bounded(account)

    assert junk.exists()


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    class _Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    return _Proc()


def test_cli_killed_by_memory_is_retried_once_after_state_reset(tmp_path, monkeypatch):
    """Убит по памяти → сброс состояния и **один** повтор.

    Ротация аккаунта тут бессмысленна: виновато накопленное состояние, а не
    аккаунт, — другой аккаунт на том же боксе дал бы тот же отказ.
    """
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    account = _area(tmp_path, junk_bytes=0)
    calls = []

    # Свободная память машины не должна влиять на этот тест: он проверяет
    # «сброс состояния и ровно один повтор», а не запас RAM. На тесной машине
    # повтор после ядерного убийства не делается — и это правильно, но не
    # предмет теста (замер 26.11: полный прогон на Windows падал здесь, пока
    # в изоляции проходил — RAM машины гуляла вокруг порога 384 МБ).
    from modules import minimax_capacity as capacity

    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 664)

    def _run(cmd, **kwargs):
        calls.append(kwargs.get("env", {}).get("NODE_OPTIONS", ""))
        if len(calls) == 1:
            return _fake_proc(-9, stderr="Killed")
        payload = json.dumps(
            {"type": "exec.result", "status": "succeeded", "output": '{"verdicts": []}'}
        )
        return _fake_proc(0, stdout=payload)

    monkeypatch.setattr(subprocess, "run", _run)

    ok, content, details = mx.run_classification(account, system="S", user="U", task="T")

    assert ok is True, "после сброса состояния повтор должен удаться"
    assert len(calls) == 2, "повтор ровно один"
    assert "--max-old-space-size=" in calls[0], "куча V8 должна быть ограничена"


def test_memory_failure_is_classified_as_engine_oom():
    """Куча V8 / ядро — свой отказ, а не «кончились очки» и не «протух вход»."""
    assert mx._classify_failure("FATAL ERROR: Reached heap limit Allocation failed") == "engine_oom"
    assert mx._classify_failure("JavaScript heap out of memory") == "engine_oom"
    # Очки и вход разбираются раньше — их маркеры не должны потеряться.
    assert mx._classify_failure("insufficient balance") == "quota_exhausted"
    assert mx._classify_failure("Sign in to MiniMax") == "auth_expired"


def test_stderr_head_is_kept_in_details(tmp_path, monkeypatch):
    """Начало stderr — в детали: у краша V8 смысл там, а в хвосте C++-стек.

    Именно поэтому отказ 22.11 не читался: движок хранил только последние
    400 символов, а диагноз («heap out of memory») — в первых.
    """
    monkeypatch.setenv(mx.ENV_CLI, sys.executable)
    account = _area(tmp_path)
    head = "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory"
    tail = "0x7f88 v8::internal::Heap::CollectGarbage(...) [minimax-code]"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: _fake_proc(1, stderr=head + "\n" + tail),
    )

    ok, _content, details = mx.run_classification(account, system="S", user="U", task="T")

    assert ok is False
    assert details.get("stderr_head", "").startswith("FATAL ERROR")
    assert "heap out of memory" in details.get("stderr_head", "")
