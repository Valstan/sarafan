#!/usr/bin/env python3
"""A/B-приёмка сжатого правилника: качество против правок оператора.

**Что это.** Сжатие правилника меняет то, что видит модель. Поэтому качество
измеряется, а не выводится: на ОДНИХ И ТЕХ ЖЕ постах (золотая выборка — правки
оператора) сравниваются два варианта правилника:

* **A** — действующие эффективные постулаты (база + словарь тем + выученные
  правила из БД);
* **B** — сжатая база + тот же словарь тем + те же выученные правила.

Сравниваются: доля совпадений с решением оператора по ``action`` и по ``theme``,
плюс размер промпта в символах и в грубых токенах.

**Приёмка сжатия:** качество варианта B не хуже варианта A. «Не хуже» — по обоим
аспектам; расхождение ниже порога шума считается равным.

**Ничего не пишет.** Ни одной строки в ``content_classifications`` и
``classification_corrections``: приёмка не должна занимать посты и не должна
менять то, что enforce блокирует прямо сейчас.

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python
        scripts/classifier_postulates_ab.py --limit 60'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

import database.models  # noqa: E402,F401
from sqlalchemy import select  # noqa: E402
from config.classifier import get_headless_chunk_size  # noqa: E402
from database.connection import AsyncSessionLocal  # noqa: E402
from database.models_extended import (  # noqa: E402
    ClassificationCorrection,
    ContentClassification,
)
from modules.classifier import headless, rules  # noqa: E402

_THEMES_MARKER = "\n\n## Разрешённые темы (theme)\n\n"


def _approx_tokens(text: str) -> int:
    """Грубая оценка токенов: русский текст — примерно 2,6 символа на токен."""
    return len(text) // 2.6


async def _golden_sample(limit: int) -> List[Dict[str, Any]]:
    """Золотая выборка: посты, где оператор ПРАВИЛ вердикт модели.

    Операторское «согласен» (outcome=agree) в выборку не входит: там нет
    решения, с которым можно сравнить. Для каждого поста помним последнее
    решение оператора по ``action`` и по ``theme``.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ClassificationCorrection.lip,
                    ClassificationCorrection.verdict_type,
                    ClassificationCorrection.operator_value,
                    ClassificationCorrection.created_at,
                )
                .where(
                    ClassificationCorrection.outcome == "correct",
                    ClassificationCorrection.verdict_type.in_(("action", "theme")),
                )
                .order_by(ClassificationCorrection.created_at.desc())
                .limit(limit * 8)
            )
        ).all()

    by_lip: Dict[str, Dict[str, Any]] = {}
    for lip, vtype, value, _created in rows:
        entry = by_lip.setdefault(lip, {"lip": lip, "action": None, "theme": None})
        if entry[vtype] is None:
            entry[vtype] = str(value) if value is not None else None
    lips = list(by_lip.values())[: limit * 3]
    if not lips:
        return []

    async with AsyncSessionLocal() as session:
        cls_rows = (
            (
                await session.execute(
                    select(ContentClassification).where(
                        ContentClassification.lip.in_([x["lip"] for x in lips])
                    )
                )
            )
            .scalars()
            .all()
        )
    snap = {
        r.lip: {
            "region_code": r.region_code,
            "post_text": r.post_text or "",
            "post_url": r.post_url or "",
        }
        for r in cls_rows
    }
    out = []
    for item in lips:
        s = snap.get(item["lip"])
        if not s or not (s["post_text"] or "").strip():
            continue
        item.update(s)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _variant_b(effective: str, compact_base: str) -> str:
    """Сжатая база + словарь тем + выученные правила (тот же хвост)."""
    if _THEMES_MARKER in effective:
        tail = effective.split(_THEMES_MARKER, 1)[1]
        return compact_base.rstrip() + _THEMES_MARKER + tail
    return compact_base.rstrip() + "\n"


