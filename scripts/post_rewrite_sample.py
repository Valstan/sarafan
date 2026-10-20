#!/usr/bin/env python3
"""Показать, как движок переписывает новость перед публикацией (P205, 20.10).

Ручной прогон пересказа: берёт свежие посты района, гоняет через тот же
``modules.publisher.rewriter``, что работает в волне, и печатает исходник и
пересказ рядом — чтобы форму текста можно было оценить глазами, не дожидаясь
публикации. Ничего не пишет и не публикует.

Пример:
    CLASSIFIER_ENGINE=opencode python scripts/post_rewrite_sample.py \
        --region mi --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.classifier import classifier_disabled, get_pending_max, get_source_days  # noqa: E402
from config.post_rewrite import get_max_chars, get_min_chars, get_min_source_chars  # noqa: E402


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    if classifier_disabled():
        return {"status": "skipped:classifier-disabled"}

    import database.models  # noqa: F401 — конфигурация мапперов
    from database.connection import AsyncSessionLocal
    from modules.classifier import service
    from modules.opencode_engine import harness_server
    from modules.publisher.rewriter import rewrite_posts

    async with AsyncSessionLocal() as session:
        posts = await service.fetch_pending(
            session,
            region_codes=args.region or None,
            limit=min(args.limit or get_pending_max(), get_pending_max()),
            days=get_source_days(),
            text_only=True,
        )
        if not posts:
            return {"status": "ok", "posts": 0}

        # Копии: rewriter правит text на месте, а исходник нужен для сравнения.
        work = [dict(p) for p in posts]
        region = args.region[0] if args.region else ""
        with harness_server():
            stats = rewrite_posts(
                work,
                region=region,
                theme=args.theme,
                limit=args.limit or len(work),
            )

        samples: List[Dict[str, Any]] = []
        for src, dst in zip(posts, work):
            if "text_original" not in dst:
                continue
            owner = src.get("owner_id", src.get("from_id"))
            samples.append(
                {
                    "lip": f"{owner}_{src.get('id')}",
                    "source_chars": len(dst["text_original"]),
                    "rewrite_chars": len(dst["text"]),
                    "source": dst["text_original"][: args.show],
                    "rewrite": dst["text"][: args.show],
                }
            )

        return {
            "status": "ok",
            "posts": len(posts),
            "limits": {
                "min_source": get_min_source_chars(),
                "min_chars": get_min_chars(),
                "max_chars": get_max_chars(),
            },
            "stats": stats.as_dict(),
            "samples": samples,
        }


def main(argv: List[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", action="append", help="код региона (можно несколько)")
    parser.add_argument("--limit", type=int, default=3, help="сколько постов взять (дефолт 3)")
    parser.add_argument("--theme", default="новости", help="тема для промпта")
    parser.add_argument("--show", type=int, default=700, help="сколько знаков печатать")
    args = parser.parse_args(argv)

    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
