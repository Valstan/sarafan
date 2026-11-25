#!/usr/bin/env python3
"""Ночная проверка среза discovery по критериям из TICKLER.

**Зачем скрипт.** Ночной срез 04:00 — единственное событие, где проверять приходится
**после** того, как оно произошло, и где легко испортить вывод двумя способами:

1. **Счётчик ядерных убийств нельзя читать из `dmesg` спустя часы** — буфер очищается,
   и «35» через несколько часов выглядит как суточный счётчик, а на деле это окно
   в 28 минут (замер 25.11). Скрипт печатает окно убийств вместе с числом, чтобы
   это было видно.
2. **Критерии разбросаны по четырём источникам** (журнал расхода MiniMax, журнал
   расхода нейросетей, лог воркера, `dmesg`) — их нужно свести к одному вердикту.

Скрипт **читает только** (логи, `dmesg`, длины очередей Redis) и ничего не публикует,
не перезапускает и не меняет.

Запуск на прод-боксе (из корня репозитория):

    cd ~/SETKA && ./venv/bin/python scripts/night_check.py

Код возврата: **0** — все проверки зелёные, **1** — есть провалы, **2** — данных
не хватило, чтобы судить.

Флаги:
  --date YYYY-MM-DD   дата ночи (по умолчанию — вчера: ночь начинается в 04:00)
  --logs DIR          каталог логов (по умолчанию ``logs``)
  --no-redis          не опрашивать Redis (очереди)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Срез стартует в 04:00 по Москве.
SLICE_START_HOUR_MSK = 4
#: Время работы среза в коде — ``time_limit=3 * 3600``.
SLICE_TIME_LIMIT_S = 3 * 3600
#: Ожидаемое число вызовов на ночь при ``DISCOVERY_RECHECK_SLICES=14``.
EXPECTED_DISCOVERY_CALLS = 190

MSK = timezone(timedelta(hours=3))
UTC = timezone.utc

RECHECK_TASK = "recheck_all_active_regions"
#: Строка результата задачи в логе воркера: ``Task ... succeeded in 123s: {…}``.
TASK_RESULT_RE = re.compile(
    r"Task tasks\.discovery_tasks\.%s\[[^\]]+\] succeeded in (?P<secs>[\d.]+)s: (?P<payload>.*)$"
    % RECHECK_TASK
)
OOM_RE = re.compile(r"OOM killed process \d+ \(minimax-code\)")


class Verdict:
    """Итог одной проверки: OK / FAIL / NA."""

    OK = "OK"
    FAIL = "FAIL"
    NA = "N/A"

    def __init__(self, name: str, status: str, detail: str = "") -> None:
        self.name = name
        self.status = status
        self.detail = detail


def parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def window_utc(night: date) -> Tuple[datetime, datetime]:
    """Окно ночи в UTC: 04:00 МСК дня ``night`` → 08:00 МСК следующего дня."""
    start = datetime(
        night.year, night.month, night.day, SLICE_START_HOUR_MSK, 0, 0, tzinfo=MSK
    ).astimezone(UTC)
    return start, start + timedelta(seconds=SLICE_TIME_LIMIT_S)


def parse_task_result(payload: str) -> Dict[str, Any]:
    """Разобрать тело результата задачи — это Python-литерал, а не JSON."""
    import ast

    try:
        value = ast.literal_eval(payload.strip())
    except (ValueError, SyntaxError):
        return {}
    return value if isinstance(value, dict) else {}


def check_discovery_calls(rows: List[Dict[str, Any]], start: datetime, end: datetime) -> List[Verdict]:
    """Вызовы движка меткой ``discovery`` за окно ночи."""
    calls = []
    for row in rows:
        if row.get("label") != "discovery":
            continue
        ts = parse_iso(str(row.get("ts") or ""))
        if ts is None or not (start <= ts < end):
            continue
        calls.append(row)
    if not calls:
        return [Verdict("срез discovery: вызовы", Verdict.NA, "в окне ночи вызовов нет")]

    ok = sum(1 for row in calls if row.get("ok"))
    reasons = Counter(str(row.get("reason") or "?") for row in calls if not row.get("ok"))
    stamps = sorted(parse_iso(str(r.get("ts"))) for r in calls if parse_iso(str(r.get("ts"))))
    span = int((stamps[-1] - stamps[0]).total_seconds()) if len(stamps) > 1 else 0

    out = [
        Verdict(
            "срез discovery: число вызовов",
            Verdict.OK if ok * 2 >= len(calls) else Verdict.FAIL,
            "%d вызовов, успешных %d, неуспешных %d %s"
            % (len(calls), ok, len(calls) - ok, dict(reasons) if reasons else ""),
        ),
        Verdict(
            "срез discovery: ожидаемо ~%d вызовов" % EXPECTED_DISCOVERY_CALLS,
            Verdict.OK if len(calls) >= EXPECTED_DISCOVERY_CALLS // 2 else Verdict.FAIL,
            "факт %d, окно между вызовами %d с" % (len(calls), span),
        ),
    ]
    return out


def check_task_report(log_text: str) -> List[Verdict]:
    """Отчёт среза: ``tokens_measured``, ``engines`` и время задачи против ``time_limit``."""
    matches = list(TASK_RESULT_RE.finditer(log_text))
    if not matches:
        killed = ("TimeLimitExceeded" in log_text) or ("SoftTimeLimitExceeded" in log_text)
        if killed:
            return [
                Verdict(
                    "срез discovery: уложился в %d ч" % (SLICE_TIME_LIMIT_S // 3600),
                    Verdict.FAIL,
                    "задача убита по time_limit — отчёта не будет",
                )
            ]
        return [Verdict("отчёт среза (tokens_measured / engines)", Verdict.NA, "строки результата нет")]
    last = matches[-1]
    elapsed = float(last.group("secs"))
    payload = parse_task_result(last.group("payload"))
    if not payload:
        return [Verdict("отчёт среза (tokens_measured / engines)", Verdict.NA, "результат не разобран")]

    measured = bool(payload.get("tokens_measured"))
    engines = payload.get("engines") or {}
    out = [
        Verdict(
            "срез discovery: уложился в %d ч" % (SLICE_TIME_LIMIT_S // 3600),
            Verdict.OK if elapsed <= SLICE_TIME_LIMIT_S else Verdict.FAIL,
            "задача шла %.0f с" % elapsed,
        ),
        Verdict(
            "отчёт среза: tokens_measured",
            Verdict.OK if measured else Verdict.FAIL,
            "%s" % measured,
        ),
        Verdict(
            "отчёт среза: engines.minimax > 0",
            Verdict.OK if int(engines.get("minimax") or 0) > 0 else Verdict.FAIL,
            "engines=%s" % engines,
        ),
    ]
    return out


def check_paid_calls(rows: List[Dict[str, Any]], start: datetime) -> List[Verdict]:
    """Платные вызовы DeepSeek в окне ночи."""
    paid = []
    for row in rows:
        if row.get("engine") != "deepseek":
            continue
        ts = parse_iso(str(row.get("ts") or ""))
        if ts is not None and ts >= start:
            paid.append(row)
    if not paid:
        return [Verdict("платные вызовы DeepSeek после 04:00", Verdict.OK, "0")]
    labels = Counter(str(row.get("label") or "?") for row in paid)
    return [
        Verdict(
            "платные вызовы DeepSeek после 04:00",
            Verdict.FAIL,
            "%d %s" % (len(paid), dict(labels)),
        )
    ]


def count_log_markers(log_text: str, needles: Tuple[str, ...]) -> int:
    return sum(log_text.count(needle) for needle in needles)


def check_oom_kills() -> List[Verdict]:
    """Ядерные убийства CLI — окно печатаем обязательно (буфер ``dmesg`` чистится)."""
    try:
        out = subprocess.run(
            ["dmesg", "-T"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return [Verdict("ядерные убийства minimax-code", Verdict.NA, "dmesg недоступен")]
    lines = [line for line in out.splitlines() if OOM_RE.search(line)]
    if not lines:
        return [Verdict("ядерные убийства minimax-code", Verdict.OK, "0")]
    first = lines[0].split("]")[0].lstrip("[")
    last = lines[-1].split("]")[0].lstrip("[")
    return [
        Verdict(
            "ядерные убийства minimax-code",
            Verdict.FAIL,
            "%d (окно dmesg: %s … %s) — ⚠️ буфер чистится, считать в момент замера"
            % (len(lines), first, last),
        )
    ]


def check_queues(redis_url: Optional[str]) -> List[Verdict]:
    """Длины очередей — растёт ли ``celery``."""
    queues = ("celery", "wave", "digest")
    lengths: Dict[str, int] = {}
    for queue in queues:
        try:
            res = subprocess.run(
                ["redis-cli", "LLEN", queue], capture_output=True, text=True, timeout=10
            )
            value = res.stdout.strip()
            lengths[queue] = int(value) if value.isdigit() else -1
        except (OSError, subprocess.SubprocessError, ValueError):
            lengths[queue] = -1
    if all(v < 0 for v in lengths.values()):
        return [Verdict("очереди", Verdict.NA, "redis недоступен")]
    detail = ", ".join("%s=%s" % (q, lengths[q]) for q in queues)
    celery = lengths.get("celery", -1)
    return [
        Verdict(
            "очереди после ночи",
            Verdict.OK if celery in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10) else Verdict.FAIL,
            detail,
        )
    ]


def check_worker_markers(log_text: str) -> List[Verdict]:
    """Маркеры отказов в логе воркера."""
    exec_failed = count_log_markers(log_text, ("не ответил (exec_failed)",))
    slot = count_log_markers(log_text, ("cli_slot_busy", "insufficient_memory"))
    cgroup = count_log_markers(log_text, ("Memory cgroup out of memory",))
    return [
        Verdict(
            "отказы движка в логе воркера (exec_failed)",
            Verdict.OK if exec_failed == 0 else Verdict.FAIL,
            "%d" % exec_failed,
        ),
        Verdict(
            "отказы регулятора (cli_slot_busy / insufficient_memory)",
            Verdict.OK if slot == 0 else Verdict.FAIL,
            "%d" % slot,
        ),
        Verdict(
            "cgroup OOM воркеров",
            Verdict.OK if cgroup == 0 else Verdict.FAIL,
            "%d" % cgroup,
        ),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Ночная проверка среза discovery")
    ap.add_argument("--date", default="", help="дата ночи (YYYY-MM-DD); по умолчанию — вчера")
    ap.add_argument("--logs", default="logs", help="каталог логов")
    ap.add_argument("--no-redis", action="store_true", help="не опрашивать Redis")
    args = ap.parse_args()

    if args.date:
        try:
            night = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print("не разобрана дата: %s" % args.date, file=sys.stderr)
            return 2
    else:
        night = date.today() - timedelta(days=1)

    logs = Path(args.logs)
    start, end = window_utc(night)

    print("=== ночная проверка среза discovery ===")
    print("ночь: %s, окно вызовов %s … %s (UTC)" % (night, start.isoformat(), end.isoformat()))
    print("замер сделан: %s (МСК) — счётчик dmesg считается сейчас, не задним числом" % datetime.now(MSK).strftime("%Y-%m-%d %H:%M"))
    print()

    verdicts: List[Verdict] = []
    verdicts += check_discovery_calls(read_jsonl(logs / "minimax_usage.jsonl"), start, end)
    verdicts += check_paid_calls(read_jsonl(logs / "neural_usage.jsonl"), start)

    worker_log = logs / "celery-worker.log"
    text = ""
    if worker_log.is_file():
        with worker_log.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    verdicts += check_task_report(text)
    verdicts += check_worker_markers(text)
    verdicts += check_oom_kills()
    if not args.no_redis:
        verdicts += check_queues(os.getenv("REDIS_URL"))

    width = max(len(v.name) for v in verdicts)
    for v in verdicts:
        print("[%s] %-*s — %s" % (v.status, width, v.name, v.detail))

    failed = [v for v in verdicts if v.status == Verdict.FAIL]
    unknown = [v for v in verdicts if v.status == Verdict.NA]
    print()
    print("итог: %d проверок, провалов %d, без данных %d" % (len(verdicts), len(failed), len(unknown)))
    if failed:
        return 1
    if unknown:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
