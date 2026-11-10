#!/usr/bin/env python3
"""Теневая сверка: локальный движок против облака на ОДНИХ И ТЕХ ЖЕ постах.

**Что это.** Решение владельца 10.11: локальная модель на ПК — основной движок,
DeepSeek по необходимости. Но качество локальной модели на наших постах
неизвестно (мозг мерял на прототипе 18,2 % совпадения с правками оператора
против 37–45 % у облака), а прямое переключение без сверки ухудшит модерацию.

Инструмент меряет честно, на одних и тех же постах:

1. **Выгрузка** (``--dump-sample``) — бокс берёт золотую выборку (посты, где
   оператор ПРАВИЛ вердикт модели), режет её тем же чанкером и тем же промптом,
   что и боевая классификация, **сам считает облачные вердикты** (эталон) и
   выгружает файл: промпты каждого чанка (для ПК) + облачные вердикты +
   решение оператора по каждому посту;
2. **Счёт** (``--score``) — на ПК локальная модель считает те же промпты и
   пишет вердикты; бокс с той же выборкой считает: сколько локальная модель
   совпала с оператором, сколько облако, и попарно — где права одна, где другая.

**Ничего не пишет.** Ни одной строки в ``content_classifications``:
измерение не должно занимать посты и менять то, что enforce блокирует.

Запуск на боксе:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python
        scripts/classifier_local_shadow.py --dump-sample /tmp/shadow_sample.json --limit 120'

    ./venv/bin/python scripts/classifier_local_shadow.py \\
        --score /tmp/shadow_sample.json --local /tmp/local_verdicts.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

from classifier_postulates_ab import _golden_sample, _score  # noqa: E402 — тот же измеритель

import database.models  # noqa: E402,F401
from config.classifier import get_headless_chunk_size, get_headless_regions_per_chunk  # noqa: E402
from database.connection import AsyncSessionLocal  # noqa: E402
from modules.classifier import headless, rules  # noqa: E402
from modules.classifier.schema import parse_verdict_loose  # noqa: E402


def _dump_posts(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Посты для выгрузки: только то, что нужно промпту и сверке."""
    out = []
    for p in posts:
        out.append(
            {
                "lip": str(p.get("lip")),
                "region_code": str(p.get("region_code") or ""),
                "action": p.get("action"),
                "theme": p.get("theme"),
                "text": str(p.get("post_text") or p.get("text") or "")[:4000],
            }
        )
    return out


