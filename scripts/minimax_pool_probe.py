#!/usr/bin/env python3
"""Проба контура MiniMax: что сейчас с пулом и куда уходит трафик.

**Зачем.** После разбора 16.11 («половина трафика в Дипсике») нужен инструмент,
которым это видно **сразу и без разборов**: состояние пула, панель аккаунта,
что предлагает Карман, и сколько вызовов съедает аккаунт в сутки. Всё, что
здесь, — только чтение: ни одного токена, ни одной записи в корпус.

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/minimax_pool_probe.py'

    --json    вывод в JSON (для панели и для автоматики)
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

from modules import karman_minimax  # noqa: E402
from modules.minimax_engine import (  # noqa: E402
    ENV_ACCOUNTS_DIR,
    _dead_account_ids,
    _down_seconds,
    _read_profile_meta,
    _shared_meta,
    _usage_log_path,
    accounts_root,
    discover_accounts,
    signin_panel,
)

AREA = os.environ.get("MINIMAX_WORK_AREA") or "work"


def _pool_state() -> dict:
    """Общее состояние пула — из файла рабочей области."""
    meta = _shared_meta()
    return {
        "dry_failures": int(meta.get("dry_failures") or 0),
        "pause_until_ms": int(meta.get("karman_pause_until_ms") or 0),
        "karman_down_until_ms": int(meta.get("karman_down_until_ms") or 0),
        "down_since_ms": int(meta.get("down_since_ms") or 0),
        "pool_dead_until_ms": int(meta.get("pool_dead_until_ms") or 0),
        "dead_account_ids": _dead_account_ids(),
        "seconds_silent": round(_down_seconds(), 1),
    }


def _panels() -> list:
    """Панели всех аккаунтов пула: клетка серии, очки, статус."""
    rows = []
    for account in discover_accounts():
        panel = signin_panel(account, force=True) or {}
        days = (panel.get("data") or {}).get("days") or []
        today = next((d for d in days if d.get("is_today")), {})
        meta = _read_profile_meta(account.root)
        rows.append(
            {
                "name": account.name,
                "email": account.email,
                "karman_id": account.karman_id,
                "today_cell": today.get("day_no"),
                "today_points": today.get("points"),
                "today_status": today.get("status"),
                "exhausted_day": meta.get("exhausted_day"),
                "leased_day": meta.get("leased_day"),
            }
        )
    return rows


def _karman_offer() -> dict:
    """Что Карман предлагает сейчас — без аренды."""
    if not karman_minimax.enabled():
        return {"enabled": False}
    try:
        offer = karman_minimax.take_account()
    except RuntimeError as exc:
        return {"enabled": True, "error": str(exc)[:200]}
    return {
        "enabled": True,
        "free": offer.free,
        "soon": offer.soon,
        "reason": offer.reason,
        "tier": offer.tier,
        "why": offer.why,
        "account_id": (offer.account or {}).get("id"),
        "account_email": (offer.account or {}).get("email"),
        "has_access_token": offer.has_tokens,
        "has_refresh_token": offer.has_refresh,
    }


def _usage(days: int = 3) -> dict:
    """Журнал расхода: вызовы по суткам и причины отказов."""
    path = _usage_log_path()
    if not path.exists():
        return {"log": str(path), "exists": False}
    ok_by_day = collections.Counter()
    fail_by_reason = collections.Counter()
    per_account = collections.Counter()
    tokens = 0
    calls = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 — битая строка журнала не повод падать
                continue
            day = str(row.get("ts") or "")[:10]
            if row.get("ok"):
                ok_by_day[day] += 1
                per_account[str(row.get("account"))] += 1
                usage = row.get("usage") or {}
                if isinstance(usage, dict):
                    tokens += int(usage.get("totalTokens") or 0)
                calls += 1
            else:
                fail_by_reason[str(row.get("reason") or "?")] += 1
    return {
        "log": str(path),
        "exists": True,
        "ok_by_day": dict(sorted(ok_by_day.items())[-days:]),
        "fail_by_reason": dict(fail_by_reason.most_common()),
        "ok_by_account": dict(per_account.most_common(5)),
        "avg_total_tokens": round(tokens / calls) if calls else 0,
    }


def collect() -> dict:
    return {
        "accounts_dir": os.getenv(ENV_ACCOUNTS_DIR) or str(accounts_root()),
        "work_area": AREA,
        "pool": _pool_state(),
        "accounts": _panels(),
        "karman": _karman_offer(),
        "usage": _usage(),
    }


def _print(data: dict) -> None:
    print("=== ПУЛ (общее состояние) ===")
    pool = data["pool"]
    print("  каталог пула: %s (рабочая область: %s)" % (data["accounts_dir"], data["work_area"]))
    print("  пустых аккаунтов подряд: %d" % pool["dry_failures"])
    print("  секунд молчания: %.0f" % pool["seconds_silent"])
    print("  пул признан мёртвым до: %s" % (pool["pool_dead_until_ms"] or "нет"))
    print("  мёртвые аккаунты (в exclude): %s" % (pool["dead_account_ids"] or "нет"))

    print("=== АККАУНТЫ ПУЛА ===")
    if not data["accounts"]:
        print("  (пул пуст — работаем по аренде у Кармана)")
    for row in data["accounts"]:
        print(
            "  %-8s id=%-5s сегодня: клетка %s, %s очков, статус %s%s"
            % (
                row["name"],
                row["karman_id"],
                row["today_cell"],
                row["today_points"],
                row["today_status"],
                " (исчерпан сегодня)" if row["exhausted_day"] else "",
            )
        )
    print(
        "  ⚠️ «статус 3» = дневная клетка **забрана**, а не «баланс есть»."
        " Модель отвечает «insufficient balance (1008)», когда баланс уже ноль."
    )

    print("=== КАРМАН ===")
    karman = data["karman"]
    if not karman.get("enabled"):
        print("  ручки пула не настроены")
    elif karman.get("error"):
        print("  Карман не ответил: %s" % karman["error"])
    else:
        print(
            "  свободно %s, скоро %s, причина %s"
            % (karman.get("free"), karman.get("soon"), karman.get("reason") or "—")
        )
        print("  %s" % (karman.get("why") or ""))
        print(
            "  предлагает аккаунт %s (%s): access=%s refresh=%s"
            % (
                karman.get("account_id"),
                karman.get("account_email"),
                karman.get("has_access_token"),
                karman.get("has_refresh_token"),
            )
        )

    print("=== РАСХОД (журнал вызовов) ===")
    usage = data["usage"]
    if not usage.get("exists"):
        print("  журнала нет: %s" % usage.get("log"))
        return
    print("  успешных вызовов по суткам: %s" % usage["ok_by_day"])
    print("  отказов по причинам: %s" % usage["fail_by_reason"])
    print("  успешных по аккаунтам: %s" % usage["ok_by_account"])
    print("  средний totalTokens на вызов: %s" % usage["avg_total_tokens"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Проба контура MiniMax (только чтение)")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    args = parser.parse_args()

    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
