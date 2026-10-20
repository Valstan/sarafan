#!/usr/bin/env python3
"""Превью вечерней сводки «Короткой строкой» без публикации (P182).

    python scripts/digest_preview.py --region mi

Печатает текст поста, который вышел бы сегодня в 21:40, и список lip'ов,
которые сборник «израсходует». Ничего не публикует, курсор дедупа не двигает,
журнал публикаций не пишет — это read-only прогон на прод-боксе.

Пустой результат — штатный ответ: «кандидатов меньше трёх» или «сборник не
влез». Причина печатается в отчёте (``skipped``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Превью вечерней сводки")
    parser.add_argument("--region", required=True, help="код региона, напр. mi")
    parser.add_argument("--json", action="store_true", help="напечатать отчёт целиком (JSON)")
    args = parser.parse_args()

    from tasks.digest_tasks import publish_daily_digest

    report = publish_daily_digest(region_code=args.region, dry_run=True)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - экзотическая консоль
            pass

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    rows = report.get("reports") or []
    if not rows:
        print("сборника нет: " + json.dumps(report, ensure_ascii=False))
        return 0
    for row in rows:
        if row.get("preview"):
            print(row["preview"])
            print()
            print("—" * 40)
            print(
                f"пунктов: {len(row.get('lips') or [])}, lip'ы: {', '.join(row.get('lips') or [])}"
            )
        else:
            print(f"{row.get('region')}: пропуск ({row.get('skipped') or row.get('error')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
