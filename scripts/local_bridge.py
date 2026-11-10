#!/usr/bin/env python3
"""Мост к локальному движку — CLI на боксе: выгрузка батча и приём вердиктов.

Две команды, которые нужны локальному демону (мандат мозга 09.11):

* ``export`` — отдать батч постов без вердикта и **тот же промпт**, какой
  получает облачный движок (системное сообщение + посты). JSON в stdout;
* ``submit`` — принять вердикты локальной модели и записать их нашим кодом
  (идемпотентно по ``lip``, чужие ``lip`` отбрасываются). JSON в stdin.

Обе команды заодно оставляют отчёт о прогоне в журнале
``logs/local_engine_runs.jsonl`` — из него питается веб-панель.

Запуск (на боксе; демон зовёт через ``scripts/local_bridge.sh``):

    ./venv/bin/python scripts/local_bridge.py export --limit 20
    ./venv/bin/python scripts/local_bridge.py submit < verdicts.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# bootstrap_secrets печатает служебные предупреждения в stdout — а stdout здесь
# это МАШИННЫЙ КАНАЛ: демон на ПК разбирает его как JSON. Поэтому на время
# bootstrap'а stdout уводится в stderr, иначе предупреждение о ключе, которого
# нет в allowlist, ломает разбор батча.
with contextlib.redirect_stdout(sys.stderr):
    from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

    bootstrap_secrets()

import database.models  # noqa: E402,F401
from config.classifier import (  # noqa: E402
    get_headless_chunk_size,
    get_headless_regions_per_chunk,
    get_region_allowlist,
    get_source_days,
)
from database.connection import AsyncSessionLocal  # noqa: E402
from modules.classifier import local_engine  # noqa: E402


async def _export(args: argparse.Namespace) -> int:
    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    async with AsyncSessionLocal() as session:
        batch = await local_engine.export_batch(
            session,
            limit=args.limit,
            regions=regions or None,
            regions_per_chunk=args.regions_per_chunk,
            chunk_size=args.chunk,
            source_days=args.days,
        )
    if not batch:
        print(json.dumps({"empty": True}, ensure_ascii=False))
        return 0
    print(json.dumps(batch, ensure_ascii=False))
    return 0


async def _submit(args: argparse.Namespace) -> int:
    raw = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": "не JSON: %s" % e}, ensure_ascii=False))
        return 1
    async with AsyncSessionLocal() as session:
        result = await local_engine.submit_batch(session, payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Мост к локальному движку")
    sub = ap.add_subparsers(dest="cmd", required=True)

    exp = sub.add_parser("export", help="выгрузить батч постов + промпт")
    exp.add_argument("--limit", type=int, default=20, help="сколько постов в батч")
    exp.add_argument("--regions", default="", help="коды районов через запятую (пусто = все)")
    exp.add_argument(
        "--regions-per-chunk", type=int, default=0, help="регионов в вызов (0 = из env)"
    )
    exp.add_argument("--chunk", type=int, default=0, help="постов в вызов (0 = из env)")
    exp.add_argument("--days", type=int, default=0, help="окно свежести источника (0 = из env)")
    exp.set_defaults(func=_export)

    sub_p = sub.add_parser("submit", help="принять вердикты локальной модели")
    sub_p.add_argument("--file", type=argparse.FileType("r", encoding="utf-8"), default=None)
    sub_p.set_defaults(func=_submit)

    args = ap.parse_args()
    if args.cmd == "export":
        if not args.regions_per_chunk:
            args.regions_per_chunk = get_headless_regions_per_chunk()
        if not args.chunk:
            args.chunk = get_headless_chunk_size()
        if not args.days:
            args.days = get_source_days()
        if not args.regions:
            args.regions = ",".join(get_region_allowlist())
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
