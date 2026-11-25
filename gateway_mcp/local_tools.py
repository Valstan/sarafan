"""Инструменты агента по локальной базе Сарафана (read-only).

Три инструмента — те, что агенту действительно нужны, чтобы «поймать»
материал в своей же базе: список регионов, выборка постов и счётчики по
региону.

**Почему они здесь, а не в `modules/`.** Слой инструментов — это то, что
**модель видит**: у каждого инструмента есть JSON Schema и вызов, и всё это
живёт рядом (см. ``gateway_mcp/tool_registry.py``). Разносить схемы по
``modules/`` — значит снова получить две жизни у одного инструмента.

**Зависимость от базы — ленивая.** Модуль импортируется без ``DATABASE_URL``
(он нужен только ``database.connection`` при первом обращении к базе), а
сам запрос идёт внутри обработчика. Поэтому пакет ``gateway_mcp`` по-прежнему
работает и тестируется в среде потребителя, где базы Сарафана нет.

Все три инструмента — только чтение: они ничего не пишут и не меняют.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from gateway_mcp.tool_registry import ToolRegistry

#: Сколько символов текста поста отдаём агенту. Пост целиком агента не
#: кормит, а контекст съедает.
TEXT_PREVIEW_CHARS = 600


class ListRegionsInput(BaseModel):
    """Параметры ``sarafan_list_regions``."""

    model_config = ConfigDict(extra="forbid")

    active_only: bool = Field(default=True, description="Только активные регионы.")
    limit: int = Field(default=200, description="Сколько регионов вернуть (1..500).", ge=1, le=500)


class FindPostsInput(BaseModel):
    """Параметры ``sarafan_find_posts``."""

    model_config = ConfigDict(extra="forbid")

    region_code: str = Field(
        ...,
        description="Код региона Сарафана (напр. 'mi', 'arbazh').",
        min_length=1,
        max_length=50,
    )
    limit: int = Field(default=20, description="Сколько постов вернуть (1..100).", ge=1, le=100)
    only_unclassified: bool = Field(
        default=False,
        description="Только посты без AI-вердикта (ai_category пуст).",
    )


class RegionStatsInput(BaseModel):
    """Параметры ``sarafan_region_stats``."""

    model_config = ConfigDict(extra="forbid")

    region_code: str = Field(
        ...,
        description="Код региона Сарафана (напр. 'mi', 'arbazh').",
        min_length=1,
        max_length=50,
    )


def _preview(text: Optional[str]) -> str:
    raw = (text or "").strip()
    if len(raw) <= TEXT_PREVIEW_CHARS:
        return raw
    return raw[:TEXT_PREVIEW_CHARS] + "…"


async def _list_regions(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.connection import get_db_session_context
    from database.models import Region

    async with get_db_session_context() as session:
        query = select(Region).order_by(Region.code)
        if arguments.get("active_only", True):
            query = query.where(Region.is_active.is_(True))
        query = query.limit(int(arguments.get("limit") or 200))
        rows = (await session.execute(query)).scalars().all()
        return {
            "count": len(rows),
            "regions": [
                {
                    "code": row.code,
                    "name": row.name,
                    "kind": row.kind,
                    "is_active": bool(row.is_active),
                }
                for row in rows
            ],
        }


async def _find_posts(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.connection import get_db_session_context
    from database.models import Post, Region

    region_code = str(arguments.get("region_code") or "")
    limit = int(arguments.get("limit") or 20)
    only_unclassified = bool(arguments.get("only_unclassified"))

    async with get_db_session_context() as session:
        region = (
            await session.execute(select(Region).where(Region.code == region_code))
        ).scalar_one_or_none()
        if region is None:
            return {"ok": False, "error": f"регион не найден: {region_code}"}

        query = select(Post).where(Post.region_id == region.id).order_by(Post.date_published.desc())
        if only_unclassified:
            query = query.where(Post.ai_category.is_(None))
        rows = (await session.execute(query.limit(limit))).scalars().all()
        return {
            "region_code": region.code,
            "count": len(rows),
            "posts": [
                {
                    "id": row.id,
                    "community_id": row.community_id,
                    "date_published": (
                        row.date_published.isoformat() if row.date_published else None
                    ),
                    "ai_category": row.ai_category,
                    "ai_relevance": row.ai_relevance,
                    "status": row.status,
                    "text": _preview(row.text),
                }
                for row in rows
            ],
        }


async def _region_stats(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from sqlalchemy import func, select

    from database.connection import get_db_session_context
    from database.models import Community, Post, Region

    region_code = str(arguments.get("region_code") or "")

    async with get_db_session_context() as session:
        region = (
            await session.execute(select(Region).where(Region.code == region_code))
        ).scalar_one_or_none()
        if region is None:
            return {"ok": False, "error": f"регион не найден: {region_code}"}

        posts_total = (
            await session.execute(
                select(func.count()).select_from(Post).where(Post.region_id == region.id)
            )
        ).scalar_one()
        posts_classified = (
            await session.execute(
                select(func.count())
                .select_from(Post)
                .where(Post.region_id == region.id, Post.ai_category.isnot(None))
            )
        ).scalar_one()
        communities_total = (
            await session.execute(
                select(func.count()).select_from(Community).where(Community.region_id == region.id)
            )
        ).scalar_one()
        communities_active = (
            await session.execute(
                select(func.count())
                .select_from(Community)
                .where(Community.region_id == region.id, Community.is_active.is_(True))
            )
        ).scalar_one()
        return {
            "region_code": region.code,
            "region_name": region.name,
            "is_active": bool(region.is_active),
            "posts_total": int(posts_total),
            "posts_classified": int(posts_classified),
            "communities_total": int(communities_total),
            "communities_active": int(communities_active),
        }


def build_local_tools(registry: ToolRegistry) -> ToolRegistry:
    """Зарегистрировать read-инструменты по локальной базе Сарафана."""
    registry.add(
        "sarafan_list_regions",
        title="Сарафан: список регионов",
        description=(
            "Список регионов Сарафана из локальной базы: код, название, тип, активность. "
            "Read-only."
        ),
        handler=_list_regions,
        params_model=ListRegionsInput,
    )
    registry.add(
        "sarafan_find_posts",
        title="Сарафан: посты региона",
        description=(
            "Выборка постов региона из локальной базы: текст (усечён), AI-вердикт, "
            "дата публикации. Read-only."
        ),
        handler=_find_posts,
        params_model=FindPostsInput,
    )
    registry.add(
        "sarafan_region_stats",
        title="Сарафан: счётчики региона",
        description=(
            "Счётчики региона из локальной базы: посты всего и с вердиктом, "
            "сообщества всего и активные. Read-only."
        ),
        handler=_region_stats,
        params_model=RegionStatsInput,
    )
    return registry


__all__: List[str] = [
    "FindPostsInput",
    "ListRegionsInput",
    "RegionStatsInput",
    "build_local_tools",
]
