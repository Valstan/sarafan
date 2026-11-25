#!/usr/bin/env python3
"""Замер потолка кучи V8 для CLI MiniMax: какое значение ставить безопасно.

**Зачем этот скрипт.** 25.11 ночью бокс в 1536 МБ без свопа снимал CLI ядром:
96 kill'ов за ночь, ``dmesg`` — ``OOM killed process (minimax-code) anon-rss:214856kB…241804kB``.
V8-собственных сообщений «heap out of memory» — **ноль**: до своего потолка CLI
не доходил, его снимали снаружи. Значит вопрос «какой потолок кучи безопасен» —
не про V8, а про то, влезает ли процесс в свободную память бокса, и главное:
**не ломает ли меньший потолок саму задачу** (CLI обязан успеть ответить, иначе
отказ уходит в платный Дипсик — деньги).

Скрипт отвечает на оба вопроса одним прогоном: гоняет **настоящий** вызов
discovery при разных значениях потолка и меряет пиковый RSS процесса CLI.

**Что делает.** Для каждого значения потолка из ``--caps``:

* ставит ``MINIMAX_NODE_MAX_OLD_SPACE_MB`` на время вызова (движок читает env
  в момент вызова и **дописывает свой флаг в конец** ``NODE_OPTIONS``, а Node
  берёт последний — замерено на боксе 25.11: ``320 … --max-old-space-size=384``
  даёт предел кучи 408 МБ, то есть выигрывает именно код движка);
* зовёт **тот же путь**, что и ночной срез
  (``modules.minimax_engine.chat`` с заданием подбора сообществ);
* раз в полсекунды читает ``/proc`` и запоминает **пиковый RSS** своего
  дерева процессов (CLI — потомок пробного процесса).

**Чего не делает.** Не пишет в БД, не ходит в VK, не меняет состояние
сообществ, не отправляет Telegram и **не зовёт платный Дипсик** (страховка
выключена: отказ MiniMax должен быть виден, а не замаскирован деньгами).
Единственное последствие — несколько вызовов free-тира MiniMax и строки в
журнале расхода с меткой ``heap_probe`` (от боевых вызовов отличаются).

Запуск на проде::

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/minimax_heap_probe.py'

    --caps 96 160 0     значения потолка в МБ; ``0`` — «как сейчас» (дефолт кода)
    --json              вывод в JSON (по умолчанию — тот же JSON)
    --dry-run           только план: какие значения и какой дефолт, без вызовов

**Как читать вердикт.** По каждому значению:

* ``ok: true`` — при этом потолке CLI **успел ответить**: значение годится;
* ``reason: exec_failed`` + ``killed_by_kernel: true`` — процесс сняли снаружи
  (не влезает в память бокса): потолок не помогает, виновата конкуренция;
* ``reason: heap_oom`` / ``js heap out of memory`` — потолок **маловат** для
  задачи: так будет и в бою, отказ уйдёт в платный Дипсик.

Вывод: минимальный потолок, при котором ``ok: true`` — и есть ответ владельцу.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Интервал опроса RSS, с.
SAMPLE_INTERVAL = 0.5

#: Потолки для замера по умолчанию: предложение владельца (96), середина (160)
#: и «как сейчас» (0 — дефолт кода, то есть 384 МБ).
DEFAULT_CAPS: Tuple[int, ...] = (96, 160, 0)

#: Метка в журнале расхода: проба не должна сливаться с боевыми вызовами.
PROBE_LABEL = "heap_probe"

#: Синтетическая карточка сообщества: вердикт очевиден (``admin``), и ошибка
#: движка не спрячется в спорной категории. Десять постов — как в бою
#: (карточка в проде обрезается до 10 постов по 250 символов).
CARD_NAME = "Тужинский район | Официальное сообщество"
CARD_DESCRIPTION = (
    "Официальная группа администрации Тужинского района Кировской области. "
    "Новости района, афиша, нормативные документы, объявления администрации."
)
CARD_REGION = "Тужинский"
CARD_POSTS: Tuple[str, ...] = (
    "Публикуем график работы администрации района на следующую неделю: "
    "приём граждан вторник и четверг с 9 до 16 часов, обед с 13 до 14.",
    "В ДК «Заря» состоится концерт ансамбля «Вдохновение» — приглашаем "
    "жителей района, вход свободный, начало в 18 часов, двери с 17:30.",
    "Напоминаем жителям о сдаче показаний счётчиков до 25 числа — "
    "показания можно подать в МФЦ, личный кабинет или почтовым отправлением.",
    "Утверждены изменения в генеральный план: новая школа на 1100 мест "
    "появится в микрорайоне Южный, срок строительства — два года.",
    "Уважаемые жители! 14 ноября с 9 до 17 часов будет отключение "
    "водоснабжения на улицах Ленина и Советской в связи с плановыми работами.",
    "Продаются участки в садовом товариществе «Берёзка», все коммуникации "
    "подведены, документы готовы, торг уместен, звоните в личные сообщения.",
    "Завершён ремонт дороги по улице Молодёжной: уложено новое покрытие, "
    "установлено четыре светильника, нанесена разметка.",
    "В библиотеке открылась книжная выставка к юбилею района, приглашаем "
    "всех желающих, вход свободный, часы работы — с 9 до 18 часов.",
    "Приглашаем на субботник по уборке придомовой территории: инструмент и "
    "мешки будут, форма одежды — по погоде, чай и печенье — с нами.",
    "Опубликованы результаты торгов на право аренды городских земельных "
    "участков; заявки принимает отдел управления имуществом.",
)


# --- чистая часть (тестируется без бокса) -----------------------------------


def parse_proc_status(text: str) -> Optional[Tuple[int, int]]:
    """Из текста ``/proc/<pid>/status`` — ``(ppid, rss_kb)``.

    ``None``, если это не статус процесса: читать чужое нечего, а падать —
    тем более.
    """
    ppid = 0
    rss_kb = 0
    for line in (text or "").splitlines():
        if line.startswith("PPid:"):
            try:
                ppid = int(line.split()[1])
            except (IndexError, ValueError):
                ppid = 0
        elif line.startswith("VmRSS:"):
            try:
                rss_kb = int(line.split()[1])
            except (IndexError, ValueError):
                rss_kb = 0
    if not ppid and not rss_kb:
        return None
    return ppid, rss_kb


def read_process_table() -> Dict[int, Tuple[int, int]]:
    """Снимок процессов: ``pid -> (ppid, rss_kb)``.

    Только ``/proc`` — на боксе это единственный источник RSS по своему
    дереву, и он не требует ни прав, ни зависимостей.
    """
    table: Dict[int, Tuple[int, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return table
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/status" % entry, "r", encoding="utf-8", errors="replace") as fh:
                parsed = parse_proc_status(fh.read())
        except OSError:
            continue
        if parsed:
            table[int(entry)] = parsed
    return table


def descendant_pids(root_pid: int, table: Dict[int, Tuple[int, int]]) -> List[int]:
    """Потомки процесса (включая его самого) по снимку процессов."""
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _rss) in table.items():
        children.setdefault(ppid, []).append(pid)
    out: List[int] = []
    seen = {root_pid}
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            stack.append(child)
    return out


def tree_memory(table: Dict[int, Tuple[int, int]], root_pid: int) -> Tuple[int, int]:
    """Память дерева процесса: ``(сумма rss_kb, максимум по одному процессу)``."""
    total = 0
    biggest = 0
    for pid in descendant_pids(root_pid, table):
        entry = table.get(pid)
        if not entry:
            continue
        rss = entry[1]
        total += rss
        biggest = max(biggest, rss)
    return total, biggest


class PeakTracker:
    """Следит за пиковым RSS дерева процессов, пока идёт вызов.

    Отдельный поток: вызов CLI идёт в основном, а память надо мешать всё время.
    """

    def __init__(self, root_pid: int, interval: float = SAMPLE_INTERVAL) -> None:
        self.root_pid = root_pid
        self.interval = interval
        self.peak_total_kb = 0
        self.peak_single_kb = 0
        self.samples = 0
        self.pids: List[int] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        while not self._stop.is_set():
            table = read_process_table()
            total, biggest = tree_memory(table, self.root_pid)
            if total:
                self.samples += 1
                self.peak_total_kb = max(self.peak_total_kb, total)
                self.peak_single_kb = max(self.peak_single_kb, biggest)
                self.pids = descendant_pids(self.root_pid, table)
            self._stop.wait(self.interval)

    def __enter__(self) -> "PeakTracker":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


def kernel_oom_pids() -> List[int]:
    """PID'ы, которые ядро сняло по памяти (из ``dmesg``).

    Нужен root: без прав список пустой, и это не ошибка — прибор честно
    отмечает, что свернуть kill с конкретным PID нечем. На боксе пароль у
    владельца пустой, поэтому проба пробует и ``sudo -n`` (проба идёт под
    ``valstan``, чтобы файлы состояния CLI остались его собственными).
    """
    import subprocess

    pids: List[int] = []
    for cmd in (["dmesg", "-T"], ["sudo", "-n", "dmesg", "-T"]):
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            ).stdout
        except Exception:  # noqa: BLE001 — прибор не должен ронять из-за dmesg
            continue
        if not out:
            continue
        for line in out.splitlines():
            if "OOM killed process" not in line:
                continue
            try:
                head = line.split("OOM killed process", 1)[1].strip()
                pid_text = head.split(" ", 1)[0]
                pids.append(int(pid_text))
            except (IndexError, ValueError):
                continue
        if pids:
            break
    return pids


def effective_heap_limit_mb(node_options: str) -> Optional[int]:
    """Предел кучи V8 по ``NODE_OPTIONS`` — как его понял бы Node.

    Node берёт **последний** ``--max-old-space-size`` (замерено на боксе
    25.11), а движок дописывает свой флаг в конец строки — значит действует
    именно значение кода, а не env-файла.
    """
    limit: Optional[int] = None
    for chunk in (node_options or "").split():
        if chunk.startswith("--max-old-space-size="):
            try:
                limit = int(chunk.split("=", 1)[1])
            except ValueError:
                continue
    return limit


def summarise(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Итог замера: минимальный потолок, при котором CLI ответил.

    Сравниваются **действующие** потолки (``effective_cap_mb``): замер «как
    сейчас» — это не 0 МБ, а дефолт кода (384 МБ), и смешивать его с нулём
    нельзя, иначе «дефолт» всегда выигрывает минимум.

    ``None`` в ``min_ok_cap_mb`` — ни один из проверенных потолков не дал
    успешного ответа: значит потолок кучи тут ни при чём (виновата память
    бокса или конкуренция), и решение владельца надо принимать по другому.
    """
    ok_caps = [int(r["effective_cap_mb"]) for r in rows if r.get("ok")]
    return {
        "min_ok_cap_mb": min(ok_caps) if ok_caps else None,
        "ok_caps": ok_caps,
        "failed_caps": [int(r["effective_cap_mb"]) for r in rows if not r.get("ok")],
        "rows": list(rows),
    }


