"""Discovery API — авто-регистрация регионов и сообществ (big idea 2026-05-22).

Endpoints:

- ``GET  /api/discovery/cities?q=…``       — VK ``database.getCities`` resolver
                                            для wizard'а нового региона.
- ``POST /api/discovery/trigger``           — запустить discovery для региона
                                            (синхронно, держим запрос пока
                                            не вернётся результат — UI wizard
                                            ждёт, обычно 10-60 сек).
- ``GET  /api/discovery/candidates?region_id=…`` — список кандидатов региона.
- ``PATCH /api/discovery/candidates/{cid}`` — approve / reject / defer.
- ``POST /api/discovery/candidates/bulk``   — массовая операция (фильтр
                                            по confidence / категории).

«Approve» создаёт запись в `communities` (через composite unique
``(region_id, vk_id)``) и помечает candidate как ``approved``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import List, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy import select, update

from config.runtime import discovery_ai_batch_mode
from database.connection import AsyncSessionLocal
from database.models import Community, CommunityCandidate, Region
from modules.discovery.ai_categorizer import ALLOWED_CATEGORIES
from modules.vk_monitor.vk_client import VKClient
from modules.vk_token_router import get_healthy_read_token
from tasks.discovery_tasks import parse_list_field, run_discovery_for_region_async
from utils.vk_url import parse_vk_group_url

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_STATUSES = {"pending", "approved", "rejected", "deferred"}


# ─────────────────────────────────────────────────────────────────
# /cities — VK database.getCities resolver
# ─────────────────────────────────────────────────────────────────


@router.get("/cities")
async def resolve_city(q: str = Query(..., min_length=1, max_length=120)):
    """Resolve human-readable city name → list of VK cities for dropdown."""
    token = await get_healthy_read_token()
    if not token:
        raise HTTPException(status_code=503, detail="no healthy VK parse-token")
    client = VKClient(token=token)
    items = client.resolve_city(query=q)
    # Trim payload to fields useful for UI.
    return {
        "items": [
            {
                "id": int(it.get("id") or 0),
                "title": it.get("title") or "",
                "area": it.get("area") or "",
                "region": it.get("region") or "",
            }
            for it in items
            if it.get("id")
        ]
    }


# ─────────────────────────────────────────────────────────────────
# /regions/{code}/config — save localities / discovery_keywords
# ─────────────────────────────────────────────────────────────────
#
# OSM Overpass auto-suggest удалён 2026-05-25 (smoke-feedback по tuzha):
# не находил мелкие районы (Тужа/Тужинский), нейросеть через clipboard-prompt
# работает значительно лучше. См. удалённый `modules/discovery/osm_overpass.py`
# и endpoint `/osm-localities` в `git log`.


class _DiscoveryConfigPatch(BaseModel):
    """Body для PATCH discovery-конфига региона.

    Поля:
    - ``localities`` / ``discovery_keywords``: list[str] или строка (raw textarea).
    - ``center_city``: строка (имя центра района, обязательное для запуска
      discovery).
    - ``vk_city_id``: integer (VK API city_id для гео-поиска, опционально).

    Парс list-полей делегируется ``parse_list_field``.
    """

    value: Union[List[str], str, int, None] = None


_DISCOVERY_LIST_FIELDS = {"localities", "discovery_keywords"}
_DISCOVERY_REGION_FIELDS = {"center_city", "vk_city_id"}


@router.patch("/regions/{code}/config/{field}")
async def patch_region_discovery_config(code: str, field: str, body: _DiscoveryConfigPatch):
    """Update discovery-конфиг региона.

    Поддерживает:
    - ``localities``, ``discovery_keywords`` → запись в ``regions.config[field]``.
    - ``center_city`` → запись в ``regions.center_city`` (Region column).
    - ``vk_city_id`` → запись в ``regions.vk_city_id`` (Region column).

    Возвращает ``{ok, count, items}`` для list-полей либо
    ``{ok, value}`` для scalar-полей (center_city / vk_city_id).

    Эксплицитный INFO-лог входа добавлен 2026-05-25 после smoke-feedback'а
    «Failed to fetch» на tuzha: запрос в uvicorn-логе не появлялся, что мешало
    диагностике. Теперь увидим факт прихода body + размер до парсинга.
    """
    raw = body.value
    raw_len = len(raw) if isinstance(raw, str) else (len(raw) if isinstance(raw, list) else 0)
    logger.info(
        "discovery.config PATCH region=%s field=%s raw_type=%s raw_len=%s",
        code,
        field,
        type(raw).__name__,
        raw_len,
    )
    if field not in _DISCOVERY_LIST_FIELDS and field not in _DISCOVERY_REGION_FIELDS:
        raise HTTPException(status_code=400, detail=f"unknown config field {field!r}")

    async with AsyncSessionLocal() as session:
        region = (
            await session.execute(select(Region).where(Region.code == code))
        ).scalar_one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail=f"region {code!r} not found")

        if field in _DISCOVERY_LIST_FIELDS:
            items = parse_list_field(raw)
            cfg = dict(region.config or {})
            cfg[field] = items
            region.config = cfg
            await session.commit()
            logger.info(
                "discovery.config PATCH region=%s field=%s saved %d items",
                code,
                field,
                len(items),
            )
            return {"ok": True, "count": len(items), "items": items, "field": field}

        # Scalar fields на Region.
        if field == "center_city":
            value = (raw or "").strip() if isinstance(raw, str) else None
            if not value:
                raise HTTPException(status_code=400, detail="center_city не может быть пустым")
            region.center_city = value
        elif field == "vk_city_id":
            if raw in (None, "", 0):
                region.vk_city_id = None
                value = None
            else:
                try:
                    value = int(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="vk_city_id должен быть integer")
                region.vk_city_id = value
        region.updated_at = datetime.utcnow()
        await session.commit()
        logger.info("discovery.config PATCH region=%s field=%s saved value=%r", code, field, value)
        return {"ok": True, "value": value, "field": field}


@router.get("/regions/{code}/config")
async def get_region_discovery_config(code: str):
    """Return current discovery-related config for a region.

    Используется prepare-страницей для предзагрузки уже сохранённых
    localities/keywords (если юзер возвращается к существующему региону).
    """
    async with AsyncSessionLocal() as session:
        region = (
            await session.execute(select(Region).where(Region.code == code))
        ).scalar_one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail=f"region {code!r} not found")
        cfg = region.config or {}
        return {
            "code": region.code,
            "name": region.name,
            "center_city": region.center_city,
            "vk_city_id": region.vk_city_id,
            "vk_group_id": region.vk_group_id,
            "localities": parse_list_field(cfg.get("localities")),
            "discovery_keywords": parse_list_field(cfg.get("discovery_keywords")),
        }


# ─────────────────────────────────────────────────────────────────
# /ai-batch — human-in-the-loop AI categorisation through clipboard
# ─────────────────────────────────────────────────────────────────
#
# Заведено, когда Groq на проде отдавал 403 (бюджета не было): программа
# готовит чанк (JSON + готовый prompt), юзер копирует в ChatGPT/Claude в
# браузере, копирует JSON-ответ обратно, программа парсит и обновляет БД.
#
# С 2026-08-12 движок — DeepSeek с рабочим ключом (D-024), и обходной путь
# больше не вынужденный. Роль ветки честно разведена флагом
# DISCOVERY_AI_BATCH_MODE (P043): auto — авто-путь главный, clipboard скрыт;
# manual — ручной запасной путь (DeepSeek недоступен); off — endpoints 404.

AI_BATCH_CHUNK_SIZE_DEFAULT = 30
AI_BATCH_CHUNK_SIZE_MAX = 100  # длинные prompt'ы режутся LLM-ом, держим разумно


def _ai_batch_mode_or_404() -> str:
    """Режим clipboard-пути; ``off`` → 404 на всех трёх endpoints (P043)."""
    mode = discovery_ai_batch_mode()
    if mode == "off":
        raise HTTPException(
            status_code=404,
            detail="ai-batch выключен флагом DISCOVERY_AI_BATCH_MODE=off",
        )
    return mode


def _build_ai_batch_prompt(region_name: str, localities: List[str], chunk: List[dict]) -> str:
    """Build the LLM prompt for one batch chunk.

    Промпт описывает задачу + перечисляет локалитеты района (чтобы LLM
    оценил geo-релевантность каждого кандидата) + даёт строгий формат
    JSON-ответа. Категории дублируют ``ALLOWED_CATEGORIES`` (см.
    modules/discovery/ai_categorizer.py) — список одной правды.
    """
    cats_list = ", ".join(ALLOWED_CATEGORIES)
    localities_str = ", ".join(localities[:30]) if localities else "<не указаны>"
    chunk_json = json.dumps(chunk, ensure_ascii=False, indent=2)
    return (
        f"Ты — модератор сети региональных VK-пабликов. Регион: «{region_name}».\n"
        f"Населённые пункты района: {localities_str}.\n\n"
        f"Ниже JSON-массив VK-сообществ. Для КАЖДОГО оцени:\n"
        f"  1. category — одна из: {cats_list}.\n"
        f"     admin=органы власти, novost=новостной, reklama=объявления/барахолка,\n"
        f"     sosed=соседи/ДТП, kultura=культура/афиша, sport=спорт,\n"
        f"     detsad=школы/детсад/родители, other=ничего из перечисленного.\n"
        f"  2. is_relevant — true/false: принадлежит ли сообщество географически\n"
        f"     этому району (упоминает локалитеты района или явно про него).\n"
        f"     ОБЯЗАТЕЛЬНО false для общегородских/областных пабликов.\n"
        f"  3. confidence — целое 0..100, насколько уверен в category.\n"
        f"  4. reasoning — одна короткая фраза, почему.\n\n"
        f"Верни СТРОГО JSON-массив, БЕЗ markdown-обёртки, БЕЗ префикса 'json':\n"
        f'[{{"id": 1, "category": "novost", "is_relevant": true, "confidence": 90,'
        f' "reasoning": "..." }}, ...]\n\n'
        f"Входные кандидаты:\n{chunk_json}"
    )


def _candidate_to_batch_item(c: CommunityCandidate) -> dict:
    """Сжатый snapshot кандидата для prompt'а — без полей, которые нейронке не нужны."""
    return {
        "id": int(c.id),
        "name": (c.name or "").strip(),
        "desc": (c.description or "").strip()[:600],
    }


