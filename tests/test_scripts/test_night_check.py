"""Ночная проверка среза discovery (`scripts/night_check.py`).

Скрипт читает логи и сводит критерии TICKLER к одному вердикту; здесь — проверка
разбора: окно ночи, вызовы движка, отчёт задачи и платные вызовы.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import night_check as nc  # noqa: E402


def _row(ts: str, label: str = "discovery", ok: bool = True, engine: str = "minimax") -> dict:
    return {"ts": ts, "label": label, "ok": ok, "reason": "" if ok else "exec_failed", "engine": engine}


def test_window_starts_at_four_msk() -> None:
    start, end = nc.window_utc(date(2026, 11, 26))
    assert start == datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)  # 04:00 МСК = 01:00 UTC
    assert (end - start).total_seconds() == nc.SLICE_TIME_LIMIT_S


def test_discovery_calls_counted_only_in_window() -> None:
    start, end = nc.window_utc(date(2026, 11, 26))
    rows = [
        _row("2026-11-26T01:10:00+00:00"),  # внутри
        _row("2026-11-26T01:20:00+00:00", ok=False),
        _row("2026-11-26T00:30:00+00:00"),  # раньше окна
        _row("2026-11-26T05:00:00+00:00", label="headless"),  # не discovery
    ]
    verdicts = {v.name: v for v in nc.check_discovery_calls(rows, start, end)}
    total = verdicts["срез discovery: число вызовов"]
    assert total.status == nc.Verdict.OK
    assert "2 вызовов, успешных 1" in total.detail


def test_discovery_calls_report_na_when_absent() -> None:
    start, end = nc.window_utc(date(2026, 11, 26))
    verdicts = nc.check_discovery_calls([], start, end)
    assert verdicts[0].status == nc.Verdict.NA


def test_discovery_calls_reports_window() -> None:
    start, end = nc.window_utc(date(2026, 11, 26))
    rows = [_row("2026-11-26T01:00:00+00:00"), _row("2026-11-26T01:30:00+00:00")]
    verdicts = {v.name: v for v in nc.check_discovery_calls(rows, start, end)}
    assert "окно между вызовами 1800 с" in verdicts["срез discovery: ожидаемо ~190 вызовов"].detail


def test_task_report_time_limit_kill_is_fail() -> None:
    log = "Task tasks.discovery_tasks.recheck_all_active_regions[abc] raised SoftTimeLimitExceeded\n"
    verdicts = nc.check_task_report(log)
    assert verdicts[0].status == nc.Verdict.FAIL
    assert "time_limit" in verdicts[0].detail


def test_task_report_elapsed_over_limit_is_fail() -> None:
    log = (
        "Task tasks.discovery_tasks.recheck_all_active_regions[abc-123] succeeded in 12000.0s: "
        "{'tokens_measured': True, 'engines': {'minimax': 190}}\n"
    )
    verdicts = {v.name: v for v in nc.check_task_report(log)}
    assert verdicts["срез discovery: уложился в 3 ч"].status == nc.Verdict.FAIL


def test_task_report_elapsed_within_limit_is_ok() -> None:
    log = (
        "Task tasks.discovery_tasks.recheck_all_active_regions[abc-123] succeeded in 4210.5s: "
        "{'tokens_measured': True, 'engines': {'minimax': 190}}\n"
    )
    verdicts = {v.name: v for v in nc.check_task_report(log)}
    assert verdicts["срез discovery: уложился в 3 ч"].status == nc.Verdict.OK


def test_parse_task_result_reads_python_literal() -> None:
    got = nc.parse_task_result("{'tokens_measured': True, 'engines': {'minimax': 12}}")
    assert got["tokens_measured"] is True
    assert got["engines"]["minimax"] == 12


def test_task_report_from_worker_log() -> None:
    log = (
        "[2026-11-26 05:12:00,000: INFO/MainProcess] "
        "Task tasks.discovery_tasks.recheck_all_active_regions[abc-123] succeeded in 4210.5s: "
        "{'success': True, 'tokens_measured': True, 'engines': {'minimax': 190}}\n"
    )
    verdicts = {v.name: v for v in nc.check_task_report(log)}
    assert verdicts["отчёт среза: tokens_measured"].status == nc.Verdict.OK
    assert verdicts["отчёт среза: engines.minimax > 0"].status == nc.Verdict.OK


def test_task_report_missing_is_na() -> None:
    verdicts = nc.check_task_report("ничего нет")
    assert verdicts[0].status == nc.Verdict.NA


def test_paid_calls_are_fail() -> None:
    start, _ = nc.window_utc(date(2026, 11, 26))
    rows = [_row("2026-11-26T02:00:00+00:00", label="discovery", ok=True, engine="deepseek")]
    verdict = nc.check_paid_calls(rows, start)[0]
    assert verdict.status == nc.Verdict.FAIL
    assert "1" in verdict.detail


def test_no_paid_calls_is_ok() -> None:
    start, _ = nc.window_utc(date(2026, 11, 26))
    verdict = nc.check_paid_calls([], start)[0]
    assert verdict.status == nc.Verdict.OK


@pytest.mark.parametrize(
    "text,regulator,exec_failed",
    [
        ("minimax: вызов не запускаю — insufficient_memory", 1, 0),
        ("minimax: вызов не запускаю — cli_slot_busy", 1, 0),
        ("headless: MiniMax не ответил (exec_failed)", 0, 1),
        ("ничего", 0, 0),
    ],
)
def test_worker_markers_counted(text: str, regulator: int, exec_failed: int) -> None:
    verdicts = {v.name: v for v in nc.check_worker_markers(text)}
    assert int(verdicts["отказы регулятора (cli_slot_busy / insufficient_memory)"].detail) == regulator
    assert int(verdicts["отказы движка в логе воркера (exec_failed)"].detail) == exec_failed
