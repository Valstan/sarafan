"""
Страница «Фильтрация»: настройки конвейера сводок и связанные поля RegionConfig.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.models import Region
from database.models_extended import RegionConfig
from modules.bulletin_pipeline_settings import (
    DEFAULT_PIPELINE,
    POSTOPUS_BULLETIN_THEMES,
    empty_bulletin_filters_template,
    get_effective_pipeline_settings,
)

router = APIRouter()


class FiltrationPutBody(BaseModel):
    """Тело сохранения: bulletin_filters целиком с фронта + прочие поля RegionConfig."""

    bulletin_filters: Optional[Dict[str, Any]] = None
    black_id: Optional[List[int]] = None
    delete_msg_blacklist: Optional[List[str]] = None
    filter_group_by_region_words: Optional[Dict[str, Any]] = None
    time_old_post: Optional[Dict[str, int]] = None
    text_post_maxsize_simbols: Optional[int] = Field(None, ge=500, le=8192)
    setka_regim_repost: Optional[bool] = None
    repost_words_blacklist: Optional[List[str]] = None
    # Населённые пункты района — расширяют region_words для
    # RegionalRelevanceFilter (см. modules/filters/regional.py).
    # Передаём как простой список строк, на стороне сервера
    # сохраняем в RegionConfig.localities (JSONB).
    localities: Optional[List[str]] = None


def _normalize_bulletin_filters(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Слитый блок для ПОКАЗА (GET, превью): дефолты кода + записанное.

    Показывать надо эффективные значения — оператор видит то, что реально
    работает. Для ЗАПИСИ эта функция не годится: она же и замораживала дефолт
    (P178) — см. _sparse_bulletin_filters.
    """
    if not data or not isinstance(data, dict):
        return empty_bulletin_filters_template()
    defaults = {**DEFAULT_PIPELINE, **(data.get("defaults") or {})}
    by_topic = data.get("by_topic") if isinstance(data.get("by_topic"), dict) else {}
    return {"defaults": defaults, "by_topic": by_topic}


