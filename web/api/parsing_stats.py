"""
Parsing Statistics API

Provides endpoints for viewing parsing and publishing statistics
migrated from old_postopus stat_mode functionality.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.models_extended import ParsingStats

router = APIRouter(prefix="/api/parsing-stats", tags=["parsing-stats"])


@router.get("/")
async def get_parsing_stats(
    region_code: Optional[str] = Query(None, description="Filter by region code"),
    theme: Optional[str] = Query(None, description="Filter by theme"),
    days: int = Query(7, ge=1, le=30, description="Number of days to look back"),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Get parsing statistics.

    Returns aggregated parsing stats for specified region/theme/time period.

    ⚠️ Читать цифры только вместе с `success`/`error_message` (P175): строка
    со всеми нулями и `success=false` означает «волна не мерила» (ранний выход
    конвейера — нет детей, нечего собирать, нет токенов), а не «померила и там
    пусто». Суммы счётчиков по таким строкам — не измерения.
    """
    # Build query
    query = select(ParsingStats)

    # Apply filters
    if region_code:
        query = query.where(ParsingStats.region_code == region_code)
    if theme:
        query = query.where(ParsingStats.theme == theme)

    # Time filter
    cutoff_date = datetime.now() - timedelta(days=days)
    query = query.where(ParsingStats.run_date >= cutoff_date)

    # Order by date
    query = query.order_by(ParsingStats.run_date.desc())

    # Execute
    result = await db.execute(query)
    stats_records = result.scalars().all()

    # Convert to dict
    stats_list = [record.to_dict() for record in stats_records]

    # Calculate aggregates
    aggregates = calculate_aggregates(stats_records)

    return {
        "stats": stats_list,
        "aggregates": aggregates,
        "total_records": len(stats_list),
        "period_days": days,
    }


