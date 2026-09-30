#!/usr/bin/env python3
"""Отчёт об источниках рекламных сводок: свои доски, чужие стены, фолбэк.

Заменяет «обещанную запись», которую P023 обещала в мае и не создала: вместо
записи в реестре вопрос получает инструмент с ответом. Три вопроса, на которые
он отвечает числами, а не ощущением:

1. **Есть ли у районов свои доски объявлений** и сколько районов без них —
   от этого зависит, кто пользуется фолбэком «все communities».
2. **Откуда приходят посты в рекламные сводки**: свои доски района или стены
   других категорий/регионов. Чужой источник в `reklama` — это тот случай,
   ради которого P023 и заводилась (инцидент Уржум, бан админа за контент).
3. **Пользовался ли кто-то фолбэком**: фолбэк печатает WARNING в лог, но лог
   читают глазами и он переписывается. Здесь считается по факту публикаций.

Уточнение про «чужие стены»: в `reklama` рекламный фильтр выключен по замыслу,
поэтому в сводку может попасть **репост** поста с любой стены, а атрибуция в
кандидатах указывает на стену оригинала. Это не фолбэк и не чуждой донор — но
именно поэтому «чужие стены» стоит считать и смотреть глазами, а не считать
признаком поломки само по себе.

Запуск на боксе под env приложения (нужна БД):

    sudo -n bash -c 'set -a
        . /etc/setka/secrets-token.env; . /etc/setka/setka.env; set +a
        cd /home/valstan/SETKA
        venv/bin/python scripts/reklama_source_report.py --days 30'

Коды возврата: 0 — отчёт построен (в том числе когда чужих стен нет); 1 — нет БД
или таблиц. Код не зависит от находок: инструмент измеряет, а не судит.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

WALL_RE = re.compile(r"wall(-?\d+)_")
DEFAULT_DAYS = 30


async def collect(days: int) -> Dict[str, Any]:
    from sqlalchemy import select

    import database.models  # noqa: F401 — конфигурация мапперов
    from database.connection import AsyncSessionLocal
    from database.models import Community, Region
    from database.models_extended import BulletinCurationRun

    async with AsyncSessionLocal() as session:
        regions = (
            await session.execute(
                select(Region.id, Region.code, Region.kind).where(Region.is_active.is_(True))
            )
        ).all()
        code_by_id = {r[0]: str(r[1]) for r in regions}

        comms = (
            await session.execute(
                select(
                    Community.region_id,
                    Community.vk_id,
                    Community.category,
                    Community.is_active,
                    Community.name,
                )
            )
        ).all()
        walls: Dict[int, tuple] = {
            abs(int(c[1])): (c[0], str(c[2]), bool(c[3]), str(c[4] or ""), int(c[1])) for c in comms
        }

        own_boards = Counter(c[0] for c in comms if str(c[2]) == "reklama" and bool(c[3]))
        raions = [r for r in regions if str(r[2]) == "raion"]
        without_board = sorted(code_by_id[r[0]] for r in raions if not own_boards.get(r[0]))

        cutoff = datetime.utcnow() - timedelta(days=max(1, days))
        runs = (
            await session.execute(
                select(
                    BulletinCurationRun.region_code,
                    BulletinCurationRun.candidates,
                    BulletinCurationRun.published_post_id,
                    BulletinCurationRun.created_at,
                )
                .where(BulletinCurationRun.theme == "reklama")
                .where(BulletinCurationRun.created_at >= cutoff)
                .order_by(BulletinCurationRun.created_at.desc())
            )
        ).all()

        own_posts = foreign_posts = unknown_posts = 0
        foreign_rows: List[Dict[str, Any]] = []
        per_region = Counter()
        for region_code, candidates, published, created in runs:
            per_region[str(region_code)] += 1
            for cand in candidates or []:
                url = str(cand.get("url") or "")
                match = WALL_RE.search(url)
                info = walls.get(abs(int(match.group(1)))) if match else None
                if info is None:
                    unknown_posts += 1
                    foreign_rows.append(
                        {
                            "region": str(region_code),
                            "category": "?",
                            "wall": url.rsplit("/", 1)[-1],
                            "name": "(стены нет в реестре communities)",
                        }
                    )
                    continue
                if str(info[1]) == "reklama" and code_by_id.get(info[0]) == str(region_code):
                    own_posts += 1
                    continue
                foreign_posts += 1
                foreign_rows.append(
                    {
                        "region": str(region_code),
                        "category": str(info[1]),
                        "wall": str(info[4]),
                        "name": info[3][:44],
                        "own_region": code_by_id.get(info[0]),
                    }
                )

        return {
            "days": days,
            "raions_total": len(raions),
            "raions_with_own_board": len(raions) - len(without_board),
            "raions_without_board": without_board,
            "runs": len(runs),
            "runs_published": sum(1 for r in runs if r[2]),
            "runs_per_region": dict(per_region),
            "own_posts": own_posts,
            "foreign_posts": foreign_posts,
            "unknown_posts": unknown_posts,
            "foreign": foreign_rows[:20],
        }


def render(report: Dict[str, Any]) -> str:
    lines = [
        f"окно {report['days']} сут.",
        f"районов активных: {report['raions_total']} · со своей доской объявлений: "
        f"{report['raions_with_own_board']} · без: {len(report['raions_without_board'])}"
        + (
            f" ({', '.join(report['raions_without_board'])})"
            if report["raions_without_board"]
            else ""
        ),
        f"рекламных сводок: {report['runs']} (опубликовано {report['runs_published']}); "
        f"по регионам: {report['runs_per_region'] or '—'}",
        f"постов в кандидатах: свои доски={report['own_posts']} · "
        f"чужие стены={report['foreign_posts']} · стены вне реестра={report['unknown_posts']}",
    ]
    if report["foreign"]:
        lines.append("чужие стены в реklama (смотреть глазами — это не приговор):")
        for row in report["foreign"]:
            lines.append(
                f"  - регион {row['region']} · стена {row.get('wall', '?')} · "
                f"категория {row['category']} · {row['name']}"
            )
    else:
        lines.append("чужих стен в реklama нет.")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="окно, сутки")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(collect(args.days))
    except Exception as exc:  # noqa: BLE001 — инструмент измеряет, а не падает молча
        print(f"отчёт не построен: {exc}", file=sys.stderr)
        return 1
    print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
