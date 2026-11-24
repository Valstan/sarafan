#!/usr/bin/env python3
"""Вернуть прежние темы донорам, ушедшим в темы без слота (последствие P235).

**Что чиним.** Применение AI-подсказок (решение владельца 24.11) перевело 958
доноров между темами. Часть из них ушла в темы, у которых **нет слота** в
beat-расписании (``sosed`` снят 14.09, областная повестка кроме
``proisshestviya`` - тоже без слотов). Волна берёт сообщества по
``category == тема``; запасной путь «весь пул» спасает только районы, где у
какой-то волны нет своих сообществ. У этих доноров такой возможности нет -
**их посты перестают попадать в сборку**.

**Как восстанавливаем прежнюю тему.** У тёмного донора каждая публикация шла
через **его собственную** волну (у региона есть сообщества в каждой волне, то
есть запасной путь не срабатывал) - значит тема его последних публикаций и
есть его прежняя категория. Берём самую частую тему **со слотом** из журнала
публикаций (``published_posts``). Если таких публикаций нет - донора не трогаем
и называем в отчёте.

**Что делает.** Ставит прежнюю категорию обратно, а AI-подсказку возвращает в
``suggested_category`` (с ``health_status='changed_category'``) - чтобы в UI она
была видна и её можно было применить **позже**, когда у темы появится слот.
То есть решение не теряется, а откладывается на осмысленное место.

**Чего не делает.** Не трогает наши собственные главные группы. Не трогает
доноров, которые молчали и до сегодняшней правки (legacy-категории ``art``,
``prikol``, ``krugozor``, ``music``, ``kino`` - у них своя история).

Запуск на проде (сначала без ``--apply`` — превью):

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
      cd /home/valstan/SETKA && ./venv/bin/python scripts/restore_silenced_categories.py'

    --apply            записать (по умолчанию — только превью)
    --json             вывод в JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: Корень репозитория в sys.path: скрипт запускают как
#: ``python scripts/restore_silenced_categories.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Темы, у которых есть слот в beat-расписании.
SLOTTED_THEMES = ("novost", "reklama", "kultura", "sport", "admin", "union", "detsad")
#: Темы, у которых фолбэк «весь пул» разрешён (у ``reklama`` - запрещён).
FALLBACK_THEMES = ("novost", "kultura", "sport", "admin", "union", "detsad")
#: Темы **без слота**, в которые AI перевёл доноров 24.11. Их и чиним.
AI_PROPOSED_NO_SLOT = (
    "sosed",
    "molodezh",
    "zdorovie",
    "nauka",
    "selhoz",
    "priroda",
    "promyshlennost",
)


def is_dark(region_rows: Sequence[Any]) -> List[Any]:
    """Какие сообщества региона никто не подберёт (у них тема без слота).

    Донор темы без слота тёмный, если у региона есть сообщества в **каждой**
    волне со слотом: тогда ни одна волна не сработает запасным путём («весь
    пул»), и донора не возьмёт никто.
    """
    cats = Counter((getattr(r, "category", "") or "") for r in region_rows)
    return [
        r
        for r in region_rows
        if (getattr(r, "category", "") or "") in AI_PROPOSED_NO_SLOT
        and all(cats[theme] > 0 for theme in FALLBACK_THEMES)
    ]


def pick_previous_theme(owner_vk: int, themes_by_owner: Dict[int, Counter]) -> Optional[str]:
    """Прежняя тема донора — самая частая тема **со слотом** в его публикациях.

    Темы без слота (``addons``, ``korotko``) игнорируются: вернуть донора туда
    = оставить его молчащим.
    """
    themes = themes_by_owner.get(abs(int(owner_vk)))
    if not themes:
        return None
    for theme, _count in themes.most_common():
        if theme in SLOTTED_THEMES:
            return theme
    return None


async def _run(dry_run: bool) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Community, Region
    from database.models_extended import PublishedPost

    async with AsyncSessionLocal() as session:
        regions = {int(r.id): r for r in (await session.execute(select(Region))).scalars().all()}
        rows = list(
            (await session.execute(select(Community).where(Community.is_active.is_(True))))
            .scalars()
            .all()
        )
        published = list((await session.execute(select(PublishedPost))).scalars().all())

    by_region = defaultdict(list)
    for row in rows:
        by_region[int(row.region_id)].append(row)

    themes_by_owner: Dict[int, Counter] = defaultdict(Counter)
    for post in published:
        theme = getattr(post, "wave_theme", None) or getattr(post, "theme", None)
        if not theme:
            continue
        try:
            owner = abs(int(str(post.lip or "").split("_", 1)[0]))
        except (TypeError, ValueError):
            continue
        themes_by_owner[owner][str(theme)] += 1

    own_main = {
        int(rid): abs(int(r.vk_group_id))
        for rid, r in regions.items()
        if getattr(r, "vk_group_id", None)
    }

    plan: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for region_id, region_rows in by_region.items():
        for row in is_dark(region_rows):
            if own_main.get(region_id) == abs(int(row.vk_id)):
                continue
            previous = pick_previous_theme(int(row.vk_id), themes_by_owner)
            code = regions[region_id].code if region_id in regions else "?"
            if not previous:
                unresolved.append(
                    {
                        "id": int(row.id),
                        "vk_id": int(row.vk_id),
                        "region": code,
                        "category": row.category,
                        "name": str(getattr(row, "name", "") or "")[:70],
                    }
                )
                continue
            plan.append(
                {
                    "id": int(row.id),
                    "vk_id": int(row.vk_id),
                    "region": code,
                    "was": row.category,
                    "becomes": previous,
                    "name": str(getattr(row, "name", "") or "")[:70],
                }
            )

    written = 0
    if not dry_run and plan:
        async with AsyncSessionLocal() as session:
            now = datetime.utcnow()
            ids = [int(item["id"]) for item in plan]
            fresh = list(
                (await session.execute(select(Community).where(Community.id.in_(ids))))
                .scalars()
                .all()
            )
            by_id = {int(item["id"]): item for item in plan}
            for row in fresh:
                item = by_id.get(int(row.id))
                if not item:
                    continue
                row.category = item["becomes"]
                # Подсказка AI остаётся видимой: тема без слота - значит, вернуть
                # её можно будет, когда у темы появится слот.
                row.suggested_category = item["was"]
                row.health_status = "changed_category"
                row.updated_at = now
            await session.commit()
            written = len(fresh)

    return {
        "dry_run": dry_run,
        "plan": plan,
        "unresolved": unresolved,
        "written": written,
    }


def _print_text(report: Dict[str, Any]) -> None:
    print(
        "вернуть прежнюю тему: %d; без прежних публикаций (не трогаем): %d"
        % (len(report["plan"]), len(report["unresolved"]))
    )
    for item in report["plan"]:
        print(
            "  %-16s %-14s -> %-12s %s"
            % (item["region"], item["was"], item["becomes"], item["name"])
        )
    for item in report["unresolved"]:
        print(
            "  НЕ ТРОГАЕМ (нет прежних публикаций) %-16s %-14s %s"
            % (item["region"], item["category"], item["name"])
        )
    if report["dry_run"]:
        print("\n[DRY-RUN] ничего не записано. Применить: --apply")
    else:
        print("\n[WRITTEN] восстановлено %d" % report["written"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Вернуть прежние темы донорам, ушедшим в темы без слота"
    )
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию превью)")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args()

    try:
        report = asyncio.run(_run(dry_run=not args.apply))
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