async def _pending_uncategorized_ids(session, region_id: int) -> List[int]:
    """Все pending-кандидаты региона без ai_category, отсортированные стабильно.

    Стабильность важна — между чанками может пройти время, при перезагрузке
    страницы юзер должен попасть на тот же чанк. Сортировка по
    ``-members_count, id`` даёт детерминированный порядок.
    """
    stmt = (
        select(CommunityCandidate)
        .where(
            CommunityCandidate.region_id == region_id,
            CommunityCandidate.status == "pending",
            CommunityCandidate.ai_category.is_(None),
        )
        .order_by(
            CommunityCandidate.members_count.desc().nullslast(),
            CommunityCandidate.id,
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


@router.get("/regions/{code}/ai-batch")
async def get_ai_batch(
    code: str,
    chunk: int = Query(0, ge=0),
    size: int = Query(AI_BATCH_CHUNK_SIZE_DEFAULT, ge=1, le=AI_BATCH_CHUNK_SIZE_MAX),
):
    """Return one chunk of pending-uncategorized candidates + ready prompt.

    Чанки нарезаются стабильно — повторный запрос с тем же chunk вернёт
    тех же кандидатов (если БД не менялась). Если chunk вышел за пределы —
    возвращается ``{items: [], prompt: "", chunk_index, chunks_total}``.
    """
    _ai_batch_mode_or_404()
    async with AsyncSessionLocal() as session:
        region = (
            await session.execute(select(Region).where(Region.code == code))
        ).scalar_one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail=f"region {code!r} not found")
        all_pending = await _pending_uncategorized_ids(session, region.id)

    total = len(all_pending)
    chunks_total = max(1, (total + size - 1) // size) if total else 0

    start = chunk * size
    end = start + size
    page = all_pending[start:end]
    items = [_candidate_to_batch_item(c) for c in page]

    cfg = region.config or {}
    localities = parse_list_field(cfg.get("localities"))
    prompt = _build_ai_batch_prompt(region.name or region.code, localities, items) if items else ""

    return {
        "region_code": region.code,
        "chunk_index": chunk,
        "chunks_total": chunks_total,
        "chunk_size": size,
        "total_pending_uncategorized": total,
        "items": items,
        "prompt": prompt,
    }


class _AiBatchItem(BaseModel):
    """Один элемент ответа нейросети."""

    id: int
    category: Optional[str] = None
    is_relevant: Optional[bool] = None
    confidence: Optional[int] = Field(default=None, ge=0, le=100)
    reasoning: Optional[str] = None

    @validator("category")
    def _norm_category(cls, v):
        if v is None or v == "":
            return None
        v = str(v).strip().lower()
        if v not in ALLOWED_CATEGORIES:
            return None  # silently drop unknown — БД сохранит NULL
        return v


class _AiBatchApply(BaseModel):
    items: List[_AiBatchItem]


@router.post("/regions/{code}/ai-batch/apply")
async def apply_ai_batch(code: str, body: _AiBatchApply):
    """Apply LLM-supplied categorisation to pending candidates.

    Перетираем поля только у status=pending — approved/rejected не трогаем
    (модератор уже решил). По каждому элементу: category, ai_is_relevant,
    confidence, reasoning. Не указанные поля (None) — не меняем.

    Возвращает ``{updated, skipped, missing_ids, summary}``.
    """
    _ai_batch_mode_or_404()
    if not body.items:
        return {"updated": 0, "skipped": 0, "missing_ids": [], "summary": {}}

    ids = [int(it.id) for it in body.items]
    async with AsyncSessionLocal() as session:
        region = (
            await session.execute(select(Region).where(Region.code == code))
        ).scalar_one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail=f"region {code!r} not found")

        existing = {
            c.id: c
            for c in (
                await session.execute(
                    select(CommunityCandidate).where(
                        CommunityCandidate.region_id == region.id,
                        CommunityCandidate.id.in_(ids),
                    )
                )
            )
            .scalars()
            .all()
        }

        updated = 0
        skipped = 0
        missing: List[int] = []
        relevant_count = 0
        irrelevant_count = 0
        for it in body.items:
            row = existing.get(int(it.id))
            if row is None:
                missing.append(int(it.id))
                continue
            if row.status != "pending":
                skipped += 1
                continue
            if it.category is not None:
                row.ai_category = it.category
            if it.is_relevant is not None:
                row.ai_is_relevant = bool(it.is_relevant)
                if it.is_relevant:
                    relevant_count += 1
                else:
                    irrelevant_count += 1
            if it.confidence is not None:
                row.ai_confidence = int(it.confidence)
            if it.reasoning is not None:
                row.ai_reasoning = (it.reasoning or "").strip()[:400] or None
            row.updated_at = datetime.utcnow()
            updated += 1

        await session.commit()

    return {
        "updated": updated,
        "skipped": skipped,
        "missing_ids": missing,
        "summary": {
            "relevant": relevant_count,
            "irrelevant": irrelevant_count,
        },
    }


@router.get("/regions/{code}/ai-batch/status")
async def ai_batch_status(code: str):
    """Compact progress endpoint for the UI heartbeat.

    Considers a candidate «done» if ai_category IS NOT NULL OR
    ai_is_relevant IS NOT NULL — оба поля заполняются apply'ем.
    """
    mode = _ai_batch_mode_or_404()
    async with AsyncSessionLocal() as session:
        region = (
            await session.execute(select(Region).where(Region.code == code))
        ).scalar_one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail=f"region {code!r} not found")

        pending_total = (
            (
                await session.execute(
                    select(CommunityCandidate).where(
                        CommunityCandidate.region_id == region.id,
                        CommunityCandidate.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        total = len(pending_total)
        processed = sum(
            1 for c in pending_total if c.ai_category is not None or c.ai_is_relevant is not None
        )
    return {
        "region_code": code,
        "total": total,
        "processed": processed,
        "remaining": total - processed,
        "mode": mode,
    }


# ─────────────────────────────────────────────────────────────────
# /trigger — kick off discovery for a region
# ─────────────────────────────────────────────────────────────────


class TriggerIn(BaseModel):
    region_id: int
    categories: Optional[List[str]] = None  # subset of CATEGORY_KEYWORDS keys
    per_query_count: int = Field(default=100, ge=10, le=1000)

    @validator("categories", each_item=True)
    def _valid_cat(cls, v):
        if v not in ALLOWED_CATEGORIES:
            raise ValueError(f"unknown category: {v!r}")
        return v


@router.post("/trigger-async")
async def trigger_discovery_async(payload: TriggerIn):
    """Async-вариант ``/trigger``: ставит задачу в Celery, возвращает task_id.

    Клиент опрашивает ``/task/{task_id}`` для прогресса. Используется UI на
    ``/regions`` (refresh-кнопка из PR #46) и на ``/regions/<code>/prepare``
    (кнопка «Запустить discovery») — там запросы длинные (до 5+ минут для
    крупных районов с 60+ нп), синхронный путь упирается в nginx-timeout
    и оставляет UI в зависшем состоянии.

    Возвращает ``{task_id, state}`` где ``state`` обычно ``PENDING`` (Celery
    ещё не подхватил) или ``STARTED``. Дубли с существующими communities
    исключаются автоматически — см. ``_existing_vk_ids``.
    """
    try:
        from tasks.celery_app import app as celery_app
    except ImportError as e:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"Celery не доступен: {e}")

    try:
        task = celery_app.send_task(
            "tasks.discovery_tasks.run_discovery_for_region",
            args=[payload.region_id, payload.categories],
        )
    except Exception as e:
        logger.exception("trigger-async send_task failed")
        raise HTTPException(status_code=500, detail=f"Не удалось поставить задачу: {e}")

    return {"task_id": task.id, "state": task.state, "region_id": payload.region_id}


@router.get("/task/{task_id}")
async def get_discovery_task_status(task_id: str):
    """Статус Celery-задачи discovery (для polling из UI).

    Возвращает ``{state, ready, result, error}``:
    - ``state`` ∈ {PENDING, STARTED, SUCCESS, FAILURE, RETRY, REVOKED}
    - ``ready`` — True если задача завершена (success или failure)
    - ``result`` — отчёт runner'а если ``state=SUCCESS`` (``found``,
      ``inserted``, ``refreshed`` и т.д.), иначе ``None``
    - ``error`` — текст ошибки если ``state=FAILURE``, иначе ``None``
    """
    try:
        from celery.result import AsyncResult

        from tasks.celery_app import app as celery_app
    except ImportError as e:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"Celery не доступен: {e}")

    ar = AsyncResult(task_id, app=celery_app)
    state = ar.state
    ready = ar.ready()

    payload = {"task_id": task_id, "state": state, "ready": ready, "result": None, "error": None}

    if state == "SUCCESS":
        try:
            payload["result"] = ar.result
        except Exception as e:  # pragma: no cover
            payload["error"] = f"не удалось получить result: {e}"
    elif state == "FAILURE":
        try:
            payload["error"] = str(ar.result)
        except Exception:  # pragma: no cover
            payload["error"] = "задача завершилась с ошибкой"

    return payload


@router.post("/trigger")
async def trigger_discovery(payload: TriggerIn):
    """Run discovery for one region. Synchronous — UI wizard ждёт результата.

    Работает как для wizard'а нового региона, так и для refresh-кнопки
    «Найти новые сообщества» на ``/regions``. Существующие communities и
    rejected-кандидаты автоматически исключаются ``_existing_vk_ids`` внутри
    ``run_discovery_for_region_async`` — дублей в БД не появится.

    При успехе обновляет ``regions.last_discovery_at`` — для отображения
    «когда последний раз искали» на ``/regions`` и для будущей beat-ротации.
    """
    try:
        result = await run_discovery_for_region_async(
            region_id=payload.region_id,
            categories=payload.categories,
            per_query_count=payload.per_query_count,
        )
    except Exception as e:
        logger.exception("discovery trigger failed")
        raise HTTPException(status_code=500, detail=str(e))
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "discovery failed")

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Region)
            .where(Region.id == payload.region_id)
            .values(last_discovery_at=datetime.utcnow())
        )
        await session.commit()

    return result


