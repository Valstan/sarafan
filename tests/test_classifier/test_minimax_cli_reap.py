"""Уборка осиротевших процессов MiniMax CLI ([P240]).

Модуль снимает только процессы ``minimax-code`` с ``PPid`` = 1 — то есть те, чей
родитель-воркер умер. Живой CLI (родитель — fork-ребёнок воркера) не трогаем.
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import List

from modules import minimax_cli_reap as reap


def _make_proc(root: Path, pid: int, comm: str, ppid: int, state: str = "S (sleeping)") -> Path:
    """Собрать поддельный каталог процесса в ``/proc``-подобном дереве."""
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")
    (entry / "status").write_text(
        "Name:\t%s\nState:\t%s\nPPid:\t%d\nThreads:\t1\n" % (comm, state, ppid),
        encoding="utf-8",
    )
    return entry


def test_orphan_cli_is_found(tmp_path: Path) -> None:
    _make_proc(tmp_path, 101, "minimax-code", 1)
    assert reap.find_orphan_cli_pids(tmp_path) == [101]


def test_live_cli_is_not_touched(tmp_path: Path) -> None:
    """CLI, чей родитель — живой fork-ребёнок воркера, не осиротевший."""
    _make_proc(tmp_path, 102, "minimax-code", 789838)
    assert reap.find_orphan_cli_pids(tmp_path) == []


def test_other_processes_are_ignored(tmp_path: Path) -> None:
    _make_proc(tmp_path, 103, "celery", 1)
    _make_proc(tmp_path, 104, "postgres", 1)
    assert reap.find_orphan_cli_pids(tmp_path) == []


def test_zombie_is_skipped(tmp_path: Path) -> None:
    """Зомби уже мёртв и ждёт reap'а — убивать нечего."""
    _make_proc(tmp_path, 105, "minimax-code", 1, state="Z (zombie)")
    assert reap.find_orphan_cli_pids(tmp_path) == []


def test_non_numeric_entries_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "self").mkdir()
    (tmp_path / "meminfo").write_text("MemAvailable: 1 kB\n", encoding="utf-8")
    _make_proc(tmp_path, 106, "minimax-code", 1)
    assert reap.find_orphan_cli_pids(tmp_path) == [106]


def test_missing_proc_root_is_not_an_error(tmp_path: Path) -> None:
    assert reap.find_orphan_cli_pids(tmp_path / "нет-такого") == []


def test_reap_kills_with_sigkill(tmp_path: Path) -> None:
    """SIGTERM CLI перехватывает — значит убивать нужно SIGKILL (на Linux)."""
    _make_proc(tmp_path, 201, "minimax-code", 1)
    _make_proc(tmp_path, 202, "minimax-code", 555)
    killed: list[tuple[int, int]] = []

    report = reap.reap_orphan_cli(tmp_path, kill=lambda pid, sig: killed.append((pid, sig)))

    assert report["found"] == 1
    assert report["killed"] == [201]
    assert killed == [(201, reap.KILL_SIGNAL)]
    if hasattr(signal, "SIGKILL"):
        assert reap.KILL_SIGNAL == signal.SIGKILL


def test_reap_dry_run_kills_nothing(tmp_path: Path) -> None:
    _make_proc(tmp_path, 301, "minimax-code", 1)
    killed: list[tuple[int, int]] = []

    report = reap.reap_orphan_cli(
        tmp_path, kill=lambda pid, sig: killed.append((pid, sig)), dry_run=True
    )

    assert report["found"] == 1
    assert report["candidates"] == [301]
    assert report["killed"] == []
    assert killed == []


def test_reap_survives_dead_process(tmp_path: Path) -> None:
    """Процесс умер между поиском и убийством — это не ошибка."""
    _make_proc(tmp_path, 401, "minimax-code", 1)

    def _kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    report = reap.reap_orphan_cli(tmp_path, kill=_kill)

    assert report["found"] == 1
    assert report["killed"] == []
    assert report["errors"] == []


def test_reap_reports_permission_error(tmp_path: Path) -> None:
    _make_proc(tmp_path, 501, "minimax-code", 1)

    def _kill(pid: int, sig: int) -> None:
        raise PermissionError("denied")

    report = reap.reap_orphan_cli(tmp_path, kill=_kill)

    assert report["killed"] == []
    assert report["errors"] == ["501: denied"]


def test_two_orphans_are_sorted(tmp_path: Path) -> None:
    _make_proc(tmp_path, 900, "minimax-code", 1)
    _make_proc(tmp_path, 100, "minimax-code", 1)
    assert reap.find_orphan_cli_pids(tmp_path) == [100, 900]


def test_reaper_runs_even_when_slot_refuses(monkeypatch, tmp_path: Path) -> None:
    """Уборка обязана выполняться и когда слот отказал.

    Живой замер 25.11 15:34: уборщик стоял после проверки слота, а на голодном боксе
    слот отказывает — то есть уборка не выполнялась ровно тогда, когда сироты и вредны
    (сирота дождалась ядерного убийства при 36 платных отказах подряд).
    """
    from modules import minimax_engine as mx
    from modules.minimax_capacity import Lease

    account = mx.MinimaxAccount(name="work", root=tmp_path)
    reaped: List[int] = []

    def _reap(*_args: object, **_kwargs: object) -> dict:
        reaped.append(1)
        return {"found": 0, "candidates": [], "killed": [], "errors": []}

    monkeypatch.setattr(mx.minimax_cli_reap, "reap_orphan_cli", _reap)
    monkeypatch.setattr(
        mx.minimax_capacity,
        "lease",
        lambda **_kwargs: Lease(ok=False, reason="insufficient_memory", detail="нет памяти"),
    )
    monkeypatch.setattr(mx.shutil, "which", lambda _name: "/usr/bin/mcode")

    ok, _text, details = mx.run_classification(account, system="s", user="u")

    assert ok is False
    assert details["reason"] == "insufficient_memory"
    assert reaped, "уборка сирот обязана выполняться и когда слот отказал"
