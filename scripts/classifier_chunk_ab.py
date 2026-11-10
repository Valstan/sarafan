#!/usr/bin/env python3
"""A/B крупных чанков: сколько регионов в одном вызове модели.

**Что это.** Префикс правил платится за **вызов**, а не за пост. При 2–3
постах на регион чанк размера 40 вырождался в «один вызов на регион»: замер
10.11 — 58 постов → 35 вызовов. Вариант с несколькими регионами в одном
вызове даёт вызовов в N раз меньше.

**Что меряется.** На ОДНИХ И ТЕХ ЖЕ постах (золотая выборка — правки
оператора) сравниваются две нарезки при одном и том же правилнике:

* **A** — ``regions_per_chunk=1`` (чанк = один регион, как сейчас);
* **B** — ``regions_per_chunk=N`` (в вызов кладутся N регионов).

Считаются: число вызовов (чанков), токены, отказы чанков (обрыв ответа) и
качество — согласие с решением оператора по ``action`` и ``theme`` плюс
**парная статистика** по одним и тем же постам (она не подменяется шумом
выборки, в отличие от двух процентов).

**Ничего не пишет.** Ни одной строки в ``content_classifications``.

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python
        scripts/classifier_chunk_ab.py --limit 300 --regions 4'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

from classifier_postulates_ab import _golden_sample, _score  # noqa: E402

import database.models  # noqa: E402,F401
from config.classifier import get_headless_chunk_size  # noqa: E402
from database.connection import AsyncSessionLocal  # noqa: E402
from modules.classifier import headless, rules  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300, help="постов в золотой выборке")
    ap.add_argument("--regions", type=int, default=4, help="регионов в вызове (вариант B)")
    ap.add_argument("--chunk", type=int, default=0, help="постов в вызове (0 = из env)")
    args = ap.parse_args()

    posts = await _golden_sample(args.limit)
    if not posts:
        print("золотая выборка пуста")
        return 1
    print(f"золотая выборка: {len(posts)} постов с решением оператора")

    async with AsyncSessionLocal() as session:
        postulates = await rules.render_effective_postulates(session)

    chunk = args.chunk or get_headless_chunk_size()
    variants = (("A (1 регион)", 1), ("B (%d региона/ов)" % args.regions, args.regions))
    runs = {}
    for name, n_regions in variants:
        run = headless.classify_posts(
            posts,
            postulates=postulates,
            chunk_size=chunk,
            regions_per_chunk=n_regions,
        )
        runs[name] = run
        print(f"\n=== {name} ===")
        print("headless:", headless.summarize(run))
        if run["failures"]:
            print("отказы чанков:", run["failures"])
        print(json.dumps(_score(posts, run["verdicts"]), ensure_ascii=False, indent=2))

    (name_a, run_a), (name_b, run_b) = list(runs.items())
    by_lip_a = {v.lip: v for v in run_a["verdicts"]}
    by_lip_b = {v.lip: v for v in run_b["verdicts"]}
    a_right_b_wrong = b_right_a_wrong = 0
    for p in posts:
        op = str(p.get("action") or "").strip().casefold()
        va = by_lip_a.get(p["lip"])
        vb = by_lip_b.get(p["lip"])
        if not op or not va or not vb:
            continue
        if va.normalized_action() == op and vb.normalized_action() != op:
            a_right_b_wrong += 1
        elif vb.normalized_action() == op and va.normalized_action() != op:
            b_right_a_wrong += 1

    print("\n=== ИТОГ ===")
    calls_a, calls_b = run_a["chunks"], run_b["chunks"]
    tok_a, tok_b = run_a["tokens"], run_b["tokens"]
    print(
        "вызовов: %d → %d (%.2f×), токенов: %d → %d (−%.1f %%)"
        % (
            calls_a,
            calls_b,
            (calls_a / calls_b) if calls_b else 0,
            tok_a,
            tok_b,
            100.0 * (tok_a - tok_b) / max(1, tok_a),
        )
    )
    print(
        "парная статистика по action: A прав/B неправ %d, B прав/A неправ %d (разница %+d)"
        % (a_right_b_wrong, b_right_a_wrong, b_right_a_wrong - a_right_b_wrong)
    )
    score_a = _score(posts, run_a["verdicts"])
    score_b = _score(posts, run_b["verdicts"])
    for aspect in ("action", "theme"):
        av = score_a[aspect]["agree_pct"]
        bv = score_b[aspect]["agree_pct"]
        if av is None or bv is None:
            continue
        print("%s: A=%.1f%% → B=%.1f%% (дельта %+.1f п.п.)" % (aspect, av, bv, bv - av))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
