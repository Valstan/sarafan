"""Рассылка рекламного оффера (Этап 4 программы «Кабинет под ключ», 2026-09-05).

Что можно по правилам ВК: сообщество **не пишет первым**. Автоматически шлём
только (1) авторам входящих ЛС — ответом в их диалог тем же сообществом и
(2) авторам предложки, у которых сообщения от сообщества разрешены
(``ad_requests.can_message``). Остальные — в ручной список: deeplink
``vk.com/im?sel=<id>`` + готовый текст, владелец пишет с личной страницы.

Кампания живёт в ``ad_outreach_campaigns``, адресаты — в
``ad_outreach_recipients`` (уникум по человеку в кампании; «один контакт
навсегда» — человек, которому оффер уже ушёл в любой кампании, в новую не
попадает). Всем адресатам заводится кабинет ``ad_clients`` с промо-пакетом
«3 поста бесплатно» — привязка при входе по VK ID или в боте идёт по
``author_vk_id``.

Диспетчер (тик раз в 5 минут, 9–21 МСК): лиз строки guarded UPDATE, лимиты
30/сутки на сообщество и 150/сутки всего (глобально), тихие часы, dry-run по
умолчанию, троттл между отправками. VK 9/14 → пауза DM-канала сообщества в
``dm_channel`` и стоп тика; кампания хранит только ``paused_reason``, остальные
сообщества продолжают. 901/902 → адресат уходит в ручной список; 900 (человек
занёс сообщество в ЧС) → стоп-лист. Всё сетевое инъектируется.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from database.models import (
    AdClient,
    AdClientPackage,
    AdOutreachBlacklist,
    AdOutreachCampaign,
    AdOutreachRecipient,
    AdPayment,
    AdPublication,
    AdRequest,
    AdScheduledPost,
    MessageTemplate,
)
from modules.ad_cabinet.interaction_log import log_interaction

logger = logging.getLogger(__name__)

MSK = timedelta(hours=3)
TEMPLATE_CATEGORY = "ad_offer"
PROMO_POSTS = 3
MAX_ATTEMPTS = 3
HEARTBEAT_KEY = "setka:ad_outreach_last_dispatch"
MANUAL_CODES = (901, 902)  # 900 — человек занёс сообщество в ЧС: стоп-лист, не ручной список
CABINET_URL = "https://сарафан.вмалмыже.рф/cabinet"
SENT_STATUSES = ("sent", "done_manual")
#: Статусы, которые **занимают лимит отправки** (30/сообщество, 150/сутки).
#: ``done_manual`` — это ручная отправка владельцем через кабинет
#: (``web/api/ad_outreach.py``: статус + ``sent_at``), и она тоже съедает лимит:
#: иначе рассылка ограничивалась бы только автоматическими отправками, а
#: ручные шли бы сверх лимита. Предикат один на всех — он и живёт здесь.

Sender = Callable[
    ..., Awaitable[Dict[str, Any]]
]  # (community_vk_id, peer_id, text, attachment, random_id)
AttachmentBuilder = Callable[[int, int, Sequence[str]], Awaitable[Optional[str]]]


def outreach_disabled() -> bool:
    """Аварийный стоп рассылки: ``AD_OUTREACH_DISABLED=1``."""
    return (os.getenv("AD_OUTREACH_DISABLED", "0") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def in_quiet_hours(now_msk: datetime, start: int, end: int) -> bool:
    """Тихие часы по МСК; ``start == end`` — тихих часов нет; окно через полночь."""
    if start == end:
        return False
    hour = now_msk.hour
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def msk_day_start_utc(now_utc: datetime) -> datetime:
    """Начало текущих МСК-суток в UTC naive (для суточных лимитов)."""
    msk = now_utc + MSK
    start_msk = datetime(msk.year, msk.month, msk.day)
    return start_msk - MSK


def person_id(ar: AdRequest) -> Optional[int]:
    """VK id человека за заявкой; ``None`` — группа или нерезолвимый peer.

    Инвариант чекеров: человек — это ``peer_id > 0`` при ``author_is_group=False``
    (подписанный пост от имени группы: ``author_vk_id`` = -группа, ``peer_id`` =
    подписант; ЛС: ``peer_id`` = собеседник). ``author_vk_id`` — запасной вариант.
    """
    if ar.author_is_group:
        return None
    for cand in (ar.peer_id, ar.author_vk_id):
        try:
            cand = int(cand or 0)
        except (TypeError, ValueError):
            continue
        if cand > 0:
            return cand
    return None


def request_mode(ar: AdRequest) -> str:
    """``auto`` — сообщество может написать само; ``manual`` — только владелец."""
    if ar.origin == "inbound_dm":
        return "auto"
    if ar.origin == "suggested" and ar.can_message is True:
        return "auto"
    return "manual"


# ------------------------------------------------------------------ аудитория


async def build_audience(
    session,
    *,
    months_back: int = 6,
    now_utc: Optional[datetime] = None,
    exclude_campaign_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Список адресатов за ``months_back`` месяцев, по одному на человека.

    Приоритет при дедупе: режим ``auto`` важнее ``manual``, затем свежая
    заявка. Исключаются: стоп-лист, люди, которым оффер уже ушёл в любой
    кампании, клиенты с заказами и архивные кабинеты.
    """
    now_utc = now_utc or datetime.utcnow()
    since = now_utc - timedelta(days=30 * max(1, int(months_back)))
    rows = (
        (
            await session.execute(
                select(AdRequest)
                .where(
                    AdRequest.origin.in_(("inbound_dm", "suggested")),
                    AdRequest.route == "ad_cabinet",  # не-реклама из ЛС идёт route=notifications
                    AdRequest.status.notin_(("deleted", "skipped")),  # решения оператора уважаем
                    # ЛС переоткрывается новым сообщением (updated_at), а detected_at —
                    # первый контакт: считаем по последней активности.
                    or_(
                        AdRequest.detected_at >= since,
                        and_(AdRequest.origin == "inbound_dm", AdRequest.updated_at >= since),
                    ),
                    AdRequest.author_is_group.is_(False),
                )
                .order_by(AdRequest.detected_at.desc())
            )
        )
        .scalars()
        .all()
    )
    bl_rows = (await session.execute(select(AdOutreachBlacklist))).scalars().all()
    blacklist = {int(b.vk_user_id) for b in bl_rows if is_blacklisted(b, now_utc)}
    # «Один контакт навсегда» + «один человек — в одной живой кампании»: мимо
    # все, у кого есть строка в любом статусе, кроме failed/skipped.
    busy_q = select(AdOutreachRecipient.vk_user_id).where(
        AdOutreachRecipient.status.notin_(("failed", "skipped"))
    )
    if exclude_campaign_id is not None:  # своя кампания дедупится по существующим строкам
        busy_q = busy_q.where(AdOutreachRecipient.campaign_id != int(exclude_campaign_id))
    already = {int(x) for x in (await session.execute(busy_q)).scalars()}
    with_orders: set = set()
    for model in (AdScheduledPost, AdPayment, AdPublication):
        with_orders |= {
            int(x)
            for x in (
                await session.execute(
                    select(AdClient.author_vk_id)
                    .join(model, model.client_id == AdClient.id)
                    .where(AdClient.author_vk_id.isnot(None))
                )
            ).scalars()
        }
    archived = {
        int(x)
        for x in (
            await session.execute(
                select(AdClient.author_vk_id).where(
                    AdClient.is_archived.is_(True), AdClient.author_vk_id.isnot(None)
                )
            )
        ).scalars()
    }

    best: Dict[int, Dict[str, Any]] = {}
    for ar in rows:  # свежие первыми
        pid = person_id(ar)
        if pid is None or pid in blacklist or pid in already or pid in with_orders:
            continue
        if pid in archived:
            continue
        mode = request_mode(ar)
        cur = best.get(pid)
        if cur is not None and not (cur["mode"] == "manual" and mode == "auto"):
            continue
        best[pid] = {
            "vk_user_id": pid,
            "community_vk_id": int(ar.community_vk_id),
            "ad_request_id": ar.id,
            "name": (ar.author_name or "").strip() or None,
            "origin": ar.origin,
            "mode": mode,
            "region_id": ar.region_id,
            "community_name": ar.community_name,
        }
    return list(best.values())


