#!/usr/bin/env python3
"""Срез боевой ночи локального стенда — один отчёт по критериям приёмки P253.

**Зачем скрипт.** Критерии приёмки ночи раскиданы по четырём источникам: логи
двух воркеров (stderr celery), Redis (хвост ``unacked`` и очереди), журналы
расхода (``neural_usage.jsonl`` — платные вызовы, ``minimax_usage.jsonl`` — вызовы
движка). Собрать их в один вердикт нужно **до** того, как доказательства пропадут:
логи юнитов обнуляются на каждом рестарте (P257), а результат задачи в celery живёт
час (``result_expires = 3600``). Поэтому скрипт ещё и **снимает снимок логов** —
чтобы ночной срез пережил рестарт.

**Что скрипт чинит в критерии.** Формулировка «0 “Discarding revoked task”»
(будильник, 28.11) не различает два разных класса отбрасывания, и на стенде
второй класс — рутина:

* **отбрасывание волны или сводки** — потеря публикации, это и есть признак
  возврата P253 (задача истекла по сроку, пока лежала в хвосте);
* **отбрасывание копии периодической задачи** (``dispatch_ad_reposts``,
  ``dispatch_broadcasts``) — штатное вытеснение более свежей копией: брокер
  отзывает старую, новая отрабатывает. Публикации это не касается.

Поэтому «0 отбрасываний» берётся **по классам**: гейт — только волны и сводка,
остальное показывается отдельной строкой, чтобы ночной отчёт не пугал отбросом,
который ничего не сломал.

**Скрипт только читает.** Ничего не публикует, не перезапускает и не меняет;
единственная запись — снимок логов и JSON-отчёт на диск.

Запуск (из корня репозитория, на боевой машине):

    .\\venv\\Scripts\\python.exe scripts\\stand_night_cut.py

Окно по умолчанию — от слота сводки (21:40 МСК) последнего сегодняшнего дня до
сейчас: ночью это окно ночи, днём — окно вчерашней ночи. Точный интервал задаётся
``--since``/``--until`` (время московское).

Код возврата: **0** — все гейты зелёные, **1** — есть провалы, **2** — данных не
хватило, чтобы судить (логов нет или окно пустое).

Канон: P253 (общий event-loop), P254 (читать живые логи), P257 (логи обнуляются
на рестарте), ADR-0022 (публикационные слоты в очереди ``wave``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

MSK = timezone(timedelta(hours=3))

#: Живые логи воркеров: celery пишет в stderr, stdout-файлы — пустышка (P254).
WORKER_LOGS = ("setka-worker.err.log", "setka-wave-worker.err.log")

#: Слот вечерней сводки — 21:40 по Москве.
DIGEST_SLOT = (21, 40)

#: Задачи волн и сводки: их отбрасывание означает потерю публикации.
WAVE_TASKS = frozenset({"run_all_regions_theme", "parse_and_publish_theme"})
DIGEST_TASKS = frozenset(
    {"publish_daily_digest", "publish_daily_digest_fanout", "publish_daily_digest_region"}
)

#: Признаки коллизии общего event-loop (диагностика P253).
P253_SIGNATURES = (
    "общий event-loop занят другим потоком",
    "This event loop is already running",
)

#: Очереди стенда.
QUEUES = ("celery", "wave", "digest", "probe")

#: База хвоста считается свежей, если записана не раньше, чем за это время до
#: начала окна. База снимается ДО волн (окно начинается со слота 21:40), поэтому
#: «раньше окна» — норма, а не протухание; протухает база **прошлой** ночи.
BASELINE_MAX_AGE = timedelta(hours=6)

_TS_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")
_REVOKED_RE = re.compile(r"Discarding revoked task:\s*(?P<task>tasks\.[A-Za-z0-9_.]+)")
_SUCCEEDED_RE = re.compile(r"Task (?P<task>tasks\.[A-Za-z0-9_.]+)\[[^\]]+\] succeeded")
_PUBLISHED_RE = re.compile(r"daily digest: регион=\S+ опубликовано=True")
_FANOUT_RE = re.compile(r"daily digest: веер — регионов=(\d+)")


def parse_ts(line: str) -> Optional[datetime]:
    """Момент времени строки лога celery (``[2026-11-27 08:08:14,146:``) → МСК."""
    match = _TS_RE.match(line)
    if not match:
        return None
    try:
        naive = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None
    return naive.replace(tzinfo=MSK)


def classify_task(task: str) -> str:
    """Класс задачи: ``wave`` / ``digest`` / ``other``."""
    leaf = task.rsplit(".", 1)[-1]
    if leaf in WAVE_TASKS:
        return "wave"
    if leaf in DIGEST_TASKS:
        return "digest"
    return "other"


def in_window(ts: Optional[datetime], since: Optional[datetime], until: Optional[datetime]) -> bool:
    """Строка попадает в окно? Строка без времени считается попавшей (осторожно)."""
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def scan_lines(
    lines: Iterable[str],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Разбор логов воркеров: диагностика P253, отбрасывания, успехи задач, сводка."""
    out: Dict[str, Any] = {
        "lines_total": 0,
        "lines_in_window": 0,
        "lines_without_ts": 0,
        "p253_diagnostics": 0,
        "p253_samples": [],
        "revoked_by_class": {"wave": 0, "digest": 0, "other": 0},
        "revoked_other_tasks": Counter(),
        "revoked_samples": [],
        "succeeded_by_task": Counter(),
        "waves_succeeded": 0,
        "digest_published": 0,
        "digest_regions_seen": 0,
        "digest_fanout_regions": None,
    }
    for raw in lines:
        out["lines_total"] += 1
        ts = parse_ts(raw)
        if ts is None:
            out["lines_without_ts"] += 1
        if not in_window(ts, since, until):
            continue
        out["lines_in_window"] += 1

        if any(signature in raw for signature in P253_SIGNATURES):
            out["p253_diagnostics"] += 1
            if len(out["p253_samples"]) < 5:
                out["p253_samples"].append(raw.strip()[:300])

        revoked = _REVOKED_RE.search(raw)
        if revoked:
            task = revoked.group("task")
            klass = classify_task(task)
            out["revoked_by_class"][klass] += 1
            if klass == "other":
                out["revoked_other_tasks"][task] += 1
            if len(out["revoked_samples"]) < 8:
                out["revoked_samples"].append(raw.strip()[:220])

        succeeded = _SUCCEEDED_RE.search(raw)
        if succeeded:
            task = succeeded.group("task")
            out["succeeded_by_task"][task] += 1
            if task.rsplit(".", 1)[-1] == "run_all_regions_theme":
                out["waves_succeeded"] += 1

        if _PUBLISHED_RE.search(raw):
            out["digest_published"] += 1
        if "daily digest: регион=" in raw:
            out["digest_regions_seen"] += 1
        fanout = _FANOUT_RE.search(raw)
        if fanout:
            out["digest_fanout_regions"] = int(fanout.group(1))
    return out


def default_since(now: Optional[datetime] = None) -> datetime:
    """Начало окна: слот сводки последнего прошедшего дня (21:40 МСК)."""
    moment = now or datetime.now(MSK)
    slot = moment.replace(hour=DIGEST_SLOT[0], minute=DIGEST_SLOT[1], second=0, microsecond=0)
    if moment < slot:
        slot -= timedelta(days=1)
    return slot


def parse_iso_moment(value: str) -> datetime:
    """ISO-время → московское (naive трактуется как МСК)."""
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:  # noqa: BLE001
        raise argparse.ArgumentTypeError(f"не разобрано время: {value!r}") from exc
    if moment.tzinfo is None:
        return moment.replace(tzinfo=MSK)
    return moment.astimezone(MSK)


def journal_rows(
    path: Path,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Строки журнала расхода в окне (``ts`` приводится к МСК)."""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = record.get("ts")
        if not stamp:
            continue
        try:
            moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=MSK)
        else:
            moment = moment.astimezone(MSK)
        if since is not None and moment < since:
            continue
        if until is not None and moment > until:
            continue
        rows.append(record)
    return rows


def paid_calls(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Платные вызовы в окне: по движку и по метке."""
    by_engine: Counter = Counter()
    by_label: Counter = Counter()
    failures: Counter = Counter()
    for record in rows:
        engine = str(record.get("engine") or "?")
        label = str(record.get("label") or "?")
        by_engine[engine] += 1
        if engine == "deepseek":
            by_label[label] += 1
        if not record.get("ok", False):
            failures[str(record.get("reason") or "без причины")] += 1
    return {
        "by_engine": dict(by_engine),
        "deepseek_by_label": dict(by_label),
        "paid_total": by_label_total(by_label),
        "failures": dict(failures),
    }


def by_label_total(counter: Counter) -> int:
    return int(sum(counter.values()))


def engine_calls(rows: List[Dict[str, Any]], label: str = "") -> Dict[str, Any]:
    """Вызовы движка в окне: успехи и отказы по причинам."""
    ok = 0
    failed = 0
    reasons: Counter = Counter()
    engines: Counter = Counter()
    for record in rows:
        if label and str(record.get("label") or "") != label:
            continue
        engines[str(record.get("engine") or "?")] += 1
        if record.get("ok", False):
            ok += 1
        else:
            failed += 1
            reasons[str(record.get("reason") or "без причины")] += 1
    return {
        "ok": ok,
        "failed": failed,
        "reasons": dict(reasons),
        "engines": dict(engines),
    }


def redis_snapshot(broker_url: str) -> Dict[str, Any]:
    """Хвост ``unacked``, очереди и состав хвоста по задачам."""
    from kombu import Connection

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from restore_unacked import describe  # noqa: E402

    with Connection(broker_url) as conn:
        client = conn.default_channel.client
        raw = client.hgetall("unacked")
        queues = {name: int(client.llen(name)) for name in QUEUES}

    composition: Dict[str, int] = {}
    for payload in raw.values():
        try:
            task, _queue, _args, _headers = describe(payload)
        except Exception:  # noqa: BLE001 — битое сообщение не должно ронять отчёт
            task = "<не разобрано>"
        composition[task] = composition.get(task, 0) + 1
    return {
        "unacked": len(raw),
        "unacked_by_task": composition,
        "queues": queues,
    }


def publication_tasks_count(composition: Dict[str, int]) -> int:
    """Сколько в хвосте `unacked` сообщений **волн и сводки**.

    Хвост колышется: периодические задачи (`dispatch_ad_reposts`,
    `dispatch_broadcasts`, `scan_suggested_ads`) в нём **живут минутами** — они
    в работе, и через минуту будут подтверждены. 27.11 хвост гулял 10 → 13 →
    10 → 14 за час. Считать гейтом весь хвост — значит ловить эту чехарду и
    получать ложную тревогу; ловить надо **только то, что означает потерю
    публикации**, — задачи волн и сводки (как и с отбрасываниями).
    """
    total = 0
    for task, count in (composition or {}).items():
        if classify_task(task) in ("wave", "digest"):
            total += int(count)
    return total


def memory_free_mb() -> Optional[int]:
    """Сколько памяти свободно (МБ). ``None`` — не смогли измерить."""
    try:
        import psutil

        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:  # noqa: BLE001 — метрика не должна ронять отчёт
        return None


def load_baseline(path: Path, since: Optional[datetime]) -> Optional[Dict[str, Any]]:
    """База хвоста ``unacked``, записанная в этом же окне.

    База нужна, потому что «хвост пуст» как критерий приёмки не работает: хвост
    прошлой ночи лежит в ``unacked`` часами (задачи ждут возврата), и без базы
    гейт красный ещё до начала ночи. Сравнивать надо **не вырос ли** хвост.
    База протухшая (записана в прошлую ночь) — не берётся.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stamp = data.get("ts")
    if not stamp:
        return None
    if data.get("publication") is None:
        # База старого формата (без счёта волн/сводки) — она измеряла весь хвост,
        # а весь хвост колышется. Такая база не годится: считаем заново.
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=MSK)
    if since is not None and moment < since - BASELINE_MAX_AGE:
        return None
    return data


def save_baseline(path: Path, unacked: int, publication: int, now: datetime) -> None:
    """Записать базу хвоста ``unacked`` (файл в ``logs/`` переживает рестарт)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": now.isoformat(),
                "unacked": int(unacked),
                "publication": int(publication),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def broker_url() -> str:
    url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL") or ""
    if not url.startswith("redis"):
        url = "redis://127.0.0.1:6379/0"
    return url


def load_env_files() -> int:
    """Подхватить env-файл стенда (на Windows — единственный источник)."""
    if not os.environ.get("SETKA_ENV_FILES"):
        default = REPO_ROOT / "config" / "local-stand.env"
        if default.exists():
            os.environ["SETKA_ENV_FILES"] = str(default)
    try:
        from infra.windows.local_env import load_local_env
    except ImportError:
        return 0
    return load_local_env()


def build_gates(
    log_stats: Dict[str, Any],
    redis_stats: Dict[str, Any],
    paid: Dict[str, Any],
    *,
    expect_waves: int,
    expect_regions: int,
    baseline: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Гейты приёмки — те, что в будильнике, но с разделением классов."""
    revoked = log_stats.get("revoked_by_class") or {"wave": 0, "digest": 0, "other": 0}
    diagnostics = int(log_stats.get("p253_diagnostics") or 0)
    waves = int(log_stats.get("waves_succeeded") or 0)
    published = int(log_stats.get("digest_published") or 0)
    publication = int(redis_stats.get("publication", 0))
    base_publication = (baseline or {}).get("publication")
    if base_publication is None:
        unacked_gate = {
            "id": "unacked",
            "title": "в хвосте нет волн/сводки",
            "value": publication,
            "expect": 0,
            "ok": False,
            "judged": False,
            "note": (
                "база не записана в этом окне: запустите скрипт ДО волн, "
                "чтобы он записал базу, и судите по второму прогону"
            ),
        }
    else:
        unacked_gate = {
            "id": "unacked",
            "title": "в хвосте нет волн/сводки",
            "value": publication,
            "expect": int(base_publication),
            "ok": publication <= int(base_publication),
            "judged": True,
            "note": "не вырос относительно базы, записанной до волн",
        }
    return [
        {
            "id": "p253",
            "title": "диагностика общего event-loop",
            "value": diagnostics,
            "expect": 0,
            "ok": diagnostics == 0,
            "judged": True,
            "note": "коллизии P253 в логах воркеров",
        },
        {
            "id": "revoked_publication",
            "title": "отброшены волны или сводка",
            "value": revoked["wave"] + revoked["digest"],
            "expect": 0,
            "ok": (revoked["wave"] + revoked["digest"]) == 0,
            "judged": True,
            "note": (
                "потеря публикации; отбрасывание копий периодики (ad_reposts, "
                "broadcasts) считается отдельно и гейтом не является"
            ),
        },
        {
            "id": "waves",
            "title": "вечерние волны выполнены",
            "value": waves,
            "expect": expect_waves,
            "ok": waves >= expect_waves,
            "judged": True,
            "note": "run_all_regions_theme … succeeded",
        },
        {
            "id": "digest",
            "title": "сборники сводки опубликованы",
            "value": published,
            "expect": expect_regions,
            "ok": published >= expect_regions,
            "judged": True,
            "note": "daily digest: регион=… опубликовано=True",
        },
        unacked_gate,
        {
            "id": "paid",
            "title": "платных вызовов DeepSeek",
            "value": paid["paid_total"],
            "expect": 0,
            "ok": paid["paid_total"] == 0,
            "judged": True,
            "note": "окно ночи; движок должен отвечать сам",
        },
    ]


def render(report: Dict[str, Any]) -> str:
    """Человекочитаемый отчёт."""
    lines: List[str] = []
    window = report["window"]
    lines.append("=== Срез боевой ночи стенда ===")
    lines.append("окно: %s … %s (МСК)" % (window["since"], window["until"] or "сейчас"))
    lines.append("логи: %s" % ", ".join(window["logs"]))
    lines.append(
        "строк в окне: %d (всего %d, без времени %d)"
        % (
            report["logs"]["lines_in_window"],
            report["logs"]["lines_total"],
            report["logs"]["lines_without_ts"],
        )
    )
    lines.append("")
    lines.append("--- гейты ---")
    for gate in report["gates"]:
        mark = "OK " if gate["ok"] else "ПРОВАЛ"
        lines.append(
            "  [%s] %-38s %s (ожидалось %s) — %s"
            % (mark, gate["title"], gate["value"], gate["expect"], gate["note"])
        )
    if (report.get("baseline") or {}).get("created"):
        lines.append("")
        lines.append(
            ">>> БАЗОВЫЙ ПРОГОН: снята база хвоста (%s) и снимок логов."
            % report["baseline"].get("value")
        )
        lines.append(
            ">>> Окно широкое (с прошлого слота 21:40) — суд по второму прогону, " "после волн."
        )
    lines.append("")
    lines.append("--- подробности ---")
    lines.append(
        "отброшено: волна=%d сводка=%d прочее=%d"
        % (
            report["logs"]["revoked_by_class"]["wave"],
            report["logs"]["revoked_by_class"]["digest"],
            report["logs"]["revoked_by_class"]["other"],
        )
    )
    other = report["logs"]["revoked_other_tasks"]
    if other:
        lines.append(
            "прочее по задачам: "
            + ", ".join("%s=%d" % (task, count) for task, count in other.most_common(5))
        )
    if report["logs"]["digest_fanout_regions"] is not None:
        lines.append(
            "веер сводки: регионов=%d, опубликовано=%d, регионов в логе=%d"
            % (
                report["logs"]["digest_fanout_regions"],
                report["logs"]["digest_published"],
                report["logs"]["digest_regions_seen"],
            )
        )
    else:
        lines.append("веер сводки: строки нет — слот 21:40 не разложил регионы")
    redis_stats = report.get("redis") or {}
    if redis_stats:
        lines.append(
            "unacked: %d (волн/сводки: %d) | очереди: %s"
            % (
                redis_stats.get("unacked", -1),
                redis_stats.get("publication", 0),
                ", ".join(
                    "%s=%d" % (name, count)
                    for name, count in sorted((redis_stats.get("queues") or {}).items())
                ),
            )
        )
        composition = redis_stats.get("unacked_by_task") or {}
        if composition:
            lines.append(
                "хвост по задачам: "
                + ", ".join("%s=%d" % (task, count) for task, count in composition.items())
            )
    paid = report["paid"]
    lines.append(
        "платные вызовы: %d (по меткам: %s)"
        % (
            paid["paid_total"],
            ", ".join(f"{k}={v}" for k, v in paid["deepseek_by_label"].items()) or "—",
        )
    )
    engine = report.get("engine_discovery") or {}
    if engine:
        lines.append(
            "движок, метка discovery: успешно=%d отказов=%d %s"
            % (
                engine.get("ok", 0),
                engine.get("failed", 0),
                (
                    (
                        "причины: "
                        + ", ".join(f"{k}={v}" for k, v in engine.get("reasons", {}).items())
                    )
                    if engine.get("reasons")
                    else ""
                ),
            )
        )
    memory = report.get("memory_free_mb")
    if memory is not None:
        lines.append(
            "память свободно: %d МБ%s"
            % (memory, " (мало!)" if memory < report["thresholds"]["min_free_mb"] else "")
        )
    if report["logs"]["p253_samples"]:
        lines.append("--- образцы диагностики P253 ---")
        for sample in report["logs"]["p253_samples"]:
            lines.append("  %s" % sample)
    if report["logs"]["revoked_samples"]:
        lines.append("--- образцы отбрасываний ---")
        for sample in report["logs"]["revoked_samples"]:
            lines.append("  %s" % sample)
    lines.append("")
    lines.append("ИТОГ: %s" % ("ГОДЕН" if report["verdict"] == "pass" else report["verdict"]))
    return "\n".join(lines)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Срез боевой ночи локального стенда по критериям приёмки P253."
    )
    parser.add_argument("--since", default="", help="начало окна (МСК), по умолчанию — слот 21:40")
    parser.add_argument("--until", default="", help="конец окна (МСК), по умолчанию — сейчас")
    parser.add_argument("--logs", default=str(REPO_ROOT / "logs"), help="каталог логов")
    parser.add_argument("--expect-waves", type=int, default=3, help="сколько вечерних волн ждём")
    parser.add_argument(
        "--expect-regions", type=int, default=84, help="сколько сборников сводки ждём"
    )
    parser.add_argument(
        "--min-free-mb", type=int, default=1000, help="ниже этого — предупреждение о памяти"
    )
    parser.add_argument("--no-redis", action="store_true", help="не опрашивать Redis")
    parser.add_argument("--no-snapshot", action="store_true", help="не снимать снимок логов")
    parser.add_argument(
        "--baseline",
        default="",
        help="файл базы хвоста unacked (по умолчанию logs/night_cut_baseline.json)",
    )
    parser.add_argument("--out", default="", help="куда положить JSON-отчёт")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    load_env_files()

    now = datetime.now(MSK)
    since = parse_iso_moment(args.since) if args.since else default_since(now)
    until = parse_iso_moment(args.until) if args.until else None

    logs_dir = Path(args.logs)
    baseline_path = Path(args.baseline) if args.baseline else logs_dir / "night_cut_baseline.json"
    baseline = load_baseline(baseline_path, since)
    found: List[Path] = []
    for name in WORKER_LOGS:
        candidate = logs_dir / name
        if candidate.exists():
            found.append(candidate)

    report: Dict[str, Any] = {
        "generated_at": now.isoformat(),
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
            "logs": [str(path) for path in found],
        },
        "thresholds": {
            "expect_waves": args.expect_waves,
            "expect_regions": args.expect_regions,
            "min_free_mb": args.min_free_mb,
        },
    }

    if not found:
        report["logs"] = scan_lines([])
        report["redis"] = {}
        report["paid"] = {"by_engine": {}, "deepseek_by_label": {}, "paid_total": 0, "failures": {}}
        report["engine_discovery"] = {}
        report["memory_free_mb"] = memory_free_mb()
        report["gates"] = build_gates(
            report["logs"],
            report["redis"],
            report["paid"],
            expect_waves=args.expect_waves,
            expect_regions=args.expect_regions,
        )
        report["verdict"] = "нет данных (логи воркеров не найдены)"
        print(render(report))
        return 2
    # Снимок логов — до разбора: рестарт юнита обнулит их (P257), а доказательства
    # ночи нужны после рестарта.
    snapshot_dir: Optional[Path] = None
    if not args.no_snapshot:
        stamp = now.strftime("%Y%m%d-%H%M%S")
        snapshot_dir = logs_dir / ("night_cut_" + stamp)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for path in found:
            try:
                shutil.copy2(path, snapshot_dir / path.name)
            except OSError:
                pass
        report["window"]["snapshot"] = str(snapshot_dir)

    lines: List[str] = []
    for path in found:
        lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    report["logs"] = scan_lines(lines, since=since, until=until)

    redis_stats: Dict[str, Any] = {}
    if not args.no_redis:
        try:
            redis_stats = redis_snapshot(broker_url())
        except Exception as exc:  # noqa: BLE001 — отчёт без redis хуже, но не пустой
            redis_stats = {"error": "%s: %s" % (type(exc).__name__, exc)}
    report["redis"] = redis_stats
    if "unacked_by_task" in redis_stats:
        redis_stats["publication"] = publication_tasks_count(
            redis_stats.get("unacked_by_task") or {}
        )

    # База хвоста: без неё гейт «в хвосте нет волн/сводки» не судим (хвост
    # прошлой ночи лежит часами). База пишется один раз — до волн; суд — по
    # второму прогону.
    baseline_created = False
    if baseline is None and "unacked" in redis_stats:
        save_baseline(
            baseline_path,
            int(redis_stats["unacked"]),
            int(redis_stats.get("publication", 0)),
            now,
        )
        baseline_created = True
        baseline = {
            "ts": now.isoformat(),
            "unacked": int(redis_stats["unacked"]),
            "publication": int(redis_stats.get("publication", 0)),
        }
    report["baseline"] = {
        "path": str(baseline_path),
        "created": baseline_created,
        "value": (baseline or {}).get("publication"),
        "unacked_total": (baseline or {}).get("unacked"),
        "ts": (baseline or {}).get("ts"),
    }

    neural_rows = journal_rows(logs_dir / "neural_usage.jsonl", since=since, until=until)
    report["paid"] = paid_calls(neural_rows)

    minimax_rows = journal_rows(
        logs_dir / "minimax_usage.jsonl", since=since, until=until
    )
    report["engine_discovery"] = engine_calls(minimax_rows, label="discovery")
    report["memory_free_mb"] = memory_free_mb()

    report["gates"] = build_gates(
        report["logs"],
        report["redis"],
        report["paid"],
        expect_waves=args.expect_waves,
        expect_regions=args.expect_regions,
        baseline=baseline if not baseline_created else None,
    )
    failed = [gate for gate in report["gates"] if not gate["ok"]]
    unjudged = [gate for gate in report["gates"] if not gate.get("judged", True)]
    if report["logs"]["lines_in_window"] == 0:
        report["verdict"] = "нет данных (в окне нет строк логов)"
    elif unjudged:
        report["verdict"] = (
            "база хвоста записана (волн/сводки: %d, всего сообщений: %d) — суд по "
            "второму прогону, после волн"
            % (
                report["baseline"].get("value") or 0,
                report["baseline"].get("unacked_total") or 0,
            )
        )
    elif failed:
        report["verdict"] = "провалы: " + ", ".join(gate["id"] for gate in failed)
    else:
        report["verdict"] = "pass"

    out_path = (
        Path(args.out) if args.out else ((snapshot_dir / "report.json") if snapshot_dir else None)
    )
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        report["report_path"] = str(out_path)

    print(render(report))
    if report["verdict"] == "pass":
        return 0
    if report["verdict"].startswith("нет данных"):
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
