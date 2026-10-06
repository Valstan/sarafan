"""Чистка multiproc-каталога метрик при старте (monitoring/metrics.py).

Инвариант: в каталоге живут ТОЛЬКО файлы живых НАШИХ процессов. Всё
остальное — сироты, которые MultiProcessCollector честно агрегирует в
экспозицию: 2026-10-06 так держались 4 928 файлов с мая (в т.ч. до-фиксовые
серии ADR-0006), 7 МБ экспозиции и OOM-смерти prometheus и uvicorn.

Ловушка, ради которой здесь два условия вместо одного: файл мёртвого
процесса с суффиксом _353 переживает проверку «жив ли PID», если PID 353
тем временем заняла grafana (доказано на проде: файлы июня, kill -0
успешен, cmdline чужой). Поэтому второе условие — cmdline наш.
"""

from __future__ import annotations

import os

import pytest

from monitoring import metrics as m


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "prom"
    d.mkdir()
    return d


def _touch(d, name):
    (d / name).write_bytes(b"\x00" * 64)
    return str(d / name)


def test_dead_pid_file_is_removed(workdir):
    _touch(workdir, "counter_424242.db")
    out = m.prune_stale_multiproc_files(
        str(workdir), _pid_alive=lambda pid: False, _pid_cmdline=lambda pid: "", _own_pid=1
    )
    assert out == {"dir": str(workdir), "kept": 0, "removed": 1, "skipped": 0}
    assert list(workdir.iterdir()) == []


def test_live_own_process_file_is_kept(workdir):
    _touch(workdir, "histogram_777.db")
    out = m.prune_stale_multiproc_files(
        str(workdir),
        _pid_alive=lambda pid: True,
        _pid_cmdline=lambda pid: "/home/valstan/SETKA/venv/bin/python uvicorn main:app",
        _own_pid=1,
    )
    assert out["kept"] == 1 and out["removed"] == 0
    assert (workdir / "histogram_777.db").exists()


def test_live_foreign_process_file_is_removed_despite_alive_pid(workdir):
    """Регрессия 06.10: файлы июня с суффиксом _353, PID заняла grafana."""
    _touch(workdir, "counter_353.db")
    out = m.prune_stale_multiproc_files(
        str(workdir),
        _pid_alive=lambda pid: True,
        _pid_cmdline=lambda pid: "/usr/share/grafana/bin/grafana server",
        _own_pid=1,
    )
    assert out["removed"] == 1
    assert list(workdir.iterdir()) == []


def test_own_pid_is_never_touched_even_if_cmdline_check_fails(workdir):
    _touch(workdir, "counter_999.db")
    out = m.prune_stale_multiproc_files(
        str(workdir),
        _pid_alive=lambda pid: (_ for _ in ()).throw(RuntimeError("nope")),
        _pid_cmdline=lambda pid: "",
        _own_pid=999,
    )
    # _own_pid проверяется раньше любых колбэков — файл цел, skipped не растёт
    assert out == {"dir": str(workdir), "kept": 1, "removed": 0, "skipped": 0}
    assert (workdir / "counter_999.db").exists()


def test_unparseable_name_is_kept_conservatively(workdir):
    _touch(workdir, "weird-name.db")
    out = m.prune_stale_multiproc_files(
        str(workdir), _pid_alive=lambda pid: False, _pid_cmdline=lambda pid: "", _own_pid=1
    )
    assert out["skipped"] == 1 and out["removed"] == 0
    assert (workdir / "weird-name.db").exists()


def test_non_db_files_are_ignored(workdir):
    _touch(workdir, "README.txt")
    out = m.prune_stale_multiproc_files(
        str(workdir), _pid_alive=lambda pid: False, _pid_cmdline=lambda pid: "", _own_pid=1
    )
    assert out == {"dir": str(workdir), "kept": 0, "removed": 0, "skipped": 0}
    assert (workdir / "README.txt").exists()


def test_missing_directory_returns_empty_summary():
    out = m.prune_stale_multiproc_files(
        "/nonexistent-dir-xyz", _pid_alive=lambda pid: False, _own_pid=1
    )
    assert out == {"dir": "/nonexistent-dir-xyz", "kept": 0, "removed": 0, "skipped": 0}


def test_empty_directory_string_means_not_configured():
    out = m.prune_stale_multiproc_files("", _own_pid=1)
    assert out == {"dir": None, "kept": 0, "removed": 0, "skipped": 0}


def test_default_pid_helpers_behave():
    assert m._default_pid_alive(os.getpid()) is True
    assert m._default_pid_alive(2**30) is False
    # /proc есть только на Linux: без него cmdline честно пуст, а не выдуман
    own_cmdline = m._default_pid_cmdline(os.getpid())
    assert (own_cmdline != "") == os.path.isdir(f"/proc/{os.getpid()}")
    assert m._default_pid_cmdline(2**30) == ""
