"""Кого берёт волна: один отбор регионов на расписание и на карту конвейера.

Отбор был зашит внутри ``tasks.parsing_scheduler_tasks.run_all_regions_theme``,
и это было терпимо, пока карте конвейера не понадобилось отвечать на вопрос
«сколько районов в этой волне». Считать то же самое второй копией — значит
показывать владельцу **не то**, что реально поедет: гейт правится в одном месте,
карта продолжает рисовать старые цифры и выглядит правдой (это класс отказа
«две копии расходятся молча», [#087]).

Поэтому отбор живёт здесь, а таск и карта зовут его оба.

Правила гейта (историю см. в комментариях ниже — каждое выстрадано):

* ``strict=False`` — регион попадает в волну, если у него есть сообщества этой
  темы **или** вообще любые (fallback на «все сообщества» внутри таска). Так
  работают агрегатные темы.
* ``strict=True`` — только регионы с сообществами именно этой темы: областные
  темы не должны затягивать районы и плодить «не свои» сводки.
"""

from __future__ import annotations

from typing import List

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Community, Region
from database.models_extended import RegionConfig


def _region_gate(theme: str, strict: bool):
    """Условия отбора региона в волну темы ``theme``."""
    has_theme_communities = (
        select(Community.id)
        .where(
            Community.region_id == Region.id,
            Community.category == theme,
            Community.is_active.is_(True),
        )
        .exists()
    )
    has_any_communities = (
        select(Community.id)
        .where(
            Community.region_id == Region.id,
            Community.is_active.is_(True),
        )
        .exists()
    )
    # NB: раньше тут был хардкод ``Region.code != "kirov_obl"`` — область
    # держалась вне тематических волн (жила на каскад-слотах). С переходом
    # kirov_obl на bulletin_mode='communities' (2026-05) исключение снято:
    # каскадные регионы (tatarstan_obl/rf) и так отсекаются проверкой наличия
    # communities ниже (у них пул пуст).
    community_gate = (
        has_theme_communities if strict else (has_theme_communities | has_any_communities)
    )
    # Регион допускается в волну, если выполнено ЛЮБОЕ из:
    #   1. есть строка RegionConfig (легаси-путь, мигрировано из Mongo);
    #   2. он community-mode (config.bulletin_mode='communities') — области/
    #      страны на собственном пуле (kirov_obl/tatarstan_obl, 2026-05);
    #   3. есть хотя бы одно активное сообщество (``has_any_communities``).
    # Пункт 3 (2026-06) чинит онбординг районов: новый РАЙОН из визарда
    # `/regions/new` не получает строку region_configs (её создавала только
    # Mongo-миграция) и без bulletin_mode молча выпадал из ВСЕХ волн, хотя пул
    # источников у него есть (Тужа: 49 сообществ, 0 публикаций). Теперь регион
    # публикует сразу после засева пула. ``parse_and_publish_theme`` при
    # отсутствии RegionConfig подставляет safe-defaults.
    # ``Region.config`` — generic JSON-колонка (без .astext), поэтому достаём
    # bulletin_mode PG-оператором ``->>`` (возвращает text).
    config_gate = (
        exists().where(RegionConfig.region_code == Region.code)
        | (Region.config.op("->>")("bulletin_mode") == "communities")
        | has_any_communities
    )
    return config_gate, community_gate


async def select_regions(session: AsyncSession, *, theme: str, strict: bool = False) -> List[str]:
    """Коды регионов, которые поедут в волне ``theme``."""
    config_gate, community_gate = _region_gate(theme, strict)
    result = await session.execute(
        select(Region.code).where(
            Region.is_active.is_(True),
            Region.vk_group_id.isnot(None),
            config_gate,
            community_gate,
        )
    )
    return list(result.scalars().all())


__all__ = ["select_regions"]