async def enroll_campaign(
    session, campaign: AdOutreachCampaign, *, now_utc: Optional[datetime] = None
) -> Dict[str, int]:
    """Набрать адресатов в кампанию (идемпотентно) и завести им кабинеты."""
    now_utc = now_utc or datetime.utcnow()
    audience = await build_audience(
        session,
        months_back=campaign.months_back,
        now_utc=now_utc,
        exclude_campaign_id=campaign.id,
    )
    existing = {
        int(x)
        for x in (
            await session.execute(
                select(AdOutreachRecipient.vk_user_id).where(
                    AdOutreachRecipient.campaign_id == campaign.id
                )
            )
        ).scalars()
    }
    stats = {"total": len(audience), "added": 0, "auto": 0, "manual": 0, "existing": 0}
    new_rows: List[AdOutreachRecipient] = []
    for a in audience:
        if a["vk_user_id"] in existing:
            stats["existing"] += 1
            continue
        r = AdOutreachRecipient(
            campaign_id=campaign.id,
            vk_user_id=a["vk_user_id"],
            community_vk_id=a["community_vk_id"],
            ad_request_id=a["ad_request_id"],
            name=a["name"],
            origin=a["origin"],
            mode=a["mode"],
            status="pending" if a["mode"] == "auto" else "manual",
        )
        session.add(r)
        new_rows.append(r)
        stats["added"] += 1
        stats[a["mode"]] += 1
    await session.flush()
    cab = await ensure_cabinets(session, new_rows, now_utc=now_utc)
    stats.update(cab)
    return stats


