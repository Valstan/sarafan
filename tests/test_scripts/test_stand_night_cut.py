"""Срез боевой ночи стенда (`scripts/stand_night_cut.py`).

Скрипт сводит критерии приёмки P253 к одному вердикту; здесь — проверка разбора:
окно, разделение двух классов отбрасывания, счёт волн и сводки, платные вызовы.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import stand_night_cut as snc  # noqa: E402

MSK = timezone(timedelta(hours=3))


def _line(ts: str, text: str) -> str:
    return "[%s: INFO/MainProcess] %s" % (ts, text)


def test_parse_ts_reads_celery_timestamp() -> None:
    moment = snc.parse_ts("[2026-11-27 08:08:14,146: INFO/MainProcess] Task ... succeeded")
    assert moment == datetime(2026, 11, 27, 8, 8, 14, 146000, tzinfo=MSK)


def test_parse_ts_none_for_foreign_line() -> None:
    assert snc.parse_ts("wave@PC79 ready.") is None


def test_classify_task_splits_classes() -> None:
    assert snc.classify_task("tasks.parsing_scheduler_tasks.run_all_regions_theme") == "wave"
    assert snc.classify_task("tasks.parsing_scheduler_tasks.parse_and_publish_theme") == "wave"
    assert snc.classify_task("tasks.digest_tasks.publish_daily_digest_region") == "digest"
    assert snc.classify_task("tasks.celery_app.dispatch_ad_reposts") == "other"
    assert snc.classify_task("tasks.broadcast_tasks.dispatch_broadcasts") == "other"


def test_scan_lines_counts_p253_signature() -> None:
    lines = [
        _line(
            "2026-11-27 08:00:00,000",
            "run_coro: общий event-loop занят другим потоком "
            "(holder_thread=Timer-1 holder_stack=...)",
        ),
        _line("2026-11-27 08:00:01,000", "This event loop is already running"),
    ]
    stats = snc.scan_lines(lines)
    assert stats["p253_diagnostics"] == 2
    assert stats["p253_samples"]


def test_revoked_periodic_copy_is_not_a_publication_loss() -> None:
    """Копия периодической задачи, вытесненная свежей, — не потеря публикации.

    Регрессия на формулировку критерия: «0 “Discarding revoked task”» без
    разделения классов даёт ложную тревогу (на стенде такие отбрасывания
    приходят от ``dispatch_ad_reposts``/``dispatch_broadcasts`` штатно).
    """
    lines = [
        _line(
            "2026-11-27 08:08:14,146",
            "Discarding revoked task: tasks.celery_app.dispatch_ad_reposts[d9466db3]",
        ),
        _line(
            "2026-11-27 08:08:16,195",
            "Discarding revoked task: tasks.broadcast_tasks.dispatch_broadcasts[9ed1613b]",
        ),
    ]
    stats = snc.scan_lines(lines)
    assert stats["revoked_by_class"]["other"] == 2
    assert stats["revoked_by_class"]["wave"] == 0
    assert stats["revoked_by_class"]["digest"] == 0


def test_revoked_wave_task_is_a_publication_loss() -> None:
    lines = [
        _line(
            "2026-11-27 22:20:04,000",
            "Discarding revoked task: tasks.parsing_scheduler_tasks.run_all_regions_theme[abc]",
        ),
        _line(
            "2026-11-27 23:00:00,000",
            "Discarding revoked task: tasks.digest_tasks.publish_daily_digest_region[def]",
        ),
    ]
    stats = snc.scan_lines(lines)
    assert stats["revoked_by_class"]["wave"] == 1
    assert stats["revoked_by_class"]["digest"] == 1


def test_scan_lines_counts_waves_and_digest_publications() -> None:
    lines = [
        _line(
            "2026-11-27 21:40:00,000", "daily digest: веер — регионов=84, окно разведения=3000 с"
        ),
        _line(
            "2026-11-27 22:00:00,000",
            "daily digest: регион=malmyzh опубликовано=True пунктов=12 знаков=900",
        ),
        _line(
            "2026-11-27 22:01:00,000",
            "daily digest: регион=kiymy опубликовано=True пунктов=8 знаков=700",
        ),
        _line(
            "2026-11-27 22:00:05,000",
            "Task tasks.parsing_scheduler_tasks.run_all_regions_theme[aaa] succeeded in 12.0s: {}",
        ),
    ]
    stats = snc.scan_lines(lines)
    assert stats["digest_fanout_regions"] == 84
    assert stats["digest_published"] == 2
    assert stats["waves_succeeded"] == 1


def test_scan_lines_respects_window() -> None:
    lines = [
        _line("2026-11-27 20:00:00,000", "daily digest: регион=old опубликовано=True"),
        _line("2026-11-27 22:00:00,000", "daily digest: регион=new опубликовано=True"),
    ]
    since = datetime(2026, 11, 27, 21, 40, tzinfo=MSK)
    stats = snc.scan_lines(lines, since=since)
    assert stats["digest_published"] == 1
    assert stats["lines_total"] == 2
    assert stats["lines_in_window"] == 1


def test_default_since_is_last_digest_slot() -> None:
    # 08:50 МСК — слот 21:40 ещё не наступил, окно берётся за вчера.
    morning = datetime(2026, 11, 27, 8, 50, tzinfo=MSK)
    assert snc.default_since(morning) == datetime(2026, 11, 26, 21, 40, tzinfo=MSK)
    # 23:30 МСК — слот сегодняшний.
    evening = datetime(2026, 11, 27, 23, 30, tzinfo=MSK)
    assert snc.default_since(evening) == datetime(2026, 11, 27, 21, 40, tzinfo=MSK)


def test_paid_calls_counts_deepseek_by_label() -> None:
    rows = [
        {"ts": "2026-11-27T00:10:00+00:00", "engine": "deepseek", "label": "headless", "ok": True},
        {"ts": "2026-11-27T00:20:00+00:00", "engine": "deepseek", "label": "headless", "ok": True},
        {"ts": "2026-11-27T00:30:00+00:00", "engine": "minimax", "label": "headless", "ok": True},
        {
            "ts": "2026-11-27T00:40:00+00:00",
            "engine": "minimax",
            "label": "headless",
            "ok": False,
            "reason": "engine_failed",
        },
    ]
    paid = snc.paid_calls(rows)
    assert paid["paid_total"] == 2
    assert paid["deepseek_by_label"] == {"headless": 2}
    assert paid["failures"] == {"engine_failed": 1}


def test_engine_calls_counts_failures_by_reason() -> None:
    rows = [
        {"engine": "minimax", "label": "discovery", "ok": True},
        {"engine": "minimax", "label": "discovery", "ok": False, "reason": "exec_failed"},
        {"engine": "minimax", "label": "headless", "ok": True},
    ]
    stats = snc.engine_calls(rows, label="discovery")
    assert stats["ok"] == 1
    assert stats["failed"] == 1
    assert stats["reasons"] == {"exec_failed": 1}


def test_build_gates_split_revocation_classes() -> None:
    log_stats = {
        "p253_diagnostics": 0,
        "revoked_by_class": {"wave": 0, "digest": 0, "other": 2},
    }
    redis_stats = {"unacked": 13, "publication": 10}
    paid = {"paid_total": 0}
    gates = snc.build_gates(
        log_stats,
        redis_stats,
        paid,
        expect_waves=3,
        expect_regions=84,
        baseline={
            "ts": "2026-11-27T21:30:00+03:00",
            "unacked": 13,
            "publication": 10,
        },
    )
    by_id = {gate["id"]: gate for gate in gates}
    assert by_id["revoked_publication"]["ok"] is True
    assert by_id["revoked_publication"]["value"] == 0
    assert by_id["waves"]["ok"] is False
    assert by_id["digest"]["ok"] is False


def test_build_gates_unacked_without_base_is_not_judged() -> None:
    """Без базы гейт по хвосту не судится: хвост прошлой ночи лежит часами."""
    gates = snc.build_gates(
        {"revoked_by_class": {}},
        {"unacked": 13, "publication": 10},
        {"paid_total": 0},
        expect_waves=3,
        expect_regions=84,
    )
    unacked = {gate["id"]: gate for gate in gates}["unacked"]
    assert unacked["judged"] is False
    assert unacked["ok"] is False


def test_build_gates_unacked_growth_fails_against_base() -> None:
    gates = snc.build_gates(
        {"revoked_by_class": {}},
        {"unacked": 16, "publication": 4},
        {"paid_total": 0},
        expect_waves=3,
        expect_regions=84,
        baseline={
            "ts": "2026-11-27T21:30:00+03:00",
            "unacked": 13,
            "publication": 3,
        },
    )
    unacked = {gate["id"]: gate for gate in gates}["unacked"]
    assert unacked["judged"] is True
    assert unacked["ok"] is False
    assert unacked["expect"] == 3


def test_publication_tasks_count_ignores_periodic_tail() -> None:
    """Гейт по хвосту считает только волны и сводку.

    Периодика в `unacked` живёт минутами (задача в работе) и колышет весь
    хвост: 27.11 он гулял 10 → 13 → 10 → 14 за час. Считать гейтом весь хвост —
    ловить чехарду.
    """
    composition = {
        "tasks.parsing_scheduler_tasks.parse_and_publish_theme": 6,
        "tasks.digest_tasks.publish_daily_digest_region": 4,
        "tasks.celery_app.dispatch_ad_reposts": 3,
        "tasks.broadcast_tasks.dispatch_broadcasts": 2,
        "tasks.celery_app.scan_suggested_ads": 1,
    }
    assert snc.publication_tasks_count(composition) == 10


def test_load_baseline_ignores_stale_file(tmp_path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text('{"ts": "2026-11-20T21:30:00+03:00", "unacked": 5}', encoding="utf-8")
    since = datetime(2026, 11, 27, 21, 40, tzinfo=MSK)
    assert snc.load_baseline(path, since) is None


def test_load_baseline_reads_fresh_file(tmp_path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        '{"ts": "2026-11-27T21:30:00+03:00", "unacked": 13, "publication": 10}',
        encoding="utf-8",
    )
    since = datetime(2026, 11, 27, 21, 40, tzinfo=MSK)
    data = snc.load_baseline(path, since)
    assert data is not None
    assert data["unacked"] == 13
    assert data["publication"] == 10


def test_load_baseline_ignores_old_format(tmp_path) -> None:
    """База без счёта волн/сводки измеряла весь хвост — а он колышется."""
    path = tmp_path / "baseline.json"
    path.write_text('{"ts": "2026-11-27T21:30:00+03:00", "unacked": 13}', encoding="utf-8")
    since = datetime(2026, 11, 27, 21, 40, tzinfo=MSK)
    assert snc.load_baseline(path, since) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-27 21:40", datetime(2026, 11, 27, 21, 40, tzinfo=MSK)),
        ("2026-11-27T18:40:00+00:00", datetime(2026, 11, 27, 21, 40, tzinfo=MSK)),
    ],
)
def test_parse_iso_moment(value: str, expected: datetime) -> None:
    assert snc.parse_iso_moment(value) == expected
