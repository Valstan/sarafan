#!/usr/bin/env python3
"""Вывод выученных правил из обращения — после того, как их содержание влито в базу.

**Зачем.** Выученные правила (БД `classification_rules`) — половина промпта
классификатора. Когда их содержание перенесено в базовый файл
`config/classification_postulates.md`, сами строки в БД становятся дублем: они
платятся в каждом вызове модели, но ничего не добавляют. Их статус меняется на
``retired`` — правило остаётся в БД (мягкий откат, ADR-0005), просто больше не
подаётся в промпт.

**По умолчанию — сухой прогон.** Правка прода меняет то, что видит модель, и
подтверждается владельцем (гейт #025). `--apply` выполняет её.

Запуск на проде:

    # посмотреть, что будет выведено
    sudo bash -c 'set -a; . /etc/setka/setka.env; set +a; cd ~/SETKA && \\
        ./venv/bin/python scripts/retire_learned_rules.py'

    # применить (только после «да» владельца)
    sudo bash -c 'set -a; . /etc/setka/setka.env; set +a; cd ~/SETKA && \\
        ./venv/bin/python scripts/retire_learned_rules.py --apply'
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database.models  # noqa: E402,F401
from database.connection import AsyncSessionLocal  # noqa: E402
from modules.classifier import rules  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="выполнить (без флага — сухой прогон)")
    ap.add_argument("--ids", default="", help="через запятую — только эти id")
    ap.add_argument("--restore", action="store_true", help="вернуть retired → approved")
    args = ap.parse_args()

    wanted = {int(x) for x in args.ids.split(",") if x.strip().isdigit()}

    async with AsyncSessionLocal() as session:
        if args.restore:
            from sqlalchemy import select

            from database.models_extended import ClassificationRule

            rows = (
                (
                    await session.execute(
                        select(ClassificationRule).where(ClassificationRule.status == "retired")
                    )
                )
                .scalars()
                .all()
            )
            targets = [r for r in rows if not wanted or r.id in wanted]
        else:
            rows = await rules.list_rules(session, status="approved", limit=500)
            targets = [r for r in rows if not wanted or r["id"] in wanted]

        if not targets:
            print("нечего делать: подходящих правил нет")
            return 1

        chars = 0
        for r in targets:
            text = r["rule_text"] if isinstance(r, dict) else (r.rule_text or "")
            chars += len(text)
            print(
                "  id=%s len=%d %s"
                % (r["id"] if isinstance(r, dict) else r.id, len(text), (text or "")[:90])
            )
        print("\nправил: %d, символов текста: %d" % (len(targets), chars))

        if not args.apply:
            print("\nсухой прогон. Для применения — с флагом --apply")
            return 0

        status = "approved" if args.restore else "retire"
        done = 0
        for r in targets:
            rule_id = r["id"] if isinstance(r, dict) else r.id
            res = await rules.decide_rule(session, rule_id, status=status)
            if res.get("ok"):
                done += 1
            else:
                print("  id=%s НЕ ВЫВЕДЕН: %s" % (rule_id, res))
        print("обработано правил: %d, статус: %s" % (done, status))

        effective = await rules.render_effective_postulates(session)
        print(
            "эффективные постулаты после: %d символов (~%d токенов)"
            % (len(effective), len(effective) // 2.6)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
