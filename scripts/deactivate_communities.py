#!/usr/bin/env python3
"""Выключить сообщества из пула региона (мягко, обратимо) — чистка доноров.

Зачем отдельный скрипт, а не `psql -c "UPDATE ..."`: выключение донора —
операция с данными на проде (гейт #025), и её нужно (а) прогнать dry-run'ом,
(б) показать человеку ровно то, что изменится, (в) не задеть чужой регион по
опечатке в id. Скрипт делает всё три вещи, SQL этого не делает.

Мягко = `is_active=False` + `disabled_at` + `disabled_reason`. Строка остаётся,
история и статистика сохраняются, вернуть = `is_active=True`.

⚠️ `disabled_reason` — не косметика. Ежемесячный `dormant_recheck` оживляет
строки **только** с `disabled_reason='dormant_t1_auto'` (`tasks/discovery_tasks.py`),
поэтому ручная чистка обязана писать СВОЮ причину, иначе автомат вернёт донора
обратно, и выключение окажется мнимым.

Запуск на проде (сначала dry-run!)::

    sudo bash -c 'set -a; . /etc/setka/setka.env; set +a; \
      cd /home/valstan/SETKA && \
      ./venv/bin/python /tmp/deactivate_communities.py \
        --region-code chistopolsky --vk-ids-file /tmp/off.json \
        --reason pool_foreign_district --dry-run'

Формат `--vk-ids-file`: JSON-список id, знак не важен (``[-123, 456]``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence, Tuple


def parse_ids_file(path: str) -> List[int]:
    """Прочитать список id (JSON-список, знак не важен) → список abs-значений.

    Кодировка — ``utf-8-sig``: файл часто готовят на Windows, где ``Out-File``
    пишет BOM, а json не терпит его в начале.
    """
    with open(path, encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("ожидался JSON-список id")
    out: List[int] = []
    for item in raw:
        v = abs(int(item))
        if v and v not in out:
            out.append(v)
    return out


def split_targets(
    rows: Sequence[Any], wanted: Iterable[int]
) -> Tuple[List[Any], List[Any], List[int]]:
    """Разложить строки региона на (включённые, уже выключенные, ненайденные id).

    Чистая функция: не трогает БД и не меняет объекты — её и проверяют тесты.
    «Ненайденные» — id, которых в этом регионе нет вообще: почти всегда опечатка
    или чужой район, и молча их глотать нельзя.
    """
    wanted_abs = {abs(int(w)) for w in wanted}
    found: Dict[int, Any] = {}
    for row in rows:
        vk = abs(int(getattr(row, "vk_id", 0) or 0))
        if vk in wanted_abs:
            found[vk] = row
    active = [r for r in found.values() if getattr(r, "is_active", False)]
    already = [r for r in found.values() if not getattr(r, "is_active", False)]
    missing = sorted(wanted_abs - set(found))
    # Порядок вывода — стабильный (по id), чтобы dry-run читался одинаково.
    active.sort(key=lambda r: abs(int(r.vk_id)))
    already.sort(key=lambda r: abs(int(r.vk_id)))
    return active, already, missing


async def _run(
    region_code: str,
    vk_ids: List[int],
    reason: str,
    dry_run: bool,
    allow_missing: bool,
) -> int:
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Community, Region

    if not vk_ids:
        print("FATAL: список id пуст — нечего выключать", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as session:
        region = (
            (await session.execute(select(Region).where(Region.code == region_code)))
            .scalars()
            .first()
        )
        if region is None:
            print(f"FATAL: region {region_code!r} not found", file=sys.stderr)
            return 2

        rows = list(
            (await session.execute(select(Community).where(Community.region_id == region.id)))
            .scalars()
            .all()
        )

        active, already, missing = split_targets(rows, vk_ids)

        print(f"region={region_code} (id={region.id})  reason={reason}")
        for r in active:
            print(f"  OFF  {-abs(int(r.vk_id)):>12}  {r.category:<8} {r.name[:52]}")
        for r in already:
            print(
                f"  already inactive {-abs(int(r.vk_id)):>12}  "
                f"({r.disabled_reason or '—'})  {r.name[:40]}"
            )
        for vk in missing:
            print(f"  NOT IN REGION  {vk:>12}  (опечатка или чужой район)")

        print(
            f"\nвыключить: {len(active)}; уже выключены: {len(already)}; "
            f"не найдено: {len(missing)}"
        )

        if missing and not allow_missing:
            print(
                "FATAL: часть id не найдена в регионе — остановка "
                "(--allow-missing, если так и задумано)",
                file=sys.stderr,
            )
            return 3

        if dry_run:
            print("[DRY-RUN] ничего не записано")
            return 0

        if not active:
            print("нечего менять: все перечисленные уже выключены")
            return 0

        now = datetime.utcnow()
        for r in active:
            r.is_active = False
            r.disabled_at = now
            r.disabled_reason = reason
        await session.commit()
        print(f"[WRITTEN] выключено {len(active)}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Мягко выключить доноров региона")
    ap.add_argument("--region-code", required=True)
    ap.add_argument("--vk-ids-file", required=True, help="JSON-список id (знак не важен)")
    ap.add_argument(
        "--reason",
        default="pool_cleanup",
        help="disabled_reason; НЕ 'dormant_t1_auto' — его оживляет авто-recheck",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--allow-missing",
        action="store_true",
        help="не падать, если часть id в регионе не найдена",
    )
    args = ap.parse_args()

    if args.reason == "dormant_t1_auto":
        print(
            "FATAL: reason='dormant_t1_auto' зарезервирован авто-политикой — "
            "ежемесячный recheck вернёт строки в пул",
            file=sys.stderr,
        )
        return 2

    try:
        vk_ids = parse_ids_file(args.vk_ids_file)
    except (OSError, ValueError) as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    return asyncio.run(
        _run(
            args.region_code,
            vk_ids,
            args.reason,
            args.dry_run,
            args.allow_missing,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
