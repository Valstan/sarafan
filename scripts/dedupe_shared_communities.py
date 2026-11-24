#!/usr/bin/env python3
"""Выключить лишние регистрации групп, заведённых в нескольких районах (P236).

**Что чиним.** 71 VK-группа заведена в ``communities`` сразу для нескольких
регионов: 187 регистраций, 116 лишних. Посты одного донора приходят читателям
нескольких районов (тот же класс, что P216 — утечка чужих районов через
агрегатор «ленты соседей»). Плюс 5 групп заведены **дважды в одном регионе**
(дубль строки) и в проде есть регион ``test``.

**Как решаем, где группа своя.** Тем же правилом, что уже работает в discovery
(P235, звено 2 — PR #1233, area-guard): у группы VK отдаёт город (``city``), у
города — область; область региона определяется по городу области (родитель-область)
либо по городу-центру района. Регистрацию оставляем **только там, где область
региона совпала с областью группы**. Нет совпадения или совпадений несколько —
**не гадаем**: такую группу оставляем как есть и называем в отчёте.

**Что делает.** Ставит ``is_active=False`` + ``disabled_at`` + ``disabled_reason``
— обратимо (строка остаётся, просто перестаёт попадать в ленту), по образцу
``scripts/deactivate_communities.py``. Ничего не удаляет.

**Чего не делает.** Не трогает наши собственные главные группы
(``regions.vk_group_id``) — в том числе их регистрацию в своём же районе.

Запуск на проде (сначала без ``--apply`` — превью):

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
      cd /home/valstan/SETKA && ./venv/bin/python scripts/dedupe_shared_communities.py'

    --apply            записать (по умолчанию — только превью)
    --json             вывод в JSON
    --reason ПРИЧИНА   disabled_reason для лишних регистраций
                       (по умолчанию pool_shared_group)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Корень репозитория в sys.path: скрипт запускают как
#: ``python scripts/dedupe_shared_communities.py``, и без этого ``database``
#: и ``modules`` не нашлись бы (как в scripts/deactivate_communities.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Регион-тест в проде: его регистрации выключаются отдельной причиной.
TEST_REGION_CODE = "test"
#: Причины для disabled_reason. НЕ 'dormant_t1_auto' — этот оживляет авто-recheck.
REASON_SHARED = "pool_shared_group"
REASON_DUPLICATE = "duplicate_row"
REASON_TEST_REGION = "region_test_cleanup"


def group_by_vk_id(rows: Sequence[Any]) -> Dict[int, List[Any]]:
    """Сгруппировать строки communities по ``abs(vk_id)``."""
    out: Dict[int, List[Any]] = defaultdict(list)
    for row in rows:
        out[abs(int(getattr(row, "vk_id", 0) or 0))].append(row)
    return dict(out)


def find_shared_and_duplicates(
    rows: Sequence[Any],
) -> Tuple[Dict[int, List[Any]], Dict[Tuple[int, int], List[Any]]]:
    """Найти (общие группы, дубли внутри региона).

    * **общая группа** — одна и та же VK-группа в **разных** регионах;
    * **дубль** — одна и та же группа **двумя строками в одном** регионе.
    """
    by_vk = group_by_vk_id(rows)
    shared: Dict[int, List[Any]] = {}
    duplicates: Dict[Tuple[int, int], List[Any]] = {}
    for vk_id, group_rows in by_vk.items():
        if not vk_id:
            continue
        by_region: Dict[int, List[Any]] = defaultdict(list)
        for row in group_rows:
            by_region[int(getattr(row, "region_id", 0) or 0)].append(row)
        if len(by_region) > 1:
            shared[vk_id] = list(group_rows)
        for region_id, same_region_rows in by_region.items():
            if len(same_region_rows) > 1:
                duplicates[(vk_id, region_id)] = list(same_region_rows)
    return shared, duplicates


def pick_duplicate_survivor(rows: Sequence[Any]) -> Any:
    """Какую из двух строк-дублей оставить: у которой больше истории.

    Порядок: больше ``posts_count`` → меньший ``id`` (старше). Порядок строк в БД
    не значит ничего, поэтому сравниваем по данным, а не по позиции.
    """
    return sorted(
        rows,
        key=lambda r: (-int(getattr(r, "posts_count", 0) or 0), int(getattr(r, "id", 0) or 0)),
    )[0]


async def _region_areas(
    session: Any, regions: Dict[int, Any], client: Any
) -> Dict[int, Optional[str]]:
    """Область каждого региона — тем же способом, что и в discovery.

    ``_own_area_city_id`` (tasks/discovery_tasks) → город области (родитель-область)
    или город-центр района; ``_resolve_own_area`` (modules/discovery/vk_search) →
    название области, как его знает VK.
    """
    from modules.discovery.vk_search import _resolve_own_area
    from tasks.discovery_tasks import _own_area_city_id

    out: Dict[int, Optional[str]] = {}
    for region_id, region in regions.items():
        try:
            city_id = await _own_area_city_id(session, region)
        except Exception:  # noqa: BLE001 — регион без гео = «область неизвестна»
            city_id = None
        out[region_id] = _resolve_own_area(client, city_id) if city_id else None
    return out


def decide_shared(
    shared: Dict[int, List[Any]],
    *,
    region_areas: Dict[int, Optional[str]],
    group_areas: Dict[int, Optional[str]],
    own_region_groups: Dict[int, int],
) -> Dict[int, Dict[str, Any]]:
    """Разметить общие группы: где оставляем, что выключаем.

    Правило: область региона совпала с областью группы → регистрация **своя**;
    обе области известны и не совпали → регистрация **лишняя**. Область
    неизвестна (ни у группы, ни у региона) — **не гадаем**: такую оставляем.

    Ключевое условие для выключения: **есть хоть одна своя регистрация**. Тогда
    понятно, где группа живёт, и лишние (чужой области) выключаются. Если своей
    нет — это уже не «лишняя регистрация», а «группа нигде не своя» (чужая
    донорская группа в N районах); area-guard такое не разбирает, и мы такое не
    трогаем — отчёт отдаёт группу на разбор.

    Чистая функция — VK и БД не трогает: её и проверяют тесты.
    """
    plan: Dict[int, Dict[str, Any]] = {}
    for vk_id, rows in shared.items():
        keep_ids: List[int] = []
        drop_ids: List[int] = []
        unknown_ids: List[int] = []
        for row in rows:
            region_id = int(getattr(row, "region_id", 0) or 0)
            region_area = region_areas.get(region_id)
            group_area = group_areas.get(vk_id)
            # Наша собственная главная группа: её регистрацию в своём же районе
            # выключать нельзя ни при каком раскладе.
            if own_region_groups.get(region_id) == vk_id:
                keep_ids.append(int(row.id))
                continue
            if region_area and group_area:
                if region_area == group_area:
                    keep_ids.append(int(row.id))
                else:
                    drop_ids.append(int(row.id))
            else:
                unknown_ids.append(int(row.id))

        if keep_ids:
            status = "ok"
            drop = drop_ids
            keep = keep_ids + unknown_ids
        else:
            # Своей регистрации нет — не решаем, где группа «наша».
            status = "unknown" if unknown_ids else "no_match"
            drop = []
            keep = keep_ids + drop_ids + unknown_ids
        plan[vk_id] = {
            "rows": rows,
            "keep": keep,
            "drop": drop,
            "status": status,
        }
    return plan


async def _run(dry_run: bool, reason: str) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Community, Region

    async with AsyncSessionLocal() as session:
        regions = {int(r.id): r for r in (await session.execute(select(Region))).scalars().all()}
        rows = list(
            (await session.execute(select(Community).where(Community.is_active.is_(True))))
            .scalars()
            .all()
        )

    shared, duplicates = find_shared_and_duplicates(rows)
    own_region_groups = {
        int(rid): abs(int(r.vk_group_id))
        for rid, r in regions.items()
        if getattr(r, "vk_group_id", None)
    }

    # --- область каждой группы: VK (город) → область -------------------
    group_areas: Dict[int, Optional[str]] = {}
    client = None
    vk_error = ""
    if shared:
        try:
            from modules.vk_monitor.vk_client import VKClient
            from modules.vk_token_router import pick_healthy_read_token

            async with AsyncSessionLocal() as session:
                candidate = await pick_healthy_read_token(session)
            token = getattr(candidate, "token", None) if candidate else None
            if not token:
                from config.runtime import VK_TOKENS

                token = next(iter(VK_TOKENS.values()), None)
            if token:
                client = VKClient(token=token)
                ids = sorted(shared.keys())
                infos = await asyncio.to_thread(client.get_groups_by_ids, ids, fields="city")
                city_ids = set()
                for info in infos or []:
                    city = (info or {}).get("city") or {}
                    if city.get("id"):
                        city_ids.add(int(city["id"]))
                cities = (
                    await asyncio.to_thread(client.get_cities_by_ids, sorted(city_ids))
                    if city_ids
                    else {}
                )
                for info in infos or []:
                    city = (info or {}).get("city") or {}
                    gid = abs(int((info or {}).get("id") or 0))
                    area = None
                    if city.get("id"):
                        area = (cities.get(int(city["id"])) or {}).get("area") or None
                    group_areas[gid] = area
            else:
                vk_error = "нет VK-токена"
        except Exception as exc:  # noqa: BLE001 — без VK решение не принимаем
            vk_error = "%s: %s" % (type(exc).__name__, exc)

    region_areas: Dict[int, Optional[str]] = {}
    if client is not None:
        async with AsyncSessionLocal() as session:
            region_areas = await _region_areas(session, regions, client)

    plan = decide_shared(
        shared,
        region_areas=region_areas,
        group_areas=group_areas,
        own_region_groups=own_region_groups,
    )

    # --- что выключаем ---------------------------------------------------
    to_deactivate: List[Dict[str, Any]] = []
    for vk_id, item in plan.items():
        for row in item["rows"]:
            if int(row.id) in item["drop"]:
                to_deactivate.append(
                    {
                        "id": int(row.id),
                        "vk_id": vk_id,
                        "region_id": int(row.region_id),
                        "reason": reason,
                        "name": str(getattr(row, "name", "") or "")[:70],
                    }
                )

    # дубль строки в одном регионе: оставляем строку с большей историей
    for (vk_id, region_id), same_rows in duplicates.items():
        survivor = pick_duplicate_survivor(same_rows)
        for row in same_rows:
            if int(row.id) == int(survivor.id):
                continue
            if any(d["id"] == int(row.id) for d in to_deactivate):
                continue
            to_deactivate.append(
                {
                    "id": int(row.id),
                    "vk_id": vk_id,
                    "region_id": region_id,
                    "reason": REASON_DUPLICATE,
                    "name": str(getattr(row, "name", "") or "")[:70],
                }
            )

    # регион test: его регистрации — мусор в проде
    test_region = regions.get(
        next(
            (rid for rid, r in regions.items() if r.code == TEST_REGION_CODE),
            -1,
        )
    )
    if test_region is not None:
        for row in rows:
            if int(row.region_id) != int(test_region.id):
                continue
            if any(d["id"] == int(row.id) for d in to_deactivate):
                continue
            to_deactivate.append(
                {
                    "id": int(row.id),
                    "vk_id": abs(int(row.vk_id)),
                    "region_id": int(test_region.id),
                    "reason": REASON_TEST_REGION,
                    "name": str(getattr(row, "name", "") or "")[:70],
                }
            )

    written = 0
    if not dry_run and to_deactivate:
        async with AsyncSessionLocal() as session:
            now = datetime.utcnow()
            ids = [int(d["id"]) for d in to_deactivate]
            fresh = list(
                (await session.execute(select(Community).where(Community.id.in_(ids))))
                .scalars()
                .all()
            )
            reasons_by_id = {int(d["id"]): d["reason"] for d in to_deactivate}
            for row in fresh:
                row.is_active = False
                row.disabled_at = now
                row.disabled_reason = reasons_by_id.get(int(row.id), reason)
            await session.commit()
            written = len(fresh)

    return {
        "dry_run": dry_run,
        "communities_total": len(rows),
        "shared_groups": len(shared),
        "shared_registrations": sum(len(v) for v in shared.values()),
        "duplicate_groups": len(duplicates),
        "unresolved_groups": sum(
            1 for v in plan.values() if v["status"] in ("unknown", "no_match")
        ),
        "vk_error": vk_error,
        "to_deactivate": to_deactivate,
        "written": written,
    }


def _print_text(report: Dict[str, Any]) -> None:
    print("активных сообществ: %d" % report["communities_total"])
    print(
        "общих групп: %d (регистраций %d); дублей внутри региона: %d; неразобранных: %d"
        % (
            report["shared_groups"],
            report["shared_registrations"],
            report["duplicate_groups"],
            report["unresolved_groups"],
        )
    )
    if report["vk_error"]:
        print(
            "⚠️  VK: %s — область групп не определена, такие строки оставлены как есть"
            % report["vk_error"]
        )
    print("\nвыключить: %d" % len(report["to_deactivate"]))
    for item in report["to_deactivate"][:200]:
        print(
            "  OFF id=%-7d %-12d region=%-6d %-22s %s"
            % (
                item["id"],
                item["vk_id"],
                item["region_id"],
                item["reason"],
                item["name"],
            )
        )
    if len(report["to_deactivate"]) > 200:
        print("  … ещё %d" % (len(report["to_deactivate"]) - 200))
    if report["dry_run"]:
        print("\n[DRY-RUN] ничего не записано. Применить: --apply")
    else:
        print("\n[WRITTEN] выключено %d" % report["written"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Выключить лишние регистрации групп, заведённых в нескольких районах"
    )
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию превью)")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--reason", default=REASON_SHARED, help="disabled_reason")
    args = ap.parse_args()

    try:
        report = asyncio.run(_run(dry_run=not args.apply, reason=args.reason))
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
