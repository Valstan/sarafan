"""Прямое применение правок выученных правил классификатора (без черновиков).

Штатная петля ADR-0005 ведёт правки через черновики: рутина чеканит `proposed`,
оператор утверждает в панели `/classifier`. Когда владелец решает **вносить
напрямую** (`/distill`, решение 2026-10-19), правка идёт тем же кодом, что кнопка
«утвердить с правкой», — ``decide_rule(..., edited_text=...)``, — только из файла,
а не из UI. Поэтому ``norm_key`` и ``decided_at`` считает приложение, а не человек.

**Гейт #025.** Инструмент пишет в прод-БД, поэтому по умолчанию он read-only:
печатает было/станет и ничего не меняет. Запись — только с ``--apply`` и только
после явного «да» владельца в том же ходе. Правки базового слоя (git-файл
``config/classification_postulates.md``) идут обычным PR — этот скрипт их не трогает.

Файл правок — JSON: ``{"<id правила>": "<новый rule_text>"}``. Ключи, начинающиеся
с ``_``, игнорируются (удобно для комментария-заголовка в файле).

Запуск на проде:

```
ssh sarafan "sudo bash -c 'set -a; . /etc/setka/setka.env; set +a; \\
  cd <корень деплоя> && exec ./venv/bin/python scripts/distill_apply_rules.py <файл правок>'"
```

Без ``--apply`` это dry-run. Выход 0 — всё записано и совпало; 1 — были отказы или
расхождения после записи; 2 — файл правок не прочитан/не прошёл валидацию.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Callable, Dict, List

import database.models  # noqa: F401 — регистрация мапперов (relationships на Region)
from database.connection import AsyncSessionLocal
from database.models_extended import ClassificationRule
from modules.classifier.rules import _MAX_RULE_LEN, decide_rule

RULE_STATUSES_ALLOWED = ("approved",)


def load_edits(path: str) -> Dict[int, str]:
    """Прочитать файл правок: ``{id: текст}`` (ключи с ``_`` — комментарии)."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("ожидался объект {id: текст}")
    edits: Dict[int, str] = {}
    for key, value in raw.items():
        if str(key).startswith("_"):
            continue
        edits[int(key)] = str(value)
    return edits


def validate_edits(edits: Dict[int, str]) -> List[str]:
    """Проблемы, при которых писать нельзя вообще (до похода в БД)."""
    problems: List[str] = []
    if not edits:
        problems.append("файл правок пуст")
    for rule_id, text in sorted(edits.items()):
        if not text.strip():
            problems.append(f"[{rule_id}] пустой текст")
        elif len(text) > _MAX_RULE_LEN:
            problems.append(
                f"[{rule_id}] {len(text)} символов при лимите {_MAX_RULE_LEN} — "
                "decide_rule обрезал бы текст молча"
            )
    return problems


async def apply_edits(
    session,
    edits: Dict[int, str],
    *,
    apply: bool,
    out: Callable[[str], None] = print,
) -> Dict[str, int]:
    """Проверить (и при ``apply`` записать) правки. Возвращает счётчики."""
    stats = {"checked": 0, "applied": 0, "refused": 0, "mismatched": 0}
    for rule_id in sorted(edits):
        new_text = edits[rule_id]
        stats["checked"] += 1
        rule = await session.get(ClassificationRule, rule_id)
        if rule is None:
            out(f"[{rule_id}] правило не найдено — отказ")
            stats["refused"] += 1
            continue
        if rule.status not in RULE_STATUSES_ALLOWED:
            out(
                f"[{rule_id}] status={rule.status}, а инструмент правит только "
                f"{'/'.join(RULE_STATUSES_ALLOWED)} — отказ"
            )
            stats["refused"] += 1
            continue
        old_text = rule.rule_text or ""
        out(f"=== id {rule_id} | len {len(old_text)} → {len(new_text)} (лимит {_MAX_RULE_LEN})")
        out("--- было:\n" + old_text)
        out("--- станет:\n" + new_text)
        if not new_text.strip() or len(new_text) > _MAX_RULE_LEN:
            out(f"[{rule_id}] отказ: текст не проходит валидацию")
            stats["refused"] += 1
            continue
        if not apply:
            continue
        result = await decide_rule(session, rule_id, status="approved", edited_text=new_text)
        if not result.get("ok"):
            out(f"[{rule_id}] decide_rule: {result}")
            stats["refused"] += 1
            continue
        stats["applied"] += 1
    if apply:
        # Успех команды доказывает, что команда отработала, а не что БД в описанном
        # состоянии: перечитываем предмет (правило из прогона уборки бэкапов 09.09).
        for rule_id in sorted(edits):
            rule = await session.get(ClassificationRule, rule_id)
            if rule is None:
                continue
            if (rule.rule_text or "") != edits[rule_id]:
                out(f"[verify {rule_id}] текст в БД не совпал с файлом правок")
                stats["mismatched"] += 1
    return stats


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Прямая правка выученных правил классификатора "
        "(ADR-0005 overlay) без черновиков"
    )
    parser.add_argument("edits", help='JSON-файл правок: {"<id>": "<новый текст>"}')
    parser.add_argument(
        "--apply", action="store_true", help="записать правки в БД (без флага — read-only dry-run)"
    )
    args = parser.parse_args(argv)

    try:
        edits = load_edits(args.edits)
    except (OSError, ValueError, TypeError) as exc:
        print(f"файл правок не прочитан: {exc}")
        return 2
    problems = validate_edits(edits)
    if problems:
        for problem in problems:
            print("отказ:", problem)
        return 2

    print(f"правил в файле: {len(edits)}; режим: {'ЗАПИСЬ' if args.apply else 'dry-run'}")
    async with AsyncSessionLocal() as session:
        stats = await apply_edits(session, edits, apply=args.apply)
    print(f"ИТОГ: {stats}")
    return 1 if stats["refused"] or stats["mismatched"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