# --- замер (нужен бокс) ------------------------------------------------------


def measure_cap(cap_mb: int, *, region: str = CARD_REGION) -> Dict[str, Any]:
    """Один замер: настоящий вызов discovery при заданном потолке кучи.

    ``cap_mb = 0`` — «как сейчас»: переменная не выставляется, действует
    дефолт кода.
    """
    from modules.discovery.ai_categorizer import _build_card, _build_system_prompt
    from modules.minimax_engine import (
        DEFAULT_NODE_MAX_OLD_SPACE_MB,
        ENV_NODE_MAX_OLD_SPACE_MB,
        TASK_TEXT_DISCOVERY,
        chat,
    )

    previous = os.environ.get(ENV_NODE_MAX_OLD_SPACE_MB)
    if cap_mb > 0:
        os.environ[ENV_NODE_MAX_OLD_SPACE_MB] = str(cap_mb)
        effective_cap = cap_mb
    else:
        os.environ.pop(ENV_NODE_MAX_OLD_SPACE_MB, None)
        # Движок дописывает свой флаг в конец NODE_OPTIONS, а Node берёт
        # последний — значит без переменной действует дефолт кода, а не то
        # значение, что лежит в env-файле бокса.
        effective_cap = int(DEFAULT_NODE_MAX_OLD_SPACE_MB)

    system = _build_system_prompt(region_name=region)
    card = _build_card(
        name=CARD_NAME,
        description=CARD_DESCRIPTION,
        members_count=1234,
        recent_posts=list(CARD_POSTS),
    )

    killed_before = set(kernel_oom_pids())
    started = time.monotonic()
    row: Dict[str, Any] = {"cap_mb": cap_mb, "effective_cap_mb": effective_cap}
    try:
        with PeakTracker(os.getpid()) as tracker:
            result = chat(
                system=system,
                user=card,
                task=TASK_TEXT_DISCOVERY,
                label=PROBE_LABEL,
            )
        row.update(
            {
                "ok": bool(result.get("ok")),
                "reason": str(result.get("reason") or ""),
                "detail": str(result.get("detail") or "")[:200],
                "duration_ms": int((time.monotonic() - started) * 1000),
                "peak_tree_rss_mb": round(tracker.peak_total_kb / 1024.0, 1),
                "peak_single_rss_mb": round(tracker.peak_single_kb / 1024.0, 1),
                "cli_pids": tracker.pids,
                "samples": tracker.samples,
            }
        )
        killed_after = set(kernel_oom_pids())
        fresh = killed_after - killed_before
        row["killed_by_kernel"] = bool(fresh & set(tracker.pids))
    finally:
        if previous is None:
            os.environ.pop(ENV_NODE_MAX_OLD_SPACE_MB, None)
        else:
            os.environ[ENV_NODE_MAX_OLD_SPACE_MB] = previous
    return row


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Замер потолка кучи V8 для CLI MiniMax (на боевом вызове discovery).",
    )
    parser.add_argument(
        "--caps",
        type=int,
        nargs="+",
        default=list(DEFAULT_CAPS),
        help="значения потолка в МБ; 0 — «как сейчас» (дефолт кода)",
    )
    parser.add_argument("--json", action="store_true", help="вывод в JSON (по умолчанию — он же)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только план: какие значения и какой действующий предел кучи, без вызовов",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    node_options = os.environ.get("NODE_OPTIONS", "")
    effective = effective_heap_limit_mb(node_options)

    if args.dry_run:
        payload = {
            "dry_run": True,
            "caps_mb": list(args.caps),
            "node_options": node_options,
            "effective_max_old_space_mb_in_node_options": effective,
            "note": "действует последний флаг в NODE_OPTIONS; движок дописывает свой в конец",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    rows: List[Dict[str, Any]] = []
    for cap_mb in args.caps:
        print("--- замер: потолок %s МБ ---" % ("дефолт кода" if not cap_mb else cap_mb))
        sys.stdout.flush()
        rows.append(measure_cap(cap_mb))

    payload = summarise(rows)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    # Код возврата: 0 — хоть один потолок дал успешный ответ.
    return 0 if payload["min_ok_cap_mb"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