async def dump_sample(path: str, limit: int, chunk_size: int, regions_per_chunk: int) -> int:
    """Выгрузить выборку, посчитать облачный эталон и записать файл."""
    posts = await _golden_sample(limit)
    if not posts:
        print("золотая выборка пуста — нечего мерить")
        return 1
    print("золотая выборка: %d постов с решением оператора" % len(posts))

    async with AsyncSessionLocal() as session:
        effective = await rules.render_effective_postulates(session)

    system = headless.build_system_prompt(effective)
    chunks = headless.chunk_posts(posts, chunk_size, regions_per_chunk=regions_per_chunk)
    print(
        "промпт: %d символов (~%d токенов); чанков %d на %d постов (регионов в чанке %d)"
        % (len(system), len(system) // 2.6, len(chunks), len(posts), regions_per_chunk)
    )

    dumped_chunks: List[Dict[str, Any]] = []
    cloud_verdicts: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        user = headless.build_user_prompt(chunk)
        result = headless.classify_chunk(chunk, postulates=effective)
        verdicts = result.get("verdicts") or []
        cloud_verdicts.extend(v.model_dump() for v in verdicts)
        dumped_chunks.append(
            {
                "index": index,
                "system": system,
                "user": user,
                "posts": _dump_posts(chunk),
            }
        )
        print(
            "  чанк %d: постов %d, облачных вердиктов %d%s"
            % (
                index,
                len(chunk),
                len(verdicts),
                "" if result.get("ok") else " (ОТКАЗ: %s)" % result.get("reason"),
            ),
            flush=True,
        )

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "posts_total": len(posts),
        "chunk_size": chunk_size,
        "regions_per_chunk": regions_per_chunk,
        "postulates_chars": len(effective),
        "posts": _dump_posts(posts),
        "chunks": dumped_chunks,
        "cloud_verdicts": cloud_verdicts,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print("выгрузка записана: %s (%d байт)" % (path, Path(path).stat().st_size))
    return 0


def _verdicts_from(raw: Any) -> List[Any]:
    """Вердикты из JSON-файла ПК → объекты нашей схемы."""
    if isinstance(raw, dict):
        chunks = raw.get("chunks") or []
    elif isinstance(raw, list):
        chunks = raw
    else:
        chunks = []
    out = []
    for chunk in chunks:
        for item in chunk.get("verdicts") or []:
            verdict = parse_verdict_loose(item)
            if verdict is not None:
                out.append(verdict)
    return out


def score(sample_path: str, local_path: str) -> int:
    """Сравнить облачный и локальный наборы с решением оператора."""
    sample = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    posts = sample.get("posts") or []
    if not posts:
        print("в выгрузке нет постов")
        return 1

    cloud = [v for v in (parse_verdict_loose(x) for x in sample.get("cloud_verdicts") or []) if v]
    local = _verdicts_from(json.loads(Path(local_path).read_text(encoding="utf-8")))

    cloud_score = _score(posts, cloud)
    local_score = _score(posts, local)
    by_cloud = {v.lip: v for v in cloud}
    by_local = {v.lip: v for v in local}

    print("=== облако (эталон) ===")
    print(json.dumps(cloud_score, ensure_ascii=False, indent=2))
    print("=== локальная модель ===")
    print(json.dumps(local_score, ensure_ascii=False, indent=2))

    # Попарно по одним и тем же постам: проценты гуляют на шуме модели, а здесь
    # считается разница по конкретным постам.
    both = only_cloud = only_local = neither = 0
    for p in posts:
        op = str(p.get("action") or "").strip().casefold()
        vc = by_cloud.get(p["lip"])
        vl = by_local.get(p["lip"])
        if not op or not vc or not vl:
            continue
        ok_c = vc.normalized_action() == op
        ok_l = vl.normalized_action() == op
        if ok_c and ok_l:
            both += 1
        elif ok_c:
            only_cloud += 1
        elif ok_l:
            only_local += 1
        else:
            neither += 1
    print(
        "\n=== парная статистика по action ===\n"
        "оба правы: %d; прав только облако: %d; прав только локальная: %d; оба неправы: %d"
        % (both, only_cloud, only_local, neither)
    )

    print("\n=== ИТОГ ===")
    for aspect in ("action", "theme"):
        cv = cloud_score[aspect]["agree_pct"]
        lv = local_score[aspect]["agree_pct"]
        if cv is None or lv is None:
            print("%s: не измерилось" % aspect)
            continue
        print(
            "%s: облако %s%% (%d) → локальная %s%% (%d), дельта %+.1f п.п."
            % (
                aspect,
                cv,
                cloud_score[aspect]["n"],
                lv,
                local_score[aspect]["n"],
                lv - cv,
            )
        )
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Теневая сверка: локальный движок против облака")
    ap.add_argument("--dump-sample", default="", help="выгрузить выборку и облачный эталон в файл")
    ap.add_argument("--score", default="", help="файл выгрузки для счёта")
    ap.add_argument("--local", default="", help="файл вердиктов локальной модели (с ПК)")
    ap.add_argument("--limit", type=int, default=120, help="постов в золотой выборке")
    ap.add_argument("--chunk", type=int, default=0, help="постов в вызов (0 = из env)")
    ap.add_argument(
        "--regions-per-chunk", type=int, default=0, help="регионов в вызов (0 = из env)"
    )
    args = ap.parse_args()

    chunk_size = args.chunk or get_headless_chunk_size()
    regions = args.regions_per_chunk or get_headless_regions_per_chunk()

    if args.dump_sample:
        return await dump_sample(args.dump_sample, args.limit, chunk_size, regions)
    if args.score:
        if not args.local:
            print("нужен --local с вердиктами ПК")
            return 1
        return score(args.score, args.local)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
