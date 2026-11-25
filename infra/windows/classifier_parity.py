#!/usr/bin/env python3
"""Построчная сверка классификатора: локальный движок против боевых вердиктов.

**Зачем.** Приёмка переезда требует: «один и тот же набор постов
классифицируется локально и прод-движком одинаково — сравнение вердиктов
построчно». Локальная база — полная копия боевой, поэтому в ней лежат
**боевые вердикты** (`content_classifications.verdict`) и снимки постов, на
которых они вынесены. Скрипт берёт те же посты, прогоняет их через
**локальный** движок тем же кодом (`modules/classifier/headless.py`) и
сравнивает вердикты построчно.

**Что сравнивается.** Решающие поля вердикта: `action`, `theme`, `urgent`,
`confidence`. Рассуждение (`reasoning`) не сравнивается — оно формулировка,
а не решение.

**Что нужно, чтобы скрипт дал полный ответ.** Живой движок: DeepSeek API
(ключ из комнаты КАРМАНа) или пул аккаунтов MiniMax Code. Пока ключ не
подключён, скрипт честно печатает `engine_unavailable` и **не** притворяется,
что сверка прошла.

Запуск (из корня репозитория, env-файл стенда подхватывается сам):

    python infra/windows/classifier_parity.py --limit 40
    python infra/windows/classifier_parity.py --limit 40 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Поля вердикта, по которым принимается решение (рассуждение — нет).
DECISION_FIELDS = ("action", "theme", "urgent", "confidence")


def load_env(env_file: str = "") -> None:
    """Подхватить env-файл стенда (тот же путь, что у приложения)."""
    from infra.windows.local_env import load_local_env

    if env_file:
        os.environ["SETKA_ENV_FILES"] = env_file
    load_local_env()


async def fetch_candidates(
    *,
    limit: int,
    region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Посты из аудита сбора, у которых в базе есть боевой вердикт."""
    from sqlalchemy import select

    # models_extended ссылается на Region из models — без её импорта
    # мапперы не собираются (Region не находится по имени).
    from database import models  # noqa: F401
    from database.connection import get_db_session_context
    from database.models_extended import CollectedPostAudit, ContentClassification

    async with get_db_session_context() as session:
        query = (
            select(CollectedPostAudit, ContentClassification)
            .join(
                ContentClassification,
                ContentClassification.lip == CollectedPostAudit.lip,
            )
            .order_by(CollectedPostAudit.collected_at.desc())
        )
        if region:
            query = query.where(CollectedPostAudit.region_code == region)
        rows = (await session.execute(query.limit(limit))).all()

    candidates: List[Dict[str, Any]] = []
    for audit, classification in rows:
        candidates.append(
            {
                "lip": audit.lip,
                "region_code": audit.region_code,
                "theme": audit.theme or "",
                "text": audit.post_text or "",
                "media": audit.media or [],
                "published_at": getattr(audit, "published_at", None),
                "views": getattr(audit, "views", None),
                "likes": getattr(audit, "likes", None),
                "comments": getattr(audit, "comments", None),
                "reposts": getattr(audit, "reposts", None),
                "_prod_verdict": classification.verdict or {},
                "_prod_model": classification.model or "",
            }
        )
    return candidates


async def fetch_postulates() -> str:
    """Рубрика (постулаты) — из локальной базы, тем же кодом, что и на проде.

    Раньше здесь был HTTP-запрос к ``/api/classifier/postulates``, но эта
    ручка закрыта ingest-ключом (а не сессией) — для сверки ключ не нужен:
    рубрика считается прямо из базы тем же кодом
    (``modules.classifier.rules.render_effective_postulates``), что и отдаёт
    боевой рутине.
    """
    from database import models  # noqa: F401
    from database.connection import get_db_session_context
    from modules.classifier.rules import render_effective_postulates

    async with get_db_session_context() as session:
        return await render_effective_postulates(session)


def compare(prod: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
    """Сравнить два вердикта по решающим полям."""
    diffs: Dict[str, Any] = {}
    for field in DECISION_FIELDS:
        left = prod.get(field)
        right = local.get(field)
        if left != right:
            diffs[field] = {"prod": left, "local": right}
    return diffs


async def run(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """Прогон сверки. Всё в одном event loop: пул соединений asyncpg привязан
    к петле, и второй ``asyncio.run`` уронил бы его («Event loop is closed»)."""
    candidates = await fetch_candidates(limit=args.limit, region=args.region or None)
    if not candidates:
        print("Нет постов с боевым вердиктом под выборку — сверять нечего.")
        return None

    try:
        postulates = await fetch_postulates()
    except Exception as exc:  # noqa: BLE001 — причина должна быть видна
        print(f"Не удалось взять рубрику из локальной базы: {exc}")
        return None

    from modules.classifier.headless import classify_posts

    payload = [dict(c) for c in candidates]
    result = classify_posts(payload, postulates=postulates)

    local_by_lip: Dict[str, Any] = {}
    for verdict in result.get("verdicts") or []:
        data = verdict.to_verdict_json() if hasattr(verdict, "to_verdict_json") else dict(verdict)
        local_by_lip[str(data.get("lip") or "")] = data

    rows: List[Dict[str, Any]] = []
    matched = 0
    for candidate in candidates:
        lip = candidate["lip"]
        prod = candidate["_prod_verdict"] or {}
        local = local_by_lip.get(lip)
        if local is None:
            rows.append({"lip": lip, "status": "no_local_verdict", "prod": prod, "local": None})
            continue
        diffs = compare(prod, local)
        if diffs:
            rows.append(
                {"lip": lip, "status": "differs", "prod": prod, "local": local, "diffs": diffs}
            )
        else:
            matched += 1
            rows.append({"lip": lip, "status": "match", "prod": prod, "local": local})

    report = {
        "candidates": len(candidates),
        "engine_failures": result.get("failures") or [],
        "engine_problems": result.get("problems") or [],
        "matched": matched,
        "differed": sum(1 for r in rows if r["status"] == "differs"),
        "no_local_verdict": sum(1 for r in rows if r["status"] == "no_local_verdict"),
        "rows": rows,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return report

    print(
        f"Постов взято: {report['candidates']} | совпало: {matched} | "
        f"расходится: {report['differed']} | без локального вердикта: {report['no_local_verdict']}"
    )
    if report["engine_failures"]:
        print(f"Отказы движка: {report['engine_failures']}")
    print()
    print(f"{'lip':<28} {'статус':<18} {'prod action/theme':<28} {'local action/theme'}")
    print("-" * 120)
    for row in rows:
        prod = row.get("prod") or {}
        local = row.get("local") or {}
        print(
            f"{row['lip']:<28} {row['status']:<18} "
            f"{str(prod.get('action')) + '/' + str(prod.get('theme')):<28} "
            f"{str(local.get('action')) + '/' + str(local.get('theme'))}"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Построчная сверка классификатора: локально против прода"
    )
    parser.add_argument(
        "--limit", type=int, default=40, help="Сколько постов сверять (по умолчанию 40)."
    )
    parser.add_argument("--region", default="", help="Код региона (по умолчанию — все).")
    parser.add_argument("--env-file", default="", help="env-файл стенда.")
    parser.add_argument("--json", action="store_true", help="Вывести только JSON.")
    args = parser.parse_args()

    load_env(args.env_file)
    report = asyncio.run(run(args))
    if report is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
