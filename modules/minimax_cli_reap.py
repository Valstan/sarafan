"""Уборка осиротевших процессов MiniMax Code CLI.

**Что это.** Процесс MiniMax Code CLI (``comm`` = ``minimax-code``) **перехватывает
SIGTERM**. Замер 25.11 13:40 на живом вызове: в ``/proc/<pid>/status`` бит сигнала 15
стоит в ``SigCgt`` (пойман), а не в ``SigIgn`` (не проигнорирован). Значит graceful-остановка
юнита его **не убивает**: ``systemctl restart setka-celery-worker`` шлёт SIGTERM всей cgroup,
CLI его перехватывает и продолжает жить.

**Чем это кончается.** Родитель CLI — fork-ребёнок Celery-воркера; когда воркер умирает,
CLI остаётся сиротой (его переподбирает init, ``PPid`` = 1) и живёт ещё **~18 минут**,
подрастая до 250–290 МБ RSS. На боксе с 1536 МБ и swap = 0 это съеденная память, а
заканчивается всё ядерным убийством, которое в логах выглядит как «новые отказы движка»:
замер 25.11 — **26** убийств в 08:13:25–08:30:36 после рестарта воркеров, при том что
**все вызовы движка в это окно прошли успешно** (см. [P240]).

**Что делает этот модуль.** Убирает **только осиротевших**: процессы ``minimax-code``, у
которых ``PPid`` = 1. Живой CLI (родитель — fork-ребёнок воркера) не трогаем никогда,
поэтому чужой вызов не прерывается. Сигнал — **SIGKILL**: SIGTERM CLI перехватывает (см.
выше), то есть «мягко» он не выйдет.

**Где вызывается:** перед каждым запуском CLI в движке
(``modules/minimax_engine.py``) и на старте воркера (``tasks/celery_app.py``). Второе важно
не меньше первого: сироты появляются **из-за** рестарта, то есть ровно в тот момент,
когда поднимается новый воркер.

**Чего это не делает.** Не заменяет ``PR_SET_PDEATHSIG`` в движке: тот убирает сироту в
момент смерти родителя, а этот — по факту (и без ``preexec_fn``, который в многопоточных
процессах Python небезопасен). Оба подхода совместимы.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: ``comm`` процесса CLI MiniMax Code на боксе.
CLI_COMM = "minimax-code"

#: Корень псевдо-файловой системы процессов.
PROC_ROOT = Path("/proc")

#: ``PPid``, который означает «родителя нет» — процесс осиротевший.
ORPHAN_PPID = 1

#: SIGKILL есть не на всех платформах (в Windows его нет) — там «убить» = TerminateProcess,
#: что и делает ``os.kill(pid, signal.SIGTERM)``. На боксе (Linux) это настоящий SIGKILL.
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _status_field(status: str, field: str) -> str:
    for line in status.splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def find_orphan_cli_pids(proc_root: Path = PROC_ROOT) -> List[int]:
    """PID процессов ``minimax-code``, чей родитель умер (``PPid`` = 1).

    Зомби пропускаются: они уже мёртвы и ждут, пока их приберёт init, — убивать нечего.
    """
    found: List[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        if _read_text(entry / "comm").strip() != CLI_COMM:
            continue
        status = _read_text(entry / "status")
        if not status:
            continue
        if _status_field(status, "State").startswith("Z"):
            continue
        if _status_field(status, "PPid") != str(ORPHAN_PPID):
            continue
        found.append(int(entry.name))
    return sorted(found)


def reap_orphan_cli(
    proc_root: Path = PROC_ROOT,
    *,
    kill: Optional[Callable[[int, int], None]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Снять осиротевшие процессы CLI. Вернуть отчёт.

    ``kill`` — подставляется в тестах. ``dry_run`` — только посчитать, ничего не убивая.
    """
    killer = kill if kill is not None else os.kill
    pids = find_orphan_cli_pids(proc_root)
    killed: List[int] = []
    errors: List[str] = []
    for pid in pids:
        if dry_run:
            continue
        try:
            # Именно SIGKILL: SIGTERM CLI перехватывает и не умирает.
            killer(pid, KILL_SIGNAL)
            killed.append(pid)
        except ProcessLookupError:
            # Уже мёртв — ничего страшного.
            continue
        except OSError as exc:
            errors.append("%d: %s" % (pid, exc))
    if killed:
        logger.warning(
            "minimax: снято осиротевших CLI-процессов: %s (пережили рестарт воркера)",
            killed,
        )
    if errors:
        logger.warning("minimax: не удалось снять осиротевших CLI: %s", "; ".join(errors))
    return {"found": len(pids), "candidates": pids, "killed": killed, "errors": errors}
