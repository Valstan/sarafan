"""Celery-задача вечерней сводки «Короткой строкой» (P182, заказ 20.10).

Сборник выходит раз в сутки, в 21:40 МСК — последним постом дня. Материал —
посты, которые сегодня прошли фильтр, но не попали в ленту: их пересказы
собирает ``modules.publisher.daily_digest``, публикация идёт обычным путём
(``VKPublisher`` + журнал публикаций + курсор дедупа), поэтому завтрашние волны
эти посты уже не возьмут.

Kill-switch — ``DAILY_DIGEST_ENABLED=0``: рубрика выключается одной переменной,
не трогая сбор и ленту. Пустой сборник (мало материала) — штатный исход, а не
сбой: в лог уходит причина, в отчёт — ``skipped``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from tasks.celery_app import app
from utils.celery_asyncio import run_coro

logger = logging.getLogger(__name__)

DIGEST_THEME = "korotko"
DIGEST_KIND = "digest"


@app.task(name="tasks.digest_tasks.publish_daily_digest")
def publish_daily_digest(
    region_code: Optional[str] = None, dry_run: bool = False
) -> Dict[str, Any]:
    """Собрать и опубликовать вечернюю сводку (или показать превью при ``dry_run``)."""
    try:
        return run_coro(_publish_daily_digest(region_code, dry_run=dry_run))
    except Exception as e:  # noqa: BLE001 — задача не должна валить beat-цикл
        logger.error("daily digest: задача упала: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def _enabled() -> bool:
    raw = (os.getenv("DAILY_DIGEST_ENABLED", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


async def _publish_daily_digest(
    region_code: Optional[str] = None, *, dry_run: bool = False
) -> Dict[str, Any]:
    if not _enabled():
        return {"success": True, "skipped": "disabled"}

    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Region

    async with AsyncSessionLocal() as session:
        stmt = select(Region).where(Region.is_active.is_(True), Region.vk_group_id.isnot(None))
        if region_code:
            stmt = stmt.where(Region.code == region_code)
        regions = (await session.execute(stmt.order_by(Region.code))).scalars().all()

        reports: List[Dict[str, Any]] = []
        for region in regions:
            try:
                report = await _digest_for_region(session, region, dry_run=dry_run)
            except Exception as e:  # noqa: BLE001 — один район не валит остальные
                logger.warning("daily digest: регион=%s сбой: %s", region.code, e, exc_info=True)
                report = {"region": region.code, "success": False, "error": str(e)}
            if report.get("published") or report.get("preview") or report.get("skipped"):
                reports.append(report)

    published = sum(1 for r in reports if r.get("published"))
    logger.info(
        "daily digest: регионов=%d опубликовано=%d%s",
        len(regions),
        published,
        "" if not dry_run else " (dry-run)",
    )
    return {
        "success": True,
        "regions_total": len(regions),
        "published": published,
        "reports": reports,
    }


async def _digest_for_region(session: Any, region: Any, *, dry_run: bool) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.models_extended import RegionConfig, WorkTable
    from modules.deduplication.bulletin_history import build_region_dedup_sets
    from modules.publisher import daily_digest
    from modules.publisher.postopus_bulletin_headers import resolve_bulletin_hashtags

    region_config = (
        (await session.execute(select(RegionConfig).where(RegionConfig.region_code == region.code)))
        .scalars()
        .first()
    )
    if region_config is None:
        logger.info("daily digest: регион=%s без RegionConfig — пропуск", region.code)
        return {"region": region.code, "skipped": "no_region_config"}

    work_tables = (
        (await session.execute(select(WorkTable).where(WorkTable.region_code == region.code)))
        .scalars()
        .all()
    )
    cursor_lips, _ = build_region_dedup_sets(work_tables)

    posts = await daily_digest.collect_candidates(session, region.code, exclude_lips=cursor_lips)
    if len(posts) < daily_digest.MIN_ITEMS:
        logger.info("daily digest: регион=%s кандидатов=%d — пропуск", region.code, len(posts))
        return {"region": region.code, "skipped": f"candidates={len(posts)}"}

    names = await _source_names(session, posts)
    theme_tags, local_hashtag = resolve_bulletin_hashtags(region_config, DIGEST_THEME, region)
    try:
        configured_max = int(getattr(region_config, "text_post_maxsize_simbols", 0) or 0)
    except (TypeError, ValueError):
        configured_max = 0
    max_total_chars = min(
        daily_digest.MAX_TOTAL_CHARS,
        configured_max if configured_max > 0 else daily_digest.MAX_TOTAL_CHARS,
    )
    draft = await daily_digest.build_digest_from_posts(
        posts,
        names=names,
        theme_tags=theme_tags,
        local_hashtag=local_hashtag,
        region_name=getattr(region, "name", "") or "",
        region_code=region.code,
        max_total_chars=max_total_chars,
    )
    if draft is None:
        return {"region": region.code, "skipped": "empty_digest"}

    if dry_run:
        return {"region": region.code, "preview": draft.text, "lips": draft.lips}

    return await _publish(session, region, draft)


async def _source_names(session: Any, posts: List[Dict[str, Any]]) -> Dict[str, str]:
    """Имена источников: из базы, поверх — официальные из VK (если доступен)."""
    from sqlalchemy import select

    from database.models import Community

    owner_ids = sorted({int(p.get("owner_id") or 0) for p in posts if p.get("owner_id")})
    if not owner_ids:
        return {}
    rows = (
        await session.execute(
            select(Community.vk_id, Community.name).where(
                Community.vk_id.in_([gid for gid in owner_ids] + [-gid for gid in owner_ids])
            )
        )
    ).fetchall()
    names: Dict[str, str] = {}
    for vk_id, name in rows:
        try:
            key = str(abs(int(vk_id)))
        except (TypeError, ValueError):
            continue
        if name:
            names[key] = str(name)

    # Официальные названия — тот же источник, что у ленты (ADR-0011). Сбой VK
    # не мешает сборнику: остаётся имя из базы.
    try:
        from modules.source_names import fetch_official_names
        from modules.vk_monitor.vk_client import VKClient
        from modules.vk_token_router import pick_healthy_read_token

        candidate = await pick_healthy_read_token(session)
        if candidate:
            official = fetch_official_names(VKClient(candidate.token), owner_ids)
            for group_id, name in (official or {}).items():
                if name:
                    names[str(abs(int(group_id)))] = str(name)
    except Exception:  # pragma: no cover - сеть/VK
        logger.debug("daily digest: официальные имена недоступны", exc_info=True)
    return names


async def _publish(session: Any, region: Any, draft: Any) -> Dict[str, Any]:
    from modules.publication_journal import record_publication
    from modules.publisher.vk_publisher_extended import VKPublisher

    publisher = await VKPublisher.create_with_policy(
        session, target_group_id=region.vk_group_id, test_polygon_mode=False
    )
    result = await publisher.publish_bulletin(
        group_id=region.vk_group_id,
        text=draft.text,
        attachments=[],
    )
    published = bool(isinstance(result, dict) and result.get("success"))

    if published:
        await record_publication(
            region_code=region.code,
            wave_theme=DIGEST_THEME,
            kind=DIGEST_KIND,
            posts_included=draft.lips,
            publish_result=result,
        )
        try:
            await _remember_published(session, region.code, draft.lips)
        except Exception:  # noqa: BLE001 — публикация уже состоялась, курсор чинится потом
            logger.warning(
                "daily digest: регион=%s курсор дедупа не обновлён",
                region.code,
                exc_info=True,
            )

    try:
        from monitoring.metrics import publish_result_label, track_digest_published

        track_digest_published(
            region=region.code, topic=DIGEST_THEME, result=publish_result_label(result)
        )
    except Exception:  # pragma: no cover - метрики не валят публикацию
        logger.warning("daily digest: метрика не записалась", exc_info=True)

    logger.info(
        "daily digest: регион=%s опубликовано=%s пунктов=%d знаков=%d",
        region.code,
        published,
        len(draft.items),
        len(draft.text),
    )
    return {
        "region": region.code,
        "published": published,
        "items": len(draft.items),
        "chars": len(draft.text),
        "url": (result or {}).get("url"),
    }


async def _remember_published(session: Any, region_code: str, lips: List[str]) -> None:
    """Отметить вышедшие посты в глобальном курсоре дедупа района.

    Журнал публикаций (``published_posts``) закрывает дедуп внутри сборника,
    но утренние волны смотрят в ``work_tables`` — без этой строки вчерашний
    пост мог бы выйти в ленту ещё раз, пока не истекли его сутки свежести.
    """
    from sqlalchemy import select

    from database.models_extended import WorkTable
    from modules.deduplication.bulletin_history import (
        GLOBAL_REGION_WORK_THEME,
        append_unique_limited,
    )
    from tasks.parsing_scheduler_tasks import WORK_TABLE_LIP_LIMIT

    row = (
        (
            await session.execute(
                select(WorkTable).where(
                    WorkTable.region_code == region_code,
                    WorkTable.theme == GLOBAL_REGION_WORK_THEME,
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = WorkTable(
            region_code=region_code,
            theme=GLOBAL_REGION_WORK_THEME,
            lip=[],
            hash=[],
        )
        session.add(row)
    row.lip = append_unique_limited(row.lip or [], lips, WORK_TABLE_LIP_LIMIT)
    await session.commit()