@router.get("/summary")
async def get_parsing_summary(
    days: int = Query(7, ge=1, le=30),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Get parsing summary by region and theme.

    Returns aggregated stats grouped by region_code and theme.
    """
    cutoff_date = datetime.now() - timedelta(days=days)

    # Get all stats for period
    result = await db.execute(select(ParsingStats).where(ParsingStats.run_date >= cutoff_date))
    stats_records = result.scalars().all()

    # Group by region/theme
    summary = {}

    for record in stats_records:
        key = f"{record.region_code}/{record.theme}"

        if key not in summary:
            summary[key] = {
                "region_code": record.region_code,
                "theme": record.theme,
                "total_runs": 0,
                "successful_runs": 0,
                "failed_runs": 0,
                "total_posts_scanned": 0,
                "total_posts_published": 0,
                "avg_posts_per_run": 0,
                "total_groups_checked": 0,
                "total_filtered_ads": 0,
                "total_filtered_duplicates": 0,
                "success_rate": 0.0,
            }

        entry = summary[key]
        entry["total_runs"] += 1

        if record.success:
            entry["successful_runs"] += 1
        else:
            entry["failed_runs"] += 1

        entry["total_posts_scanned"] += record.total_posts_scanned
        entry["total_posts_published"] += record.posts_final_count
        entry["total_groups_checked"] += record.total_groups_checked
        entry["total_filtered_ads"] += record.posts_filtered_advertisement
        entry["total_filtered_duplicates"] += (
            record.posts_filtered_duplicate_lip
            + record.posts_filtered_duplicate_text
            + record.posts_filtered_duplicate_foto
        )

    # Calculate averages and rates
    for key, entry in summary.items():
        if entry["total_runs"] > 0:
            entry["avg_posts_per_run"] = entry["total_posts_published"] / entry["total_runs"]
            entry["success_rate"] = entry["successful_runs"] / entry["total_runs"] * 100

    return {
        "summary": list(summary.values()),
        "period_days": days,
        "total_regions_themes": len(summary),
    }


@router.get("/timeline")
async def get_parsing_timeline(
    region_code: str = Query(..., description="Region code"),
    theme: str = Query(..., description="Theme"),
    hours: int = Query(24, ge=1, le=168, description="Hours to look back"),
    db: AsyncSession = Depends(get_db_session),
):
    """
    Get parsing timeline for specific region/theme.

    Returns timeline of parsing runs with success/failure.
    """
    cutoff_time = datetime.now() - timedelta(hours=hours)

    result = await db.execute(
        select(ParsingStats)
        .where(
            and_(
                ParsingStats.region_code == region_code,
                ParsingStats.theme == theme,
                ParsingStats.run_date >= cutoff_time,
            )
        )
        .order_by(ParsingStats.run_date.asc())
    )

    stats_records = result.scalars().all()

    # Build timeline
    timeline = []
    for record in stats_records:
        timeline.append(
            {
                "timestamp": record.run_date.isoformat(),
                "success": record.success,
                "posts_scanned": record.total_posts_scanned,
                "posts_published": record.posts_final_count,
                "duration_seconds": record.duration_seconds,
                "error": record.error_message,
            }
        )

    return {
        "region_code": region_code,
        "theme": theme,
        "timeline": timeline,
        "period_hours": hours,
    }


@router.get("/recent")
async def get_recent_parsing_stats(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db_session),
):
    """Get most recent parsing runs across all regions/themes."""
    result = await db.execute(
        select(ParsingStats).order_by(ParsingStats.run_date.desc()).limit(limit)
    )

    stats_records = result.scalars().all()

    return {
        "recent_stats": [record.to_dict() for record in stats_records],
        "count": len(stats_records),
    }


@router.get("/publications")
async def get_publications(
    region_code: Optional[str] = Query(None, description="Filter by region code"),
    theme: Optional[str] = Query(None, description="Filter by theme"),
    days: int = Query(30, ge=1, le=90, description="Number of days to look back"),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db_session),
):
    """История публикаций сводок по регионам/темам.

    Возвращает фактические выпуски (успешные прогоны с непустой сводкой) с
    ссылкой на VK-пост — удобный просмотр «что и когда вышло», без хождения по
    VK-стенам (PENDING 🟢). Источник — ``parsing_stats`` (каждый прогон
    ``parse_and_publish_theme`` пишет строку; ``published_url`` заполняется при
    реальной публикации). Тест-полигонные прогоны и пустые выпуски исключены.
    """
    query = select(ParsingStats).where(
        ParsingStats.success.is_(True),
        ParsingStats.published_to_test_polygon.is_(False),
        ParsingStats.posts_final_count > 0,
    )
    if region_code:
        query = query.where(ParsingStats.region_code == region_code)
    if theme:
        query = query.where(ParsingStats.theme == theme)

    cutoff_date = datetime.now() - timedelta(days=days)
    query = query.where(ParsingStats.run_date >= cutoff_date)
    query = query.order_by(ParsingStats.run_date.desc()).limit(limit)

    result = await db.execute(query)
    records = result.scalars().all()

    publications = [
        {
            "id": r.id,
            "region_code": r.region_code,
            "theme": r.theme,
            "run_date": r.run_date.isoformat() if r.run_date else None,
            "posts_final_count": r.posts_final_count,
            "published_url": r.published_url,
            "published_post_id": r.published_post_id,
        }
        for r in records
    ]
    return {
        "publications": publications,
        "total_records": len(publications),
        "period_days": days,
    }


def calculate_aggregates(stats_records: List[ParsingStats]) -> dict:
    """Calculate aggregate statistics.

    ⚠️ Суммы счётчиков считают только строки-замеры (P175): у строк ранних
    выходов (`success=false`, все нули) цифры — «не мерили», и в
    `total_posts_scanned`/`total_groups_checked` они дают ноль честно, но
    `success_rate` такие строки топят вниз осознанно — волна действительно не
    отработала.
    """
    """Calculate aggregate statistics."""
    if not stats_records:
        return {}

    total_runs = len(stats_records)
    successful_runs = sum(1 for r in stats_records if r.success)

    return {
        "total_runs": total_runs,
        "successful_runs": successful_runs,
        "failed_runs": total_runs - successful_runs,
        "success_rate": (successful_runs / total_runs * 100) if total_runs > 0 else 0,
        "total_posts_scanned": sum(r.total_posts_scanned for r in stats_records),
        "total_posts_published": sum(r.posts_final_count for r in stats_records),
        "avg_posts_per_run": (
            (sum(r.posts_final_count for r in stats_records) / total_runs) if total_runs > 0 else 0
        ),
        "avg_duration_seconds": (
            (sum(r.duration_seconds or 0 for r in stats_records) / total_runs)
            if total_runs > 0
            else 0
        ),
        "total_groups_checked": sum(r.total_groups_checked for r in stats_records),
        "total_filtered_ads": sum(r.posts_filtered_advertisement for r in stats_records),
        "total_filtered_duplicates": sum(
            r.posts_filtered_duplicate_lip
            + r.posts_filtered_duplicate_text
            + r.posts_filtered_duplicate_foto
            for r in stats_records
        ),
    }
