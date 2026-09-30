"""Celery wrapper + async core for region community discovery.

The heavy lifting lives in ``run_discovery_for_region_async`` so the web
endpoint can call it directly inside the FastAPI loop without going through
Celery (the wizard wants a synchronous «launch → I see candidates» UX).
The Celery wrapper is provided for future scheduled / batched runs.

Не вызывает Celery beat по умолчанию — нет шедула в `tasks/celery_app.py`.
Запуск ad-hoc через
``app.send_task('tasks.discovery_tasks.run_discovery_for_region', args=[region_id])``
или через POST `/api/discovery/trigger`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func, select, update

from database.connection import AsyncSessionLocal
from database.models import Community, CommunityCandidate, Region
from modules.discovery import dormant_outcomes
from modules.discovery.ai_categorizer import categorize_candidate, sum_ai_tokens
from modules.discovery.health_check import (
    DEFAULT_DORMANT_DAYS,
    DEFAULT_POSTS_SAMPLE,
    CommunityHealth,
    check_community_health,
    classify_dormant_tier,
)
from modules.discovery.vk_search import DiscoveredGroup, discover_for_region
from modules.vk_monitor.vk_client import VKClient

logger = logging.getLogger(__name__)


async def _pick_parse_token() -> Optional[str]:
    """Return a VK token suitable for parse-side calls (groups.search etc.).

    Учитывает ``vk_tokens.disabled_until`` через TokenPolicy (миграция 014).
    Если Valstan в cooldown — берётся следующий.

    **Асинхронна намеренно, и это не косметика.** Раньше здесь звался
    sync-мост ``get_active_parse_tokens_sync``, а все три вызова этой функции
    живут внутри ``async def``. Мост падал на уже крутящейся петле, роутер
    глотал исключение своим ``except Exception`` и возвращал токены из env
    **мимо реестра имён** — то есть источник истины подменялся (env вместо
    БД, ровно тот рассинхрон, против которого писался роутер после инцидента
    VALSTAN 2026-05-28), а расход уезжал в отчёт `/tokens` строкой
    ``UNKNOWN:<fp>``. Найдено 2026-08-31: 1475 вызовов ``wall.get``
    еженедельного health-recheck'а. Прямой вызов async-функции роутера
    возвращает и правильный источник, и объявляет имя учёту.
    """
    from modules.vk_token_router import get_active_parse_tokens

    async with AsyncSessionLocal() as session:
        for name, tok in (await get_active_parse_tokens(session)).items():
            if tok:
                logger.debug("discovery: using token %s", name)
                return tok
    return None


async def _touch_region_last_discovery_at(region_id: int) -> None:
    """UPDATE ``regions.last_discovery_at = NOW()`` для региона.

    Используется как из Celery wrapper (после async-trigger через
    ``send_task``), так и из sync FastAPI endpoint'а. Идемпотентна:
    повторный вызов просто перезапишет значение.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Region).where(Region.id == region_id).values(last_discovery_at=datetime.utcnow())
        )
        await session.commit()


async def _existing_vk_ids(session, region_id: int) -> set[int]:
    """Vk_ids already in this region: established communities + previously
    rejected candidates. Used to skip wasteful re-discovery / re-AI.

    Не исключаем ``pending`` / ``deferred`` кандидатов — UI хочет refresh'ить
    их (AI-score мог измениться, появилась более свежая активность). Так что
    они получают ``ON CONFLICT DO UPDATE`` ниже.
    """
    q1 = await session.execute(select(Community.vk_id).where(Community.region_id == region_id))
    q2 = await session.execute(
        select(CommunityCandidate.vk_id).where(
            CommunityCandidate.region_id == region_id,
            CommunityCandidate.status == "rejected",
        )
    )
    out: set[int] = set()
    for (vk_id,) in q1.all():
        if vk_id is not None:
            out.add(abs(int(vk_id)))
    for (vk_id,) in q2.all():
        if vk_id is not None:
            out.add(abs(int(vk_id)))
    return out


