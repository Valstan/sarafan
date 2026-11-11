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

#: Человеческие названия **путей доставки** вердикта. ``source`` в базе — служебный
#: ключ (``routine`` / ``api`` / ``headless`` / ``local``), и в панели он должен
#: читаться, а не шифровать.
#:
#: ⚠️ **Путь — это не движок.** Одна и та же волна (``headless``) умеет считать и
#: платным Дипсиком, и бесплатным Опенкодом: движок выбирает ``CLASSIFIER_ENGINE``.
#: Панель, которая зовёт «облако» = «Дипсик», врёт на 23 813 вердиктов за месяц —
#: столько набрала бесплатная ветка Опенкода внутри той же волны. Движок лежит в
#: ``content_classifications.model``, и считать его надо по нему (см.
#: :func:`engine_of_model`).
SOURCE_LABELS: Dict[str, str] = {
    "headless": "Волна на сервере",
    "local": "Демон Опенкода",
    "routine": "Старая рутина (удалена)",
    "api": "Приём готовых вердиктов",
}

#: Живой ли путь. ``routine`` — облачная рутина на Клоде: владелец её удалил
#: физически, денег она не тратит и вердиктов больше не пишет. Показывать её
#: живой нельзя — в панели она выглядела как «ещё один источник, который
#: обрабатывает посты».
SOURCE_ALIVE: Dict[str, bool] = {
    "headless": True,
    "local": True,
    "routine": False,
    "api": True,
}

#: По одной строке на путь: что это и откуда взялось.
SOURCE_NOTES: Dict[str, str] = {
    "headless": "часовая волна на боксе. Движок выбирает переключатель "
    "CLASSIFIER_ENGINE: Дипсик (платно) или Опенкод (бесплатно).",
    "local": "Демон Опенкода на боксе: раз в 5 минут забирает посты, которые "
    "остались без вердикта, и считает их бесплатными моделями.",
    "routine": "Облачная рутина на Клоде. Удалена владельцем 11.11, денег не "
    "тратит, вердикты в корпусе — только до 06.11.",
    "api": "Приём готовых вердиктов по POST /api/classifier/verdicts.",
}

# ───────── Движки: кто ответил на вердикт ─────────

#: ``source`` — путь, ``model`` — движок. Движок достаётся из имени модели.
ENGINE_DEEPSEEK = "deepseek"
ENGINE_OPENCODE = "opencode"
ENGINE_UNKNOWN = "unknown"

ENGINE_LABELS: Dict[str, str] = {
    ENGINE_DEEPSEEK: "Дипсик — платно, по ключу",
    ENGINE_OPENCODE: "Опенкод — бесплатный тир",
    ENGINE_UNKNOWN: "неизвестно (движок не записан)",
}

#: Человеческие названия моделей: ключ — как модель лежит в ``model``.
MODEL_LABELS: Dict[str, str] = {
    "deepseek-flash": "Дипсик V4.1 Flash",
    "deepseek-chat": "Дипсик (алиас chat) — снят 06.11",
    "deepseek-v4-pro": "Дипсик V4 Pro — в коде не используется",
    "opencode/deepseek-v4.1-flash-free": "Опенкод: Дипсик V4.1 Flash",
    "opencode/glm-5.3-flash-free": "Опенкод: GLM 5.3 Flash",
    "opencode/longcat-2.5-preview-free": "Опенкод: LongCat 2.5",
    "opencode/space-bunny-free": "Опенкод: Space Bunny",
    "opencode/mimo-v2.6-flash-free": "Опенкод: Mimo v2.6 Flash",
    "deepseek-v4.1-flash-free": "Опенкод: Дипсик V4.1 Flash",
    "flint-40b-free": "Опенкод: Flint 40B",
    "glm-5.3-flash-free": "Опенкод: GLM 5.3 Flash",
}


def engine_of_model(model: Optional[str]) -> str:
    """Какой движок ответил — по имени модели в корпусе.

    Имена в корпусе разные, и это не опечатки, а два разных пути:

    * ``deepseek-flash`` / ``deepseek-chat`` / ``deepseek-v4-pro`` — **платный**
      Дипсик по нашему ключу;
    * ``opencode/<модель>`` — волна, посчитанная бесплатным Опенкодом
      (движок был переключён на free-тир);
    * ``<модель>-free`` без префикса — демон Опенкода на боксе: он зовёт
      ``opencode serve`` и пишет модель как есть;
    * пусто — вердикты, у которых движок не записан (старая рутина).

    Функция чистая: её проверяет тест, а панель рисует по ней разрез «кем
    ответили» — без него «облако» читалось как «Дипсик», хотя в «облаке» за
    месяц было 23 813 вердиктов бесплатного Опенкода.
    """
    name = (model or "").strip().lower()
    if not name:
        return ENGINE_UNKNOWN
    if name.startswith("opencode/"):
        return ENGINE_OPENCODE
    if name.endswith("-free"):
        return ENGINE_OPENCODE
    if name.startswith("deepseek"):
        return ENGINE_DEEPSEEK
    return ENGINE_UNKNOWN


