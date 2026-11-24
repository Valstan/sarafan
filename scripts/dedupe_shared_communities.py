#!/usr/bin/env python3
"""Выключить лишние регистрации групп, заведённых в нескольких районах (P236).

**Что чиним.** 71 VK-группа была заведена в ``communities`` сразу для нескольких
регионов (измерение 23.11: 187 регистраций, 116 лишних; к 24.11 активными
остались 26 таких групп). Посты одного донора приходят читателям нескольких
районов (тот же класс, что P216 — утечка чужих районов через агрегатор «ленты
соседей»). Плюс 5 групп заведены **дважды в одном регионе** (дубль строки) и в
проде есть регион ``test``.

**Как решаем, где группа своя — по географии, на том уровне, который данные
позволяют.** У группы VK отдаёт город (``groups.getById fields=city``); у
региона город — это город его **главной группы** (``regions.vk_group_id``,
есть у всех 84 активных регионов). Регистрацию оставляем там, где города
совпали, а лишние (город чужой) выключаем.

⚠️ **Почему не «область», как в discovery.** Вариант «город → область» на проде
**не работает**: ``database.getCities`` по конкретному ``city_id`` отдаёт список
важных городов страны без поля ``area`` (проверено на живом API 24.11), а у
80 из 84 активных регионов пуст ``center_city`` — то есть область региона
неизвестна. Правило «город региона против города группы» — тот же принцип
(география решает), но на том разрешении, которое данные действительно дают, и
оно **строго finer**: отличает район от района, а не область от области.

**«Не знаю» ≠ «чужая».** Если город группы не совпал ни с одним городом
региона (деревня, областной центр) — регистрацию оставляем: это не «лишняя
регистрация», а группа, которую надо смотреть глазами. Если своих регистраций
не нашлось вовсе — ничего не выключаем.

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


def norm_city(title: Optional[str]) -> str:
    """Привести название города к сравнимому виду.

    «г. Котельнич», «Котельнич», «город Котельнич» — это один город. Плюс
    ``ё`` → ``е``: в VK и в БД название пишется по-разному.
    """
    t = (title or "").strip().lower().replace("ё", "е")
    for junk in ("г.", "город", "городская", "пос.", "поселок"):
        t = t.replace(junk, " ")
    return " ".join(t.split())


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


def decide_shared(
    shared: Dict[int, List[Any]],
    *,
    group_cities: Dict[int, str],
    region_cities: Dict[int, str],
    own_region_groups: Dict[int, int],
) -> Dict[int, Dict[str, Any]]:
    """Разметить общие группы: где оставляем, что выключаем.

    Правило: город региона (город его главной группы) совпал с городом группы →
    регистрация **своя**; оба города известны и не совпали → регистрация
    **лишняя**. Город неизвестен — **не гадаем**: оставляем как есть.

    Ключевое условие для выключения: **есть хоть одна своя регистрация**. Тогда
    понятно, где группа живёт, и лишние выключаются. Если своей нет — это уже
    не «лишняя регистрация», а «группа нигде не своя» (деревня или областной
    центр); такое чистить нельзя, отчёт отдаёт группу на разбор.

    Чистая функция — VK и БД не трогает: её и проверяют тесты.
    """
    plan: Dict[int, Dict[str, Any]] = {}
    for vk_id, rows in shared.items():
        keep_ids: List[int] = []
        drop_ids: List[int] = []
        unknown_ids: List[int] = []
        group_city = norm_city(group_cities.get(vk_id))
        for row in rows:
            region_id = int(getattr(row, "region_id", 0) or 0)
            region_city = norm_city(region_cities.get(region_id))
            # Наша собственная главная группа: её регистрацию в своём же районе
            # выключать нельзя ни при каком раскладе.
            if own_region_groups.get(region_id) == vk_id:
                keep_ids.append(int(row.id))
                continue
            if group_city and region_city:
                if group_city == region_city:
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

    # --- город каждой стороны: VK (groups.getById fields=city) -----------
    group_cities: Dict[int, str] = {}
    region_cities: Dict[int, str] = {}
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

                # Город региона — город его главной группы.
                main_ids = sorted(
                    {abs(int(r.vk_group_id)) for r in regions.values() if r.vk_group_id}
                )
                main_infos = await asyncio.to_thread(
                    client.get_groups_by_ids, main_ids, fields="city"
                )
                main_city: Dict[int, str] = {}
                for info in main_infos or []:
                    city = (info or {}).get("city") or {}
                    gid = abs(int((info or {}).get("id") or 0))
                    if city.get("title"):
                        main_city[gid] = str(city.get("title"))
                for rid, r in regions.items():
                    if getattr(r, "vk_group_id", None):
                        region_cities[int(rid)] = main_city.get(abs(int(r.vk_group_id)), "")

                # Город самой группы.
                infos = await asyncio.to_thread(
                    client.get_groups_by_ids, sorted(shared.keys()), fields="city"
                )
                for info in infos or []:
                    city = (info or {}).get("city") or {}
                    gid = abs(int((info or {}).get("id") or 0))
                    group_cities[gid] = str(city.get("title") or "")
            else:
                vk_error = "нет VK-токена"
        except Exception as exc:  # noqa: BLE001 — без VK решение не принимаем
            vk_error = "%s: %s" % (type(exc).__name__, exc)

    plan = decide_shared(
        shared,
        group_cities=group_cities,
        region_cities=region_cities,
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
                        "group_city": group_cities.get(vk_id, ""),
                        "region_city": region_cities.get(int(row.region_id), ""),
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
        "resolved_groups": sum(1 for v in plan.values() if v["status"] == "ok"),
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
        "общих групп: %d (регистраций %d); решается городом: %d; неразобранных: %d; "
        "дублей внутри региона: %d"
        % (
            report["shared_groups"],
            report["shared_registrations"],
            report["resolved_groups"],
            report["unresolved_groups"],
            report["duplicate_groups"],
        )
    )
    if report["vk_error"]:
        print(
            "⚠️  VK: %s — город групп не определён, такие строки оставлены как есть"
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
            + (
                "   [город группы %s / город региона %s]"
                % (item.get("group_city") or "?", item.get("region_city") or "?")
                if item.get("reason") == REASON_SHARED
                else ""
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
