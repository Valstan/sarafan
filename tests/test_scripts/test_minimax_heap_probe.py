"""Unit-тесты для ``scripts/minimax_heap_probe.py`` — замер потолка кучи V8 (P239).

Скрипт — CLI-утилита вне пакета, грузим через importlib (как
``tests/test_scripts/test_minimax_renew_check.py``). На бокс здесь ничего не
ходит: проверяются только чистые части — разбор ``/proc``, обход дерева
процессов, разбор ``NODE_OPTIONS`` и свод замера.

Главное, что тут проверяется по сути: **«как сейчас» не должен выигрывать
минимум** (замер «как сейчас» — это дефолт кода 384 МБ, а не 0 МБ), иначе
вывод «минимальный безопасный потолок» был бы всегда 0 и говорил бы владельцу
ничего.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "minimax_heap_probe", REPO_ROOT / "scripts" / "minimax_heap_probe.py"
)
probe = importlib.util.module_from_spec(_spec)
sys.modules["minimax_heap_probe"] = probe
_spec.loader.exec_module(probe)


def _status(ppid: int, rss_kb: int) -> str:
    return "Name:\tnode\nPPid:\t%d\nVmRSS:\t%d kB\n" % (ppid, rss_kb)


def test_parse_proc_status_reads_ppid_and_rss():
    parsed = probe.parse_proc_status(_status(42, 2048))
    assert parsed == (42, 2048)


def test_parse_proc_status_ignores_foreign_text():
    assert probe.parse_proc_status("hello world") is None
    assert probe.parse_proc_status("") is None


def test_parse_proc_status_survives_broken_lines():
    """Битые значения — не процесс: возвращаем ``None``, а не исключение."""
    assert probe.parse_proc_status("PPid:\tне число\nVmRSS:\tтоже нет\n") is None


def test_descendant_pids_walks_the_whole_tree():
    # 10 — корень, 11 и 12 — его дети, 13 — внук.
    table = {
        10: (1, 100),
        11: (10, 200),
        12: (10, 300),
        13: (11, 400),
        20: (1, 500),
    }
    assert sorted(probe.descendant_pids(10, table)) == [11, 12, 13]


def test_descendant_pids_survives_cycles():
    table = {1: (2, 10), 2: (1, 20)}
    assert probe.descendant_pids(1, table) == [2]


def test_tree_memory_sums_and_finds_biggest():
    table = {
        10: (1, 100),
        11: (10, 250),
        12: (10, 400),
        20: (1, 900),
    }
    total, biggest = probe.tree_memory(table, 10)
    assert total == 250 + 400
    assert biggest == 400


def test_effective_heap_limit_takes_the_last_flag():
    """Node берёт последний ``--max-old-space-size`` — движок дописывает свой в конец."""
    options = "--max-old-space-size=320 --dns-result-order=ipv4first --max-old-space-size=96"
    assert probe.effective_heap_limit_mb(options) == 96


def test_effective_heap_limit_without_flag_is_unknown():
    assert probe.effective_heap_limit_mb("--dns-result-order=ipv4first") is None


def test_summarise_picks_smallest_successful_cap():
    rows: List[Dict[str, Any]] = [
        {"effective_cap_mb": 96, "ok": True},
        {"effective_cap_mb": 160, "ok": True},
        {"effective_cap_mb": 384, "ok": False},
    ]
    out = probe.summarise(rows)
    assert out["min_ok_cap_mb"] == 96
    assert out["ok_caps"] == [96, 160]
    assert out["failed_caps"] == [384]


def test_summarise_ignores_cap_zero_as_a_number():
    """Замер «как сейчас» — это дефолт кода (384 МБ), а не 0 МБ."""
    rows: List[Dict[str, Any]] = [
        {"effective_cap_mb": 384, "ok": True},
        {"effective_cap_mb": 96, "ok": False},
    ]
    out = probe.summarise(rows)
    assert out["min_ok_cap_mb"] == 384


def test_summarise_without_success_is_honest():
    rows: List[Dict[str, Any]] = [{"effective_cap_mb": 96, "ok": False}]
    out = probe.summarise(rows)
    assert out["min_ok_cap_mb"] is None
    assert out["failed_caps"] == [96]


def test_dry_run_reports_plan_without_calls():
    payload = probe.main(["--dry-run", "--caps", "96", "0"])
    assert payload == 0