async def _ai_categorize_all(
    groups: Sequence[DiscoveredGroup],
    region_name: str,
    *,
    client: Optional[VKClient] = None,
    posts_per_group: int = 10,
    max_concurrent: int = 8,
) -> Dict[int, Dict[str, Any]]:
    """Run ai_categorizer for every group, bounded concurrency.

    Если ``client`` передан — для каждой группы дополнительно тянем
    ``wall.get(count=posts_per_group)`` в `to_thread` (sync VK API → не
    блокирует event-loop). VKClient внутри сериализует все vk-вызовы через
    rate-limit Lock (``GLOBAL_PARSE_INTERVAL_SECONDS``), поэтому реального
    параллелизма по VK нет, но event-loop остаётся свободным для других
    AI-вызовов. AI-категоризация (DeepSeek) параллелится честно через semaphore.

    ``max_concurrent=8`` — подобрано под прежний движок (Groq free tier);
    у DeepSeek лимиты другие, и потолок стоит пересмотреть, когда появятся
    числа реального прогона, а не наугад.
    Возвращает map ``{vk_id: ai_result_dict}``. Failures остаются в map с
    ``success: False`` — caller сам решит сохранять с ai_*=NULL или пропустить.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _fetch_posts(g: DiscoveredGroup) -> None:
        if client is None or g.recent_posts:
            return
        try:
            posts = await asyncio.to_thread(
                client.get_wall_posts, owner_id=-g.vk_id, count=posts_per_group
            )
        except Exception as e:
            logger.debug("discovery: wall.get failed for %s: %s", g.vk_id, e)
            return
        g.recent_posts = [
            (p.get("text") or "").strip() for p in posts if (p.get("text") or "").strip()
        ]

    async def _one(g: DiscoveredGroup) -> tuple[int, Dict[str, Any]]:
        async with semaphore:
            await _fetch_posts(g)
            res = await categorize_candidate(
                name=g.name,
                description=g.description,
                members_count=g.members_count,
                recent_posts=g.recent_posts,
                region_name=region_name,
            )
        return g.vk_id, res

    tasks = [_one(g) for g in groups]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return dict(results)


async def _upsert_candidates(
    session,
    region_id: int,
    groups: Sequence[DiscoveredGroup],
    ai_by_vk_id: Dict[int, Dict[str, Any]],
) -> Dict[str, int]:
    """Upsert discovered groups into community_candidates.

    Стратегия:
    - Новая запись (нет такой `(region_id, vk_id)`) → INSERT со status='pending'.
    - Существующая `pending` / `deferred` → UPDATE snapshot + ai_* (refresh).
    - Существующая `approved` / `rejected` → не трогать (модератор уже решил).

    Возвращает счётчики для отчёта.
    """
    inserted = 0
    refreshed = 0
    skipped = 0

    # One round-trip to fetch all existing candidates for this region.
    q = await session.execute(
        select(CommunityCandidate).where(
            CommunityCandidate.region_id == region_id,
            CommunityCandidate.vk_id.in_([g.vk_id for g in groups]),
        )
    )
    existing = {c.vk_id: c for c in q.scalars().all()}

    now = datetime.utcnow()
    for g in groups:
        ai = ai_by_vk_id.get(g.vk_id) or {}
        ai_ok = bool(ai.get("success"))
        cat = ai.get("category") if ai_ok else None
        conf = ai.get("confidence") if ai_ok else None
        reasoning = ai.get("reasoning") if ai_ok else None
        is_info = bool(ai.get("is_info_page")) if ai_ok else False

        row = existing.get(g.vk_id)
        if row is None:
            row = CommunityCandidate(
                region_id=region_id,
                vk_id=g.vk_id,
                name=g.name,
                screen_name=g.screen_name,
                photo_url=g.photo_url,
                description=g.description,
                members_count=g.members_count,
                ai_category=cat,
                ai_confidence=conf,
                ai_reasoning=reasoning,
                ai_is_info_page=is_info,
                status="pending",
                discovered_via=g.discovered_via,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            inserted += 1
        elif row.status in ("pending", "deferred"):
            # Refresh snapshot + AI fields. Не двигаем status.
            row.name = g.name or row.name
            row.screen_name = g.screen_name or row.screen_name
            row.photo_url = g.photo_url or row.photo_url
            if g.description is not None:
                row.description = g.description
            if g.members_count is not None:
                row.members_count = g.members_count
            if ai_ok:
                row.ai_category = cat
                row.ai_confidence = conf
                row.ai_reasoning = reasoning
                row.ai_is_info_page = is_info
            row.discovered_via = g.discovered_via or row.discovered_via
            row.updated_at = now
            refreshed += 1
        else:
            skipped += 1

    await session.commit()
    return {"inserted": inserted, "refreshed": refreshed, "skipped_existing": skipped}


def parse_list_field(val: Any) -> List[str]:
    """Coerce input to a clean list[str]: strip, dedup case-insensitive, preserve order.

    Поддерживает форматы:
    - ``list[str]``: ``["Тужа", "Шешурга", ...]``.
    - ``str``: один или несколько элементов через перевод строки / запятую /
      точку с запятой (regex ``[\\n,;]+``).
    - ``None`` или прочее → ``[]``.

    Используется и discover_for_region (read region.config), и web/api endpoint
    при сохранении localities/keywords из textarea.
    """
    if val is None:
        return []
    if isinstance(val, str):
        raw = re.split(r"[\n,;]+", val)
    elif isinstance(val, (list, tuple)):
        raw = [str(x) for x in val]
    else:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for item in raw:
        s = (item or "").strip()
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out


def _read_region_discovery_config(region: Region) -> tuple[List[str], List[str]]:
    """Из ``region.config`` достаём ``localities`` и ``discovery_keywords``.

    Возвращает ``(localities, keywords)`` — оба list[str], strip+dedup
    с сохранением порядка. Парс делегируется ``parse_list_field``.
    """
    cfg = region.config or {}
    return (
        parse_list_field(cfg.get("localities")),
        parse_list_field(cfg.get("discovery_keywords")),
    )


async def run_discovery_for_region_async(
    region_id: int,
    *,
    categories: Optional[Sequence[str]] = None,  # kept for API compat; unused
    per_query_count: int = 100,
) -> Dict[str, Any]:
    """Async core. Pure on top of session — no Celery dependency.

    Used both by the Celery task (via ``asyncio.run``) and directly by the
    FastAPI handler.

    Возвращает структурированный отчёт ``{success, region, found, filtered_out,
    inserted, refreshed, skipped_existing, skipped_ai_failed}``.

    ``categories`` оставлен в сигнатуре для backwards-compat (в Celery wrapper
    его кто-то мог пробрасывать) — теперь поиск идёт по
    ``region.config['localities']`` + ``['discovery_keywords']``, либо fallback
    на flat ``CATEGORY_KEYWORDS`` если config пуст.
    """
    async with AsyncSessionLocal() as session:
        region: Optional[Region] = (
            await session.execute(select(Region).where(Region.id == region_id))
        ).scalar_one_or_none()
        if region is None:
            return {"success": False, "error": f"region {region_id} not found"}
        if not region.center_city:
            return {
                "success": False,
                "error": "region.center_city is empty — set it before running discovery",
                "region": region.code,
            }
        exclude_ids = await _existing_vk_ids(session, region_id)
        localities, keywords = _read_region_discovery_config(region)

    token = await _pick_parse_token()
    if not token:
        return {
            "success": False,
            "error": "no VK parse-token configured (VK_TOKENS empty)",
        }

    client = VKClient(token=token)

    # search_groups + get_groups_by_ids + wall.get(info_page) — sync; не
    # блокируем event loop.
    groups: List[DiscoveredGroup] = await asyncio.to_thread(
        discover_for_region,
        client=client,
        center_city=region.center_city,
        vk_city_id=region.vk_city_id,
        vk_group_id=region.vk_group_id,
        localities=localities,
        keywords=keywords,
        per_query_count=per_query_count,
        exclude_vk_ids=exclude_ids,
    )

    if not groups:
        return {
            "success": True,
            "region": region.code,
            "found": 0,
            "inserted": 0,
            "refreshed": 0,
            "skipped_existing": 0,
            "skipped_ai_failed": 0,
            "localities_count": len(localities),
            "keywords_count": len(keywords),
        }

    ai_results = await _ai_categorize_all(groups, region.name, client=client)
    ai_failed = sum(1 for r in ai_results.values() if not r.get("success"))

    async with AsyncSessionLocal() as session:
        counts = await _upsert_candidates(session, region_id, groups, ai_results)

    return {
        "success": True,
        "region": region.code,
        "found": len(groups),
        "inserted": counts["inserted"],
        "refreshed": counts["refreshed"],
        "skipped_existing": counts["skipped_existing"],
        "skipped_ai_failed": ai_failed,
        "localities_count": len(localities),
        "keywords_count": len(keywords),
        # Цена одного прогона подбора — то самое «сколько это стоит в токенах»,
        # ради которого P001 четыре месяца висит вопросом владельца. Раньше
        # число существовало только в ротированных логах.
        **sum_ai_tokens(ai_results.values()),
    }


# ─── Recheck (weekly health-check для already-added communities) ───
#
# Чисто read-write по `communities`: новых строк не создаём, только обновляем
# health_status / last_post_at / checked_at / suggested_category. Discovery
# rerun (поиск новых кандидатов) — отдельная задача и пока не шедулится.


def _dormant_days_for_region(region: Region) -> int:
    """Per-region override через ``region.config['dormant_days']``."""
    try:
        cfg = region.config or {}
        val = int(cfg.get("dormant_days"))
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return DEFAULT_DORMANT_DAYS


async def _recheck_one(
    client: VKClient,
    community: Community,
    region_name: str,
    dormant_days: int,
    posts_sample: int,
    semaphore: asyncio.Semaphore,
) -> CommunityHealth:
    async with semaphore:
        return await check_community_health(
            client=client,
            community=community,
            region_name=region_name,
            dormant_days=dormant_days,
            posts_sample=posts_sample,
        )


async def recheck_communities_for_region_async(
    region_id: int,
    *,
    dormant_days: Optional[int] = None,
    posts_sample: int = DEFAULT_POSTS_SAMPLE,
    max_concurrent: int = 4,
) -> Dict[str, Any]:
    """Health-check для всех ``is_active=True`` сообществ одного региона.

    Перебирает Community → ``check_community_health`` → in-place UPDATE
    полей ``health_status`` / ``last_post_at`` / ``checked_at`` /
    ``suggested_category``. Не трогает строки, помеченные модератором
    ``is_active=False`` (история постов остаётся валидной, но recheck
    не нужен).

    Возвращает структурированный отчёт ``{success, region, total, active,
    dormant, dead, changed_category, errors}``. ``errors`` — счётчик
    transient ошибок (VK rate-limit, network), не сменивших health_status.
    """
    token = await _pick_parse_token()
    if not token:
        return {
            "success": False,
            "error": "no VK parse-token configured (VK_TOKENS empty)",
        }

    async with AsyncSessionLocal() as session:
        region: Optional[Region] = (
            await session.execute(select(Region).where(Region.id == region_id))
        ).scalar_one_or_none()
        if region is None:
            return {"success": False, "error": f"region {region_id} not found"}

        rows = (
            (
                await session.execute(
                    select(Community).where(
                        Community.region_id == region_id,
                        Community.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        if not rows:
            return {
                "success": True,
                "region": region.code,
                "total": 0,
                "active": 0,
                "dormant": 0,
                "dead": 0,
                "changed_category": 0,
                "errors": 0,
                "disabled_t1": 0,
            }

        client = VKClient(token=token)
        dd = dormant_days if dormant_days is not None else _dormant_days_for_region(region)
        semaphore = asyncio.Semaphore(max_concurrent)
        region_name = region.name or region.code

        results: List[CommunityHealth] = await asyncio.gather(
            *(_recheck_one(client, c, region_name, dd, posts_sample, semaphore) for c in rows),
            return_exceptions=False,
        )

        now = datetime.utcnow()
        counts = {
            "active": 0,
            "dormant": 0,
            "dead": 0,
            "changed_category": 0,
            "errors": 0,
            "disabled_t1": 0,
        }
        by_id = {c.id: c for c in rows}
        for res in results:
            counts[res.status] = counts.get(res.status, 0) + 1
            if res.error_code is not None and res.status not in ("dead",):
                counts["errors"] += 1
            row = by_id.get(res.community_id)
            if row is None:
                continue
            prev_status = row.health_status
            row.health_status = res.status
            if res.last_post_at is not None:
                row.last_post_at = res.last_post_at
            row.checked_at = now
            # VK error_code этого recheck'а (миграция 058): NULL = чисто. Даёт
            # машинный split dead 18/100 «удалён» vs 15/203 «недоступен» (#041).
            row.last_error_code = res.error_code
            # suggested_category пишем только для changed_category; в остальных
            # случаях очищаем, чтобы UI не подсвечивал устаревшую подсказку.
            row.suggested_category = (
                res.suggested_category if res.status == "changed_category" else None
            )
            # Dormant-политика T1 (brain OK 2026-06-30): «2 подряд dormant» без
            # новой схемы = предыдущий health_status уже был dormant И новый
            # recheck снова dormant. Только T1 (>12 мес); empty_wall (last_post_at
            # IS NULL) сюда не попадает — re-probe, не авто-kill. Обратимый
            # soft-disable, видимость через ежемесячный digest (условие brain).
            if (
                res.status == "dormant"
                and prev_status == "dormant"
                and classify_dormant_tier(row.last_post_at, now=now) == "t1"
            ):
                row.is_active = False
                row.disabled_at = now
                row.disabled_reason = "dormant_t1_auto"
                counts["disabled_t1"] += 1

        await session.commit()

        return {
            "success": True,
            "region": region.code,
            "total": len(rows),
            **counts,
            # Цена прогона — в самом отчёте, а не в логе: «включать ли
            # авто-одбор» (P001) — вопрос о стоимости, и он должен читаться
            # из результата задачи, а не собираться грепом по ротированным
            # логам через полгода.
            **_tokens_of_health(results),
        }


def _tokens_of_health(results: Sequence[CommunityHealth]) -> Dict[str, Any]:
    """Сумма токенов AI-классификации по результатам recheck.

    ``tokens_measured=False`` означает, что хотя бы часть вызовов не вернула
    счётчиков (``tokens_known=False``), и сумма — нижняя оценка: читать её как
    полный расход нельзя. Флаг, а не эвристика «ноль бывает только у мёртвых
    стен»: у стены с текстом, но без счётчиков от провайдера, ноль — обман.
    """
    prompt = sum(int(getattr(r, "tokens_prompt", 0) or 0) for r in results)
    completion = sum(int(getattr(r, "tokens_completion", 0) or 0) for r in results)
    unmeasured = sum(1 for r in results if not getattr(r, "tokens_known", True))
    return {
        "tokens_prompt": prompt,
        "tokens_completion": completion,
        "tokens_measured": unmeasured == 0,
        "tokens_unmeasured_calls": unmeasured,
    }


async def recheck_all_active_regions_async(
    *,
    posts_sample: int = DEFAULT_POSTS_SAMPLE,
    max_concurrent_per_region: int = 4,
    send_telegram: bool = True,
) -> Dict[str, Any]:
    """Health-check по всем `Region.is_active=True`, последовательно.

    Между регионами — последовательно, чтобы не разрывать rate-limit одного
    VK-токена. Внутри региона — параллельно (Semaphore=4).

    По итогам прогона отправляет агрегированный Telegram-alert (если
    есть non-active изменения и настроены TELEGRAM_TOKENS/CHAT_ID).
    """
    async with AsyncSessionLocal() as session:
        region_ids = [
            r.id
            for r in (
                await session.execute(
                    select(Region).where(Region.is_active.is_(True)).order_by(Region.code)
                )
            )
            .scalars()
            .all()
        ]

    if not region_ids:
        return {"success": True, "regions": [], "total_regions": 0}

    reports: List[Dict[str, Any]] = []
    for rid in region_ids:
        report = await recheck_communities_for_region_async(
            rid,
            posts_sample=posts_sample,
            max_concurrent=max_concurrent_per_region,
        )
        reports.append(report)

    if send_telegram:
        try:
            _maybe_send_recheck_telegram_alert(reports)
        except Exception as e:  # pragma: no cover — alerting не должен валить таску
            logger.warning("recheck: telegram alert failed: %s", e)

    return {
        "success": True,
        "total_regions": len(region_ids),
        "regions": reports,
        # Сумма по всем регионам: недельный прогон recheck — самая крупная
        # статья расхода в модуле discovery, и без числа в отчёте её не видно
        # нигде, кроме логов (замер 01.10.2026: 1786 вызовов за прогон).
        "tokens_prompt": sum(int(r.get("tokens_prompt") or 0) for r in reports),
        "tokens_completion": sum(int(r.get("tokens_completion") or 0) for r in reports),
        "tokens_measured": all(bool(r.get("tokens_measured")) for r in reports),
    }


def _has_interesting_findings(reports: List[Dict[str, Any]]) -> bool:
    """True, если хотя бы один регион нашёл dead / dormant / changed_category."""
    for r in reports:
        if not r.get("success"):
            continue
        if any(r.get(k, 0) for k in ("dead", "dormant", "changed_category")):
            return True
    return False


def _format_recheck_message(reports: List[Dict[str, Any]]) -> str:
    """Telegram-сообщение (HTML) по итогам recheck'а."""
    totals = {
        "dead": 0,
        "dormant": 0,
        "changed_category": 0,
        "errors": 0,
        "total": 0,
        "disabled_t1": 0,
    }
    region_lines: List[str] = []
    for r in reports:
        if not r.get("success"):
            region_lines.append(f"  • <b>{r.get('region', '?')}</b>: ошибка — {r.get('error', '')}")
            continue
        for k in totals:
            totals[k] += int(r.get(k, 0) or 0)
        non_active = (
            (r.get("dead") or 0) + (r.get("dormant") or 0) + (r.get("changed_category") or 0)
        )
        if non_active == 0:
            continue
        parts: List[str] = []
        if r.get("dead"):
            parts.append(f"💀 dead: {r['dead']}")
        if r.get("dormant"):
            parts.append(f"😴 dormant: {r['dormant']}")
        if r.get("changed_category"):
            parts.append(f"🔀 changed_category: {r['changed_category']}")
        if r.get("disabled_t1"):
            parts.append(f"🚫 выведено T1: {r['disabled_t1']}")
        region_lines.append(f"  • <b>{r.get('region', '?')}</b> — " + ", ".join(parts))

    lines: List[str] = []
    lines.append("<b>🔬 Discovery recheck</b>")
    lines.append("")
    lines.append(f"Регионов: <b>{len(reports)}</b>, сообществ проверено: <b>{totals['total']}</b>")
    lines.append(
        f"💀 dead: <b>{totals['dead']}</b>, "
        f"😴 dormant: <b>{totals['dormant']}</b>, "
        f"🔀 changed_category: <b>{totals['changed_category']}</b>"
    )
    if totals["disabled_t1"]:
        lines.append(f"🚫 выведено dormant-политикой T1 (обратимо): <b>{totals['disabled_t1']}</b>")
    if totals["errors"]:
        lines.append(f"⚠ transient errors (не сместили статус): {totals['errors']}")
    if region_lines:
        lines.append("")
        lines.extend(region_lines)
    return "\n".join(lines)


