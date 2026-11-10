"""Мост к локальному движку: выгрузка батча и приём вердиктов (схема Pull).

**Что это.** Локальная модель живёт на ПК владельца, а прод — на боксе. Связь
Pull: демон на ПК **сам** забирает батч постов, считает локально и отдаёт
вердикты обратно. Домашний ПК не нужно делать видимым из интернета.

**Две команды, которые нужны мозгу** (мандат 09.11):

* :func:`export_batch` — отдать N постов без вердикта + **тот же самый промпт**,
  какой получает облачный движок (системное сообщение = задача + эффективные
  постулаты, пользовательское = посты батча). Тождество промпта — условие
  честного сравнения: локальная модель должна отвечать на тот же вопрос;
* :func:`submit_batch` — записать вердикты **нашим кодом** (идемпотентно по
  `lip`, чужие `lip` отбрасываются).

**Где живут вердикты.** Таблица ``content_classifications`` — та же, что и у
облачного движка, но с ``source="local"``. Ключ ``lip`` уникален, поэтому
«кто первый» и решает, кто классифицировал пост: если локальный демон не
успел, пост забирает облачный headless — это и есть требуемый фолбэк
(DeepSeek подключается, когда локальная недоступна или пропустила волну).

**Панель.** Каждый прогон демона оставляет отчёт в JSONL-журнале
(``logs/local_engine_runs.jsonl``) — по нему считается статистика в веб-панели
(:func:`stats`): что сделал локальный движок, сколько, когда и с каким
качеством.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, literal_column, select

import database.models  # noqa: F401 — регистрация моделей: без неё не собрать запросы
from database.models_extended import (
    ClassificationCorrection,
    ClassificationRule,
    ContentClassification,
)
from modules.classifier import headless, rules, service
from modules.classifier.schema import ClassifierVerdict, parse_verdict_loose

logger = logging.getLogger(__name__)

#: Источник вердиктов локального движка в ``content_classifications.source``.
SOURCE_LOCAL = "local"

#: Журнал прогонов локального движка (одна строка JSON на прогон).
RUNS_LOG_PATH = Path(
    os.getenv("LOCAL_ENGINE_RUNS_LOG", "") or Path("logs") / "local_engine_runs.jsonl"
)


def _runs_path() -> Path:
    raw = (os.getenv("LOCAL_ENGINE_RUNS_LOG") or "").strip()
    return Path(raw) if raw else RUNS_LOG_PATH


async def export_batch(
    session,
    *,
    limit: int = 20,
    regions: Optional[Sequence[str]] = None,
    regions_per_chunk: int = 8,
    chunk_size: int = 40,
    source_days: int = 3,
    exclude_lips: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Отдать один батч постов без вердикта + промпт, идентичный облачному.

    ``None`` — очередь пуста (нечего классифицировать).

    Батч режется **тем же** чанкером, что и облачный путь
    (:func:`headless.chunk_posts`), чтобы локальная модель получала
    сопоставимые куски, а вызовы считались так же.

    ``exclude_lips`` — посты, которые демон уже посчитал: в режиме тени
    вердикты не пишутся в корпус, поэтому пост остаётся «неразобранным» и
    без исключения демон считал бы один и тот же батч по кругу. Исключение
    применяется **до** нарезки на чанк — иначе промпт и посты разошлись бы.
    """
    region_codes = [r.strip() for r in (regions or []) if r and r.strip()] or None
    posts = await service.fetch_pending(
        session,
        region_codes=region_codes,
        limit=max(1, int(limit)),
        days=int(source_days),
        text_only=True,
    )
    if exclude_lips:
        skip = {str(x) for x in exclude_lips}
        posts = [p for p in posts if str(p.get("lip")) not in skip]
    if not posts:
        return None

    postulates = await rules.render_effective_postulates(session)
    chunks = headless.chunk_posts(posts, chunk_size, regions_per_chunk=regions_per_chunk)
    chunk = chunks[0] if chunks else posts[:1]

    return {
        "batch_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "system": headless.build_system_prompt(postulates),
        "user": headless.build_user_prompt(chunk),
        "posts": [
            {
                "lip": str(p.get("lip")),
                "region_code": str(p.get("region_code") or ""),
                "text": (p.get("text") or p.get("post_text") or "")[:4000],
                "url": str(p.get("url") or p.get("post_url") or "")[:300],
            }
            for p in chunk
        ],
        "pending_total": len(posts),
        "regions_in_batch": sorted({str(p.get("region_code") or "") for p in chunk}),
    }


