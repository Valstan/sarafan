"""Единый журнал вызовов нейросетей: запись, чтение, сутки по Москве.

**Что здесь стережётся.**

1. Журнал — **одна строка на вызов**, и она не должна ронять вызов: деньги
   потрачены, вердикт нужен.
2. Сутки — **московские**. Именно тут и рождалась путаница на панели: день,
   начавшийся в 00:00 UTC, это 03:00 МСК, и «сегодня» уезжало на третьи
   сутки. Проверка на стыке — главный тест модуля.
3. Считать платные вызовы нужно по **тому же журналу**, что человек видит в
   панели: две копии одного числа разъезжаются молча.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from modules import neural_usage


def _write(path, rows):
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_record_round_trip(tmp_path, monkeypatch):
    """Запись → чтение: одна строка на вызов, поля на месте."""
    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))

    neural_usage.record(
        engine="deepseek",
        label="post_rewrite",
        model="deepseek-flash",
        ok=True,
        posts=3,
        duration_ms=1234,
        prompt_tokens=900,
        cache_hit=800,
        cache_miss=100,
        completion_tokens=120,
    )

    rows = neural_usage.read_records(days=1)
    assert len(rows) == 1
    row = rows[0]
    assert row["engine"] == "deepseek"
    assert row["label"] == "post_rewrite"
    assert row["ok"] is True
    assert row["posts"] == 3
    assert row["prompt_tokens"] == 900
    assert row["completion_tokens"] == 120
    assert row["day"] == neural_usage.msk_today()


def test_record_survives_unwritable_log(tmp_path, monkeypatch):
    """Журнал недоступен — вызов не должен падать."""
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(tmp_path / "нет-такого-каталога" / "x" / "y.jsonl"))
    # Каталог создаётся, поэтому ломаем запись иначе: путь — это файл.
    blocker = tmp_path / "файл"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(blocker / "neural.jsonl"))

    neural_usage.record(engine="deepseek", label="headless", ok=True)  # не должно бросить


def test_msk_day_boundary():
    """Стык суток — главная проверка: 23:30 МСК и 20:30 UTC — один день."""
    # 2026-11-16 20:30 UTC = 23:30 МСК → 16 ноября.
    assert neural_usage.msk_day(datetime(2026, 11, 16, 20, 30, tzinfo=timezone.utc)) == "2026-11-16"
    # 2026-11-16 21:00 UTC = 00:00 МСК 17 ноября → уже 17-е.
    assert neural_usage.msk_day(datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc)) == "2026-11-17"
    # Наивное время из БД считается UTC (конвенция проекта).
    assert neural_usage.msk_day(datetime(2026, 11, 16, 20, 30)) == "2026-11-16"


def test_read_records_filters_by_msk_day(tmp_path, monkeypatch):
    """В окно попадают только строки нужных московских суток."""
    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))
    now = datetime.now(timezone.utc)
    _write(
        log,
        [
            {"ts": now.isoformat(), "engine": "deepseek", "label": "headless", "ok": True},
            # 40 часов назад — по Москве это позавчера, в окно не попадает.
            {
                "ts": (now - timedelta(hours=40)).isoformat(),
                "engine": "deepseek",
                "label": "headless",
                "ok": True,
            },
        ],
    )

    rows = neural_usage.read_records(days=1)

    assert len(rows) == 1
    assert rows[0]["day"] == neural_usage.msk_today()


def test_read_records_skips_broken_lines(tmp_path, monkeypatch):
    """Оборванная строка (kill во время записи) не должна ронять чтение."""
    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))
    log.write_text(
        '{"ts": "%s", "engine": "deepseek", "label": "headless", "ok": true}\n'
        "не JSON\n"
        '{"ts": "%s", "engine": "minimax", "label": "headless", "ok": true}\n'
        % (
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
        encoding="utf-8",
    )

    rows = neural_usage.read_records(days=1)

    assert len(rows) == 2


def test_read_records_missing_file_is_empty(tmp_path, monkeypatch):
    """Журнала ещё нет — это не ошибка, а «движок ещё не звали»."""
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(tmp_path / "нет-такого.jsonl"))
    assert neural_usage.read_records(days=1) == []


def test_aggregate_by_day_splits_flow_and_engine(tmp_path, monkeypatch):
    """Сводка: сутки × поток × движок, с причинами отказов."""
    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))
    now = datetime.now(timezone.utc).isoformat()
    _write(
        log,
        [
            {"ts": now, "engine": "minimax", "label": "headless", "ok": True, "posts": 20},
            {"ts": now, "engine": "minimax", "label": "headless", "ok": False, "reason": "timeout"},
            {
                "ts": now,
                "engine": "deepseek",
                "label": "post_rewrite",
                "ok": True,
                "prompt_tokens": 800,
            },
        ],
    )

    rows = neural_usage.aggregate_by_day(neural_usage.read_records(days=1))

    assert len(rows) == 2
    minimax = [r for r in rows if r["engine"] == "minimax"][0]
    assert minimax["calls"] == 2
    assert minimax["ok"] == 1
    assert minimax["failed"] == 1
    assert minimax["posts"] == 20
    assert minimax["reasons"] == {"timeout": 1}
    assert minimax["flow"] == "Классификация постов"
    rewrite = [r for r in rows if r["engine"] == "deepseek"][0]
    assert rewrite["flow"] == "Переписывание постов"
    assert rewrite["prompt_tokens"] == 800


def test_count_paid_calls_counts_only_paid_headless(tmp_path, monkeypatch):
    """Платные вызовы считаются по журналу: только Дипсик и только классификация."""
    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))
    now = datetime.now(timezone.utc).isoformat()
    _write(
        log,
        [
            {"ts": now, "engine": "deepseek", "label": "headless", "ok": True},
            {"ts": now, "engine": "deepseek", "label": "headless", "ok": True},
            {"ts": now, "engine": "deepseek", "label": "post_rewrite", "ok": True},
            {"ts": now, "engine": "minimax", "label": "headless", "ok": True},
        ],
    )

    assert neural_usage.count_paid_calls(label="headless") == 2


def test_flow_and_engine_labels_are_human():
    """Названия потоков и движков — человеческие, а не идентификаторы."""
    assert neural_usage.flow_label("headless") == "Классификация постов"
    assert neural_usage.flow_label("post_rewrite") == "Переписывание постов"
    assert neural_usage.engine_label("deepseek") == "Дипсик (платно)"
    assert neural_usage.engine_label("minimax") == "Минимакс (очки)"
    # Неизвестное не выдумываем: показываем как есть.
    assert neural_usage.flow_label("что-то-новое") == "что-то-новое"


def test_deepseek_call_leaves_a_journal_row(tmp_path, monkeypatch):
    """Вызов Дипсика оставляет строку в журнале: поток, объём, исход, токены."""
    from modules import deepseek_client as dc

    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))

    dc._record_usage(
        label="post_rewrite",
        body={"model": "deepseek-flash"},
        response={
            "model": "deepseek-flash",
            "usage": {
                "prompt_tokens": 900,
                "prompt_cache_hit_tokens": 800,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens": 120,
            },
        },
        status=200,
        posts=2,
        duration_ms=1500,
    )

    rows = neural_usage.read_records(days=1)
    assert len(rows) == 1
    assert rows[0]["engine"] == "deepseek"
    assert rows[0]["label"] == "post_rewrite"
    assert rows[0]["ok"] is True
    assert rows[0]["posts"] == 2
    assert rows[0]["prompt_tokens"] == 900
    assert rows[0]["cache_hit"] == 800
    assert rows[0]["duration_ms"] == 1500


def test_deepseek_failure_is_recorded_as_failed(tmp_path, monkeypatch):
    """Отказ — тоже строка журнала: с причиной, а не молчание."""
    from modules import deepseek_client as dc

    log = tmp_path / "neural.jsonl"
    monkeypatch.setenv("NEURAL_USAGE_LOG", str(log))

    dc._record_usage(
        label="conveyor",
        body={"model": "deepseek-flash"},
        response={},
        status=402,
        posts=0,
        duration_ms=10,
        detail="Insufficient Balance",
    )

    rows = neural_usage.read_records(days=1)
    assert rows[0]["ok"] is False
    assert rows[0]["reason"] == "http_402"
