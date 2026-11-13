#!/usr/bin/env python3
"""Выгрузка сигнала дообучения: правки оператора → обучающая выборка + золотая.

**Что это.** Мозг готовит контур дообучения локальной модели (LoRA) на наших
данных, а условие работоспособности локального контура — именно дообучение на
правках оператора (мандат владельца 13.11, D-212). Наш вклад по мандату:
выгрузка обучающей выборки в согласованном формате и проверка качества
обученной модели на нашей золотой выборке.

**Откуда данные.** ``classification_corrections`` — лог реакции оператора на
вердикт (ADR-0003). Сигналом обучения служат только ``outcome='correct'``:
операторское «согласен» ничему не учит — правило из этого не рождается. Плюс
``content_classifications`` (текст поста, вердикт модели) и
``collected_post_audit`` (метрики, вложения, дата публикации).

**Формат.** Обучение идёт на том же промпте, что и на проде: system — ровно
``TASK_PROMPT`` + правилник, user — ровно ``build_user_prompt`` одного поста,
assistant — JSON-вердикт в той же обёртке, которую модель отдаёт на проде.
Иначе модель учится отвечать не тем, о чём её спросят на живом контуре.

**Что в цели.** Правила решения — те, что дал оператор; остальные поля —
как у вердикта модели. Два исключения, оба осознанные:

* ``reasoning`` — пустая строка. Своего обоснования у оператора нет, а
  обоснование модели противоречит исправленному решению: учить на нём —
  значит учить объяснять неверное решение верными словами.
* ``confidence`` — как у модели. Это не сигнал обучения (оператор уверенность
  не выставлял), а корпус целиком состоит из случаев «модель ошиблась» —
  модель наследует завышенную уверенность модели. Поле можно выкинуть при
  дообучении; в манифесте указано, сколько выборок его не имеют.

**Читает, ничего не пишет.** Один ``SELECT`` — ни одной строки в БД не
меняется: ни ``content_classifications``, ни ``classification_corrections``.

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python
        scripts/export_training_sample.py --out ~/SETKA/classifier_run/training'

На выходе: ``train.jsonl`` (обучающая выборка), ``gold.jsonl`` (золотая выборка
с решениями оператора — ею меряется обученная модель), ``manifest.json``
(счётчики, отпечаток правилника, формат, оговорки).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Корень репозитория: обычно — родитель каталога scripts/, но при запуске из
# /tmp (скрипт на боксе лежит рядом с выгрузкой, а код — в ~/SETKA) берём
# текущий каталог. Иначе скрипт искал бы модули в /tmp и не нашёл бы их.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if not (_ROOT / "config").is_dir():
    _ROOT = Path.cwd()
sys.path.insert(0, str(_ROOT))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

from sqlalchemy import select  # noqa: E402

import database.models  # noqa: E402,F401
from database.connection import AsyncSessionLocal  # noqa: E402
from database.models_extended import (  # noqa: E402
    ClassificationCorrection,
    CollectedPostAudit,
    ContentClassification,
)
from modules.classifier import headless, rules  # noqa: E402

#: Каталог выгрузки по умолчанию. Внутри ``classifier_run/`` — он уже в
#: .gitignore: это рабочие файлы прогонов, и класть корпус в публичный
#: репозиторий нельзя (тексты постов и правки оператора — наш внутренний
#: редакционный материал, а не код).
DEFAULT_OUT = "classifier_run/training"
#: Правилник по умолчанию — сжатая версия (ADR-0016), тот же, что уходит в
#: локальный контур.
DEFAULT_RULEBOOK = _ROOT / "config" / "classification_postulates.compact.md"
#: Поля, которые оператор умеет править (ADR-0003 §F).
CORRECTABLE = ("action", "theme", "merge")


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


async def _collect(session, days: int, limit: int) -> List[Dict[str, Any]]:
    """Сырьё: правки оператора + снимок поста + вердикт модели.

    Одна выборка — один ``lip``. По ``(lip, verdict_type)`` берётся **последняя**
    правка: оператор мог передумать, и учить на устаревшей правке — значит
    учить не тому, на чём остановился человек.
    """
    stmt = (
        select(
            ClassificationCorrection.lip,
            ClassificationCorrection.verdict_type,
            ClassificationCorrection.operator_value,
            ClassificationCorrection.ai_value,
            ClassificationCorrection.created_at,
            ContentClassification.region_code,
            ContentClassification.post_text,
            ContentClassification.post_url,
            ContentClassification.verdict,
            CollectedPostAudit.theme,
            CollectedPostAudit.media,
            CollectedPostAudit.published_at,
            CollectedPostAudit.views,
            CollectedPostAudit.likes,
            CollectedPostAudit.comments,
            CollectedPostAudit.reposts,
        )
        .join(
            ContentClassification,
            ClassificationCorrection.classification_id == ContentClassification.id,
        )
        .outerjoin(CollectedPostAudit, CollectedPostAudit.lip == ContentClassification.lip)
        .where(ClassificationCorrection.outcome == "correct")
        .order_by(ClassificationCorrection.created_at, ClassificationCorrection.id)
    )
    if days > 0:
        cutoff = datetime.utcnow() - timedelta(days=days)
        stmt = stmt.where(ClassificationCorrection.created_at >= cutoff)

    rows = (await session.execute(stmt)).all()

    posts: Dict[str, Dict[str, Any]] = {}
    overrides: Dict[str, Dict[str, Any]] = defaultdict(dict)
    corrected_at: Dict[str, datetime] = {}

    for row in rows:
        (
            lip,
            verdict_type,
            operator_value,
            ai_value,
            created_at,
            region_code,
            post_text,
            post_url,
            verdict,
            audit_theme,
            media,
            published_at,
            views,
            likes,
            comments,
            reposts,
        ) = row
        if not lip or verdict_type not in CORRECTABLE:
            continue
        posts.setdefault(
            lip,
            {
                "lip": lip,
                "region_code": region_code or "",
                "post_text": post_text or "",
                "post_url": post_url or "",
                "verdict": dict(verdict or {}),
                "theme_hint": audit_theme or "",
                "media": list(media or []),
                "published_at": published_at,
                "views": views,
                "likes": likes,
                "comments": comments,
                "reposts": reposts,
            },
        )
        overrides[lip][verdict_type] = operator_value
        if created_at is not None:
            corrected_at[lip] = created_at

    out: List[Dict[str, Any]] = []
    for lip, post in posts.items():
        post["overrides"] = overrides[lip]
        post["corrected_at"] = corrected_at.get(lip)
        out.append(post)

    if limit > 0:
        out = out[-limit:]
    return out


def _post_payload(post: Dict[str, Any]) -> Dict[str, Any]:
    """Пост в том виде, в каком его видит модель на проде."""
    return {
        "lip": post["lip"],
        "region_code": post.get("region_code") or "",
        "theme": post.get("theme_hint") or "",
        "media": post.get("media") or [],
        "views": post.get("views"),
        "likes": post.get("likes"),
        "comments": post.get("comments"),
        "reposts": post.get("reposts"),
        "published_at": post.get("published_at"),
        "text": post.get("post_text") or "",
    }


def _target_verdict(post: Dict[str, Any]) -> Dict[str, Any]:
    """Целевой вердикт: решение оператора + остальное от вердикта модели.

    ``merge_with`` в таргете — **пустой список**, кроме случая, когда оператор
    правил склейку сам. Обоснование: пример — один пост, и склеивать его не с
    чем; а ``merge_with`` от модели — это её догадка, а не решение человека
    (в проде код всё равно отбрасывает lip'ы, которых нет в пачке, —
    ``headless.parse_chunk_response``). Учиться на догадке модели — значит
    учить её выдумывать чужие lip'ы.
    """
    ai = post.get("verdict") or {}
    over = post.get("overrides") or {}

    theme = ai.get("theme") or ""
    action = ai.get("action") or "hold"
    merge_with: List[str] = []
    split = bool(ai.get("split"))

    if "theme" in over and over["theme"] is not None:
        theme = str(over["theme"])
    if "action" in over and over["action"] is not None:
        action = str(over["action"])
    if "merge" in over and over["merge"] is not None:
        merge_with, split = _merge_correction(over["merge"], split)

    out: Dict[str, Any] = {
        "lip": post["lip"],
        "theme": str(theme or ""),
        "action": str(action or "hold"),
        "merge_with": merge_with,
        "split": split,
        "confidence": int(ai.get("confidence") or 0),
        "urgent": bool(ai.get("urgent")),
        "importance": int(ai.get("importance") or 0),
        # Обоснования оператора нет — обоснование модели противоречит правке.
        "reasoning": "",
    }
    if ai.get("media_summary"):
        out["media_summary"] = str(ai["media_summary"])
    return out


def _merge_correction(value: Any, split: bool) -> Tuple[List[str], bool]:
    """Правка оператора по склейке → (список lip'ов, признак split).

    Форма в БД неоднородна, и это ровно тот случай, где молчаливое
    приведение учит модель мусору:

    * ``{"split": true}`` — оператор отметил, что пост надо разъединить
      (такой формой пишут все 4 правки склейки в корпусе);
    * список lip'ов — склейка с конкретными постами;
    * строка-флаг (``"split"``) — старый формат.

    Из строки нельзя делать список посимвольно: ``list("split")`` даёт
    ``["s", "p", "l", "i", "t"]`` — и в таргет уезжает мусор, который ни один
    разбор не поймает.
    """
    if isinstance(value, dict):
        lips = value.get("merge_with") or value.get("lips") or []
        split = split or bool(value.get("split"))
        if isinstance(lips, (list, tuple)):
            return [str(x) for x in lips], split
        return [], split
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value], split
    if isinstance(value, str) and value.strip():
        low = value.strip().lower()
        if low in ("split", "разделить", "true", "yes"):
            return [], True
    return [], split


def _assistant_message(verdict: Dict[str, Any]) -> str:
    """Ответ в той же обёртке, что на проде: ``{"verdicts": [...]}``."""
    return json.dumps({"verdicts": [verdict]}, ensure_ascii=False)


async def _rulebook(system_mode: str, rulebook_path: str) -> Tuple[str, Dict[str, Any]]:
    """Правильник для system-сообщения + описание источника.

    Файл читается напрямую, а не через ``config.classifier``: скрипт должен
    работать и на боксе, где код старее этого файла (выгрузка — операция над
    данными, а не часть релиза).
    """
    if system_mode == "effective":
        async with AsyncSessionLocal() as session:
            text = await rules.render_effective_postulates(session)
        meta = {
            "mode": "effective",
            "source": "config/classification_postulates.md + выученные правила из БД",
            "path": "",
            "sha256": "",
        }
        return text, meta
    path = Path(rulebook_path) if rulebook_path else DEFAULT_RULEBOOK
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    meta = {
        "mode": "compact",
        "source": str(path),
        "path": str(path),
        "sha256": _sha256(path),
    }
    return text, meta


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT, help="каталог для файлов выгрузки")
    ap.add_argument(
        "--system",
        choices=["compact", "effective"],
        default="compact",
        help="какой правилник кладём в system-сообщение: "
        "compact — сжатый файл целиком (локальный контур), "
        "effective — действующий (база + живые выученные из БД)",
    )
    ap.add_argument(
        "--rulebook",
        default="",
        help="путь к файлу правилника для режима compact (дефолт — репозиторный)",
    )
    ap.add_argument("--days", type=int, default=0, help="окно правок в сутках (0 — вся история)")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число постов (0 — все)")
    args = ap.parse_args()

    rulebook, rulebook_meta = await _rulebook(args.system, args.rulebook)
    if not rulebook.strip():
        print("правилник пуст — выгружать нечего: модель училась бы вслепую")
        return 1
    system = headless.build_system_prompt(rulebook)

    async with AsyncSessionLocal() as session:
        posts = await _collect(session, args.days, args.limit)

    if not posts:
        print("правок оператора не найдено — сигнала обучения нет")
        return 1

    train_rows: List[Dict[str, Any]] = []
    gold_rows: List[Dict[str, Any]] = []
    with_importance = 0

    for post in posts:
        verdict = _target_verdict(post)
        if (post.get("verdict") or {}).get("importance") is not None:
            with_importance += 1
        user = headless.build_user_prompt([_post_payload(post)])
        train_rows.append(
            {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": _assistant_message(verdict)},
                ]
            }
        )
        over = post.get("overrides") or {}
        gold_rows.append(
            {
                "lip": post["lip"],
                "region_code": post.get("region_code") or "",
                "post_url": post.get("post_url") or "",
                "text": post.get("post_text") or "",
                "expected": {
                    "action": verdict["action"] if "action" in over else None,
                    "theme": verdict["theme"] if "theme" in over else None,
                },
                "ai": {
                    "action": (post.get("verdict") or {}).get("action"),
                    "theme": (post.get("verdict") or {}).get("theme"),
                },
                "corrected": sorted(over.keys()),
                "corrected_at": (
                    post["corrected_at"].isoformat() if post.get("corrected_at") else None
                ),
            }
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "train.jsonl", train_rows)
    _write_jsonl(out_dir / "gold.jsonl", gold_rows)

    gold_action = sum(1 for g in gold_rows if g["expected"]["action"] is not None)
    gold_theme = sum(1 for g in gold_rows if g["expected"]["theme"] is not None)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rulebook": dict(rulebook_meta, chars=len(rulebook)),
        "system_prompt_chars": len(system),
        "counts": {
            "posts_with_correction": len(gold_rows),
            "train_samples": len(train_rows),
            "gold_action": gold_action,
            "gold_theme": gold_theme,
            "gold_action_and_theme": sum(
                1
                for g in gold_rows
                if g["expected"]["action"] is not None and g["expected"]["theme"] is not None
            ),
            "with_importance": with_importance,
        },
        "format": {
            "train.jsonl": "одна строка — один JSON-объект с ключом messages "
            "(system/user/assistant), один пост на пример",
            "gold.jsonl": "золотая выборка: lip, текст, решение оператора "
            "(expected) и вердикт модели (ai)",
            "manifest.json": "этот файл: счётчики, отпечаток правилника, оговорки",
        },
        "notes": [
            "Сигнал — только outcome='correct': согласие оператора ничему не учит.",
            "reasoning пустой: своего обоснования у оператора нет, а "
            "обоснование модели противоречит исправленному решению.",
            "merge_with в таргете пустой (кроме правок склейки самого "
            "оператора): пример — один пост, склеивать не с чем, а merge_with "
            "от модели — её догадка, а не решение человека.",
            "confidence взят у модели: это не сигнал обучения, а корпус целиком "
            "состоит из ошибок модели — поле наследует её завышенную уверенность.",
            "importance подставлен 0 там, где у модели её не было.",
            "system-сообщение совпадает с продовым: TASK_PROMPT + правилник.",
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "out": str(out_dir),
                "posts": len(gold_rows),
                "train_samples": len(train_rows),
                "gold_action": gold_action,
                "gold_theme": gold_theme,
                "system_chars": len(system),
                "rulebook": rulebook_meta["source"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
