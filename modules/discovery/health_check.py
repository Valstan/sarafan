"""Health-check for already-added VK communities.

Runs against ``Community.is_active=True`` rows on weekly cadence (Celery beat,
см. `tasks/discovery_tasks.recheck_*`) и обновляет четыре поля,
заведённые миграцией 011:

- ``last_post_at`` — timestamp последнего поста на стене (для UI «когда было
  последнее движение»);
- ``checked_at`` — когда выполнялась эта проверка;
- ``health_status`` — `active` / `dormant` / `dead` / `changed_category`;
- ``suggested_category`` — если AI определил, что характер постов сместился.

Чистая логика, ничего не пишет в БД. Возвращает ``CommunityHealth`` —
вызывающий код (recheck-таски) уже сам решает, что положить в session.

Дизайн-решения:

- ``wall.get`` через ``client.api_call`` (а не ``get_wall_posts``) — нужен
  raw `error_code`, чтобы отличить «группа удалена» (15/18/100/203) от
  пустой стены. ``get_wall_posts`` глотает ApiError и возвращает [].
- AI-категоризация только при condition «свежих постов достаточно». Если
  стена пуста или последний пост старше ``dormant_days`` — сразу dormant,
  модель не дёргаем.
- AI failure (нет API-key, 429, malformed JSON) → `active`, не меняем
  ничего. Лучше промолчать, чем срывать модератора фолз-позитивом.
- ``changed_category`` ставится только при ``confidence >= 70`` и
  ``ai_cat != community.category`` и ``ai_cat != 'other'``. ``other`` —
  escape hatch в самом promptе, его не считаем за «новую категорию».
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from database.models import Community
from modules.discovery.ai_categorizer import categorize_candidate
from modules.discovery.ai_categorizer import usage_tokens as _usage_tokens
from modules.vk_monitor.vk_client import VKClient


def ai_tokens_kwargs(ai_result: dict) -> dict:
    """Токены AI-вызова как kwargs для ``CommunityHealth``.

    Тонкость, из-за которой это обёртка, а не прямое ``tokens_prompt=ai.get(...)``:
    неизмеренный вызов — это ``None`` (нечем померить), а не ``0`` (стоит ноль).
    Поле в структуре при этом ``int``, поэтому ``None`` становится нулём, а рядом
    ставится ``tokens_known=False`` — иначе «не измерено» и «ноль» слились бы в
    одно число и занизили цену прогона (P001).
    """
    prompt, completion = _usage_tokens(ai_result)
    known = prompt is not None and completion is not None
    return {
        "tokens_prompt": int(prompt or 0),
        "tokens_completion": int(completion or 0),
        "tokens_known": known,
    }


logger = logging.getLogger(__name__)

# VK error codes, при которых считаем сообщество мёртвым / недоступным:
# 15  — Access denied (приватная стена или забанили токен)
# 18  — User was deleted or banned (для пабликов тоже бывает)
# 100 — One of parameters specified was invalid (бывает на пропавших id)
# 203 — Access to group denied
DEAD_ERROR_CODES = frozenset({15, 18, 100, 203})

DEFAULT_DORMANT_DAYS = 60
DEFAULT_POSTS_SAMPLE = 10
CHANGED_CATEGORY_CONFIDENCE_THRESHOLD = 70

# Тиры dormant-политики (одобрена brain 2026-06-30): возраст last_post_at
# первичен, dormant_streak как вторичный сигнал — позже, не сейчас.
DORMANT_T1_DAYS = 365  # T1 «заброшен» — soft-disable после 2 подряд dormant
DORMANT_T2_DAYS = 180  # T2 «застойный» — watch, не трогаем


def classify_dormant_tier(
    last_post_at: Optional[datetime], *, now: Optional[datetime] = None
) -> str:
    """Тир dormant-политики по возрасту последнего поста.

    - ``t1`` — >12 мес: kill-кандидат (disable после 2 подряд dormant-recheck'ей);
    - ``t2`` — 6–12 мес: застойный, watch;
    - ``t3`` — <6 мес: сезонный/тихий, KEEP (сельские ДК замолкают в межсезонье);
    - ``empty_wall`` — постов нет вовсе: отдельный re-probe, НЕ авто-kill.
    """
    if last_post_at is None:
        return "empty_wall"
    now = now or datetime.utcnow()
    age_days = (now - last_post_at).days
    if age_days > DORMANT_T1_DAYS:
        return "t1"
    if age_days > DORMANT_T2_DAYS:
        return "t2"
    return "t3"


@dataclass
class CommunityHealth:
    """Outcome of a single community recheck."""

    community_id: int
    vk_id: int
    # active | dormant | dead | changed_category
    status: str
    last_post_at: Optional[datetime]
    posts_sampled: int
    suggested_category: Optional[str]
    error_code: Optional[int]
    reasoning: Optional[str]
    # Токены, потраченные на AI-классификацию этой стены. Считаются здесь, а не
    # потом собираются грепом по логу: «сколько стоит прогон» — вопрос, на который
    # отчёт recheck обязан отвечать сам (P001, «цена в токенах» как условие
    # включения авто-подбора).
    #
    # ``tokens_known`` различает три состояния, которые ноль склеивает в одно:
    # вызова не было (стена без текста — 0 честно), вызов проваидером не
    # посчитан (0 обманчив) и вызов стоил ноль (не бывает). По одному
    # ``tokens_prompt`` это не восстановить, поэтому флаг явный.
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_known: bool = True


def _extract_last_post_dt(items: list) -> Optional[datetime]:
    timestamps = [int(it.get("date") or 0) for it in items if it.get("date")]
    if not timestamps:
        return None
    return datetime.utcfromtimestamp(max(timestamps))


def _collect_post_texts(items: list, limit: int = 5) -> list[str]:
    out: list[str] = []
    for it in items[:limit]:
        text = (it.get("text") or "").strip()
        if text:
            out.append(text)
    return out


async def _call_wall_get(client: VKClient, owner_id: int, count: int) -> Dict[str, Any]:
    """Sync `api_call` wrapped in to_thread so caller stays async-friendly."""
    return await asyncio.to_thread(
        client.api_call,
        "wall.get",
        {"owner_id": owner_id, "count": count, "extended": 0},
    )


async def check_community_health(
    *,
    client: VKClient,
    community: Community,
    region_name: str,
    dormant_days: int = DEFAULT_DORMANT_DAYS,
    posts_sample: int = DEFAULT_POSTS_SAMPLE,
    now: Optional[datetime] = None,
) -> CommunityHealth:
    """Run one health check for a single Community row.

    Args:
        client: VKClient (parse-token).
        community: ORM row (read-only here — caller writes back to session).
        region_name: для AI-prompt'а ("Регион: <name>").
        dormant_days: за сколько суток без новых постов считать сообщество спящим.
        posts_sample: ``wall.get(count=…)``. 10 — хороший trade-off:
            достаточно для AI-сэмпла (5 первых текстов) и для оценки давности.
        now: для тестов; иначе ``datetime.utcnow()``.

    Returns:
        CommunityHealth — выходные поля для прямого присваивания в БД.
    """
    now = now or datetime.utcnow()
    vk_id = abs(int(community.vk_id or 0))
    if vk_id == 0:
        return CommunityHealth(
            community_id=community.id,
            vk_id=0,
            status=community.health_status or "active",
            last_post_at=community.last_post_at,
            posts_sampled=0,
            suggested_category=community.suggested_category,
            error_code=None,
            reasoning="community.vk_id is empty — skipped",
        )

    owner_id = -vk_id  # wall.get на сообщество требует отрицательный owner_id
    resp = await _call_wall_get(client, owner_id, posts_sample)

    # ── VK API error path ──
    if isinstance(resp, dict) and resp.get("error"):
        err = resp["error"] or {}
        code = int(err.get("error_code") or 0)
        msg = (err.get("error_msg") or "").strip() or None
        if code in DEAD_ERROR_CODES:
            return CommunityHealth(
                community_id=community.id,
                vk_id=vk_id,
                status="dead",
                last_post_at=community.last_post_at,
                posts_sampled=0,
                suggested_category=None,
                error_code=code,
                reasoning=msg,
            )
        # Прочие ошибки — transient (rate-limit, timeout, network). Не меняем
        # health_status: будет повторная попытка на следующей неделе.
        logger.warning(
            "recheck: transient VK error for community %s (vk_id=%s): code=%s msg=%s",
            community.id,
            vk_id,
            code,
            msg,
        )
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status=community.health_status or "active",
            last_post_at=community.last_post_at,
            posts_sampled=0,
            suggested_category=community.suggested_category,
            error_code=code,
            reasoning=f"transient VK error {code}: {msg}" if msg else f"transient VK error {code}",
        )

    # ── Empty wall / no items ──
    items = (resp or {}).get("items") or []
    if not items:
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status="dormant",
            last_post_at=None,
            posts_sampled=0,
            suggested_category=None,
            error_code=None,
            reasoning="wall is empty",
        )

    last_post_at = _extract_last_post_dt(items)
    if last_post_at is None:
        # items есть, но без timestamps — странно, но не падаем
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status="active",
            last_post_at=community.last_post_at,
            posts_sampled=len(items),
            suggested_category=None,
            error_code=None,
            reasoning="items without timestamps",
        )

    age_days = (now - last_post_at).days
    if age_days > dormant_days:
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status="dormant",
            last_post_at=last_post_at,
            posts_sampled=len(items),
            suggested_category=None,
            error_code=None,
            reasoning=f"last post {age_days} days ago",
        )

    # ── AI category re-check ──
    texts = _collect_post_texts(items, limit=5)
    if not texts:
        # Wall активна, но посты без текста (только медиа/репосты). AI не дёргаем —
        # категоризация по пустым текстам только сожжёт quota.
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status="active",
            last_post_at=last_post_at,
            posts_sampled=len(items),
            suggested_category=None,
            error_code=None,
            reasoning="no text content to analyse",
        )

    ai = await categorize_candidate(
        name=community.name or "",
        description=None,  # у Community нет description-поля в БД
        members_count=None,
        recent_posts=texts,
        region_name=region_name,
    )
    if not ai.get("success"):
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status="active",
            last_post_at=last_post_at,
            posts_sampled=len(items),
            suggested_category=None,
            error_code=None,
            reasoning=f"AI categorize failed: {ai.get('error')}",
        )

    ai_cat = (ai.get("category") or "").lower()
    confidence = int(ai.get("confidence") or 0)
    current_cat = (community.category or "").lower()

    category_drift = (
        ai_cat
        and ai_cat != "other"
        and ai_cat != current_cat
        and confidence >= CHANGED_CATEGORY_CONFIDENCE_THRESHOLD
    )
    if category_drift:
        return CommunityHealth(
            community_id=community.id,
            vk_id=vk_id,
            status="changed_category",
            last_post_at=last_post_at,
            posts_sampled=len(items),
            suggested_category=ai_cat,
            error_code=None,
            reasoning=ai.get("reasoning"),
            **ai_tokens_kwargs(ai),
        )

    return CommunityHealth(
        community_id=community.id,
        vk_id=vk_id,
        status="active",
        last_post_at=last_post_at,
        posts_sampled=len(items),
        suggested_category=None,
        error_code=None,
        reasoning=ai.get("reasoning"),
        **ai_tokens_kwargs(ai),
    )