def model_label(model: Optional[str]) -> str:
    """Название модели по-русски; неизвестная — как есть."""
    name = (model or "").strip()
    if not name:
        return "движок не записан"
    return MODEL_LABELS.get(name.lower(), name)


def engine_buckets(rows: Sequence[Any]) -> Dict[str, int]:
    """Вердикты по движку из строк ``(model, n)``.

    Чистая функция — считает то, что панель показывает как «кем отвечено».
    """
    bucket = {ENGINE_DEEPSEEK: 0, ENGINE_OPENCODE: 0, ENGINE_UNKNOWN: 0}
    for model, n in rows:
        bucket[engine_of_model(str(model or ""))] += int(n or 0)
    return bucket


def sum_by_source(rows: Sequence[Any]) -> Dict[str, int]:
    """Сумма вердиктов по пути доставки из строк ``(source, day, n)``.

    Именно сумма: словарь «последняя строка на путь» показывал бы цифры
    одного дня вместо окна — на проде «за 30 дней» выглядело как «за
    вчера».
    """
    total: Dict[str, int] = {}
    for src, _day, n in rows:
        key = str(src or "?")
        total[key] = total.get(key, 0) + int(n or 0)
    return total


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


def _engine_counts_query(days: int):
    """Вердикты по модели за окно — из этого считается, кто отвечал."""
    return (
        select(
            ContentClassification.model,
            func.count().label("n"),
        )
        .where(ContentClassification.created_at >= datetime.utcnow() - timedelta(days=days))
        .group_by(ContentClassification.model)
    )


def _engine_by_day_query(days: int):
    """Вердикты по модели по дням — «кто отвечал» по дням."""
    day = _trunc("day", ContentClassification.created_at)
    return (
        select(
            day.label("d"),
            ContentClassification.model,
            func.count().label("n"),
        )
        .where(ContentClassification.created_at >= datetime.utcnow() - timedelta(days=days))
        .group_by(day, ContentClassification.model)
        .order_by(day)
    )


def _last_verdict_query():
    """Время последнего вердикта в корпусе — «жив ли классификатор»."""
    return select(func.max(ContentClassification.created_at))


