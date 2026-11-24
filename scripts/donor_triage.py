#!/usr/bin/env python3
"""Разбор доноров: общие группы и молчащие доноры — отчёт и запись решений (P236/P235).

**Что это.** Два хвоста, которые решает владелец:

* **общие группы** — одна VK-группа заведена в нескольких регионах (P236);
* **молчающие доноры** — доноры в темах без слота, которые не публикуются
  вообще (P235).

Модуль ``modules/donor_triage.py`` считает, что можно решить **автоматически**
(по названию группы и нашим населённым пунктам), и отдаёт остальное с
рекомендацией. Этот скрипт — headless-обход того же разбора, что и страница
``/donor-triage``: правила живут в модуле, разойтись не могут.

Запуск на проде (сначала без ``--apply`` — превью):

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
      cd /home/valstan/SETKA && ./venv/bin/python scripts/donor_triage.py'

    --json                      вывод в JSON
    --decisions-out ФАЙЛ        записать рекомендованные решения в JSON (правьте руками)
    --decisions ФАЙЛ            взять решения из файла
    --apply                     записать решения (по умолчанию — только превью)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

#: Корень репозитория в sys.path: скрипт запускают как
#: ``python scripts/donor_triage.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules import donor_triage  # noqa: E402  — после sys.path


async def _fetch_vk_info(vk_ids: List[int], main_group_ids: List[int]) -> Dict[str, Any]:
    """Города и описания групп из VK (``groups.getById fields=city,description``)."""
    cities: Dict[int, str] = {}
    descriptions: Dict[int, str] = {}
    error = ""
    if not vk_ids and not main_group_ids:
        return {"cities": cities, "descriptions": descriptions, "error": "нет групп для опроса"}
    try:
        from database.connection import AsyncSessionLocal
        from modules.vk_monitor.vk_client import VKClient
        from modules.vk_token_router import pick_healthy_read_token

        async with AsyncSessionLocal() as session:
            candidate = await pick_healthy_read_token(session)
        token = getattr(candidate, "token", None) if candidate else None
        if not token:
            from config.runtime import VK_TOKENS

            token = next(iter(VK_TOKENS.values()), None)
        if not token:
            return {"cities": cities, "descriptions": descriptions, "error": "нет VK-токена"}

        client = VKClient(token=token)

        def _read(infos: List[Any]) -> None:
            for info in infos or []:
                city = (info or {}).get("city") or {}
                gid = abs(int((info or {}).get("id") or 0))
                if city.get("title"):
                    cities[gid] = str(city.get("title"))
                if (info or {}).get("description"):
                    descriptions[gid] = str((info or {}).get("description"))

        if main_group_ids:
            _read(await asyncio.to_thread(client.get_groups_by_ids, main_group_ids, fields="city"))
        if vk_ids:
            _read(
                await asyncio.to_thread(client.get_groups_by_ids, vk_ids, fields="city,description")
            )
    except Exception as exc:  # noqa: BLE001 — без VK разбор не падает
        error = "%s: %s" % (type(exc).__name__, exc)
    return {"cities": cities, "descriptions": descriptions, "error": error}


async def _collect() -> Dict[str, Any]:
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

    # Общие группы: одна VK-группа в нескольких регионах.
    regions_by_vk: Dict[int, set] = {}
    for row in rows:
        regions_by_vk.setdefault(abs(int(row.vk_id or 0)), set()).add(int(row.region_id))
    shared_vk = sorted(vk for vk, rids in regions_by_vk.items() if vk and len(rids) > 1)

    main_ids = sorted({abs(int(r.vk_group_id)) for r in regions.values() if r.vk_group_id})
    vk = await _fetch_vk_info(shared_vk, main_ids)
    region_cities = {
        rid: vk["cities"].get(abs(int(r.vk_group_id)), "")
        for rid, r in regions.items()
        if getattr(r, "vk_group_id", None)
    }
    async with AsyncSessionLocal() as session:
        report = await donor_triage.collect_candidates(
            session,
            group_cities=vk["cities"],
            region_cities=region_cities,
            group_descriptions=vk["descriptions"],
        )
    report["vk_error"] = vk["error"]
    return report


def _recommended_decisions(report: Dict[str, Any]) -> Dict[str, Any]:
    """Решения, которые предлагает модуль (точка старта для правки руками)."""
    decisions: Dict[str, Any] = {"shared": {}, "donors": {}}
    for group in report.get("shared", []):
        rec = group.get("recommendation") or {}
        action = rec.get("action")
        if action == "keep_region":
            keep = rec.get("keep_region_ids") or []
            if keep:
                decisions["shared"][str(group["vk_id"])] = {
                    "action": "keep_region",
                    "keep_region_id": int(keep[0]),
                }
        elif action == "keep_all":
            decisions["shared"][str(group["vk_id"])] = {"action": "keep_all"}
    for donor in report.get("donors", []):
        rec = donor.get("recommendation") or {}
        action = rec.get("action")
        if action == "set_theme":
            decisions["donors"][str(donor["row_id"])] = {
                "action": "set_theme",
                "theme": rec.get("theme"),
            }
        elif action == "disable":
            decisions["donors"][str(donor["row_id"])] = {"action": "disable"}
    return decisions


def _print_text(report: Dict[str, Any]) -> None:
    shared = report.get("shared", [])
    donors = report.get("donors", [])
    print(
        "общих групп: %d (решается автоматически: %d, нужен человек: %d)"
        % (
            len(shared),
            sum(1 for g in shared if (g.get("recommendation") or {}).get("action") != "decide"),
            sum(1 for g in shared if (g.get("recommendation") or {}).get("action") == "decide"),
        )
    )
    for group in shared:
        rec = group.get("recommendation") or {}
        print("\n[vk%s] %s" % (group["vk_id"], group["name"][:70]))
        print(
            "   город=%s публикаций=%s → %s (%s): %s"
            % (
                group.get("group_city") or "?",
                group.get("publications", 0),
                rec.get("action"),
                rec.get("confidence"),
                rec.get("reason"),
            )
        )
        for reg in group["regs"]:
            print(
                "     - %-18s %-22s posts=%-4s учеников=%-8s %s"
                % (
                    reg["region_code"],
                    reg["region_name"][:22],
                    reg["posts_count"],
                    reg.get("members"),
                    "СВОЯ" if reg.get("is_own_main") else "",
                )
            )
    print(
        "\nмолчащих доноров: %d (решается автоматически: %d, нужен человек: %d)"
        % (
            len(donors),
            sum(1 for d in donors if (d.get("recommendation") or {}).get("action") != "decide"),
            sum(1 for d in donors if (d.get("recommendation") or {}).get("action") == "decide"),
        )
    )
    for donor in donors:
        rec = donor.get("recommendation") or {}
        print(
            "  %-18s %-14s учеников=%-8s %-42s → %s: %s"
            % (
                donor["region_code"],
                donor["category"],
                donor.get("members"),
                donor["name"][:42],
                rec.get("action"),
                rec.get("reason"),
            )
        )


async def _apply(decisions: Dict[str, Any]) -> Dict[str, Any]:
    from database.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await donor_triage.apply_decisions(session, decisions)


def main() -> int:
    ap = argparse.ArgumentParser(description="Разбор доноров: общие группы и молчащие доноры")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--decisions-out", help="файл для рекомендованных решений")
    ap.add_argument("--decisions", help="файл с решениями (используется с --apply)")
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию — превью)")
    args = ap.parse_args()

    try:
        report = asyncio.run(_collect())
    except Exception as exc:  # noqa: BLE001 — CLI-утилита
        print("FATAL: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    decisions = _recommended_decisions(report)
    if args.decisions_out:
        Path(args.decisions_out).write_text(
            json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("решения записаны: %s" % args.decisions_out)

    if args.json:
        payload = {"report": report, "decisions": decisions}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if report.get("vk_error"):
            print("⚠️  VK: %s" % report["vk_error"])
        _print_text(report)

    if args.apply:
        if not args.decisions:
            print("\n--apply требует --decisions ФАЙЛ", file=sys.stderr)
            return 2
        try:
            with open(args.decisions, encoding="utf-8") as fh:
                payload = json.load(fh)
        except OSError as exc:
            print("FATAL: решения не прочитаны: %s" % exc, file=sys.stderr)
            return 2
        applied = asyncio.run(_apply(payload))
        print(
            "\n[WRITTEN] общие группы: выключено %d; доноры: изменено %d; пропущено %d"
            % (
                len(applied.get("shared", [])),
                len(applied.get("donors", [])),
                len(applied.get("skipped", [])),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
