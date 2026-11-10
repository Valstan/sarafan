"""Веб-панель локального движка: что сделал, какое качество, работает ли обучение.

Роутер отдаёт одну сводку (:func:`modules.classifier.local_engine.stats`):
прогоны локального демона, согласие оператора отдельно по облаку и по локальной
модели, динамика качества по неделям («растёт ли фильтрация») и поток обучения
(коррекции → черновики правил → утверждённые правила).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request

from database.connection import AsyncSessionLocal
from modules.classifier import local_engine

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/stats")
async def stats(request: Request) -> Dict[str, Any]:
    """Сводка по локальному движку, качеству и обучению."""
    async with AsyncSessionLocal() as session:
        data = await local_engine.stats(session)
    return {"ok": True, **data}
