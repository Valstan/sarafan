#!/usr/bin/env python3
"""Сжатые формулировки выученных правил — применить к БД (через код панели).

**Что это.** Выученные правила (34 строки в БД ``classification_rules``) — почти
половина промпта классификатора. Их тексты можно сжать, **не вынося правила из
БД**: правило остаётся правилом оператора, живая петля (ADR-0005) цела, правка —
по-прежнему через панель.

Набор сжатых формулировок лежит в
[`config/learned_rules_compact.json`](../../config/learned_rules_compact.json):
для каждого id — ``old`` (текущий текст, для отката) и ``new`` (сжатый).

**Как применяется.** Тем же кодом, что и кнопка «✏️ изменить текст правила» в
панели: ``rules.decide_rule(..., status="approved", edited_text=...)``. То есть
можно и руками в панели (34 правки), и одной командой отсюда — результат
одинаковый.

**По умолчанию — сухой прогон.** Правка прода меняет то, что видит модель,
и подтверждается владельцем (гейт #025). ``--apply`` выполняет, ``--restore``
возвращает прежние тексты.

Запуск на проде:

    # посмотреть, что изменится
    sudo bash -c 'set -a; . /etc/setka/setka.env; set +a; cd ~/SETKA && \\
        ./venv/bin/python scripts/apply_compact_rules.py'

    # применить (только после «да» владельца)
    sudo bash -c 'set -a; . /etc/setka/setka.env; set +a; cd ~/SETKA && \\
        ./venv/bin/python scripts/apply_compact_rules.py --apply'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database.models  # noqa: E402,F401
from database.connection import AsyncSessionLocal  # noqa: E402
from modules.classifier import rules  # noqa: E402

SET_PATH = Path(__file__).resolve().parent.parent / "config" / "learned_rules_compact.json"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="применить новые тексты")
    ap.add_argument("--restore", action="store_true", help="вернуть прежние тексты")
    ap.add_argument("--ids", default="", help="через запятую — только эти id")
    args = ap.parse_args()

    payload = json.loads(SET_PATH.read_text(encoding="utf-8"))
    wanted = {int(x) for x in args.ids.split(",") if x.strip().isdigit()}

    async with AsyncSessionLocal() as session:
        current = await rules.list_rules(session, status="approved", limit=500)
        by_id = {r["id"]: r for r in current}

        touched = []
        for item in payload:
            rid = int(item["id"])
            if wanted and rid not in wanted:
                continue
            live = by_id.get(rid)
            if live is None:
                print("  id=%s — в БД нет (пропуск)" % rid)
                continue
            new_text = item["new"]
            old_text = item["old"]
            if live["rule_text"].strip() != old_text.strip():
                print(
                    "  id=%s — текст в БД отличается от сохранённого «old», "
                    "правило менялось мимо набора; пропускаю" % rid
                )
                continue
            touched.append((rid, old_text, new_text))

        if not touched:
            print("нечего применять: подходящих правил нет")
            return 1

        print("правил к обработке: %d" % len(touched))
        for rid, old_text, new_text in touched:
            print("\n--- id=%s (%d → %d символов)" % (rid, len(old_text), len(new_text)))
            print("  БЫЛО: %s" % old_text)
            print("  СТАЛО: %s" % new_text)

        before = sum(len(o) for _, o, _ in touched)
        after = sum(len(n) for _, _, n in touched)
        print(
            "\nтекст правил: %d → %d символов (−%d, −%.1f %%)"
            % (before, after, before - after, 100.0 * (before - after) / max(1, before))
        )

        if not args.apply and not args.restore:
            print("\nсухой прогон. Для применения — --apply, для отката — --restore")
            return 0

        done = 0
        for rid, old_text, new_text in touched:
            text = old_text if args.restore else new_text
            res = await rules.decide_rule(session, rid, status="approved", edited_text=text)
            if res.get("ok"):
                done += 1
            else:
                print("  id=%s НЕ ПРИМЕНЕНО: %s" % (rid, res))
        print(
            "\nобработано правил: %d (режим: %s)" % (done, "restore" if args.restore else "apply")
        )

        effective = await rules.render_effective_postulates(session)
        print(
            "эффективные постулаты после: %d символов (~%d токенов)"
            % (len(effective), len(effective) // 2.6)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
