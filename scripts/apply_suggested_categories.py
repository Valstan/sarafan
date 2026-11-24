#!/usr/bin/env python3
"""Массово применить AI-подсказки категории (``suggested_category`` → ``category``).

**Зачем.** 23.11 ночной recheck пометил 964 сообщества как «сменили тематику»
(``health_status='changed_category'``, ``suggested_category`` заполнена). В UI есть
только **одна** кнопка применения на группу
(``POST /api/communities/{id}/apply-suggested-category``), то есть 964 клика руками —
не рабочий способ. Скрипт делает то же, что кнопка, но разом и с отчётом.

⚠️ **Категория = тема.** Применение подсказки — это **перевод донора между темами**,
а не «исправление метки»: посты группы попадают в ленту темы ``category``. Тема
``detsad`` имеет **один слот в сутки** (13:30) — перевод туда 443 школ переполняет
её. Скрипт печатает разбивку по вёдрам и по целевым темам **до** записи, чтобы это
было видно заранее.

**Что не делает.** Не трогает наши собственные группы (``regions.vk_group_id``) —
перевод темы своей группы ударил бы по маршрутизации нашей ленты; такие строки
скрипт пропускает и называет в отчёте. Не удаляет строки, не меняет ``is_active``.

Запуск на проде (сначала без ``--apply`` — превью):

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
      cd /home/valstan/SETKA && ./venv/bin/python scripts/apply_suggested_categories.py'

    --apply             записать (по умолчанию — только превью)
    --json              вывод в JSON
    --limit N           применить не больше N записей (для пробного захода)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence

#: Темы с **одним** слотом в сутки: перевод туда переполняет тему.
SINGLE_SLOT_THEMES = frozenset({"detsad"})


def plan_applies(rows: Sequence[Any], own_group_ids: Iterable[int]) -> Dict[str, Any]:
    """Разложить строки на (применить, пропустить, вёдра, целевые темы).

    Чистая функция — БД не трогает: её и проверяют тесты.

    * ``rows`` — сообщества с ``health_status='changed_category'`` и непустой
      ``suggested_category``;
    * ``own_group_ids`` — id наших собственных групп (``regions.vk_group_id``);
      они не переводятся никогда.
    """
    own = {abs(int(gid)) for gid in own_group_ids if gid}
    to_apply: List[Any] = []
    skipped_own: List[Any] = []
    skipped_same: List[Any] = []
    buckets: Counter = Counter()
    targets: Counter = Counter()

    for row in rows:
        suggested = str(getattr(row, "suggested_category", "") or "").strip().lower()
        stored = str(getattr(row, "category", "") or "").strip().lower()
        if not suggested:
            continue
        if suggested == stored:
            # Предлагают то же самое, что уже стоит: переводить нечего, но флаг
            # висит. Такие строки чинятся сбросом флага, а не переводом.
            skipped_same.append(row)
            continue
        if abs(int(getattr(row, "vk_id", 0) or 0)) in own:
            skipped_own.append(row)
            continue
        to_apply.append(row)
        buckets["%s → %s" % (stored or "—", suggested)] += 1
        targets[suggested] += 1

    to_apply.sort(
        key=lambda r: (str(getattr(r, "category", "") or ""), abs(int(getattr(r, "vk_id", 0) or 0)))
    )
    skipped_own.sort(key=lambda r: abs(int(getattr(r, "vk_id", 0) or 0)))
    skipped_same.sort(key=lambda r: abs(int(getattr(r, "vk_id", 0) or 0)))
    return {
        "to_apply": to_apply,
        "skipped_own": skipped_own,
        "skipped_same": skipped_same,
        "buckets": dict(buckets.most_common()),
        "targets": dict(targets.most_common()),
    }


def _report_rows(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    """Строки отчёта в JSON: id, vk_id, было, станет, имя."""
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(getattr(row, "id", 0) or 0),
                "vk_id": int(getattr(row, "vk_id", 0) or 0),
                "was": str(getattr(row, "category", "") or ""),
                "becomes": str(getattr(row, "suggested_category", "") or ""),
                "name": str(getattr(row, "name", "") or "")[:80],
            }
        )
    return out


async def _run(dry_run: bool, limit: int) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Community, Region

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(Community).where(
                        Community.health_status == "changed_category",
                        Community.suggested_category.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        own_group_ids = [
            int(gid)
            for gid in (
                await session.execute(
                    select(Region.vk_group_id).where(Region.vk_group_id.isnot(None))
                )
            )
            .scalars()
            .all()
        ]

    plan = plan_applies(rows, own_group_ids)
    to_apply: List[Any] = plan["to_apply"]

    if limit > 0:
        to_apply = to_apply[:limit]

    written = 0
    if not dry_run and to_apply:
        async with AsyncSessionLocal() as session:
            now = datetime.utcnow()
            ids = [int(r.id) for r in to_apply]
            fresh = list(
                (await session.execute(select(Community).where(Community.id.in_(ids))))
                .scalars()
                .all()
            )
            for row in fresh:
                row.category = row.suggested_category
                row.suggested_category = None
                row.health_status = "active"
                row.updated_at = now
            await session.commit()
            written = len(fresh)

    return {
        "dry_run": dry_run,
        "rows_with_suggestion": len(rows),
        "to_apply": len(to_apply),
        "written": written,
        "skipped_own": len(plan["skipped_own"]),
        "skipped_same": len(plan["skipped_same"]),
        "buckets": plan["buckets"],
        "targets": plan["targets"],
        "single_slot_themes": sorted(SINGLE_SLOT_THEMES),
        "skipped_own_rows": _report_rows(plan["skipped_own"]),
        "rows": _report_rows(to_apply),
    }


def _print_text(report: Dict[str, Any]) -> None:
    print("строк с подсказкой: %d" % report["rows_with_suggestion"])
    print(
        "применить: %d; наши группы (не трогаем): %d; подсказка = текущая: %d"
        % (report["to_apply"], report["skipped_own"], report["skipped_same"])
    )
    print("\nвёдра (было → станет):")
    for pair, count in report["buckets"].items():
        print("  %-28s %5d" % (pair, count))
    print("\nцелевые темы:")
    for theme, count in report["targets"].items():
        mark = "  ⚠️  ОДИН СЛОТ В СУТКИ" if theme in SINGLE_SLOT_THEMES else ""
        print("  %-14s %5d%s" % (theme, count, mark))
    for row in report["skipped_own_rows"]:
        print("  ПРОПУЩЕНО (наша группа): %s %s" % (row["vk_id"], row["name"]))
    if report["dry_run"]:
        print("\n[DRY-RUN] ничего не записано. Применить: --apply")
    else:
        print("\n[WRITTEN] применено %d" % report["written"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Массово применить AI-подсказки категории")
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию превью)")
    ap.add_argument("--limit", type=int, default=0, help="не больше N записей")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args()

    try:
        report = asyncio.run(_run(dry_run=not args.apply, limit=args.limit))
    except Exception as exc:  # noqa: BLE001 — CLI-утилита, а не часть приложения
        print("FATAL: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
