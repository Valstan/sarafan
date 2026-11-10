"""Замер: сколько именно уходит в промпт классификатора (прод, read-only).

Печатает: длину эффективных постулатов (база + темы + выученные правила) в
символах и в грубых токенах, число утверждённых правил и сумму их текста.
Ничего не пишет и не меняет.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

# Порядок импорта важен: реестр SQLAlchemy собирает все модели, а
# ``models_extended`` тянет ``models`` — иначе класс не находится.
import database.models  # noqa: F401
from database.models_extended import ClassificationRule
from modules.classifier.rules import render_effective_postulates


async def main() -> None:
    from database.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        eff = await render_effective_postulates(session)
        rules = (
            (
                await session.execute(
                    select(ClassificationRule).where(ClassificationRule.status == "approved")
                )
            )
            .scalars()
            .all()
        )
        texts = [(r.rule_text or "").strip() for r in rules]
        total_rules = sum(len(t) for t in texts)

    print(f"эффективные постулаты: {len(eff)} символов (~{len(eff) // 2.6:.0f} токенов)")
    print(f"утверждённых правил: {len(rules)}, их текст: {total_rules} символов")
    print(f"доля выученных в общем объёме: {100.0 * total_rules / max(1, len(eff)):.1f} %")
    base = eff.split("## Выученные правила")[0]
    print(f"база+темы: {len(base)} символов")
    lens = sorted(len(t) for t in texts)
    if lens:
        print(f"длина правил: мин {lens[0]}, медиана {lens[len(lens) // 2]}, макс {lens[-1]}")


if __name__ == "__main__":
    asyncio.run(main())
