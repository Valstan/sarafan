"""Сторож «MiniMax молчит — трафик идёт в платный Дипсик».

Инцидент 19–21.11: два суток весь трафик шёл на платный Дипсик, и **никто этого
не заметил** — продукт публиковался (страховка), сторож сводок молчал. Здесь
проверяется главное: сторож **не шумит** на здоровом движке (аккаунты с нулевым
балансом дают отказ и успех следом) и **кричит**, когда пошли деньги.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from modules import minimax_watchdog as mw

NOW = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)


def _row(minutes_ago: int, *, engine: str, ok: bool, reason: str = "") -> dict:
    ts = NOW - timedelta(minutes=minutes_ago)
    return {
        "ts": ts.isoformat(),
        "engine": engine,
        "label": "headless",
        "ok": ok,
        "reason": reason,
    }


def test_healthy_window_does_not_alert():
    """Здоровый движок: успехи есть, платных вызовов единицы."""
    rows = [_row(i, engine="minimax", ok=True) for i in range(20)]
    rows += [_row(i, engine="deepseek", ok=True) for i in range(3, 8, 2)]

    stats = mw.summarise_window(rows, now=NOW)

    assert stats["attempts"] == 20
    assert stats["ok"] == 20
    assert stats["paid"] == 3
    assert mw.should_alert(stats) is False


def test_outage_with_paid_flood_alerts():
    """MiniMax молчит, платный Дипсик работает — это и есть инцидент."""
    rows = [_row(i, engine="minimax", ok=False, reason="exec_failed") for i in range(30)]
    rows += [_row(i, engine="deepseek", ok=True) for i in range(30)]

    stats = mw.summarise_window(rows, now=NOW)

    assert stats["ok"] == 0
    assert stats["fail"] == 30
    assert stats["paid"] == 30
    assert stats["reasons"].get("exec_failed") == 30
    assert mw.should_alert(stats) is True


def test_quota_rotations_with_successes_do_not_alert():
    """Отказ аккаунта с нулевым балансом + успех следом — движок жив.

    Это главный ложный-сигнал: в здоровые дни такие отказы есть каждый день
    (движок берёт следующий аккаунт и работает дальше). Тревога только на деньги.
    """
    rows = []
    for i in range(6):
        rows.append(_row(i, engine="minimax", ok=False, reason="quota_exhausted"))
        rows.append(_row(i, engine="minimax", ok=True))
    rows += [_row(i, engine="deepseek", ok=True) for i in range(3)]

    stats = mw.summarise_window(rows, now=NOW)

    assert stats["fail"] == 6
    assert stats["ok"] == 6
    assert mw.should_alert(stats) is False


def test_silence_is_not_an_alarm():
    """Нет вызовов в окне — тревожиться не о чем (тишина ≠ поломка)."""
    assert mw.should_alert(mw.summarise_window([], now=NOW)) is False
    # И вызовы за пределами окна — тоже не в счёт.
    old = [_row(120, engine="minimax", ok=False, reason="exec_failed") for _ in range(30)]
    stats = mw.summarise_window(old, window_minutes=30, now=NOW)
    assert stats["attempts"] == 0
    assert mw.should_alert(stats) is False


def test_window_ignores_rows_outside_the_window():
    """В окно попадают только свежие строки."""
    rows = [_row(5, engine="minimax", ok=True) for _ in range(4)]
    rows += [_row(90, engine="deepseek", ok=True) for _ in range(40)]

    stats = mw.summarise_window(rows, window_minutes=30, now=NOW)

    assert stats["attempts"] == 4
    assert stats["paid"] == 0


def test_alert_text_carries_numbers_reasons_and_the_money_brake():
    """Алёрт должен сказать: сколько стоило, почему и что делать."""
    rows = [_row(i, engine="minimax", ok=False, reason="exec_failed") for i in range(12)]
    rows += [_row(i, engine="deepseek", ok=True) for i in range(12)]

    stats = mw.summarise_window(rows, now=NOW)
    text = mw.build_alert_text(stats)

    assert "12" in text
    assert "exec_failed" in text
    assert "CLASSIFIER_PAID_DAILY_CALLS" in text
    assert "du -sh" in text


def test_watchdog_is_registered_in_the_beat_schedule():
    """Сторож без слота в расписании не существует — молчание возвращается."""
    from tasks.celery_app import app

    entry = app.conf.beat_schedule.get("minimax-engine-watchdog")
    assert entry is not None
    assert entry["task"] == "tasks.celery_app.check_minimax_engine_heartbeat"
    assert entry["task"] in app.tasks