def _send_telegram_html(text: str, *, log_prefix: str) -> None:
    """Send an HTML message to TELEGRAM_ALERT_CHAT_ID.

    Общий паттерн alert-рассылок discovery (тот же, что в
    ``tasks.celery_app._maybe_send_telegram_notifications_alert``):
    pick первого работающего бот-токена + ``TELEGRAM_ALERT_CHAT_ID``.
    """
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules import telegram_http as tg_http
    except ImportError as e:  # pragma: no cover
        logger.warning("%s: telegram deps missing: %s", log_prefix, e)
        return

    bot_token = None
    for key in ("VALSTANBOT", "ALERT", "AFONYA"):
        bot_token = (TELEGRAM_TOKENS or {}).get(key)
        if bot_token:
            break
    if not bot_token:
        bot_token = next(iter((TELEGRAM_TOKENS or {}).values()), None)
    if not bot_token or not TELEGRAM_ALERT_CHAT_ID:
        logger.info("%s: telegram not configured, skipping alert", log_prefix)
        return

    try:
        resp = tg_http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": TELEGRAM_ALERT_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "%s: telegram sendMessage failed: %s %s",
                log_prefix,
                resp.status_code,
                resp.text[:300],
            )
        else:
            logger.info("%s: telegram alert sent", log_prefix)
    except Exception as e:  # pragma: no cover
        logger.warning("%s: telegram send error: %s", log_prefix, e)


