#!/usr/bin/env python3
"""Проба контура MiniMax: пул, авто-чекин, ротация и один реальный вердикт.

**Что это.** Диагностика движка MiniMax Code на живой машине: показывает
состояние каждого аккаунта пула (панель чек-ина), порядок ротации по правилу
Кармана и — по запросу — один настоящий вердикт на постах из очереди.

**Читает, ничего не пишет.** Ни вердиктов в корпус, ни отметок в БД: проба
берёт посты через ``fetch_pending`` (тот же read-only путь, что у рутины) и
зовёт движок напрямую. Единственное, что меняется, — access-токен профиля,
когда он протух (его обновление пишется в токен-стор, а не в корпус).

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/minimax_check.py --verify'

Флаги:
  --verify        сходить до модели и вынести вердикт (без него — только панель и ротация)
  --accounts-dir  каталог пула (по умолчанию берётся из MINIMAX_ACCOUNTS_DIR)
  --posts         сколько постов взять для пробы (по умолчанию 3)
  --json          вывод в JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

import database.models  # noqa: E402,F401 — регионы и прочие модели для мапперов
from database.connection import AsyncSessionLocal  # noqa: E402
from modules.classifier import headless, rules  # noqa: E402
from modules.minimax_engine import (  # noqa: E402
    account_sort_key,
    claim_points,
    discover_accounts,
    signin_panel,
)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Проба контура MiniMax")
    ap.add_argument("--verify", action="store_true", help="сходить до модели и вынести вердикт")
    ap.add_argument("--accounts-dir", default="", help="каталог пула аккаунтов")
    ap.add_argument("--posts", type=int, default=3, help="сколько постов взять для пробы")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args()

    accounts = discover_accounts(args.accounts_dir or None)
    if not accounts:
        print("пул пуст: нет ни одного профиля MiniMax Code с auth.json", file=sys.stderr)
        return 1

    report: Dict[str, Any] = {"accounts": [], "rotation": []}

    for account in accounts:
        panel = signin_panel(account)
        today = {}
        days = ((panel or {}).get("data") or {}).get("days") or []
        for day in days:
            if day.get("is_today"):
                today = day
        entry = {
            "name": account.name,
            "panel": bool(panel),
            "day_no": today.get("day_no"),
            "points": today.get("points"),
            "status": today.get("status"),
            "claimable": today.get("status") == 2,
            "claimed": today.get("status") == 3,
        }
        if args.verify and panel and today.get("status") == 2:
            # Авто-чекин: забрать очки, если клетка позволяет.
            entry["claim"] = claim_points(account)
        report["accounts"].append(entry)

    accounts.sort(key=account_sort_key)
    report["rotation"] = [a.name for a in accounts]

    if args.verify:
        from modules.classifier import service as classifier_service

        async with AsyncSessionLocal() as session:
            pending = await classifier_service.fetch_pending(
                session, limit=max(1, args.posts), days=1, text_only=True
            )
            postulates = await rules.render_effective_postulates(session)
        if not pending:
            report["verdict"] = {"ok": False, "reason": "нет постов без вердикта"}
        else:
            from modules.minimax_engine import chat as minimax_chat

            result = minimax_chat(
                system=headless.build_system_prompt(postulates),
                user=headless.build_user_prompt(pending),
                posts=len(pending),
            )
            payload = headless._extract_json(str(result.get("content") or ""))
            verdicts, problems = (
                headless.parse_chunk_response(payload, pending) if payload else ([], ["нет JSON"])
            )
            report["verdict"] = {
                "ok": bool(result.get("ok")),
                "account": result.get("account"),
                "model": result.get("model"),
                "usage": result.get("usage"),
                "reason": result.get("reason"),
                "problems": problems,
                "verdicts": [
                    {
                        "lip": v.lip,
                        "theme": v.theme,
                        "action": v.action,
                        "confidence": v.confidence,
                        "reasoning": (v.reasoning or "")[:120],
                    }
                    for v in verdicts
                ],
            }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=== пул аккаунтов ===")
    for entry in report["accounts"]:
        print(
            "  %-12s панель=%s день=%s очки=%s статус=%s (%s)"
            % (
                entry["name"],
                "да" if entry["panel"] else "нет",
                entry["day_no"],
                entry["points"],
                entry["status"],
                (
                    "можно забрать"
                    if entry["claimable"]
                    else ("уже забрано" if entry["claimed"] else "нет очков")
                ),
            )
        )
        if "claim" in entry:
            print("      claim:", entry["claim"])
    print("=== порядок ротации ===")
    print("  " + " → ".join(report["rotation"]))
    if "verdict" in report:
        print("=== вердикт ===")
        print(json.dumps(report["verdict"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