# ─────────────────────────────────────────────────────────────────
# /candidates — list / filter
# ─────────────────────────────────────────────────────────────────


@router.get("/candidates")
async def list_candidates(
    region_id: int = Query(..., ge=1),
    status: Optional[str] = Query(None),
    min_confidence: Optional[int] = Query(None, ge=0, le=100),
    only_info_pages: bool = Query(False),
):
    """List candidates for a region, filterable by status / confidence / flag."""
    if status and status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status {status!r}")

    async with AsyncSessionLocal() as session:
        stmt = (
            select(CommunityCandidate)
            .where(CommunityCandidate.region_id == region_id)
            .order_by(
                CommunityCandidate.ai_is_info_page.desc(),
                CommunityCandidate.ai_confidence.desc().nullslast(),
                CommunityCandidate.members_count.desc().nullslast(),
                CommunityCandidate.id.desc(),
            )
        )
        if status:
            stmt = stmt.where(CommunityCandidate.status == status)
        if min_confidence is not None:
            stmt = stmt.where(CommunityCandidate.ai_confidence >= min_confidence)
        if only_info_pages:
            stmt = stmt.where(CommunityCandidate.ai_is_info_page.is_(True))

        rows = (await session.execute(stmt)).scalars().all()
        return {"candidates": [r.to_dict() for r in rows], "count": len(rows)}


