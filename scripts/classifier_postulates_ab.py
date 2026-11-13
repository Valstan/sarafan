#!/usr/bin/env python3
"""A/B-приёмка сжатого правилника: качество против правок оператора.

**Что это.** Сжатие правилника меняет то, что видит модель. Поэтому качество
измеряется, а не выводится: на ОДНИХ И ТЕХ ЖЕ постах (золотая выборка — правки
оператора) сравниваются два варианта правилника:

* **A** — действующие эффективные постулаты (база + словарь тем + выученные
  правила из БД);
* **B** — кандидат: по умолчанию сжатый файл целиком
  (``config/classification_postulates.compact.md``, ADR-0016 — база + выученные
  в одном файле); либо сжатая база + словарь тем + выученные правила из БД
  (режимы ``tail``/``standalone`` — только для кандидата БЕЗ выученного слоя,
  иначе слои задваиваются, и измерялось бы не то, что в бою).

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
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

from sqlalchemy import select  # noqa: E402

import database.models  # noqa: E402,F401
from config.classifier import get_headless_chunk_size  # noqa: E402
from database.connection import AsyncSessionLocal  # noqa: E402
from database.models_extended import ClassificationCorrection, ContentClassification  # noqa: E402
from modules.classifier import headless, rules  # noqa: E402

_THEMES_MARKER = "\n\n## Разрешённые темы (theme)\n\n"
_LEARNED_HEADER = "## Выученные правила"


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


def _variant_b(effective: str, base_text: str, mode: str) -> str:
    """Вариант B: кандидатная база + словарь тем (+ выученные правила, если tail).

    ``tail`` — к кандидату добавляется весь хвост действующих эффективных
    постулатов (словарь тем + выученные правила из БД): так сравнивается
    «сжатая база» с «действующий промпт».

    ``standalone`` — к кандидату добавляется **только словарь тем**, без
    выученных правил: так сравнивается «сводный файл, в который влиты
    выученные правила» с действующим промптом (вывод выученных из БД — за
    владельцем, поэтому вариант считается отдельно).

    ``compactlearned`` — действующая база + словарь тем + **сжатые формулировки**
    выученных правил (из ``config/learned_rules_compact.json``): так измеряется
    вклад сжатия формулировок, а не выноса правил из БД.

    ``merged`` — сжатый файл целиком, как есть: в нём уже есть и база, и
    выученные правила (ADR-0016). Хвост действующих постулатов НЕ добавляется:
    иначе выученные правила попали бы в промпт дважды — из файла и из БД.
    """
    if mode == "merged":
        return base_text.rstrip() + "\n"
    if _LEARNED_HEADER in base_text:
        # Кандидат уже слитый (base + выученные). Добавление хвоста дало бы
        # выученные правила дважды — и замер считал бы не то, что в бою.
        raise ValueError(
            "кандидат уже содержит раздел «Выученные правила» — режимы "
            "tail/standalone добавят их второй раз; для такого файла режим merged"
        )
    if _THEMES_MARKER not in effective:
        return base_text.rstrip() + "\n"
    tail = effective.split(_THEMES_MARKER, 1)[1]
    if mode == "standalone":
        themes_end = tail.find("## Выученные правила")
        themes = tail[:themes_end] if themes_end > 0 else tail
        return base_text.rstrip() + _THEMES_MARKER + themes
    if mode == "compactlearned":
        return _with_compact_learned(effective)
    return base_text.rstrip() + _THEMES_MARKER + tail


def _with_compact_learned(effective: str) -> str:
    """Действующие постулаты, где тексты выученных правил заменены сжатыми.

    Правило в промпте может занимать несколько строк (перенос в исходном
    тексте) — значит, строки собираются блоками: от «N. » до следующего «M. ».
    Иначе хвост правила теряется, и вариант B измеряет не сжатие, а обрезку.
    """
    import json

    path = Path(__file__).resolve().parent.parent / "config" / "learned_rules_compact.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Набор — список в порядке промпта: позиция (номер в промпте) и id строки БД.
    # Сопоставлять по номеру в промпте нельзя: он не совпадает с id (id=3 — это
    # первое правило в промпте, id=60 — тридцать четвёртое). Путаница роняла
    # качество варианта на 8 п.п. — правила просто менялись местами.
    by_pos = {str(item["pos"]): item for item in payload}
    header_at = effective.find("## Выученные правила")
    if header_at < 0:
        return effective
    head = effective[:header_at].rstrip()
    body = effective[header_at:]
    lines = body.splitlines()

    out = [lines[0], ""]
    current_num: Optional[int] = None

    def _flush() -> None:
        if current_num is None:
            return
        item = by_pos.get(str(current_num))
        if item is None:
            return
        scope = ""
        if item.get("region_code"):
            scope = " _(район %s)_" % item["region_code"]
        out.append("%d. %s%s" % (current_num, item["new"].strip(), scope))

    for line in lines[1:]:
        m = re.match(r"^(\d+)\. (.*)$", line)
        if m:
            _flush()
            current_num = int(m.group(1))
        elif current_num is not None:
            # Продолжение правила (перенос строки) — часть того же правила.
            pass
    _flush()
    return head + "\n\n" + "\n".join(out) + "\n"


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
        "--variant-b-mode",
        choices=["tail", "standalone", "compactlearned", "merged"],
        default="merged",
        help="merged — сжатый файл целиком, как есть (ADR-0016, дефолт); "
        "tail — кандидат + словарь тем + выученные правила; "
        "standalone — только кандидат + словарь тем; "
        "compactlearned — действующая база + сжатые формулировки правил",
    )
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

    compact_base = ""
    if args.variant_b_mode != "compactlearned":
        # В режиме compactlearned кандидат не нужен: там B — действующие
        # постулаты, где подменены только тексты выученных правил.
        compact_path = Path(__file__).resolve().parent.parent / args.compact
        compact_base = compact_path.read_text(encoding="utf-8")
    try:
        variant_b = _variant_b(effective, compact_base, args.variant_b_mode)
    except ValueError as exc:
        print("измерять нельзя: %s" % exc)
        return 1

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

    # Парная статистика: она чувствительнее сравнения двух процентов, потому что
    # смотрит на ОДНИ И ТЕ ЖЕ посты. Шум выборки (модель не детерминирована,
    # temperature 0.2) бьёт по обоим процентам сразу, а здесь считается только
    # разница по конкретным постам.
    a_right_b_wrong = b_right_a_wrong = 0
    for p in posts:
        op = str(p.get("action") or "").strip().casefold()
        va = verdicts_by_variant["A"].get(p["lip"])
        vb = verdicts_by_variant["B"].get(p["lip"])
        if not op or not va or not vb:
            continue
        a_ok = va.normalized_action() == op
        b_ok = vb.normalized_action() == op
        if a_ok and not b_ok:
            a_right_b_wrong += 1
        elif b_ok and not a_ok:
            b_right_a_wrong += 1
    print(
        "\n=== парная статистика по action ===\n"
        "A прав, B неправ: %d; B прав, A неправ: %d (разница %+d)"
        % (a_right_b_wrong, b_right_a_wrong, b_right_a_wrong - a_right_b_wrong)
    )

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
