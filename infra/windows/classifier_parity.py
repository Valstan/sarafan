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

**Что нужно, чтобы скрипт дал полный ответ.** Живой движок. Движок выбирается
переменной `CLASSIFIER_ENGINE` (движок проекта, тот же, что на бою): на локальной
машине это **MiniMax Code CLI** (`minimax`) — та же модель
`MiniMax-M3.1-Flash`, которой вынесены боевые вердикты в скопированной базе,
то есть сверка идёт «та же модель против той же модели». Платный Дипсик на
локальной машине не нужен: страховку стоит выключить
(`CLASSIFIER_PAID_FALLBACK=0`), иначе отказ MiniMax тихо уйдёт в Дипсик, а
вердикта не будет.

Пул аккаунтов MiniMax на локальной машине — `MINIMAX_ACCOUNTS_DIR`
(заводится один раз: `infra/windows/setup_minimax_area.py`).

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


def field_breakdown(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Согласие по каждому решающему полю отдельно.

    **Зачем это важнее, чем «совпало/не совпало».** Строгая сверка по четырём
    полям сразу смешивает две разные вещи: расхождение **решения** (действие
    или тема — то есть публикация поста в ленту) и расхождение **числа**
    (confidence — то есть насколько модель уверена в том же решении). Для
    переезда первое — риск, второе — шум. Поэтому отчёт говорит и то, и то.
    """
    stats: Dict[str, Dict[str, int]] = {
        field: {"agree": 0, "differ": 0, "compared": 0} for field in DECISION_FIELDS
    }
    for row in rows:
        if row.get("status") == "no_local_verdict":
            continue
        prod = row.get("prod") or {}
        local = row.get("local") or {}
        for field in DECISION_FIELDS:
            stats[field]["compared"] += 1
            if prod.get(field) == local.get(field):
                stats[field]["agree"] += 1
            else:
                stats[field]["differ"] += 1
    return stats


def index_verdicts(verdicts: Any) -> Dict[str, Dict[str, Any]]:
    """Собрать локальные вердикты в карту ``lip → решение``.

    **Почему ключ берётся из объекта вердикта, а не из его JSON.** ``lip`` —
    это ключ поста, а не решение: в ``ClassifierVerdict.to_verdict_json()`` его
    нет (там только theme/action/confidence/urgent и прочее решение). Если
    брать ключ из словаря решения, все вердикты схлопываются в одну запись
    ``""`` — и сверка показывает «нет локального вердикта» по каждому посту
    при полном ответе модели (так и случилось 26.11: движок ответил, а отчёт
    был пустым).
    """
    out: Dict[str, Dict[str, Any]] = {}
    for verdict in verdicts or []:
        data = verdict.to_verdict_json() if hasattr(verdict, "to_verdict_json") else dict(verdict)
        lip = str(getattr(verdict, "lip", "") or (data.get("lip") or ""))
        if not lip:
            continue
        out[lip] = data
    return out


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
    # Служебные поля (боевой вердикт) в модель не идут: движок читает из поста
    # только lip/region_code/theme/text/media/метрики, но лишнее в payload
    # мешает читать прогон.
    for item in payload:
        item.pop("_prod_verdict", None)
        item.pop("_prod_model", None)
    result = classify_posts(payload, postulates=postulates)

    local_by_lip = index_verdicts(result.get("verdicts") or [])

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
        "chunks": result.get("chunks"),
        "engine_failures": result.get("failures") or [],
        "engine_problems": result.get("problems") or [],
        "matched": matched,
        "differed": sum(1 for r in rows if r["status"] == "differs"),
        "no_local_verdict": sum(1 for r in rows if r["status"] == "no_local_verdict"),
        "by_field": field_breakdown(rows),
        "rows": rows,
    }

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"Отчёт сохранён: {out_path}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return report

    print(
        f"Постов взято: {report['candidates']} | совпало: {matched} | "
        f"расходится: {report['differed']} | без локального вердикта: {report['no_local_verdict']}"
    )
    print(
        "Согласие по полям: "
        + " | ".join(
            f"{field}: {stats['agree']}/{stats['compared']}"
            for field, stats in report["by_field"].items()
        )
    )
    if report["engine_failures"]:
        print(f"Отказы движка: {report['engine_failures']}")
    # Проблемы разбора — отдельной строкой: раньше они молчали, и сверка
    # выглядела как «модель не ответила», хотя ответ был и был разобран частично.
    if report["engine_problems"]:
        print(f"Проблемы разбора вердиктов ({len(report['engine_problems'])}):")
        for problem in report["engine_problems"][:10]:
            print(f"  - {problem}")
    print()
    print(
        f"{'lip':<28} {'статус':<18} {'prod action/theme':<28} "
        f"{'local action/theme':<28} расхождения"
    )
    print("-" * 150)
    for row in rows:
        prod = row.get("prod") or {}
        local = row.get("local") or {}
        diffs = row.get("diffs") or {}
        diff_text = ", ".join(
            f"{field}({diffs[field]['prod']}→{diffs[field]['local']})" for field in sorted(diffs)
        )
        print(
            f"{row['lip']:<28} {row['status']:<18} "
            f"{str(prod.get('action')) + '/' + str(prod.get('theme')):<28} "
            f"{str(local.get('action')) + '/' + str(local.get('theme')):<28} {diff_text}"
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
    parser.add_argument(
        "--json-out",
        default="",
        help="Куда сохранить полный отчёт в JSON (пусто — не сохранять).",
    )
    parser.add_argument("--json", action="store_true", help="Вывести только JSON.")
    args = parser.parse_args()

    load_env(args.env_file)
    report = asyncio.run(run(args))
    if report is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