def _maybe_send_recheck_telegram_alert(reports: List[Dict[str, Any]]) -> None:
    """Send Telegram bulletin if recheck found anything actionable."""
    if not _has_interesting_findings(reports):
        logger.info("recheck: nothing to report, skipping Telegram alert")
        return
    _send_telegram_html(_format_recheck_message(reports), log_prefix="recheck")


# ─── Rolling discovery: 1 регион в день, по очереди ─────────────────
#
# Beat-таска ежедневно выбирает регион с самым давним ``last_discovery_at``
# (NULL — highest priority, «никогда не запускали»), прогоняет для него
# полный discovery и шлёт Telegram-alert если появились новые кандидаты.
#
# Discovery исключает уже-добавленные communities автоматически
# (см. ``_existing_vk_ids``), новых дублей не создаст. Сравнение «было/стало»
# на основе count(pending candidates) — модератор увидит ровно delta'у.


async def _select_oldest_discovery_region(session) -> Optional[Region]:
    """Выбрать активный регион с самым старым ``last_discovery_at``.

    Только регионы с заполненным config (localities + center_city) и
    vk_group_id — без них discovery физически не запустится.

    Сортировка: NULL (никогда не запускали) первым, далее — по возрастанию
    last_discovery_at, при равных — по code (детерминизм для тестов).
    """
    rows = (
        (
            await session.execute(
                select(Region).where(
                    Region.is_active.is_(True),
                    Region.vk_group_id.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )

    eligible: List[Region] = []
    for r in rows:
        cfg = r.config if isinstance(r.config, dict) else {}
        if not cfg.get("localities") or not r.center_city:
            continue
        eligible.append(r)

    if not eligible:
        return None

    eligible.sort(
        key=lambda r: (
            r.last_discovery_at is not None,
            r.last_discovery_at or datetime.min,
            r.code,
        )
    )
    return eligible[0]


async def discover_rolling_one_region_async(*, send_telegram: bool = True) -> Dict[str, Any]:
    """Daily rolling: 1 регион — самый давний discovery первым.

    Discovery исключает уже-добавленные communities через
    ``_existing_vk_ids`` — дублей в БД не появится.

    При успехе обновляет ``regions.last_discovery_at = NOW()`` (даже если
    не нашли новых — само событие важно для ротации). Telegram-alert
    отправляем только если ``new_pending > 0``.
    """
    async with AsyncSessionLocal() as session:
        region = await _select_oldest_discovery_region(session)
        if region is None:
            logger.info("rolling discovery: no eligible regions")
            return {"success": True, "skipped": "no eligible regions"}

        before_pending = (
            await session.execute(
                select(func.count(CommunityCandidate.id)).where(
                    CommunityCandidate.region_id == region.id,
                    CommunityCandidate.status == "pending",
                )
            )
        ).scalar() or 0

        region_id = region.id
        region_code = region.code
        region_name = region.name

    logger.info(
        "rolling discovery: starting region=%s (id=%s, before_pending=%s)",
        region_code,
        region_id,
        before_pending,
    )

    result = await run_discovery_for_region_async(region_id)

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Region).where(Region.id == region_id).values(last_discovery_at=datetime.utcnow())
        )
        await session.commit()

        after_pending = (
            await session.execute(
                select(func.count(CommunityCandidate.id)).where(
                    CommunityCandidate.region_id == region_id,
                    CommunityCandidate.status == "pending",
                )
            )
        ).scalar() or 0

    new_pending = max(0, after_pending - before_pending)
    report: Dict[str, Any] = {
        "success": bool(result.get("success", False)),
        "region": region_code,
        "region_name": region_name,
        "region_id": region_id,
        "new_pending": new_pending,
        "total_pending": after_pending,
    }
    for k in ("found", "inserted", "refreshed", "error"):
        if k in result:
            report[k] = result[k]

    logger.info(
        "rolling discovery: finished region=%s new_pending=%s total_pending=%s",
        region_code,
        new_pending,
        after_pending,
    )

    if send_telegram and new_pending > 0:
        try:
            _maybe_send_rolling_telegram_alert(report)
        except Exception as e:  # pragma: no cover — alerting не должен валить таску
            logger.warning("rolling discovery: telegram alert failed: %s", e)

    return report


def _format_rolling_message(report: Dict[str, Any]) -> str:
    """HTML-сообщение в Telegram про rolling discovery."""
    code = report.get("region", "?")
    name = report.get("region_name") or code
    new_n = report.get("new_pending", 0)
    total = report.get("total_pending", 0)
    return (
        f"<b>🔍 Найдены новые кандидаты для региона: <code>{code}</code></b>\n"
        f"({name})\n\n"
        f"Новых: <b>{new_n}</b>\n"
        f"Всего на проверку: <b>{total}</b>\n\n"
        f"Открыть: /regions/{code}/discovery"
    )


def _maybe_send_rolling_telegram_alert(report: Dict[str, Any]) -> None:
    """Telegram-alert для rolling discovery (общий sender)."""
    _send_telegram_html(_format_rolling_message(report), log_prefix="rolling")


# ─── Dormant-политика: ежемесячный digest вынесенных ─────────────────
#
# Условие brain (OK 2026-06-30): auto-disable T1 — только с ежемесячным
# digest'ом вынесенных. Под автономией «молча вынесли» дороже видимой
# ошибки (#018) — digest даёт владельцу окно возразить (soft-disable
# обратим: is_active=true возвращает в парс).

DORMANT_DIGEST_WINDOW_DAYS = 32  # месяц + запас на дрейф расписания


def _format_dormant_digest(items: List[Dict[str, Any]]) -> str:
    """HTML-digest сообществ, выведенных dormant-политикой за окно."""
    lines: List[str] = []
    lines.append("<b>😴 Dormant-политика: выведено из парса за месяц</b>")
    lines.append("")
    lines.append(f"Всего: <b>{len(items)}</b> (T1 — без постов &gt;12 мес, 2 recheck'а подряд)")
    lines.append("")
    for it in items:
        last = it.get("last_post_at")
        last_s = last.strftime("%Y-%m-%d") if last else "—"
        lines.append(
            f"  • <b>{it.get('region', '?')}</b> — {it.get('name', '?')}"
            f" (vk.com/club{it.get('vk_id')}, последний пост {last_s})"
        )
    lines.append("")
    lines.append("Обратимо: is_active=true вернёт сообщество в парс.")
    return "\n".join(lines)


async def dormant_disable_digest_async(*, send_telegram: bool = True) -> Dict[str, Any]:
    """Monthly digest: communities soft-disabled by the dormant policy.

    Читает `communities.disabled_reason='dormant_t1_auto'` за последние
    ``DORMANT_DIGEST_WINDOW_DAYS`` суток. Пусто → тишина (алёрт не шлём).
    """
    since = datetime.utcnow() - timedelta(days=DORMANT_DIGEST_WINDOW_DAYS)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Community, Region.code)
                .join(Region, Community.region_id == Region.id)
                .where(
                    Community.disabled_reason == "dormant_t1_auto",
                    Community.disabled_at >= since,
                )
                .order_by(Region.code, Community.name)
            )
        ).all()

    items = [
        {
            "region": code,
            "name": c.name,
            "vk_id": abs(int(c.vk_id or 0)),
            "last_post_at": c.last_post_at,
            "disabled_at": c.disabled_at,
        }
        for c, code in rows
    ]

    if not items:
        logger.info("dormant-digest: nothing disabled in window, skipping")
        return {"success": True, "count": 0}

    if send_telegram:
        _send_telegram_html(_format_dormant_digest(items), log_prefix="dormant-digest")

    return {"success": True, "count": len(items), "items": items}