def _score(posts: List[Dict[str, Any]], verdicts: List[Any]) -> Dict[str, Any]:
    by_lip = {v.lip: v for v in verdicts}
    action_n = action_hit = theme_n = theme_hit = 0
    for p in posts:
        v = by_lip.get(p["lip"])
        if v is None:
            continue
        if p.get("action"):
            action_n += 1
            if v.normalized_action() == str(p["action"]).strip().casefold():
                action_hit += 1
        if p.get("theme"):
            theme_n += 1
            if v.theme.strip().casefold() == str(p["theme"]).strip().casefold():
                theme_hit += 1
    return {
        "action": {
            "n": action_n,
            "agree_pct": round(100.0 * action_hit / action_n, 1) if action_n else None,
        },
        "theme": {
            "n": theme_n,
            "agree_pct": round(100.0 * theme_hit / theme_n, 1) if theme_n else None,
        },
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60, help="сколько постов в золотой выборке")
    ap.add_argument("--chunk", type=int, default=0, help="постов в вызове (0 = из env)")
    ap.add_argument("--show", type=int, default=12, help="сколько расхождений напечатать")
    ap.add_argument(
        "--compact",
        default="config/classification_postulates.compact.md",
        help="кандидат сжатой базы ( относительно корня репо)",
    )
    args = ap.parse_args()

    posts = await _golden_sample(args.limit)
    if not posts:
        print("золотая выборка пуста — нечего мерить")
        return 1
    print(f"золотая выборка: {len(posts)} постов с решением оператора")

    async with AsyncSessionLocal() as session:
        effective = await rules.render_effective_postulates(session)

    compact_path = Path(__file__).resolve().parent.parent / args.compact
    compact_base = compact_path.read_text(encoding="utf-8")
    variant_b = _variant_b(effective, compact_base)

    print(
        "\nвариант A (действующий): %d символов (~%d токенов)"
        % (len(effective), _approx_tokens(effective))
    )
    print(
        "вариант B (сжатый):      %d символов (~%d токенов)"
        % (len(variant_b), _approx_tokens(variant_b))
    )
    print(
        "экономия промпта: %.1f %%"
        % (100.0 * (len(effective) - len(variant_b)) / max(1, len(effective)))
    )

    chunk = args.chunk or get_headless_chunk_size()
    results = {}
    verdicts_by_variant = {}
    for name, postulates in (("A", effective), ("B", variant_b)):
        run = headless.classify_posts(posts, postulates=postulates, chunk_size=chunk)
        score = _score(posts, run["verdicts"])
        results[name] = score
        verdicts_by_variant[name] = {v.lip: v for v in run["verdicts"]}
        print(f"\n=== вариант {name} ===")
        print("headless:", headless.summarize(run))
        print(json.dumps(score, ensure_ascii=False, indent=2))

    if args.show:
        print("\n=== расхождения (A против оператора, B против оператора) ===")
        for p in posts:
            va = verdicts_by_variant["A"].get(p["lip"])
            vb = verdicts_by_variant["B"].get(p["lip"])
            if not va or not vb:
                continue
            op = str(p.get("action") or "").strip().casefold()
            aa = va.normalized_action()
            bb = vb.normalized_action()
            if aa == bb:
                continue
            marker = " ← расходятся с оператором" if (op and aa != op and bb != op) else ""
            print("-" * 70)
            print(
                "%s [%s] оператор=%s  A=%s  B=%s%s"
                % (p["lip"], p.get("region_code"), p.get("action"), aa, bb, marker)
            )
            print("  A думает: %s" % (va.reasoning or "")[:150])
            print("  B думает: %s" % (vb.reasoning or "")[:150])
            print("  текст: %s" % (p.get("post_text") or "")[:220].replace("\n", " "))

    a, b = results["A"], results["B"]
    print("\n=== ИТОГ ===")
    for aspect in ("action", "theme"):
        av, bv = a[aspect]["agree_pct"], b[aspect]["agree_pct"]
        if av is None or bv is None:
            print(f"{aspect}: не измерилось")
            continue
        delta = round(bv - av, 1)
        verdict = "НЕ ХУЖЕ" if delta >= -1.0 else "ХУЖЕ"
        print(
            f"{aspect}: A={av}% ({a[aspect]['n']}) → B={bv}% ({b[aspect]['n']}), "
            f"дельта {delta:+.1f} п.п. → {verdict}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
