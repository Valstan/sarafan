#!/usr/bin/env python3
"""Одноразовый прогон классификатора движком OpenCode (пилот 14.10).

Тот же поток, что у таски ``classify_pending_posts`` (fetch_pending →
постулаты → classify_posts → record_verdicts), но запускается руками и
печатает разбор прогона, которого таска в лог не даёт:

- гистограмма действий (publish/delete/hold);
- **сомнительные отдельно** — ``hold`` и низкая уверенность (<60): lip, тема,
  причина. Это ровно та очередь, которую владелец разбирает в панели
  ``/classifier`` и правит вердикты (``correction`` → сырьё дистилляции);
- причины отказов чанков — какие модели цепочки молчали.

По умолчанию ``--dry-run``: вердикты НЕ пишутся, только считаются. Живая
запись — явным ``--write`` (как ``/reliz``: гейт перед тем, что касается
публикаций). Движок выбирает ``CLASSIFIER_ENGINE`` (opencode|deepseek).

Пример:
    CLASSIFIER_ENGINE=opencode python scripts/opencode_classify_once.py \
        --region mi --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.classifier import (  # noqa: E402
    classifier_disabled,
    get_pending_max,
    get_source_days,
)
from config.opencode import engine_enabled, get_model_chain  # noqa: E402

DOUBT_CONFIDENCE = 60  # порог «сомнительно»: ниже — в очередь приёмки


async def _run(args: argparse.Namespace) -> Dict[str, Any]:
    if classifier_disabled():
        return {"status": "skipped:classifier-disabled"}

    from database.connection import AsyncSessionLocal
    from modules.classifier import headless, rules, service

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
        postulates = await rules.render_effective_postulates(session)
        run = headless.classify_posts(posts, postulates=postulates)

        verdicts = run.get("verdicts") or []
        histogram = headless.actions_histogram(verdicts)

        doubtful: List[Dict[str, Any]] = []
        for v in verdicts:
            action = v.normalized_action()
            conf = v.confidence if isinstance(v.confidence, int) else None
            if action == "hold" or (conf is not None and conf < DOUBT_CONFIDENCE):
                doubtful.append(
                    {
                        "lip": v.lip,
                        "region": v.region_code,
                        "theme": v.normalized_theme(),
                        "action": action,
                        "confidence": conf,
                        "reasoning": (v.reasoning or "")[:140],
                    }
                )

        stats: Dict[str, Any] = {}
        if args.write:
            stats = await service.record_verdicts(
                session,
                verdicts,
                source="headless",
                region_codes_fallback=args.region or None,
            )
            await session.commit()

        return {
            "status": "ok",
            "engine": "opencode" if engine_enabled() else "deepseek",
            "model_chain": get_model_chain() if engine_enabled() else None,
            "posts": run["posts_in"],
            "chunks": run["chunks"],
            "skipped_no_text": run["skipped_no_text"],
            "actions": histogram,
            "failures": run["failures"],
            "problems": run["problems"][:10],
            "doubtful": len(doubtful),
            "doubtful_list": doubtful[: args.show],
            "tokens": run["tokens"],
            "written": bool(args.write),
            **stats,
        }


def main(argv: List[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--limit", type=int, default=0, help="потолок постов в батче (0 = дефолт конфига)"
    )
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        help="код региона (можно несколько раз); пусто = allowlist сервера",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="записать вердикты в БД (по умолчанию dry-run)",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=25,
        help="сколько сомнительных показать в отчёте",
    )
    args = parser.parse_args(argv)
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