# ─── Ре-проверка авто-вынесенных (рекомендация brain 2026-08-22, пул #182) ───

DORMANT_RECHECK_WALL_COUNT = 5  # хватает: ищем сам факт поста позже disabled_at
DORMANT_RECHECK_LIMIT = 200  # предохранитель от неожиданно большой выборки


def _format_dormant_recheck(revived: List[Dict[str, Any]], counts: Dict[str, int]) -> str:
    lines = [
        "<b>Ре-проверка авто-вынесенных</b>",
        "",
        (
            f"проверено {counts.get('checked', 0)} · ожили {counts.get('revived', 0)} · "
            f"спят {counts.get('asleep', 0)} · недоступны {counts.get('unreachable', 0)} · "
            f"неясно {counts.get('unknown', 0)}"
        ),
    ]
    if revived:
        lines.append("")
        lines.append("<b>Возвращены в парс:</b>")
        for it in revived:
            lines.append(f"• {it['name']} (vk {it['vk_id']}) — {it['note']}")
    lines.append("")
    lines.append("Вынос dormant-политикой отзывается автоматически, если стена ожила.")
    return "\n".join(lines)


async def dormant_recheck_disabled_async(
    *,
    send_telegram: bool = True,
    revive: bool = True,
    reason: str = "dormant_t1_auto",
    limit: int = DORMANT_RECHECK_LIMIT,
) -> Dict[str, Any]:
    """Ежемесячно перепроверить сообщества, вынесенные dormant-политикой.

    **Зачем.** Авто-вынос выводит предмет из области наблюдения: вынесенное
    сообщество больше не опрашивается, и ошибка выноса становится ненаблюдаемой
    по построению. Пока вердикт ставил человек, «вынес и забыл» было приемлемо;
    с июля его ставит автомат (рекомендация brain 2026-08-22, замер «3 судьбы»
    2026-08-21: 46 спят, 2 ожили, 0 недоступны). Правило: **автоматический
    вердикт, выводящий предмет из наблюдения, не должен быть неотзывным.**

    **Флип-флопа не будет.** Обратный вынос требует двух подряд `dormant` **и**
    tier T1 (последний пост старше 12 мес). У ожившего `last_post_at` свежий,
    значит T1 он уже не проходит — вернуть его обратно в вынос ближайший
    weekly-recheck не может.

    Цена — по одному `wall.get` на строку, ~48 вызовов в месяц.
    """
    token = await _pick_parse_token()
    if not token:
        logger.warning("dormant-recheck: нет VK parse-токена, пропуск")
        return {"success": False, "error": "no_token"}

    async with AsyncSessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(Community)
                    .where(
                        Community.disabled_reason == reason,
                        Community.is_active.is_(False),
                    )
                    .order_by(Community.disabled_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        counts = {"checked": 0, "revived": 0, "asleep": 0, "unreachable": 0, "unknown": 0}
        revived_items: List[Dict[str, Any]] = []

        if not rows:
            logger.info("dormant-recheck: выносов нет, пропуск")
            return {"success": True, **counts}

        client = VKClient(token=token)
        now = datetime.utcnow()

        for row in rows:
            owner_id = -abs(int(row.vk_id or 0))
            try:
                resp = await dormant_outcomes.wall_get(client, owner_id, DORMANT_RECHECK_WALL_COUNT)
            except Exception as e:  # pragma: no cover — сеть/VK
                logger.warning("dormant-recheck: %s — ошибка запроса: %s", row.vk_id, e)
                counts["unknown"] += 1
                continue

            outcome = dormant_outcomes.classify(row, resp)
            counts["checked"] += 1
            row.checked_at = now

            if outcome.fate == dormant_outcomes.FATE_REVIVED:
                counts["revived"] += 1
                if outcome.newest_post_at_vk is not None:
                    row.last_post_at = outcome.newest_post_at_vk
                if revive:
                    row.is_active = True
                    row.disabled_at = None
                    row.disabled_reason = None
                    row.health_status = "active"
                    row.last_error_code = None
                revived_items.append(
                    {
                        "name": outcome.name,
                        "vk_id": outcome.vk_id,
                        "note": outcome.note,
                    }
                )
                logger.info(
                    "dormant-recheck: ОЖИЛ %s (vk %s) — %s",
                    outcome.name,
                    outcome.vk_id,
                    outcome.note,
                )
            elif outcome.fate == dormant_outcomes.FATE_UNREACHABLE:
                counts["unreachable"] += 1
                # Вынесен как dormant, а на деле удалён/забанен — записываем истинную
                # причину, но из выноса НЕ возвращаем.
                row.health_status = "dead"
                row.last_error_code = outcome.error_code
            elif outcome.fate == dormant_outcomes.FATE_UNKNOWN:
                counts["unknown"] += 1
            else:
                counts["asleep"] += 1

        await session.commit()

    if send_telegram and counts["revived"]:
        _send_telegram_html(
            _format_dormant_recheck(revived_items, counts), log_prefix="dormant-recheck"
        )

    return {"success": True, **counts, "revived_items": revived_items}


# ─── Celery wrapper (для будущего шедулирования) ───
# Импортируем app только тут, чтобы тесты на async-core не тащили Celery.

try:
    from tasks.celery_app import app as _celery_app
    from utils.celery_asyncio import run_coro as _run_coro

    @_celery_app.task(name="tasks.discovery_tasks.run_discovery_for_region")
    def run_discovery_for_region(region_id: int, categories: Optional[List[str]] = None):
        """Celery task: запускает discovery для одного региона.

        После успеха обновляет ``regions.last_discovery_at = NOW()`` —
        синхронный endpoint ``/api/discovery/trigger`` делает это сам, но
        async-путь (через ``trigger-async``) полагается на эту запись, иначе
        UI ``/regions`` не увидит свежую дату после polling-завершения.
        """
        result = _run_coro(run_discovery_for_region_async(region_id, categories=categories))
        if result.get("success"):
            try:
                _run_coro(_touch_region_last_discovery_at(region_id))
            except Exception as e:  # pragma: no cover — touch не критичен
                logger.warning("celery: last_discovery_at update failed: %s", e)
        return result

    @_celery_app.task(name="tasks.discovery_tasks.recheck_communities_for_region")
    def recheck_communities_for_region(region_id: int):
        """Celery task: health-check для одного региона (ad-hoc, без beat)."""
        return _run_coro(recheck_communities_for_region_async(region_id))

    @_celery_app.task(
        name="tasks.discovery_tasks.recheck_all_active_regions",
        # Свой лимит вместо общего 3600 из config/celery_config.py.
        # Замер по журналам воркера 2026-09-22: это САМАЯ долгая задача сети —
        # 1750 с (29 минут) против 368 с у второй по длительности. Она обходит
        # все активные сообщества всех регионов через per-token тормоз ВК,
        # поэтому растёт вместе с сетью: каждая новая порция районов её
        # удлиняет. Общий лимит дал бы ей запас меньше двух раз, и в какой-то
        # момент недельная проверка начала бы обрываться — раз в неделю, в
        # 04:00, то есть незаметно.
        #
        # Мягкого лимита нет здесь по той же причине, что и в общем конфиге:
        # задача идёт через общий персистентный event loop, и мягкий лимит
        # оставил бы на нём недобитую корутину.
        time_limit=3 * 3600,
    )
    def recheck_all_active_regions():
        """Celery beat task: weekly recheck по всем активным регионам."""
        return _run_coro(recheck_all_active_regions_async())

    @_celery_app.task(name="tasks.discovery_tasks.discover_rolling_one_region")
    def discover_rolling_one_region():
        """Celery beat task: daily rolling discovery (1 регион — самый давний)."""
        return _run_coro(discover_rolling_one_region_async())

    @_celery_app.task(name="tasks.discovery_tasks.dormant_recheck_disabled")
    def dormant_recheck_disabled():
        """Celery beat task: ежемесячная ре-проверка авто-вынесенных (brain 2026-08-22)."""
        return _run_coro(dormant_recheck_disabled_async())

    @_celery_app.task(name="tasks.discovery_tasks.dormant_disable_digest")
    def dormant_disable_digest():
        """Celery beat task: monthly digest вынесенных dormant-политикой."""
        return _run_coro(dormant_disable_digest_async())

except Exception as _import_err:  # pragma: no cover
    # При локальном импорте без Celery (например, в тестах web-API)
    # Celery wrapper необязателен.
    logger.debug("Celery wrapper not registered: %s", _import_err)