async def submit_batch(
    session,
    payload: Dict[str, Any],
    *,
    source: str = SOURCE_LOCAL,
    record: bool = True,
) -> Dict[str, Any]:
    """Записать вердикты локального движка (наш код, идемпотентно по ``lip``).

    ``payload``: ``{"verdicts": [...], "run": {...}}``. ``run`` — отчёт о прогоне
    (модель, посты, время, ошибки) — он уходит в журнал прогонов, из него
    питается панель.

    ``record=False`` — **режим тени**: вердикты НЕ пишутся в корпус, только
    отчёт о прогоне. Так демон может работать, не занимая посты и не вытесняя
    облачные вердикты, пока качество локальной модели не измерено (мандат мозга
    09.11: «старт в тени»).
    """
    raw_list = payload.get("verdicts") if isinstance(payload, dict) else None
    if not isinstance(raw_list, list):
        return {"ok": False, "error": "body must be {'verdicts': [...]}"}

    parsed = [parse_verdict_loose(r) for r in raw_list]
    good: List[ClassifierVerdict] = [v for v in parsed if v is not None]
    skipped_invalid = len(parsed) - len(good)

    counts = {"recorded": 0, "skipped_existing": 0, "skipped_missing": 0}
    if good and record:
        counts = await service.record_verdicts(session, good, source=source)

    run = dict(payload.get("run") or {})
    run.update(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "verdicts_sent": len(raw_list),
            "verdicts_parsed": len(good),
            "skipped_invalid": skipped_invalid,
            "shadow": not record,
            **counts,
        }
    )
    append_run(run)
    return {
        "ok": True,
        "skipped_invalid": skipped_invalid,
        "shadow": not record,
        **counts,
        "run": run,
    }


def append_run(run: Dict[str, Any]) -> None:
    """Дописать отчёт о прогоне в JSONL-журнал (создаём при первом прогоне)."""
    path = _runs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — журнал статистики не должен ронять приём вердиктов
        logger.warning("local engine: не удалось записать отчёт о прогоне", exc_info=True)


def read_runs(limit: int = 200) -> List[Dict[str, Any]]:
    """Прочитать последние отчёты о прогонах из журнала."""
    path = _runs_path()
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:  # noqa: BLE001
        logger.warning("local engine: не удалось прочитать журнал прогонов", exc_info=True)
        return []
    return rows[-limit:]


def _trunc(unit: str, column: Any):
    """``date_trunc`` с **литералом** единицы измерения, а не с биндом.

    ``func.date_trunc("day", col)`` SQLAlchemy рендерит как ``date_trunc($1, col)``:
    единица уезжает параметром. Тот же вызов в ``GROUP BY`` получает другой номер
    параметра ($3), и Postgres перестаёт узнавать выражение — «column must appear
    in the GROUP BY clause». Поймано на проде 10.11: 500 на
    ``/api/local-engine/stats``. Литерал даёт одинаковый текст в SELECT, GROUP BY
    и ORDER BY, и группировка проходит.
    """
    if unit not in ("day", "week"):
        raise ValueError("неизвестная единица усечения: %r" % unit)
    return func.date_trunc(literal_column("'%s'" % unit), column)


def _daily_by_source_query():
    """Вердикты по источникам по дням (30 дней)."""
    day = _trunc("day", ContentClassification.created_at)
    return (
        select(
            ContentClassification.source,
            day.label("d"),
            func.count().label("n"),
        )
        .where(ContentClassification.created_at >= datetime.utcnow() - timedelta(days=30))
        .group_by(ContentClassification.source, day)
        .order_by(day)
    )


def _weekly_quality_query():
    """Согласие оператора по источнику по неделям (60 дней)."""
    week = _trunc("week", ClassificationCorrection.created_at)
    return (
        select(
            week.label("w"),
            ContentClassification.source,
            ClassificationCorrection.outcome,
            func.count().label("n"),
        )
        .join(
            ClassificationCorrection,
            ClassificationCorrection.classification_id == ContentClassification.id,
        )
        .where(ClassificationCorrection.created_at >= datetime.utcnow() - timedelta(days=60))
        .group_by(
            week,
            ContentClassification.source,
            ClassificationCorrection.outcome,
        )
        .order_by(week)
    )


