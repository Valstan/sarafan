"""HTTP-интерфейс HITL-классификатора для облачной рутины (``/api/classifier``).

Этап B (ADR-0003, решение владельца 2026-07-05): классификацию делает
scheduled cloud agent (Claude Code routine), пока нет ``ANTHROPIC_API_KEY``.
Рутина ходит сюда:
- ``GET  /api/classifier/pending``   — забрать батч постов без вердикта;
- ``GET  /api/classifier/postulates``— текст файла-корректировщика для промпта;
- ``POST /api/classifier/verdicts``  — вернуть вердикты.

Защита — API-ключ рутины (``X-API-Key`` = env ``CLASSIFIER_INGEST_KEY``,
constant-time), как VK-шлюз. Путь в ``PUBLIC_PREFIXES`` (своя защита, не
сессия). Kill-switch ``CLASSIFIER_DISABLED`` → 503. Когда появится Claude API,
этот ingest уступит место Celery-таску (та же ``modules.classifier.service``),
а роутер можно выключить ключом.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response

from config.classifier import (
    classifier_disabled,
    get_ingest_key,
    get_pending_max,
    get_region_allowlist,
    get_source_days,
)
from database.connection import AsyncSessionLocal
from modules.classifier import pilot_run, rules, service
from modules.classifier.schema import RuleProposalBatch, parse_verdict_loose

logger = logging.getLogger(__name__)
router = APIRouter()


def _check_enabled() -> None:
    if classifier_disabled():
        raise HTTPException(status_code=503, detail="classifier disabled")


async def require_ingest_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """Проверить ключ рутины (constant-time). Ключ не задан в env → 503 (ingest выкл.)."""
    expected = get_ingest_key()
    if not expected:
        raise HTTPException(status_code=503, detail="classifier ingest not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-API-Key")


@router.get("/pending")
async def pending(
    region: str = Query("", description="код района; пусто = allowlist из env"),
    limit: int = Query(0, ge=0, le=200),
    _auth: None = Depends(require_ingest_key),
):
    """Батч постов без вердикта для классификации рутиной.

    region пуст → берём allowlist ``CLASSIFIER_REGION_CODES`` (обкатка на одном
    районе). Батч отдаётся целиком, чтобы рутина видела посты вместе (merge).
    """
    _check_enabled()
    codes = [region.strip()] if region.strip() else get_region_allowlist()
    cap = get_pending_max()
    n = min(limit or cap, cap)
    async with AsyncSessionLocal() as session:
        posts = await service.fetch_pending(
            session, region_codes=codes or None, limit=n, days=get_source_days()
        )
    return {"region_filter": codes, "count": len(posts), "posts": posts}


@router.get("/postulates", response_class=PlainTextResponse)
async def postulates():
    """Эффективные постулаты для промпта: базовый git-файл + утверждённые выученные
    правила (overlay, ADR-0005). Рутина вставляет результат в промпт классификации."""
    _check_enabled()
    async with AsyncSessionLocal() as session:
        return await rules.render_effective_postulates(session)


@router.post("/verdicts")
async def verdicts(
    batch: dict,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_ingest_key),
):
    """Принять пакет вердиктов от рутины → записать в content_classifications.

    Разбор толерантный, per-item (``parse_verdict_loose``): один кривой вердикт
    (перелимит эха текста, мусорный confidence) больше НЕ роняет весь батч
    422-й — прогон рутины стоит токенов, чинимое чиним, нечинимое считаем в
    ``skipped_invalid``.

    **Отметка о прогоне** (``modules.classifier.pilot_run``) вешается фоновой
    задачей после записи: время разбора уходит владельцу в Telegram, чтобы
    «пора ли новый» отвечала отметка, а не память человека (pool #152).
    Ответ не ждёт Telegram — сетевой сбой не должен задерживать приём вердиктов.
    """
    _check_enabled()
    raw_list = batch.get("verdicts") if isinstance(batch, dict) else None
    if not isinstance(raw_list, list):
        raise HTTPException(status_code=422, detail="body must be {'verdicts': [...]}")
    parsed = [parse_verdict_loose(r) for r in raw_list]
    good = [v for v in parsed if v is not None]
    skipped_invalid = len(parsed) - len(good)
    if skipped_invalid:
        logger.warning(
            "classifier ingest: %d of %d verdicts unparseable — skipped",
            skipped_invalid,
            len(parsed),
        )
    async with AsyncSessionLocal() as session:
        counts = await service.record_verdicts(
            session,
            good,
            source="routine",
            region_codes_fallback=get_region_allowlist() or None,
        )
    if pilot_run.notify_enabled():
        background_tasks.add_task(
            _note_pilot_run,
            recorded=int(counts.get("recorded") or 0),
            verdicts=[dict(v) for v in good],
        )
    return {"ok": True, "skipped_invalid": skipped_invalid, **counts}


async def _note_pilot_run(*, recorded: int, verdicts: list) -> None:
    """Фоновая отметка о прогоне. Ошибки внутри ``note_run`` глушатся там."""
    await pilot_run.note_run(recorded=recorded, verdicts=verdicts)


# --- Media-прокси: рутина смотрит фото/PDF постов без текста ---------------------
#
# Egress облачного окружения рутины пускает только наш хост (Network access →
# Custom), напрямую к VK CDN ей нельзя. Прокси скачивает вложение с VK и отдаёт
# рутине. Только суффиксы VK-хостов, только под ключом, с потолком размера.

_MEDIA_ALLOWED_HOST_SUFFIXES = (".userapi.com", ".vk.com", ".vk.me", ".mycdn.me")
_MEDIA_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — фото/PDF; видео не проксируем
_MEDIA_TIMEOUT_S = 20.0


def _media_url_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return bool(host) and (
        host.endswith(_MEDIA_ALLOWED_HOST_SUFFIXES) or host in ("vk.com", "vk.me")
    )


@router.get("/media")
async def media_proxy(
    url: str = Query(..., min_length=12, max_length=2000),
    _auth: None = Depends(require_ingest_key),
):
    """Проксировать вложение VK для облачной рутины (фото/PDF постов без текста).

    ``url`` приходит из ``/pending`` (поле ``media[].url`` — снапшот аудита
    сбора), т.е. это ссылки, которые VK сам отдал при сборе. Allowlist хостов —
    защита от превращения прокси в открытый SSRF-фетчер.
    """
    _check_enabled()
    if not _media_url_allowed(url):
        raise HTTPException(status_code=400, detail="url host not allowed")
    try:
        async with httpx.AsyncClient(
            timeout=_MEDIA_TIMEOUT_S, follow_redirects=True, max_redirects=3
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=502, detail=f"upstream returned {resp.status_code}"
                    )
                # редиректы могли увести с VK-хостов — перепроверить финальный URL
                if not _media_url_allowed(str(resp.url)):
                    raise HTTPException(status_code=502, detail="redirected off allowed hosts")
                chunks = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MEDIA_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="media too large")
                    chunks.append(chunk)
                content_type = resp.headers.get("content-type", "application/octet-stream")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {exc}") from exc
    return Response(content=b"".join(chunks), media_type=content_type)


# --- Петля обучения (ADR-0005): дистилляция коррекций → черновики правил --------


@router.get("/corrections")
async def corrections(
    limit: int = Query(100, ge=1, le=500),
    days: int = Query(30, ge=1, le=180),
    _auth: None = Depends(require_ingest_key),
):
    """Коррекции оператора (несогласия) + снапшот поста — сырьё для рутины-дистиллятора.

    Рутина смотрит на них + текущие эффективные правила (``/postulates``) и предлагает
    обобщённые правила через ``POST /rule-proposals``."""
    _check_enabled()
    async with AsyncSessionLocal() as session:
        items = await rules.fetch_corrections_for_distill(session, limit=limit, days=days)
    return {"count": len(items), "corrections": items}


@router.post("/rule-proposals")
async def rule_proposals(batch: RuleProposalBatch, _auth: None = Depends(require_ingest_key)):
    """Принять черновики выученных правил от рутины → записать как ``proposed``.

    Не применяются автоматически: оператор утверждает/правит/отклоняет в ленте
    ``/classifier`` (человек в петле, ADR-0005). Дедуп против активных правил."""
    _check_enabled()
    async with AsyncSessionLocal() as session:
        counts = await rules.record_rule_proposals(session, batch.proposals, source="routine")
    return {"ok": True, **counts}