# ─────────────────────────────────────────────────────────────────
# PATCH /candidates/{id} — approve / reject / defer
# ─────────────────────────────────────────────────────────────────


class CandidatePatch(BaseModel):
    # И status, и category — опциональны. Допустимые комбинации:
    #   {status: approved, category: ...}  — одобрить с конкретной категорией
    #   {status: rejected}                 — отклонить
    #   {status: deferred}                 — отложить
    #   {category: ...}                    — только сменить AI-категорию
    #                                        (двух-этапный flow: модератор
    #                                        перетасовывает по тематикам до
    #                                        финального commit'а региона)
    status: Optional[str] = None
    category: Optional[str] = None

    @validator("status")
    def _valid_status(cls, v):
        if v is None:
            return None
        v = (v or "").strip().lower()
        if v not in {"approved", "rejected", "deferred"}:
            raise ValueError(f"invalid status {v!r}")
        return v

    @validator("category")
    def _valid_cat(cls, v):
        if v is None or v == "":
            return None
        if v not in ALLOWED_CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v


async def _approve_candidate(session, candidate: CommunityCandidate, category: str) -> Community:
    """Create a Community row for an approved candidate.

    Идемпотентность — на уровне ``(region_id, vk_id, category)``: одна VK-группа
    может жить в `communities` с разными category одновременно (см.
    database/migrations/011 — комментарий к idx_communities_region_vk).
    Если запись с такой тройкой уже есть, освежаем её; иначе INSERT.
    """
    existing: Optional[Community] = (
        await session.execute(
            select(Community)
            .where(
                Community.region_id == candidate.region_id,
                Community.vk_id == candidate.vk_id,
                Community.category == category,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.name = candidate.name or existing.name
        existing.screen_name = candidate.screen_name or existing.screen_name
        existing.is_active = True
        return existing
    community = Community(
        region_id=candidate.region_id,
        vk_id=candidate.vk_id,
        name=candidate.name,
        screen_name=candidate.screen_name,
        category=category,
        is_active=True,
        health_status="active",
    )
    session.add(community)
    return community


@router.patch("/candidates/{candidate_id}")
async def patch_candidate(candidate_id: int, payload: CandidatePatch):
    """Approve / reject / defer / re-categorise a candidate.

    Допустимые комбинации body — см. ``CandidatePatch``. Если задан только
    ``category`` без ``status`` — обновляем `ai_category` (для двух-этапного
    UI flow). Approve без конкретной category → 400.
    """
    if payload.status is None and payload.category is None:
        raise HTTPException(status_code=400, detail="body must include status and/or category")

    async with AsyncSessionLocal() as session:
        cand = await session.get(CommunityCandidate, candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")

        # Category-only patch — re-categorise (для inline-dropdown в UI).
        if payload.status is None:
            cand.ai_category = payload.category
            cand.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(cand)
            return {"candidate": cand.to_dict()}

        new_status = payload.status
        if new_status == "approved":
            category = payload.category or cand.ai_category
            if not category or category not in ALLOWED_CATEGORIES or category == "other":
                raise HTTPException(
                    status_code=400,
                    detail="approve requires a concrete category (cannot be 'other' or empty)",
                )
            community = await _approve_candidate(session, cand, category)
            cand.status = "approved"
            await session.commit()
            await session.refresh(cand)
            await session.refresh(community)
            return {
                "candidate": cand.to_dict(),
                "community_id": community.id,
            }

        # reject / defer — просто обновляем статус.
        cand.status = new_status
        await session.commit()
        await session.refresh(cand)
        return {"candidate": cand.to_dict()}


# ─────────────────────────────────────────────────────────────────
# DELETE /candidates/{id} — hard-delete (физически убрать из БД)
# ─────────────────────────────────────────────────────────────────


@router.delete("/candidates/{candidate_id}")
async def delete_candidate(candidate_id: int):
    """Физически удалить кандидата из community_candidates.

    Семантика vs reject:
    - ``reject`` (PATCH со status=rejected) — soft. Запись остаётся, при следующем
      запуске discovery её vk_id попадает в exclude_ids и группа не вернётся.
    - ``delete`` (этот endpoint) — hard. Запись физически удалена, при rerun
      discovery эту группу может найти снова, если VK всё ещё её отдаёт. Удобно
      для откровенно нерелевантного шума, который не страшно увидеть снова
      (или который VK больше не вернёт, потому что она уехала из результатов).
    """
    async with AsyncSessionLocal() as session:
        cand = await session.get(CommunityCandidate, candidate_id)
        if cand is None:
            raise HTTPException(status_code=404, detail="candidate not found")
        await session.delete(cand)
        await session.commit()
        return {"deleted": candidate_id}


# ─────────────────────────────────────────────────────────────────
# POST /candidates/bulk — массовые операции
# ─────────────────────────────────────────────────────────────────


class BulkPatch(BaseModel):
    region_id: int
    status: str  # approved / rejected / deferred
    min_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    categories: Optional[List[str]] = None  # фильтр по AI category
    only_info_pages: bool = False

    @validator("status")
    def _valid_status(cls, v):
        v = (v or "").strip().lower()
        if v not in {"approved", "rejected", "deferred"}:
            raise ValueError(f"invalid status {v!r}")
        return v


@router.post("/candidates/bulk")
async def bulk_patch(payload: BulkPatch):
    """Bulk operation. For ``approved`` we ONLY auto-approve candidates whose
    ``ai_category`` is concrete (not None / not 'other') — иначе approve
    требует ручного выбора категории."""
    async with AsyncSessionLocal() as session:
        stmt = select(CommunityCandidate).where(
            CommunityCandidate.region_id == payload.region_id,
            CommunityCandidate.status == "pending",
        )
        if payload.min_confidence is not None:
            stmt = stmt.where(CommunityCandidate.ai_confidence >= payload.min_confidence)
        if payload.categories:
            stmt = stmt.where(CommunityCandidate.ai_category.in_(payload.categories))
        if payload.only_info_pages:
            stmt = stmt.where(CommunityCandidate.ai_is_info_page.is_(True))

        cands = (await session.execute(stmt)).scalars().all()

        if payload.status == "approved":
            approved_n = 0
            skipped_no_cat = 0
            for cand in cands:
                cat = cand.ai_category
                if not cat or cat == "other":
                    skipped_no_cat += 1
                    continue
                await _approve_candidate(session, cand, cat)
                cand.status = "approved"
                approved_n += 1
            await session.commit()
            return {
                "matched": len(cands),
                "approved": approved_n,
                "skipped_no_category": skipped_no_cat,
            }

        # rejected / deferred — массово.
        n = 0
        for cand in cands:
            cand.status = payload.status
            n += 1
        await session.commit()
        return {"matched": len(cands), "updated": n, "status": payload.status}


# ─────────────────────────────────────────────────────────────────
# POST /candidates/bulk-action — массовое действие по выбранным id
# ─────────────────────────────────────────────────────────────────
#
# Отличается от /candidates/bulk (по фильтрам): здесь — явный список id
# из UI-выбора чекбоксами. Поддерживаемые действия:
#
#   reject / defer        — soft status change
#   approve               — создать Community для каждого выбранного с
#                           конкретной ai_category (или общим category override)
#   delete                — physical delete (как DELETE /candidates/{id})
#   set_category          — обновить ai_category у выбранных pending'ов
#                           без смены status (для перетасовки тематик)
#
# Все ветки атомарны: одна транзакция, либо всё, либо ничего.


_BULK_ACTION_ALLOWED = {"reject", "defer", "approve", "delete", "set_category"}


class BulkActionPayload(BaseModel):
    """Body для bulk-action по списку id, выбранному чекбоксами в UI.

    Поля:
    - ``ids``: непустой список id кандидатов.
    - ``action``: одно из ``_BULK_ACTION_ALLOWED``.
    - ``category``: обязателен для ``set_category``; для ``approve`` —
      опциональный override (если задан, перетирает ai_category каждого
      кандидата; иначе используется собственный ai_category каждого).
    """

    ids: List[int] = Field(..., min_items=1)
    action: str
    category: Optional[str] = None

    @validator("action")
    def _valid_action(cls, v):
        v = (v or "").strip().lower()
        if v not in _BULK_ACTION_ALLOWED:
            raise ValueError(f"unknown action {v!r}")
        return v

    @validator("category")
    def _valid_cat(cls, v):
        if v is None or v == "":
            return None
        if v not in ALLOWED_CATEGORIES:
            raise ValueError(f"unknown category {v!r}")
        return v


@router.post("/candidates/bulk-action")
async def bulk_action(payload: BulkActionPayload):
    """Apply ``payload.action`` to candidates listed in ``payload.ids``.

    Семантика статусов:
    - ``reject`` / ``defer`` — обновляем status у любых найденных id (включая
      уже approved/rejected — для отмены ошибочного approve через UI).
    - ``approve`` — берём только pending; требует, чтобы у каждого выбранного
      была конкретная категория (через ``payload.category`` или собственная
      ai_category, не 'other'). Если хоть у одного нет — возвращаем
      ``skipped_no_category`` со списком id, но approved остальные.
    - ``delete`` — физически удаляем все найденные.
    - ``set_category`` — обновляем ``ai_category`` (требует payload.category),
      работает на любом status. Status не трогаем.

    Возвращает: ``{action, matched, updated, missing_ids, skipped_no_category}``.
    ``skipped_no_category`` присутствует только для action=approve.
    """
    ids = list(dict.fromkeys(int(i) for i in payload.ids))  # dedup, preserve order
    action = payload.action

    if action == "set_category" and not payload.category:
        raise HTTPException(
            status_code=400,
            detail="action=set_category требует поле category",
        )

    async with AsyncSessionLocal() as session:
        rows: List[CommunityCandidate] = (
            (
                await session.execute(
                    select(CommunityCandidate).where(CommunityCandidate.id.in_(ids))
                )
            )
            .scalars()
            .all()
        )
        found_ids = {c.id for c in rows}
        missing_ids = [i for i in ids if i not in found_ids]

        if action == "delete":
            for cand in rows:
                await session.delete(cand)
            await session.commit()
            return {
                "action": action,
                "matched": len(rows),
                "updated": len(rows),
                "missing_ids": missing_ids,
            }

        if action == "set_category":
            for cand in rows:
                cand.ai_category = payload.category
                cand.updated_at = datetime.utcnow()
            await session.commit()
            return {
                "action": action,
                "matched": len(rows),
                "updated": len(rows),
                "missing_ids": missing_ids,
                "category": payload.category,
            }

        if action in ("reject", "defer"):
            new_status = "rejected" if action == "reject" else "deferred"
            for cand in rows:
                cand.status = new_status
                cand.updated_at = datetime.utcnow()
            await session.commit()
            return {
                "action": action,
                "matched": len(rows),
                "updated": len(rows),
                "missing_ids": missing_ids,
            }

        # approve
        approved_n = 0
        skipped_no_category: List[int] = []
        for cand in rows:
            if cand.status != "pending":
                # уже approved/rejected/deferred — пропускаем без шума
                continue
            cat = payload.category or cand.ai_category
            if not cat or cat == "other" or cat not in ALLOWED_CATEGORIES:
                skipped_no_category.append(cand.id)
                continue
            await _approve_candidate(session, cand, cat)
            cand.status = "approved"
            cand.updated_at = datetime.utcnow()
            approved_n += 1
        await session.commit()
        return {
            "action": action,
            "matched": len(rows),
            "updated": approved_n,
            "missing_ids": missing_ids,
            "skipped_no_category": skipped_no_category,
        }


# ─────────────────────────────────────────────────────────────────
# /resolve-vk-url — превратить ссылку на VK-сообщество в (group_id, name)
# ─────────────────────────────────────────────────────────────────


@router.get("/resolve-vk-url")
async def resolve_vk_url(url: str = Query(..., min_length=1, max_length=500)):
    """Превратить URL/screen_name/ID VK-сообщества в `{group_id, name}`.

    Используется wizard'ом для поля «Главная группа региона». Если URL —
    screen_name, делает один VK API `utils.resolveScreenName` + `groups.getById`
    для подтверждения и получения title; если уже числовой club/public id —
    идёт сразу в `groups.getById`.
    """
    group_id, screen_name = parse_vk_group_url(url)
    if group_id is None and screen_name is None:
        raise HTTPException(status_code=400, detail="не удалось распознать VK-ссылку")

    token = await get_healthy_read_token()
    if not token:
        raise HTTPException(status_code=503, detail="no healthy VK parse-token")
    client = VKClient(token=token)

    if group_id is None and screen_name is not None:
        try:
            resolved = client.vk.utils.resolveScreenName(screen_name=screen_name)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"VK resolveScreenName failed: {e}")
        if not resolved or resolved.get("type") != "group":
            raise HTTPException(
                status_code=404,
                detail=f"VK не нашёл группу с адресом '{screen_name}'",
            )
        group_id = int(resolved.get("object_id") or 0)

    if not group_id:
        raise HTTPException(status_code=400, detail="не удалось определить group_id")

    try:
        infos = client.get_groups_by_ids([group_id], fields="screen_name,members_count,photo_200")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"VK groups.getById failed: {e}")
    if not infos:
        raise HTTPException(status_code=404, detail=f"VK group {group_id} не найден")
    info = infos[0]
    return {
        "group_id": group_id,
        "screen_name": info.get("screen_name") or screen_name,
        "name": info.get("name") or "",
        "members_count": info.get("members_count"),
        "photo_url": info.get("photo_200"),
    }


# ─────────────────────────────────────────────────────────────────
# /commit/{region_id} — финализировать черновик региона
# ─────────────────────────────────────────────────────────────────


@router.post("/commit/{region_id}")
async def commit_region(region_id: int):
    """Финализация двух-этапного flow создания региона.

    Что делает:
    1. Проверяет `region.vk_group_id NOT NULL` (без главной группы регион
       не попадёт в beat-расписание — см. `parsing_scheduler_tasks.py`).
    2. Bulk-approve всех **pending** кандидатов с `ai_category` ∈
       ALLOWED_CATEGORIES (кроме 'other') — создаёт `Community.is_active=True`
       для каждого через существующую `_approve_candidate` helper.
    3. Поднимает `region.is_active=True` (черновик → активный).
    4. Кандидаты, которых модератор перевёл в `rejected` / `deferred` — не
       трогаем. Остальные pending без подходящей категории остаются pending
       (модератор разберётся позже).

    Returns: ``{region_code, communities_created, pending_left, region_id}``.
    """
    async with AsyncSessionLocal() as session:
        region: Optional[Region] = (
            await session.execute(select(Region).where(Region.id == region_id))
        ).scalar_one_or_none()
        if region is None:
            raise HTTPException(status_code=404, detail="region not found")

        if not region.vk_group_id:
            raise HTTPException(
                status_code=400,
                detail="у региона не задана главная VK-группа (vk_group_id) — без неё "
                "он не попадёт в расписание парсинга",
            )

        cands = (
            (
                await session.execute(
                    select(CommunityCandidate).where(
                        CommunityCandidate.region_id == region_id,
                        CommunityCandidate.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )

        approved_n = 0
        pending_left = 0
        for cand in cands:
            cat = cand.ai_category
            if not cat or cat == "other":
                pending_left += 1
                continue
            await _approve_candidate(session, cand, cat)
            cand.status = "approved"
            approved_n += 1

        if approved_n == 0:
            raise HTTPException(
                status_code=400,
                detail="нет ни одного кандидата с категорией для approve — "
                "распределите кандидатов по тематикам или используйте reject/defer",
            )

        if not region.is_active:
            region.is_active = True
        region.updated_at = datetime.utcnow()

        await session.commit()

        return {
            "region_id": region.id,
            "region_code": region.code,
            "communities_created": approved_n,
            "pending_left": pending_left,
        }