def _source_counts_query(days: int):
    """Вердикты по пути доставки за окно — «кто разбирает посты»."""
    return (
        select(
            ContentClassification.source,
            func.count().label("n"),
        )
        .where(ContentClassification.created_at >= datetime.utcnow() - timedelta(days=days))
        .group_by(ContentClassification.source)
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

    # Сумма по дням, а не последняя строка: раньше здесь стоял словарь,
    # который перетирал предыдущие дни, — и «за 30 дней» показывало цифры
    # одного дня (на проде: 4 430 вместо 5 065 за 11.11).
    verdicts_by_source: Dict[str, int] = sum_by_source(by_source_day)
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

    # ── Кто отвечал: движок по имени модели ──
    # Путь (`source`) и движок (`model`) — разные вещи: одна волна считает и
    # платным Дипсиком, и бесплатным Опенкодом. Панель показывает оба среза.
    engine_windows: Dict[str, Dict[str, Any]] = {}
    models_30d: List[Dict[str, Any]] = []
    for win, days in (("24h", 1), ("7d", 7), ("30d", 30)):
        rows = (await session.execute(_engine_counts_query(days))).all()
        bucket = engine_buckets(rows)
        by_model: Dict[str, int] = {}
        for m, n in rows:
            name = str(m or "")
            by_model[name] = by_model.get(name, 0) + int(n or 0)
        engine_windows[win] = {
            "by_engine": bucket,
            "total": sum(bucket.values()),
            "by_model": by_model,
        }
        if win == "30d":
            models_30d = [
                {
                    "model": name,
                    "label": model_label(name),
                    "engine": engine_of_model(name),
                    "n": int(n),
                }
                for name, n in sorted(by_model.items(), key=lambda kv: -kv[1])
            ]

    day_rows = (await session.execute(_engine_by_day_query(30))).all()
    engine_by_day: List[Dict[str, Any]] = []
    day_map: Dict[str, Dict[str, int]] = {}
    for d, m, n in day_rows:
        day = str(d)[:10]
        bucket = day_map.setdefault(
            day, {ENGINE_DEEPSEEK: 0, ENGINE_OPENCODE: 0, ENGINE_UNKNOWN: 0}
        )
        bucket[engine_of_model(str(m or ""))] += int(n or 0)
    for day in sorted(day_map, reverse=True):
        row = {"day": day}
        row.update(day_map[day])
        engine_by_day.append(row)

    last_verdict = (await session.execute(_last_verdict_query())).scalar()
    # Время из БД — **UTC без зоны** (конвенция проекта). Отдаём с пометкой UTC:
    # иначе браузер прочитает «2026-11-11 18:13:06» как московское время и
    # покажет «4 ч назад» вместо «1 ч назад» — минус три часа врёт.
    last_verdict_at: Optional[str] = None
    if isinstance(last_verdict, datetime):
        last_verdict_at = last_verdict.replace(tzinfo=timezone.utc).isoformat()
    elif last_verdict is not None:
        last_verdict_at = str(last_verdict)

    src_24h = {
        str(s or "?"): int(n or 0)
        for s, n in (await session.execute(_source_counts_query(1))).all()
    }

    # ── Сейчас: на каком движке считает волна ──
    mode = (os.getenv("CLASSIFIER_ENGINE") or "").strip().lower() or "deepseek"
    engine_now = {
        "mode": mode,
        "label": ENGINE_LABELS[ENGINE_OPENCODE if mode == "opencode" else ENGINE_DEEPSEEK],
        "env": "CLASSIFIER_ENGINE",
    }

    # ── Демон Опенкода: живость и окна ──
    now = datetime.now(timezone.utc)

    def _run_ts(run: Dict[str, Any]) -> Optional[datetime]:
        raw = str(run.get("ts") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _runs_in(hours: int) -> List[Dict[str, Any]]:
        out = []
        for r in runs:
            ts = _run_ts(r)
            if ts is not None and ts >= now - timedelta(hours=hours):
                out.append(r)
        return out

    def _runs_summary(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "runs": len(subset),
            "posts": sum(int(r.get("posts") or r.get("verdicts_sent") or 0) for r in subset),
            "verdicts_sent": sum(int(r.get("verdicts_sent") or 0) for r in subset),
            "recorded": sum(int(r.get("recorded") or 0) for r in subset),
            "errors": sum(1 for r in subset if r.get("error")),
        }

    runs_24h = _runs_in(24)
    runs_7d = _runs_in(24 * 7)
    last_run_ts = _run_ts(runs[-1]) if runs else None
    daemon = {
        "runs_24h": _runs_summary(runs_24h),
        "runs_7d": _runs_summary(runs_7d),
        "all_time": _runs_summary(runs),
        "last_run_ts": last_run_ts.isoformat() if last_run_ts else None,
        "minutes_ago": (
            round((now - last_run_ts).total_seconds() / 60.0, 1) if last_run_ts else None
        ),
        "models": sorted(
            {str(r.get("model") or "?") for r in runs if r.get("model")},
        ),
    }

    return {
        "runs": runs[-50:],
        "runs_total": len(runs),
        "runs_posts": total_run_posts,
        "runs_recorded": total_run_recorded,
        "runs_errors": sum(1 for r in runs if r.get("error")),
        "last_run": runs[-1] if runs else None,
        "source_labels": dict(SOURCE_LABELS),
        "source_meta": {
            key: {
                "label": SOURCE_LABELS.get(key, key),
                "alive": SOURCE_ALIVE.get(key, True),
                "note": SOURCE_NOTES.get(key, ""),
            }
            for key in sorted(set(SOURCE_LABELS) | set(verdicts_by_source))
        },
        "verdicts_by_source": verdicts_by_source,
        "verdicts_by_source_24h": src_24h,
        "daily_by_source": daily,
        "quality_by_source": quality,
        "weekly_quality": weekly,
        "corrections_by_week": corrections_by_week,
        "rules_by_status": {str(s or "?"): int(n) for s, n in rule_rows},
        # ── разрез «кем ответили» ──
        "engine_labels": dict(ENGINE_LABELS),
        "model_labels": dict(MODEL_LABELS),
        "engine_windows": engine_windows,
        "engine_by_day": engine_by_day,
        "models_30d": models_30d,
        "engine_now": engine_now,
        "daemon": daemon,
        "last_verdict_at": last_verdict_at,
    }
