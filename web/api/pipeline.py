"""Карта конвейера: данные для страницы ``/pipeline`` и JSON ``/api/pipeline/map``.

Страница отвечает на вопрос владельца (21.10): «что и когда работает, из чего
состоят волны, куда они идут» — и на второй, который из него следует: «где
лишние движения».

Три блока и три источника:

* **расписание** — сам ``app.conf.beat_schedule`` (не копия: см.
  ``modules/pipeline_map``), плюс число регионов, которые слот реально заденет
  (``modules.wave_targets`` — тот же отбор, что у таска);
* **выход тем** — ``parsing_stats`` за период: волны, посты, средняя
  длительность. Из этого считается «волн на пост»;
* **сейчас** — длина очереди в Redis и активная задача через ``inspect``
  (короткий таймаут: страница не должна висеть, если воркер занят).

Тяжёлая часть (расписание + отбор регионов + агрегат статистики) кэшируется на
10 минут: расписание меняется только деплоем, отбор — пулом сообществ. Блок
«сейчас» не кэшируется никогда, иначе он врёт именно в тот момент, когда на
него смотрят.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.models_extended import ParsingStats
from modules import engine_guard
from modules.pipeline_map import (
    attach_regions,
    group_by_hour,
    region_key,
    theme_table,
    totals,
    wave_slots,
)
from modules.publisher.postopus_bulletin_headers import theme_title
from modules.wave_targets import select_regions
from utils.timezone import utc_to_moscow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["Pipeline"])

CACHE_TTL_SECONDS = 600
_CACHE: Dict[str, Any] = {"stamp": 0.0, "days": 0, "payload": None}


def get_schedule() -> Dict[str, Any]:
    """Расписание beat из живого приложения Celery.

    Импорт ленивый: веб-процесс не тянет Celery при старте, а страница карты —
    единственное место, где расписание нужно целиком.
    """
    from tasks.celery_app import app

    return dict(app.conf.beat_schedule or {})


async def _region_counts(db: AsyncSession, pairs: List[tuple[str, bool]]) -> Dict[str, int]:
    """Сколько регионов заденет слот каждой темы (отбор — как у таска).

    Ключ — ``region_key(theme, strict)``: областные темы идут строгим гейтом
    (единицы регионов), районные — мягким (десятки), и путать их нельзя —
    именно на этом карта 21.10 показала 2 925 волн в сутки вместо ~1 600.
    """
    counts: Dict[str, int] = {}
    for theme, strict in pairs:
        key = region_key(theme, strict)
        try:
            counts[key] = len(await select_regions(db, theme=theme, strict=strict))
        except Exception:  # noqa: BLE001 — одна тема не должна ломать карту
            logger.warning(
                "pipeline map: отбор регионов для темы %s (strict=%s) не удался",
                theme,
                strict,
                exc_info=True,
            )
            counts[key] = 0
    return counts


async def _theme_stats(db: AsyncSession, days: int) -> List[Dict[str, Any]]:
    """Агрегат ``parsing_stats`` по темам за период."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    published = func.count(func.nullif(ParsingStats.published_post_id, 0))
    rows = (
        await db.execute(
            select(
                ParsingStats.theme,
                func.count(),
                func.avg(ParsingStats.duration_seconds),
                func.sum(ParsingStats.duration_seconds),
                published,
                func.max(ParsingStats.run_date),
            )
            .where(ParsingStats.run_date >= since)
            .group_by(ParsingStats.theme)
        )
    ).all()

    out: List[Dict[str, Any]] = []
    for theme, runs, avg_seconds, seconds, pub, last_run in rows:
        out.append(
            {
                "theme": str(theme or ""),
                "runs": int(runs or 0),
                "avg_seconds": float(avg_seconds or 0),
                "seconds": float(seconds or 0),
                "published": int(pub or 0),
                "max_run_date": (
                    utc_to_moscow(last_run).strftime("%d.%m %H:%M") if last_run else None
                ),
            }
        )
    return out


