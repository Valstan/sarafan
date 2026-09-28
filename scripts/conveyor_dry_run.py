#!/usr/bin/env python3
"""Сухой прогон контент-конвейера по одному сайту — отбор без классификации и доставки.

Шаг мандата D-093 («сухой прогон» перед первой живой доставкой): посмотреть на
живых данных, что именно конвейер собирается отправить на сайт, до того как он
это отправит. Журнал не пишется, наружу ничего не уходит, LLM не вызывается —
работает и при простое DeepSeek.

Запуск на боксе под env приложения (нужен DATABASE_URL из setka.env):

    sudo bash -c 'set -a; . /etc/setka/setka.env; set +a;
                   cd ~/SETKA && venv/bin/python scripts/conveyor_dry_run.py --site sabantuy'

Коды выхода: 0 — прогон выполнен (в т.ч. ноль отобранных — это тоже ответ);
1 — нет такого сайта / нет БД.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run_dry(
    site_key: str,
    *,
    days: Optional[int] = None,
    limit: Optional[int] = None,
    session=None,
) -> Dict[str, Any]:
    """Сухой прогон по сайту. Сессию можно подсунуть (тесты), иначе откроем сами."""
    from config.content_conveyor import get_batch_max, get_site, get_source_days
    from modules.conveyor import runner as runner_mod
    from modules.conveyor.rules import load_rules

    site = get_site(site_key)
    if site is None:
        return {"ok": False, "reason": f"нет сайта с ключом {site_key!r}"}

    async def go(sess):
        rules = load_rules(str(site.get("key") or ""))
        return await runner_mod.run_site(
            sess,
            site,
            rules=rules,
            days=days if days is not None else get_source_days(),
            limit=limit if limit is not None else get_batch_max(),
            dry_run=True,
            sleep=None,
        )

    async def with_own_session():
        from database.connection import AsyncSessionLocal

        async with AsyncSessionLocal() as sess:
            return await go(sess)

    if session is not None:
        stats = asyncio.run(go(session))
    else:
        stats = asyncio.run(with_own_session())
    return {"ok": True, "stats": stats}


def render_human(stats: Dict[str, Any]) -> str:
    """Одна строка итога + превью кандидатов для глаза."""
    lines = [
        f"site={stats.get('site')} selected={stats.get('selected')} "
        f"deduped={stats.get('deduped')}"
    ]
    for p in stats.get("preview") or []:
        lines.append(
            f"  - {p.get('lip')} тема={p.get('theme')} "
            f"символов={p.get('chars')} медиа={p.get('media')}"
        )
    for d in stats.get("preview_duplicates") or []:
        lines.append(f"  = дубль {d.get('lip')} → {d.get('dup_of')}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", required=True, help="ключ сайта из SITES (напр. sabantuy)")
    parser.add_argument("--days", type=int, default=None, help="окно свежести, сутки")
    parser.add_argument("--limit", type=int, default=None, help="потолок постов")
    parser.add_argument("--json", action="store_true", help="вывести сводку JSON")
    args = parser.parse_args(argv)

    out = run_dry(args.site, days=args.days, limit=args.limit)
    if not out.get("ok"):
        print(out["reason"], file=sys.stderr)
        return 1
    stats = out["stats"]
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, default=str))
    else:
        print(render_human(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
