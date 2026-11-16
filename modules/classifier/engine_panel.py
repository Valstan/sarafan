"""Живая сводка классификатора: волны, публикации, сайты, качество по движкам.

Панель «Движок классификации» (``/local-engine``) показывает не только «кем
ответили», но и **как система работает целиком**:

* **волны** — сколько прогонов прошло, сколько пропущено и почему
  (``parsing_stats``: по прогону на регион и тему);
* **публикации** — сколько постов ушло по регионам и по темам волн
  (``published_posts``);
* **сайты** — куда уходят посты для сайтов кластера, что доставлено, что
  отклонено и почему (``conveyor_deliveries``);
* **источники** — откуда инфа для сайтов вообще берётся (собранные посты по
  темам, ``work_tables``);
* **качество по движкам** — согласие оператора отдельно по Дипсику и
  Минимаксу (а не «по облаку»);
* **Минимакс** — живой журнал вызовов движка (``logs/minimax_usage.jsonl``):
  успех, отказы, причины, токены.

Всё считается из базы и журналов; ничего не тасуется из воздуха — каждый
блок панели умеет сказать, откуда он взял цифры.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

from sqlalchemy import func, literal_column, select

import database.models  # noqa: F401 — регистрация моделей: без неё не собрать запросы
from database.models_extended import (
    ClassificationCorrection,
    ClassificationRule,
    ContentClassification,
    ConveyorDelivery,
    ParsingStats,
    PublishedPost,
    WorkTable,
)
from modules.classifier.local_engine import ENGINE_DEEPSEEK, ENGINE_MINIMAX, engine_of_model

logger = logging.getLogger(__name__)

#: Журнал вызовов MiniMax Code на боксе (одна строка JSON на вызов).
MINIMAX_USAGE_LOG = Path(os.getenv("MINIMAX_USAGE_LOG", "") or Path("logs") / "minimax_usage.jsonl")

#: Сколько последних вызовов показывать в таблице «Минимакс: последние прогоны».
MINIMAX_RUNS_LIMIT = 40

#: Сколько последних правил показывать в блоке «Обучение».
RULES_LIMIT = 12


def _trunc(unit: str, column):
    """``date_trunc`` с **литералом** единицы измерения (как в local_engine)."""
    if unit not in ("day", "week"):
        raise ValueError("неизвестная единица усечения: %r" % unit)
    return func.date_trunc(literal_column("'%s'" % unit), column)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_naive() -> datetime:
    """``datetime.utcnow()`` — в проекте время в базе UTC без зоны."""
    return datetime.utcnow()


# ───────── Минимакс: журнал вызовов ─────────


def read_minimax_usage(limit: int = MINIMAX_RUNS_LIMIT) -> List[Dict[str, Any]]:
    """Прочитать последние вызовы MiniMax из журнала на боксе.

    Журнал растёт сам по себе (движок пишет каждую попытку), поэтому панель
    читает его как есть: пустой файл — не ошибка, а «движок ещё не звали».
    """
    path = MINIMAX_USAGE_LOG
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
    except OSError:
        logger.warning("панель: не удалось прочитать журнал MiniMax", exc_info=True)
        return []
    return rows[-limit:]


def minimax_summary(rows: Sequence[Dict[str, Any]], hours: int) -> Dict[str, Any]:
    """Сводка по вызовам MiniMax за окно в часах.

    Чистая функция (её проверяет тест): успех, отказы с причинами, токены.
    """
    now = _now()
    cutoff = now - timedelta(hours=hours)
    picked: List[Dict[str, Any]] = []
    for row in rows:
        raw = str(row.get("ts") or "")
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            picked.append(row)
    ok = [r for r in picked if r.get("ok")]
    fail = [r for r in picked if not r.get("ok")]
    reasons = Counter(str(r.get("reason") or "без причины") for r in fail)
    tokens = sum(int((r.get("usage") or {}).get("totalTokens") or 0) for r in ok)
    spent = sum(int((r.get("usage") or {}).get("totalTokens") or 0) for r in fail)
    return {
        "calls": len(picked),
        "ok": len(ok),
        "failed": len(fail),
        "ok_pct": round(100.0 * len(ok) / len(picked), 1) if picked else None,
        "tokens": tokens,
        "tokens_failed": spent,
        "reasons": dict(reasons.most_common(6)),
        "last_ts": str(picked[-1].get("ts")) if picked else None,
    }


def minimax_runs(
    rows: Sequence[Dict[str, Any]], limit: int = MINIMAX_RUNS_LIMIT
) -> List[Dict[str, Any]]:
    """Последние вызовы для таблицы: время, исход, причина, токены."""
    out: List[Dict[str, Any]] = []
    for row in list(rows)[-limit:][::-1]:
        usage = row.get("usage") or {}
        out.append(
            {
                "ts": str(row.get("ts") or ""),
                "ok": bool(row.get("ok")),
                "reason": str(row.get("reason") or ""),
                "account": str(row.get("account") or ""),
                "tokens": int(usage.get("totalTokens") or 0),
                "input": int(usage.get("inputTokens") or 0),
                "output": int(usage.get("outputTokens") or 0),
                "cache": int(usage.get("cacheReadTokens") or 0),
            }
        )
    return out


# ───────── Волны ─────────


async def _waves(session, days: int = 7) -> Dict[str, Any]:
    """Прогоны волн из ``parsing_stats``: сколько прошло, сколько пропущено.

    «Пропущен» — прогон без публикации: посты разобрали, публиковать нечего
    (всё отсеяно фильтрами) либо прогон упал с ошибкой.
    """
    since = _utcnow_naive() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                ParsingStats.theme,
                ParsingStats.success,
                ParsingStats.published_post_id,
                ParsingStats.error_message,
                ParsingStats.total_posts_scanned,
                ParsingStats.posts_final_count,
                ParsingStats.duration_seconds,
            ).where(ParsingStats.run_date >= since)
        )
    ).all()

    total = len(rows)
    published = sum(1 for r in rows if r[2] is not None)
    failed = sum(1 for r in rows if not r[1])
    scanned = sum(int(r[4] or 0) for r in rows)
    final = sum(int(r[5] or 0) for r in rows)
    errors = Counter(str(r[3] or "") for r in rows if r[3])
    by_theme: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"runs": 0, "published": 0, "scanned": 0}
    )
    for theme, success, pub_id, _err, scanned_r, final_r, _dur in rows:
        bucket = by_theme[str(theme or "?")]
        bucket["runs"] += 1
        bucket["scanned"] += int(scanned_r or 0)
        if pub_id is not None:
            bucket["published"] += 1
    return {
        "days": days,
        "runs": total,
        "published": published,
        "skipped": total - published - failed,
        "failed": failed,
        "scanned": scanned,
        "final": final,
        "errors": [{"reason": k, "n": v} for k, v in errors.most_common(8)],
        "by_theme": [
            {"theme": t, **v} for t, v in sorted(by_theme.items(), key=lambda kv: -kv[1]["runs"])
        ],
    }


async def _waves_by_day(session, days: int = 14) -> List[Dict[str, Any]]:
    """Прогоны и пропуски по календарным дням (UTC)."""
    since = _utcnow_naive() - timedelta(days=days)
    day = _trunc("day", ParsingStats.run_date)
    rows = (
        await session.execute(
            select(
                day.label("d"),
                func.count().label("runs"),
                func.count().filter(ParsingStats.published_post_id.isnot(None)).label("published"),
                func.count().filter(ParsingStats.success.is_(False)).label("failed"),
            )
            .where(ParsingStats.run_date >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    return [
        {"day": str(d)[:10], "runs": int(runs), "published": int(pub), "failed": int(failed)}
        for d, runs, pub, failed in rows
    ]


# ───────── Публикации ─────────


async def _published(session, days: int = 7) -> Dict[str, Any]:
    """Опубликованные посты: по регионам и по темам волн."""
    since = _utcnow_naive() - timedelta(days=days)
    by_region = (
        await session.execute(
            select(PublishedPost.region_code, func.count().label("n"))
            .where(PublishedPost.published_at >= since)
            .group_by(PublishedPost.region_code)
            .order_by(func.count().desc())
        )
    ).all()
    by_theme = (
        await session.execute(
            select(PublishedPost.wave_theme, func.count().label("n"))
            .where(PublishedPost.published_at >= since)
            .group_by(PublishedPost.wave_theme)
            .order_by(func.count().desc())
        )
    ).all()
    total = sum(int(n) for _r, n in by_region)
    return {
        "days": days,
        "total": total,
        "regions": [{"region": str(r or "?"), "n": int(n)} for r, n in by_region[:25]],
        "regions_total": len(by_region),
        "themes": [{"theme": str(t or "?"), "n": int(n)} for t, n in by_theme],
    }


# ───────── Сайты и источники ─────────


async def _sites(session, days: int = 30) -> Dict[str, Any]:
    """Доставки на сайты кластера: что доставлено, что отклонено и почему."""
    since = _utcnow_naive() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                ConveyorDelivery.site,
                ConveyorDelivery.status,
                ConveyorDelivery.reason,
                ConveyorDelivery.created_at,
            ).where(ConveyorDelivery.created_at >= since)
        )
    ).all()

    by_site: Dict[str, Dict[str, Any]] = {}
    reasons: Dict[str, Counter] = defaultdict(Counter)
    for site, status, reason, created in rows:
        bucket = by_site.setdefault(str(site or "?"), {"total": 0, "statuses": {}, "last_at": None})
        bucket["total"] += 1
        bucket["statuses"][str(status or "?")] = bucket["statuses"].get(str(status or "?"), 0) + 1
        if reason:
            reasons[str(site or "?")][str(reason)] += 1
        stamp = str(created or "")
        if stamp and (bucket["last_at"] is None or stamp > bucket["last_at"]):
            bucket["last_at"] = stamp

    out = []
    for site, bucket in by_site.items():
        out.append(
            {
                "site": site,
                "title": _site_title(site),
                "source_region": _site_source_region(site),
                "active": site in _active_sites(),
                "total": bucket["total"],
                "statuses": bucket["statuses"],
                "reasons": [{"reason": r, "n": n} for r, n in reasons[site].most_common(5)],
                "last_at": bucket["last_at"],
            }
        )
    out.sort(key=lambda s: -s["total"])
    return {"days": days, "sites": out, "active": sorted(_active_sites())}


def _site_config() -> Dict[str, Dict[str, Any]]:
    """Описания сайтов-получателей из конфига конвейера."""
    try:
        from config.content_conveyor import SITES

        return {str(s.get("key")): s for s in SITES if isinstance(s, dict)}
    except Exception:  # noqa: BLE001 — конфиг не должен ронять панель
        return {}


def _active_sites() -> set:
    raw = (os.getenv("CONVEYOR_SITES") or "").strip()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _site_title(site: str) -> str:
    cfg = _site_config().get(site) or {}
    return str(cfg.get("title") or site)


def _site_source_region(site: str) -> str:
    cfg = _site_config().get(site) or {}
    return str(cfg.get("source_region") or "")


async def _collected(session, days: int = 7) -> Dict[str, Any]:
    """Откуда берётся инфа: сколько постов собрано по темам (work_tables).

    Конвейер берёт сайту посты из **опубликованных сводок района** — то есть
    из этого потока; показать его объём — значит показать, сколько материала
    вообще может уехать на сайты.
    """
    since = _utcnow_naive() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                WorkTable.theme,
                func.count().label("regions"),
                func.coalesce(func.sum(func.json_array_length(WorkTable.lip)), 0).label("lips"),
            )
            .where(WorkTable.updated_at >= since)
            .group_by(WorkTable.theme)
        )
    ).all()
    by_theme = {
        str(theme or "?"): {"regions": int(regions or 0), "lips": int(lips or 0)}
        for theme, regions, lips in rows
    }
    return {
        "days": days,
        "themes": [
            {"theme": t, **v} for t, v in sorted(by_theme.items(), key=lambda kv: -kv[1]["lips"])
        ],
        "lips": sum(v["lips"] for v in by_theme.values()),
    }


# ───────── Качество по движкам ─────────


async def _quality_by_engine(session, days: int = 120) -> Dict[str, Any]:
    """Согласие оператора по **движку** (Дипсик / Минимакс / Опенкод).

    Раньше панель считала согласие «по пути доставки» — а это путь волны, а
    не модель: волна считает и Дипсиком, и Минимаксом, и в один месяц в ней
    было 23 897 бесплатных Опенкодов. Считать надо по модели вердикта.
    """
    since = _utcnow_naive() - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                ContentClassification.model,
                ClassificationCorrection.outcome,
                func.count().label("n"),
            )
            .join(
                ClassificationCorrection,
                ClassificationCorrection.classification_id == ContentClassification.id,
            )
            .where(ClassificationCorrection.created_at >= since)
            .group_by(ContentClassification.model, ClassificationCorrection.outcome)
        )
    ).all()

    by_engine: Dict[str, Dict[str, int]] = defaultdict(lambda: {"agree": 0, "correct": 0})
    last_correction = None
    for model, outcome, n in rows:
        engine = engine_of_model(str(model or ""))
        bucket = by_engine[engine]
        if outcome in bucket:
            bucket[outcome] += int(n or 0)

    last = (await session.execute(select(func.max(ClassificationCorrection.created_at)))).scalar()
    if isinstance(last, datetime):
        last_correction = last.replace(tzinfo=timezone.utc).isoformat()

    out = {}
    for engine, vals in by_engine.items():
        total = vals["agree"] + vals["correct"]
        out[engine] = {
            "agree": vals["agree"],
            "correct": vals["correct"],
            "total": total,
            "agree_pct": round(100.0 * vals["agree"] / total, 1) if total else None,
        }
    return {"by_engine": out, "last_correction_at": last_correction, "days": days}


async def _weekly_quality_by_engine(session, days: int = 120) -> List[Dict[str, Any]]:
    """Согласие по движку по неделям — «растёт ли качество фильтрации»."""
    since = _utcnow_naive() - timedelta(days=days)
    week = _trunc("week", ClassificationCorrection.created_at)
    rows = (
        await session.execute(
            select(
                week.label("w"),
                ContentClassification.model,
                ClassificationCorrection.outcome,
                func.count().label("n"),
            )
            .join(
                ClassificationCorrection,
                ClassificationCorrection.classification_id == ContentClassification.id,
            )
            .where(ClassificationCorrection.created_at >= since)
            .group_by(week, ContentClassification.model, ClassificationCorrection.outcome)
            .order_by(week)
        )
    ).all()

    by_week: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for w, model, outcome, n in rows:
        engine = engine_of_model(str(model or ""))
        inner = by_week[str(w)[:10]][engine]
        if outcome in ("agree", "correct"):
            inner[outcome] = inner.get(outcome, 0) + int(n or 0)
    out = []
    for week_key in sorted(by_week):
        row = {"week": week_key}
        for engine, vals in by_week[week_key].items():
            total = vals.get("agree", 0) + vals.get("correct", 0)
            row[engine] = {
                "agree": vals.get("agree", 0),
                "correct": vals.get("correct", 0),
                "agree_pct": round(100.0 * vals.get("agree", 0) / total, 1) if total else None,
            }
        out.append(row)
    return out


# ───────── Обучение ─────────


async def _rules(session) -> Dict[str, Any]:
    """Поток обучения: правки оператора → черновики правил → утверждённые.

    Показываем не только счётчики статусов, но и **свежесть**: когда последнее
    правило вступило в силу и когда была последняя правка оператора — по
    этим двум датам видно, живой ли контур обучения.
    """
    rows = (
        await session.execute(
            select(
                ClassificationRule.id,
                ClassificationRule.region_code,
                ClassificationRule.rule_text,
                ClassificationRule.status,
                ClassificationRule.source,
                ClassificationRule.created_at,
                ClassificationRule.last_effective_at,
            ).order_by(ClassificationRule.created_at.desc())
        )
    ).all()

    by_status = Counter(str(r[3] or "?") for r in rows)
    by_source = Counter(str(r[4] or "?") for r in rows)
    last_effective = None
    for r in rows:
        stamp = r[6]
        if isinstance(stamp, datetime):
            iso = stamp.replace(tzinfo=timezone.utc).isoformat()
            if last_effective is None or iso > last_effective:
                last_effective = iso
    latest = [
        {
            "id": int(r[0]),
            "region": str(r[1] or "") or "",
            "text": str(r[2] or "")[:220],
            "status": str(r[3] or "?"),
            "source": str(r[4] or "?"),
            "created_at": (
                r[5].replace(tzinfo=timezone.utc).isoformat()
                if isinstance(r[5], datetime)
                else str(r[5] or "")
            ),
            "last_effective_at": (
                r[6].replace(tzinfo=timezone.utc).isoformat() if isinstance(r[6], datetime) else ""
            ),
        }
        for r in rows[:RULES_LIMIT]
    ]
    return {
        "total": len(rows),
        "by_status": dict(by_status),
        "by_source": dict(by_source),
        "last_effective_at": last_effective,
        "latest": latest,
    }


# ───────── Сборка ─────────


async def stats(session) -> Dict[str, Any]:
    """Вся новая сводка панели одним заходом."""
    usage = read_minimax_usage(limit=600)
    return {
        "waves": await _waves(session),
        "waves_by_day": await _waves_by_day(session),
        "published": await _published(session),
        "sites": await _sites(session),
        "collected": await _collected(session),
        "quality_by_engine": await _quality_by_engine(session),
        "weekly_quality_by_engine": await _weekly_quality_by_engine(session),
        "rules": await _rules(session),
        "minimax": {
            "runs": minimax_runs(usage),
            "summary_24h": minimax_summary(usage, 24),
            "summary_7d": minimax_summary(usage, 24 * 7),
            "engine": ENGINE_MINIMAX,
            "fallback": ENGINE_DEEPSEEK,
        },
    }