async def ensure_cabinets(
    session, recipients: Sequence[AdOutreachRecipient], *, now_utc: Optional[datetime] = None
) -> Dict[str, int]:
    """Кабинет + промо-пакет каждому адресату (идемпотентно по ``author_vk_id``)."""
    now_utc = now_utc or datetime.utcnow()
    stats = {"clients_created": 0, "promos_created": 0}
    region_ids: Dict[int, Optional[int]] = {}
    requests: Dict[int, AdRequest] = {}
    req_ids = [int(r.ad_request_id) for r in recipients if r.ad_request_id]
    if req_ids:
        for ar in (
            await session.execute(select(AdRequest).where(AdRequest.id.in_(req_ids)))
        ).scalars():
            pid = person_id(ar)
            if pid is not None:
                region_ids[pid] = ar.region_id
                requests[pid] = ar
    for r in recipients:
        client = (
            await session.execute(
                select(AdClient).where(AdClient.author_vk_id == int(r.vk_user_id))
            )
        ).scalar_one_or_none()
        if client is None:
            client = AdClient(
                author_vk_id=int(r.vk_user_id),
                author_is_group=False,
                name=r.name,
                stage="detected",
                region_id=region_ids.get(int(r.vk_user_id)),
            )
            session.add(client)
            created = True
            try:
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError:  # гонка с upsert-from-request по uq author_vk_id
                session.expunge(client)
                created = False
                client = (
                    await session.execute(
                        select(AdClient).where(AdClient.author_vk_id == int(r.vk_user_id))
                    )
                ).scalar_one()
            if created:
                stats["clients_created"] += 1
                log_interaction(
                    session,
                    kind="detected",
                    client_id=client.id,
                    ad_request_id=r.ad_request_id,
                    summary="Кабинет заведён для рассылки оффера",
                    actor="system",
                )
        r.client_id = client.id
        ar = requests.get(int(r.vk_user_id))
        if ar is not None and ar.client_id is None:
            ar.client_id = client.id  # заявка → кабинет, как в upsert-from-request
        # Промо не даём, если у клиента уже есть активный пакет или промо когда-либо было.
        has_pkg = (
            await session.execute(
                select(func.count(AdClientPackage.id)).where(
                    AdClientPackage.client_id == client.id,
                    or_(
                        AdClientPackage.is_active.is_(True),
                        AdClientPackage.kind == "free_promo",
                    ),
                )
            )
        ).scalar_one()
        if not has_pkg:
            session.add(
                AdClientPackage(
                    client_id=client.id,
                    kind="free_promo",
                    posts_total=PROMO_POSTS,
                    posts_used=0,
                    price=0,
                    paid_at=now_utc,
                    is_active=True,
                    note="акция для адресатов рассылки: 3 поста бесплатно",
                )
            )
            stats["promos_created"] += 1
    await session.flush()
    return stats


# ------------------------------------------------------------------ текст