def _values_equal(a: Any, b: Any) -> bool:
    """Равны ли значения для целей диффа. Числа сравниваем как числа:
    записанное `1` и дефолт `1.0` — одно и то же, замораживать нечего.
    Мусор («не число») не выбрасываем — effective уронит его в дефолт сам,
    а молча выкидывать ввод оператора нельзя.
    """
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _sparse_bulletin_filters(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Разрежённый блок для ЗАПИСИ (PUT): только отличия от DEFAULT_PIPELINE.

    Лекарство от P178: раньше сюда писался весь слитый блок, и дефолт кода,
    бывший актуальным на момент сохранения, становился личной настройкой
    района — правка дефолта до него молча не доезжала. Теперь равное дефолту
    не пишется вовсе: район следует коду, пока оператор не отличится
    по-настоящему. Неизвестные коду ключи и мусор храним как есть.
    """
    if not data or not isinstance(data, dict):
        return {"defaults": {}, "by_topic": {}}
    incoming = data.get("defaults") or {}
    if not isinstance(incoming, dict):
        incoming = {}
    sparse = {
        k: v
        for k, v in incoming.items()
        if not (k in DEFAULT_PIPELINE and _values_equal(v, DEFAULT_PIPELINE[k]))
    }
    by_topic = data.get("by_topic") if isinstance(data.get("by_topic"), dict) else {}
    return {"defaults": sparse, "by_topic": by_topic}


def _normalize_localities(raw: Optional[List[str]]) -> List[str]:
    """Очистка списка localities перед сохранением в RegionConfig.

    - Убираем пустые строки и пробелы;
    - убираем дубли (без учёта регистра);
    - сохраняем исходный порядок и оригинальный регистр первой
      встретившейся записи.
    """
    if not raw:
        return []
    seen: set[str] = set()
    cleaned: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        v = item.strip()
        if not v:
            continue
        key = v.lower().replace("ё", "е")
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(v)
    return cleaned


@router.get("/meta")
async def filtration_meta():
    """Справочник тем и дефолтов для UI."""
    return {
        "themes": POSTOPUS_BULLETIN_THEMES,
        "default_pipeline": DEFAULT_PIPELINE,
        "description": {
            "max_post_age_hours": "Макс. возраст поста (часы) для отбора в сводку",
            "max_posts_per_bulletin": "Сколько новостей максимум в одном посте-сводке",
            "min_rafinad_len_core_dedup": "Мин. длина «rafinad»-текста для дедупа по «ядру»",
            "posts_per_community_fetch": (
                "Сколько последних постов запрашивать с каждого сообщества"
            ),
        },
    }


@router.get("/regions")
async def list_regions_with_config(session: AsyncSession = Depends(get_db_session)):
    """Список регионов (для выпадающего списка)."""
    r = await session.execute(select(Region).order_by(Region.code))
    regions = r.scalars().all()
    return [
        {
            "code": reg.code,
            "name": reg.name,
            "is_active": reg.is_active,
        }
        for reg in regions
    ]


@router.get("/{region_code}")
async def get_filtration(region_code: str, session: AsyncSession = Depends(get_db_session)):
    """Полные настройки фильтрации по региону."""
    reg_result = await session.execute(select(Region).where(Region.code == region_code))
    region = reg_result.scalars().first()
    if not region:
        raise HTTPException(status_code=404, detail="Region not found")

    cfg_result = await session.execute(
        select(RegionConfig).where(RegionConfig.region_code == region_code)
    )
    cfg = cfg_result.scalars().first()
    if not cfg:
        raise HTTPException(
            status_code=404,
            detail="RegionConfig not found — выполните миграцию/скрипт region_configs",
        )

    df = _normalize_bulletin_filters(cfg.bulletin_filters)
    preview = {t: get_effective_pipeline_settings(cfg, t) for t in POSTOPUS_BULLETIN_THEMES}

    return {
        "region_code": region.code,
        "region_name": region.name,
        "bulletin_filters": df,
        "effective_pipeline_preview": preview,
        "black_id": cfg.black_id or [],
        "delete_msg_blacklist": cfg.delete_msg_blacklist or [],
        "filter_group_by_region_words": cfg.filter_group_by_region_words or {},
        "time_old_post": cfg.time_old_post or {"hard": 86400, "medium": 172800, "light": 604800},
        "text_post_maxsize_simbols": cfg.text_post_maxsize_simbols or 4096,
        "setka_regim_repost": bool(cfg.setka_regim_repost),
        "repost_words_blacklist": cfg.repost_words_blacklist or [],
        "localities": list(getattr(cfg, "localities", None) or []),
    }


@router.put("/{region_code}")
async def put_filtration(
    region_code: str,
    payload: FiltrationPutBody,
    session: AsyncSession = Depends(get_db_session),
):
    """Сохранить настройки фильтрации."""
    reg_result = await session.execute(select(Region).where(Region.code == region_code))
    if not reg_result.scalars().first():
        raise HTTPException(status_code=404, detail="Region not found")

    cfg_result = await session.execute(
        select(RegionConfig).where(RegionConfig.region_code == region_code)
    )
    cfg = cfg_result.scalars().first()
    if not cfg:
        raise HTTPException(status_code=404, detail="RegionConfig not found")

    if payload.bulletin_filters is not None:
        # Пишем разреженно (только отличия от дефолта, P178): слитый блок
        # здесь же и замораживал дефолт. Показ (GET) по-прежнему слитый.
        cfg.bulletin_filters = _sparse_bulletin_filters(payload.bulletin_filters)

    if payload.black_id is not None:
        cfg.black_id = payload.black_id
    if payload.delete_msg_blacklist is not None:
        cfg.delete_msg_blacklist = payload.delete_msg_blacklist
    if payload.filter_group_by_region_words is not None:
        cfg.filter_group_by_region_words = payload.filter_group_by_region_words
    if payload.time_old_post is not None:
        cfg.time_old_post = payload.time_old_post
    if payload.text_post_maxsize_simbols is not None:
        cfg.text_post_maxsize_simbols = payload.text_post_maxsize_simbols
    if payload.setka_regim_repost is not None:
        cfg.setka_regim_repost = payload.setka_regim_repost
    if payload.repost_words_blacklist is not None:
        cfg.repost_words_blacklist = payload.repost_words_blacklist
    if payload.localities is not None:
        cfg.localities = _normalize_localities(payload.localities)

    await session.commit()
    await session.refresh(cfg)

    df = _normalize_bulletin_filters(cfg.bulletin_filters)
    preview = {t: get_effective_pipeline_settings(cfg, t) for t in POSTOPUS_BULLETIN_THEMES}

    return {
        "success": True,
        "region_code": region_code,
        "bulletin_filters": df,
        "effective_pipeline_preview": preview,
    }