async def _queue_state() -> Dict[str, Any]:
    """Длина очереди и активная задача. Никогда не бросает — карта не падает."""
    state: Dict[str, Any] = {"queue_depth": None, "unacked": None, "active": [], "error": None}
    try:
        import redis

        from config.runtime import REDIS

        client = redis.Redis(
            host=REDIS["host"],
            port=REDIS["port"],
            db=REDIS["db"],
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        state["queue_depth"] = int(client.llen("celery"))
        # ``unacked`` у redis-транспорта Celery — **хеш**, а не список: ``llen``
        # на нём падает с WRONGTYPE, и «взято в работу» тихо оставалось пустым
        # (поймано на первом же прогоне карты 21.10).
        try:
            state["unacked"] = int(client.hlen("unacked"))
        except Exception:  # noqa: BLE001 — на другой версии транспорта это список
            state["unacked"] = int(client.llen("unacked"))
    except Exception as exc:  # pragma: no cover - сетевой сбой
        state["error"] = f"redis: {exc}"

    try:
        from tasks.celery_app import app

        inspect = app.control.inspect(timeout=1.5)
        active = inspect.active() or {}
        for worker, tasks in active.items():
            for task in tasks or []:
                state["active"].append(
                    {
                        "worker": worker,
                        "name": str(task.get("name") or ""),
                        "id": str(task.get("id") or ""),
                        "args": ", ".join(str(a) for a in (task.get("args") or []))[:60],
                    }
                )
    except Exception as exc:  # pragma: no cover - сетевой сбой
        state.setdefault("error", None)
        state["error"] = (state["error"] + "; " if state["error"] else "") + f"inspect: {exc}"
    return state


async def build_map(db: AsyncSession, *, days: int = 7) -> Dict[str, Any]:
    """Полная карта: расписание, темы, состояние слота."""
    cached = _CACHE.get("payload")
    if (
        cached
        and _CACHE.get("days") == days
        and (time.time() - _CACHE.get("stamp", 0)) < CACHE_TTL_SECONDS
    ):
        schedule_part = cached
    else:
        try:
            schedule = get_schedule()
        except Exception:  # noqa: BLE001 — без Celery карта показывает только статистику
            logger.warning("pipeline map: расписание beat недоступно", exc_info=True)
            schedule = {}

        rows = wave_slots(schedule)
        pairs = sorted({(r["theme"], bool(r["strict"])) for r in rows if r["theme"]})
        counts = await _region_counts(db, pairs)
        rows = attach_regions(rows, counts)
        stats = await _theme_stats(db, days)

        table = theme_table(wave_rows=rows, stats=stats, region_counts=counts)
        busy = sum(float(s.get("seconds") or 0) for s in stats) / max(days, 1) / 3600
        schedule_part = {
            "days": days,
            "schedule_rows": rows,
            "hours": group_by_hour(rows),
            "themes": table,
            "totals": totals(wave_rows=rows, themes=table, busy_hours=busy),
            "theme_titles": {t["theme"]: theme_title(t["theme"]) for t in table},
        }
        _CACHE.update({"stamp": time.time(), "days": days, "payload": schedule_part})

    payload = dict(schedule_part)
    payload["generated_at"] = utc_to_moscow(datetime.now(timezone.utc)).strftime(
        "%d.%m.%Y %H:%M МСК"
    )
    payload["now"] = {
        "slot": "один (max-concurrency=1)",
        "breaker": engine_guard.snapshot(),
        **(await _queue_state()),
    }
    return payload


@router.get("/map")
async def get_pipeline_map(
    days: int = Query(7, ge=1, le=30, description="Окно статистики, дней"),
    db: AsyncSession = Depends(get_db_session),
):
    """Карта конвейера в JSON (её же рисует страница ``/pipeline``)."""
    return await build_map(db, days=days)


__all__ = ["build_map", "get_schedule", "router"]
