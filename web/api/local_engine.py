"""Веб-панель локального движка: что сделал, какое качество, работает ли обучение.

Роутер отдаёт одну сводку: :func:`modules.classifier.local_engine.stats` (движок,
качество, обучение) плюс :func:`modules.classifier.engine_panel.stats` (волны,
публикации, сайты, источники, живой журнал MiniMax).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request

from database.connection import AsyncSessionLocal
from modules.classifier import engine_panel, local_engine

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/stats")
async def stats(request: Request) -> Dict[str, Any]:
    """Сводка по локальному движку, качеству и обучению."""
    async with AsyncSessionLocal() as session:
        data = await local_engine.stats(session)
        panel = await engine_panel.stats(session)
    return {"ok": True, **data, **panel}
