"""API разбора доноров: общие группы и молчащие доноры (P236 / P235).

**Что это.** Два хвоста, которые решает владелец, — и страница, где он решает
их мышкой. Модуль ``modules/donor_triage.py`` считает, что можно решить
автоматически, и отдаёт рекомендацию с объяснением; решение — за человеком,
поэтому здесь есть и «посмотреть», и «применить».

**Гейт.** Путь не публичный: он закрыт сессией оператора (см.
``middleware/auth_gate.py``), как остальные страницы ленты.

**Запись обратима.** Ничего не удаляется: лишние регистрации получают
``is_active=False`` + ``disabled_at`` + ``disabled_reason``, тема донора
меняется на тему со слотом, а AI-подсказка остаётся в ``suggested_category``.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from modules import donor_triage

logger = logging.getLogger(__name__)

router = APIRouter()


class SharedDecision(BaseModel):
    """Решение по общей группе: где оставляем регистрацию."""

    action: str = Field(default="skip", description="keep_region | keep_all | disable_all | skip")
    keep_region_id: Optional[int] = None


class DonorDecision(BaseModel):
    """Решение по молчащему донору."""

    action: str = Field(default="skip", description="set_theme | disable | skip")
    theme: Optional[str] = None


class ApplyRequest(BaseModel):
    """Решения по обоим разборам сразу."""

    shared: Dict[str, SharedDecision] = Field(default_factory=dict)
    donors: Dict[str, DonorDecision] = Field(default_factory=dict)


@router.get("/candidates")
async def get_candidates(db: AsyncSession = Depends(get_db_session)) -> Dict[str, Any]:
    """Кандидаты с рекомендациями: общие группы и молчащие доноры.

    **Сбор не роняет страницу.** Если разбор упал (БД недоступна, запрос
    слишком тяжёлый), владелец видит причину в карточке-предупреждении, а не
    белый экран: решение «что куда» он всё равно принимает сам.
    """
    try:
        report = await donor_triage.collect_with_vk(db)
    except Exception as exc:  # noqa: BLE001 — деградация вместо 500
        logger.warning("donor-triage: сбор разбора не удался", exc_info=True)
        return {
            "shared": [],
            "donors": [],
            "slotted_themes": list(donor_triage.SLOTTED_THEMES),
            "no_slot_themes": list(donor_triage.NO_SLOT_THEMES),
            "vk_error": "",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }
    return {
        "shared": report.get("shared", []),
        "donors": report.get("donors", []),
        "slotted_themes": report.get("slotted_themes", []),
        "no_slot_themes": report.get("no_slot_themes", []),
        "vk_error": report.get("vk_error", ""),
        "error": "",
    }


@router.post("/apply")
async def apply_decisions(
    payload: ApplyRequest, db: AsyncSession = Depends(get_db_session)
) -> Dict[str, Any]:
    """Записать решения владельца."""
    decisions = {
        "shared": {
            key: {"action": value.action, "keep_region_id": value.keep_region_id}
            for key, value in (payload.shared or {}).items()
        },
        "donors": {
            key: {"action": value.action, "theme": value.theme}
            for key, value in (payload.donors or {}).items()
        },
    }
    if not decisions["shared"] and not decisions["donors"]:
        raise HTTPException(status_code=400, detail="нечего применять")
    applied = await donor_triage.apply_decisions(db, decisions)
    return {
        "ok": True,
        "shared_changed": len(applied.get("shared", [])),
        "donors_changed": len(applied.get("donors", [])),
        "skipped": len(applied.get("skipped", [])),
        "detail": applied,
    }