async def resolve_template(session, campaign: AdOutreachCampaign) -> Optional[str]:
    """Тело шаблона кампании: явный ``template_id``, иначе последний активный ``ad_offer``."""
    if campaign.template_id:
        t = await session.get(MessageTemplate, int(campaign.template_id))
        if t is not None and t.body:
            return t.body
    t = (
        await session.execute(
            select(MessageTemplate)
            .where(
                MessageTemplate.category == TEMPLATE_CATEGORY,
                MessageTemplate.is_active.is_(True),
            )
            .order_by(MessageTemplate.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return t.body if t is not None else None


def offer_placeholders() -> Dict[str, str]:
    """Плейсхолдеры оффера сверх ``message_builder``: цены, ссылки, промо."""
    from config.ad_landing import PACKAGES, PIN_PRICE_RUB, PRICE_SINGLE_RUB
    from config.runtime import get_sarafan_vk_community_id

    by_covers = {p.get("covers"): p for p in PACKAGES}
    sarafan = get_sarafan_vk_community_id()
    return {
        "price_single": f"{PRICE_SINGLE_RUB} ₽",
        "price_5": f"{by_covers.get(5, {}).get('price', '')} ₽" if 5 in by_covers else "",
        "price_10": f"{by_covers.get(10, {}).get('price', '')} ₽" if 10 in by_covers else "",
        "price_all": f"{by_covers.get(None, {}).get('price', '')} ₽" if None in by_covers else "",
        "pin_price": f"{PIN_PRICE_RUB} ₽",
        "promo_posts": str(PROMO_POSTS),
        "cabinet_link": CABINET_URL,
        "bot_link": f"https://vk.me/club{sarafan}" if sarafan else CABINET_URL,
    }


def render_offer(
    template_body: str,
    *,
    author_name: Optional[str],
    community_name: Optional[str] = None,
    region_name: Optional[str] = None,
    cabinet_id: Optional[int] = None,
) -> str:
    """Оффер: плейсхолдеры цен/ссылок подставляются до ``message_builder.render``."""
    from modules.ad_cabinet.message_builder import render

    body = template_body or ""
    values = offer_placeholders()
    values["cabinet_id"] = str(cabinet_id) if cabinet_id else ""
    for key, value in values.items():
        body = body.replace("{" + key + "}", value)
    return render(
        body,
        author_name=author_name,
        community_name=community_name,
        region_name=region_name,
    )


# ------------------------------------------------------------------ тик диспетчера


def touch_heartbeat() -> None:
    try:
        from modules.ad_cabinet import owner_ping

        client = owner_ping._redis()
        if client is not None:
            client.set(HEARTBEAT_KEY, str(datetime.utcnow().timestamp()), ex=14 * 24 * 3600)
    except Exception:  # noqa: BLE001
        pass


MAX_PER_DAY_ENV = "AD_OUTREACH_MAX_PER_DAY"  # глобальный потолок сверх кампании
PER_TICK_ENV = "AD_OUTREACH_PER_TICK"  # отправок за один тик (beat раз в 5 мин)
STALE_CLAIM = timedelta(minutes=10)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


async def reclaim_stale(session, now_utc: datetime) -> int:
    """Строки, зависшие в ``claimed`` (воркер упал до commit) → ``failed``, без повтора:
    исход отправки неизвестен, повторять нельзя (образец promotion.dispatcher)."""
    res = await session.execute(
        update(AdOutreachRecipient)
        .where(
            AdOutreachRecipient.status == "claimed",
            AdOutreachRecipient.claimed_at < now_utc - STALE_CLAIM,
        )
        .values(status="failed", error="зависло в claimed (рестарт?) — исход неизвестен")
    )
    return int(res.rowcount or 0)


async def _sent_today(session, day_start_utc: datetime) -> Dict[Optional[int], int]:
    """``{community_vk_id: отправок сегодня}`` по ВСЕМ кампаниям плюс ключ ``None`` — всего.

    Лимиты — глобальные (решение владельца: 30/сообщество, 150/сутки), а не на
    кампанию: две кампании не удваивают поток.
    """
    rows = (
        await session.execute(
            select(AdOutreachRecipient.community_vk_id, func.count())
            .where(
                or_(
                    and_(
                        # Отправленные — и автоматические, и ручные (владелец
                        # написал сам через кабинет): обе занимают лимит.
                        AdOutreachRecipient.status.in_(SENT_STATUSES),
                        AdOutreachRecipient.sent_at >= day_start_utc,
                    ),
                    # Взятые параллельным тиком — тоже занимают лимит.
                    and_(
                        AdOutreachRecipient.status == "claimed",
                        AdOutreachRecipient.claimed_at >= day_start_utc,
                    ),
                )
            )
            .group_by(AdOutreachRecipient.community_vk_id)
        )
    ).all()
    out: Dict[Optional[int], int] = {int(c): int(n) for c, n in rows}
    out[None] = sum(out.values())
    return out


def _interleave_by_community(rows: Sequence[AdOutreachRecipient]) -> List[AdOutreachRecipient]:
    """Чередовать сообщества (round-robin): большое ИНФО не «морит» остальные
    при лимите на тик и на сообщество."""
    buckets: Dict[int, List[AdOutreachRecipient]] = {}
    order: List[int] = []
    for r in rows:
        cid = int(r.community_vk_id)
        if cid not in buckets:
            buckets[cid] = []
            order.append(cid)
        buckets[cid].append(r)
    out: List[AdOutreachRecipient] = []
    while any(buckets[c] for c in order):
        for c in order:
            if buckets[c]:
                out.append(buckets[c].pop(0))
    return out


async def _queue_empty(session, campaign_id: int) -> bool:
    """Нет ни авто-очереди, ни неразобранного ручного списка — кампанию можно закрыть."""
    left = (
        await session.execute(
            select(func.count(AdOutreachRecipient.id)).where(
                AdOutreachRecipient.campaign_id == int(campaign_id),
                AdOutreachRecipient.status.in_(("pending", "claimed", "manual")),
            )
        )
    ).scalar_one()
    return not left


async def _claim(session, recipient_id: int, now_utc: datetime) -> bool:
    res = await session.execute(
        update(AdOutreachRecipient)
        .where(AdOutreachRecipient.id == recipient_id, AdOutreachRecipient.status == "pending")
        .values(status="claimed", claimed_at=now_utc, attempts=AdOutreachRecipient.attempts + 1)
    )
    return (res.rowcount or 0) == 1


async def _mark_request_not_messageable(session, r: AdOutreachRecipient, now_utc: datetime) -> None:
    """900/901/902: инбокс должен знать, что сообщество этому человеку писать не может."""
    if not r.ad_request_id:
        return
    ar = await session.get(AdRequest, int(r.ad_request_id))
    if ar is not None:
        ar.can_message = False
        ar.can_message_checked_at = now_utc


async def _render_context(session, r: AdOutreachRecipient, cache: Dict[int, Dict[str, Any]]):
    """``community_name``/``region_name`` заявки адресата (кеш на тик по заявке)."""
    if not r.ad_request_id:
        return {}
    key = int(r.ad_request_id)
    if key in cache:
        return cache[key]
    ar = await session.get(AdRequest, key)
    ctx: Dict[str, Any] = {}
    if ar is not None:
        ctx["community_name"] = ar.community_name
        if ar.region_id:
            from database.models import Region

            region = await session.get(Region, int(ar.region_id))
            ctx["region_name"] = region.name if region is not None else None
    cache[key] = ctx
    return ctx


async def _mark_request_contacted(session, r: AdOutreachRecipient, now_utc: datetime) -> None:
    if not r.ad_request_id:
        return
    ar = await session.get(AdRequest, int(r.ad_request_id))
    if ar is None:
        return
    if ar.status == "new":
        ar.status = "contacted"
        ar.contacted_at = now_utc
        ar.vk_message_id = r.vk_message_id
        ar.via = "outreach"


async def run_outreach_tick(
    *,
    session_factory: Optional[Callable] = None,
    send: Optional[Sender] = None,
    attachment_builder: Optional[AttachmentBuilder] = None,
    now_utc: Optional[datetime] = None,
    interval: Optional[float] = None,
    alert: Optional[Callable[[str], Any]] = None,
    campaign_id: Optional[int] = None,
    per_tick: Optional[int] = None,
) -> Dict[str, Any]:
    """Один тик: по каждой запущенной кампании — лимиты, лиз, отправка.

    ``per_tick`` — потолок отправок за тик (env ``AD_OUTREACH_PER_TICK``, 10):
    beat ходит раз в 5 минут, 10 × 5 с троттла укладываются в ``expires``.
    """
    if outreach_disabled():
        return {"skipped": "disabled", "sent": 0, "dry_run": 0}
    if session_factory is None:
        from database.connection import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    now_utc = now_utc or datetime.utcnow()
    now_msk = now_utc + MSK
    if interval is None:
        interval = float(os.getenv("AD_OUTREACH_INTERVAL_SECONDS", "5") or 5)
    if per_tick is None:
        per_tick = _env_int(PER_TICK_ENV, 10)
    touch_heartbeat()

    stats: Dict[str, Any] = {
        "campaigns": 0,
        "sent": 0,
        "dry_run": 0,
        "manual": 0,
        "failed": 0,
        "stopped": False,
        "skipped": None,
    }
    async with session_factory() as session:
        reclaimed = await reclaim_stale(session, now_utc)
        if reclaimed:
            await session.commit()
            logger.warning("outreach: реклеймлено зависших строк: %s", reclaimed)
        q = select(AdOutreachCampaign).where(AdOutreachCampaign.status == "running")
        if campaign_id is not None:
            q = q.where(AdOutreachCampaign.id == int(campaign_id))
        campaigns = (await session.execute(q)).scalars().all()
        posted_any = False
        tick_left = per_tick
        from modules.ad_cabinet import dm_channel
        from modules.ad_cabinet.message_builder import looks_mangled

        for camp in campaigns:
            stats["campaigns"] += 1
            if camp.paused_until and camp.paused_until > now_utc:
                stats["skipped"] = "paused"
                continue
            if in_quiet_hours(now_msk, int(camp.quiet_start), int(camp.quiet_end)):
                stats["skipped"] = "quiet-hours"
                continue
            template = await resolve_template(session, camp)
            if not template:
                stats["skipped"] = "no-template"
                logger.warning("outreach %s: нет шаблона ad_offer — тик пропущен", camp.id)
                continue
            if looks_mangled(template):
                stats["skipped"] = "mangled-template"
                logger.warning("outreach %s: шаблон похож на битую кодировку", camp.id)
                continue
            day_start = msk_day_start_utc(now_utc)
            sent = await _sent_today(session, day_start)
            cap = min(int(camp.total_daily), _env_int(MAX_PER_DAY_ENV, int(camp.total_daily)))
            budget = max(0, cap - sent.get(None, 0))
            if budget <= 0:
                stats["skipped"] = "daily-cap"
                continue
            if tick_left <= 0:
                stats["skipped"] = "tick-cap"
                continue
            pending_rows = (
                (
                    await session.execute(
                        select(AdOutreachRecipient)
                        .where(
                            AdOutreachRecipient.campaign_id == camp.id,
                            AdOutreachRecipient.mode == "auto",
                            AdOutreachRecipient.status == "pending",
                        )
                        .order_by(AdOutreachRecipient.id.asc())
                        .limit(2000)
                    )
                )
                .scalars()
                .all()
            )
            pending = _interleave_by_community(pending_rows)
            if not pending:
                if not camp.dry_run and await _queue_empty(session, camp.id):
                    camp.status = "done"
                    camp.finished_at = now_utc
                    await session.commit()
                continue
            if send is None and not camp.dry_run:
                send = await build_default_sender()
            ctx_cache: Dict[int, Dict[str, Any]] = {}
            for r in pending:
                if budget <= 0 or tick_left <= 0:
                    break
                cid = int(r.community_vk_id)
                if sent.get(cid, 0) >= int(camp.per_community_daily):
                    continue
                if not camp.dry_run and dm_channel.paused_until(cid) is not None:
                    continue  # канал этого сообщества молчит после 9/14 — остальные идут
                if not await _claim(session, r.id, now_utc):
                    await session.commit()
                    continue
                await session.commit()
                await session.refresh(r)
                ctx = await _render_context(session, r, ctx_cache)
                r.body = render_offer(template, author_name=r.name, cabinet_id=r.client_id, **ctx)
                if camp.dry_run:
                    r.status = "dry_run"
                    r.attempts = max(0, int(r.attempts or 0) - 1)  # сухой прогон — не попытка
                    stats["dry_run"] += 1
                    tick_left -= 1
                    await session.commit()
                    continue
                if posted_any and interval:
                    await asyncio.sleep(interval)
                attachment = None
                if attachment_builder is not None and camp.images_json:
                    try:
                        attachment = await attachment_builder(
                            cid, int(r.vk_user_id), list(camp.images_json)
                        )
                    except Exception as e:  # noqa: BLE001 - картинки best-effort
                        logger.warning("outreach: attachment failed for %s: %s", r.id, e)
                try:
                    res = await send(cid, int(r.vk_user_id), r.body, attachment, int(r.id))
                except Exception as e:  # noqa: BLE001
                    res = {"success": False, "error_code": None, "error": str(e)[:300]}
                posted_any = True
                tick_left -= 1
                code = res.get("error_code")
                # Капча без кода (текстом) — тоже 14: пауза канала ставится здесь же.
                if not code and "captcha" in str(res.get("error") or "").lower():
                    code = 14
                    until = dm_channel.note_error(cid, 14)
                    res = {
                        **res,
                        "error_code": 14,
                        "paused_until": until.isoformat() if until else None,
                    }
                if res.get("success"):
                    r.status = "sent"
                    r.sent_at = now_utc
                    r.vk_message_id = res.get("message_id")
                    r.error_code = None
                    r.error = None
                    sent[cid] = sent.get(cid, 0) + 1
                    budget -= 1
                    stats["sent"] += 1
                    await _mark_request_contacted(session, r, now_utc)
                    log_interaction(
                        session,
                        kind="outreach_sent",
                        client_id=r.client_id,
                        ad_request_id=r.ad_request_id,
                        summary=f"Оффер отправлен от сообщества {cid} (кампания #{camp.id})",
                        actor="system",
                    )
                elif code == 900:
                    # Человек занёс сообщество в ЧС — в стоп-лист, а не в ручной список.
                    r.status = "skipped"
                    r.error_code = 900
                    r.error = str(res.get("error") or "")[:300]
                    stats["blacklisted"] = stats.get("blacklisted", 0) + 1
                    await _mark_request_not_messageable(session, r, now_utc)
                    if await session.get(AdOutreachBlacklist, int(r.vk_user_id)) is None:
                        session.add(
                            AdOutreachBlacklist(vk_user_id=int(r.vk_user_id), reason="VK 900")
                        )
                elif code in MANUAL_CODES:
                    r.mode = "manual"
                    r.status = "manual"
                    r.error_code = int(code)
                    r.error = str(res.get("error") or "")[:300]
                    stats["manual"] += 1
                    # Только настоящий ответ VK (allowed=False); синтетический «нет токена»
                    # заявку не трогает — сообщество, может быть, и умеет писать.
                    if res.get("allowed") is False:
                        await _mark_request_not_messageable(session, r, now_utc)
                elif code in (9, 14) or res.get("paused_until"):
                    # Свежий 9/14: send_message уже поставил паузу DM-канала этого
                    # сообщества (dm_channel); остальные сообщества идут дальше в
                    # следующем тике. Этот тик по кампании — стоп, владельцу — алёрт.
                    r.status = "pending"
                    r.attempts = max(0, int(r.attempts or 0) - 1)  # пауза канала — не попытка
                    r.error_code = int(code) if code else None
                    r.error = str(res.get("error") or "")[:300]
                    camp.paused_reason = f"VK {code} в {cid}: {res.get('error')}"[:300]
                    stats["stopped"] = True
                    await session.commit()
                    if alert is not None:
                        await _maybe_await(
                            alert(
                                f"⏸ Рассылка #{camp.id}: VK {code} от сообщества {cid} — канал на "
                                "паузе (dm_channel), тик остановлен, остальные продолжат."
                            )
                        )
                    break
                else:
                    r.error_code = int(code) if code else None
                    r.error = str(res.get("error") or "")[:300]
                    if int(r.attempts or 0) >= MAX_ATTEMPTS:
                        r.status = "failed"
                        stats["failed"] += 1
                    else:
                        r.status = "pending"
                await session.commit()
            if stats["stopped"]:
                continue  # следующая кампания — другие сообщества, своя очередь
            # Очередь исчерпана в этом же тике (всё ушло/провалилось) — закрываем сразу,
            # не дожидаясь следующего пустого тика.
            if not camp.dry_run and await _queue_empty(session, camp.id):
                camp.status = "done"
                camp.finished_at = now_utc
                await session.commit()
    return stats


async def _maybe_await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


async def build_default_sender() -> Sender:  # pragma: no cover - сеть
    """Отправитель по умолчанию: community-токен сообщества, ``vk_actions.send_message``."""
    from modules.notifications.vk_actions import send_message
    from modules.vk_token_router import load_community_routing

    community_tokens = await load_community_routing()

    async def _send(
        community_vk_id: int,
        peer_id: int,
        text: str,
        attachment: Optional[str],
        random_id: Optional[int] = None,
    ):
        gid = abs(int(community_vk_id))
        if gid not in community_tokens:
            return {
                "success": False,
                "error_code": 901,
                "no_token": True,
                "error": "нет community-токена сообщества — только с личной страницы",
            }
        return await asyncio.to_thread(
            send_message,
            group_id=gid,
            peer_id=int(peer_id),
            message=text,
            user_token="",
            community_tokens=community_tokens,
            random_id=random_id,  # стабильный = id адресата: повтор не дублирует ЛС у VK
            attachment=attachment,
        )

    return _send


async def manual_list(session, campaign_id: int) -> List[Dict[str, Any]]:
    """Ручной список: адресаты ``manual`` с готовым текстом и deeplink.

    **Отданный текст запоминается здесь же** (найдено 2026-09-14). Авто-ветка
    пишет ``body`` с самого начала, ручная не писала его нигде: 42 записи из 58
    остались без текста, и на вопрос «что мы ему писали» ответа не было ни у нас,
    ни в CRM — а встаёт он ровно тогда, когда человек отвечает через неделю.

    Почему запоминаем в момент выдачи, а не в момент отметки «отправлено».
    Оператор копирует **ровно эту** строку. Отрендерить её заново при отметке
    было бы дешевле, но шаблон к тому времени мог смениться (строка про чек
    самозанятого добавлена 12.09, две «двери» — 14.09), и в карточке оказался бы
    текст, которого адресат не получал. Запись, называющая не тот предмет, хуже
    пустой записи: пустую видно ([#303](https://github.com/Valstan/brain_matrica)).

    Поэтому же ``r.body`` не перезаписывается, если он уже есть: первым победил
    тот текст, который реально ушёл.
    """
    camp = await session.get(AdOutreachCampaign, int(campaign_id))
    if camp is None:
        return []
    template = await resolve_template(session, camp) or ""
    rows = (
        (
            await session.execute(
                select(AdOutreachRecipient)
                .where(
                    AdOutreachRecipient.campaign_id == camp.id,
                    AdOutreachRecipient.mode == "manual",
                    AdOutreachRecipient.status.in_(("manual", "pending")),
                )
                .order_by(AdOutreachRecipient.id.asc())
            )
        )
        .scalars()
        .all()
    )
    out = []
    cache: Dict[int, Dict[str, Any]] = {}
    stored = 0
    for r in rows:
        ctx = await _render_context(session, r, cache)
        body = r.body or render_offer(template, author_name=r.name, cabinet_id=r.client_id, **ctx)
        if body and not (r.body or "").strip():
            r.body = body
            stored += 1
        d = r.to_dict()
        d["body"] = body
        out.append(d)
    if stored:
        await session.commit()
    return out


async def campaign_counters(session, campaign_id: int) -> Dict[str, int]:
    rows = (
        await session.execute(
            select(AdOutreachRecipient.status, func.count())
            .where(AdOutreachRecipient.campaign_id == int(campaign_id))
            .group_by(AdOutreachRecipient.status)
        )
    ).all()
    out = {str(s): int(n) for s, n in rows}
    out["total"] = sum(out.values())
    return out


def is_blacklisted(entry: Optional[AdOutreachBlacklist], now_utc: datetime) -> bool:
    """Жива ли запись чёрного списка (бессрочная или срок не прошёл).

    Правило одно на всех: запись действует, пока у неё не истёк ``until``.
    Раньше оно было продублировано инлайном в :func:`build_audience` — две
    копии правила разъезжаются при правке, поэтому инлайн зовёт эту функцию.
    """
    return entry is not None and (entry.until is None or entry.until > now_utc)


__all__ = [
    "build_audience",
    "enroll_campaign",
    "ensure_cabinets",
    "render_offer",
    "resolve_template",
    "run_outreach_tick",
    "manual_list",
    "campaign_counters",
    "in_quiet_hours",
    "msk_day_start_utc",
    "person_id",
    "request_mode",
    "outreach_disabled",
    "PROMO_POSTS",
    "MANUAL_CODES",
]