def _corrections_by_week_query():
    """Поток коррекций по неделям (60 дней)."""
    week = _trunc("week", ClassificationCorrection.created_at)
    return (
        select(
            week.label("w"),
            func.count().label("n"),
        )
        .where(ClassificationCorrection.created_at >= datetime.utcnow() - timedelta(days=60))
        .group_by(week)
        .order_by(week)
    )


async def stats(session) -> Dict[str, Any]:
    """Статистика для панели: движок, качество, обучение.

    * **Движок** — прогоны локального демона (когда, сколько постов, сколько
      вердиктов, сколько длилось, ошибки);
    * **Качество** — согласие оператора отдельно по облаку и по локальной
      модели (agree-rate по типам) плюс динамика по неделям: растёт ли
      качество;
    * **Обучение** — поток коррекций и выученных правил (предложено /
      утверждено / в отказе), и доля вердиктов по источнику.
    """
    runs = read_runs(limit=500)

    by_source_day = (await session.execute(_daily_by_source_query())).all()

    verdicts_by_source = {str(src or "?"): int(n) for src, _d, n in by_source_day}
    daily: Dict[str, List[Dict[str, Any]]] = {}
    for src, d, n in by_source_day:
        daily.setdefault(str(src or "?"), []).append({"day": str(d)[:10], "n": int(n)})

    # Согласие оператора по источнику: agree / (agree + correct).
    rows = (
        await session.execute(
            select(
                ContentClassification.source,
                ClassificationCorrection.verdict_type,
                ClassificationCorrection.outcome,
                func.count().label("n"),
            )
            .join(
                ClassificationCorrection,
                ClassificationCorrection.classification_id == ContentClassification.id,
            )
            .group_by(
                ContentClassification.source,
                ClassificationCorrection.verdict_type,
                ClassificationCorrection.outcome,
            )
        )
    ).all()
    agree: Dict[str, Dict[str, Dict[str, int]]] = {}
    for src, vtype, outcome, n in rows:
        bucket = agree.setdefault(str(src or "?"), {})
        inner = bucket.setdefault(str(vtype), {"agree": 0, "correct": 0})
        if outcome in inner:
            inner[outcome] = int(n or 0)

    def _pct(a: int, c: int) -> Optional[float]:
        total = a + c
        return round(100.0 * a / total, 1) if total else None

    quality = {
        src: {
            vtype: {
                "agree": vals.get("agree", 0),
                "correct": vals.get("correct", 0),
                "agree_pct": _pct(vals.get("agree", 0), vals.get("correct", 0)),
            }
            for vtype, vals in types.items()
        }
        for src, types in agree.items()
    }

    # Динамика качества по неделям — «растёт ли фильтрация».
    weekly_rows = (await session.execute(_weekly_quality_query())).all()
    weekly: Dict[str, Dict[str, Dict[str, int]]] = {}
    for w, src, outcome, n in weekly_rows:
        week = str(w)[:10]
        inner = weekly.setdefault(week, {}).setdefault(str(src or "?"), {"agree": 0, "correct": 0})
        if outcome in ("agree", "correct"):
            inner[outcome] += int(n or 0)

    # Обучение: коррекции по неделям и судьба выученных правил.
    corr_rows = (await session.execute(_corrections_by_week_query())).all()
    corrections_by_week = [{"week": str(w)[:10], "n": int(n)} for w, n in corr_rows]

    rule_rows = (
        await session.execute(
            select(ClassificationRule.status, func.count().label("n")).group_by(
                ClassificationRule.status
            )
        )
    ).all()

    total_run_posts = sum(int(r.get("verdicts_sent") or 0) for r in runs)
    total_run_recorded = sum(int(r.get("recorded") or 0) for r in runs)
    return {
        "runs": runs[-50:],
        "runs_total": len(runs),
        "runs_posts": total_run_posts,
        "runs_recorded": total_run_recorded,
        "runs_errors": sum(1 for r in runs if r.get("error")),
        "last_run": runs[-1] if runs else None,
        "verdicts_by_source": verdicts_by_source,
        "daily_by_source": daily,
        "quality_by_source": quality,
        "weekly_quality": weekly,
        "corrections_by_week": corrections_by_week,
        "rules_by_status": {str(s or "?"): int(n) for s, n in rule_rows},
    }
