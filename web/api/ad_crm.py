"""CRM рекламного кабинета — блок C (`/api/ad-crm`).

Учёт заказчиков, оплат и публикаций поверх существующих заявок (`ad_requests`)
и отложенных постов (`ad_scheduled_posts`):

- ``GET    /clients``                       — список клиентов + агрегаты (оплачено, публикаций);
- ``POST   /clients``                       — завести клиента вручную;
- ``GET    /clients/{id}``                  — карточка: клиент + его оплаты + публикации;
- ``PATCH  /clients/{id}``                  — правка полей/стадии воронки;
- ``DELETE /clients/{id}``                  — удалить (оплаты каскадом, публикации → NULL);
- ``POST   /clients/upsert-from-request/{request_id}`` — завести/привязать клиента из заявки;
- ``POST   /payments`` / ``DELETE /payments/{id}``         — учёт оплат;
- ``POST   /publications`` / ``DELETE /publications/{id}`` — учёт публикаций;
- ``GET    /funnel``                        — воронка: число клиентов по стадиям + итоги.

Ключ сведения заявок предложки и ЛС в одну карточку — ``author_vk_id``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.models import (
    AdChatMessage,
    AdClient,
    AdClientPackage,
    AdInteraction,
    AdOrderItem,
    AdPayment,
    AdPublication,
    AdRequest,
    AdScheduledPost,
    Region,
)
from modules.ad_cabinet.auto_greeting import get_network_stats, resolve_greeting_text
from modules.ad_cabinet.balance import compute_balance, summarize
from modules.ad_cabinet.interaction_log import log_interaction
from modules.ad_cabinet.message_builder import looks_mangled, render
from utils.search_query import compact_number, normalize_query, query_variants

logger = logging.getLogger(__name__)

# Порог pg_trgm similarity для fuzzy-фолбэка поиска клиентов (#035, Уровень 3).
_FUZZY_SIMILARITY_THRESHOLD = 0.3
router = APIRouter()

# Воронка сделки. Порядок — для сортировки/прогресса в UI.
_VALID_STAGES = ("detected", "contacted", "scheduled", "published", "paid", "lost")
_PUBLICATION_STATUSES = {"published", "removed"}
_PAYMENT_STATUSES = {"awaiting", "paid"}
_ORDER_ITEM_STATUSES = {"planned", "scheduled", "published", "cancelled"}

# Фикс-список банков для дропдауна оплаты. Правится здесь (owner: «фикс-список»).
# Порядок — частые сверху; «Наличные»/«Перевод» как нефинансовые способы.
AD_PAYMENT_BANKS = (
    "Сбербанк",
    "Т-Банк",
    "Альфа-Банк",
    "ВТБ",
    "Озон Банк",
    "Наличные",
    "Перевод",
)


# ---------------------------------------------------------------- schemas


class ClientCreateIn(BaseModel):
    author_vk_id: Optional[int] = None
    author_is_group: bool = False
    name: Optional[str] = None
    vk_url: Optional[str] = None
    contact: Optional[str] = None
    region_id: Optional[int] = None
    stage: str = "detected"
    notes: Optional[str] = None
    phone: Optional[str] = None
    telegram: Optional[str] = None
    email: Optional[str] = None
    postal_address: Optional[str] = None


class ClientUpdateIn(BaseModel):
    """Частичная правка карточки клиента (применяются только переданные поля)."""

    name: Optional[str] = None
    vk_url: Optional[str] = None
    contact: Optional[str] = None
    region_id: Optional[int] = None
    stage: Optional[str] = None
    notes: Optional[str] = None
    author_is_group: Optional[bool] = None
    phone: Optional[str] = None
    telegram: Optional[str] = None
    email: Optional[str] = None
    postal_address: Optional[str] = None
    # Кабинет рекламодателя: ручной тумблер доверия (авто — по порогу
    # одобренных постов, modules/ad_cabinet/client_orders.TRUST_AFTER_POSTS).
    trusted: Optional[bool] = None


class PaymentCreateIn(BaseModel):
    client_id: int
    amount: float
    method: Optional[str] = None
    status: str = "paid"  # awaiting | paid
    units_paid: Optional[int] = None  # за сколько публикаций (пакет); None = не указано
    bank: Optional[str] = None
    ad_request_id: Optional[int] = None
    scheduled_post_id: Optional[int] = None
    note: Optional[str] = None
    paid_at: Optional[str] = None  # ISO; по умолчанию — сейчас


class PaymentUpdateIn(BaseModel):
    """Частичная правка оплаты (применяются только переданные поля)."""

    amount: Optional[float] = None
    method: Optional[str] = None
    status: Optional[str] = None
    units_paid: Optional[int] = None
    bank: Optional[str] = None
    note: Optional[str] = None
    paid_at: Optional[str] = None


class InteractionCreateIn(BaseModel):
    """Ручная заметка/событие в таймлайн клиента."""

    client_id: int
    kind: str = "note"
    summary: str
    created_at: Optional[str] = None  # ISO; по умолчанию — сейчас


class InteractionUpdateIn(BaseModel):
    """Правка события таймлайна (только переданные поля)."""

    summary: Optional[str] = None
    kind: Optional[str] = None
    created_at: Optional[str] = None


class PublicationCreateIn(BaseModel):
    community_vk_id: int
    client_id: Optional[int] = None
    vk_post_id: Optional[int] = None
    region_id: Optional[int] = None
    ad_request_id: Optional[int] = None
    scheduled_post_id: Optional[int] = None
    price: Optional[float] = None
    status: str = "published"
    note: Optional[str] = None
    published_at: Optional[str] = None  # ISO; по умолчанию — сейчас


class OrderItemCreateIn(BaseModel):
    """Позиция заказа клиента (что/сколько/период). Вручную или из заявки."""

    client_id: int
    description: Optional[str] = None
    quantity: int = 1
    period_start: Optional[str] = None  # ISO date
    period_end: Optional[str] = None
    status: str = "planned"
    ad_request_id: Optional[int] = None
    scheduled_post_id: Optional[int] = None
    publication_id: Optional[int] = None
    note: Optional[str] = None


class OrderItemUpdateIn(BaseModel):
    """Частичная правка позиции заказа (только переданные поля)."""

    description: Optional[str] = None
    quantity: Optional[int] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    status: Optional[str] = None
    note: Optional[str] = None


class CommunityInfoIn(BaseModel):
    """Разрешить VK-сообщество по URL/screen_name/ID."""

    url: str


class ResolveProfileIn(BaseModel):
    """Разрешить VK-профиль человека или сообщества по URL/screen_name/ID."""

    url: str


class ScanPostsIn(BaseModel):
    """Сканировать стену сообщества на посты заказчика."""

    community_vk_id: int
    keywords: Optional[str] = None
    author_name: Optional[str] = None
    date_from: Optional[str] = None  # ISO date, с какой даты сканировать
    date_to: Optional[str] = None  # ISO date, по какую дату (по умолчанию — сегодня)
    max_pages: int = 10  # максимум страниц (по 100 постов) для пагинации


class PublicationUpdateIn(BaseModel):
    """Частичная правка публикации (цена, статус оплаты, заметка)."""

    price: Optional[float] = None
    paid_status: Optional[str] = None
    note: Optional[str] = None
    published_at: Optional[str] = None


class ReportFiltersIn(BaseModel):
    """Фильтры для отчёта клиента."""

    community_vk_ids: Optional[list[int]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    paid_status: Optional[str] = None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _parse_date(value: Optional[str]):
    """ISO-дата (YYYY-MM-DD) → date | None. Терпимо к пустым/битым строкам."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- clients


def _compact_contact_col():
    """``contact`` без разделителей — матч «номерных» токенов (``8912…`` ≡ ``8-912 …``)."""
    col = func.coalesce(AdClient.contact, "")
    for sep in (" ", "-", ".", "/"):
        col = func.replace(col, sep, "")
    return col


def _text_search_conditions(tokens: list[str]) -> list:
    """Многотокен AND (#035, Уровень 1): каждый токен — substring в name/contact."""
    conds = []
    compact_col = _compact_contact_col()
    for token in tokens:
        like = f"%{token}%"
        cond = AdClient.name.ilike(like) | AdClient.contact.ilike(like)
        compact = compact_number(token)
        if compact:
            cond = cond | compact_col.like(f"%{compact}%")
        conds.append(cond)
    return conds


def _supports_trgm(db: AsyncSession) -> bool:
    """pg_trgm есть только на Postgres; на других диалектах fuzzy пропускаем."""
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


async def _search_client_rows(db: AsyncSession, stmt, q: Optional[str], limit: int):
    """Tiered-поиск клиентов (#035): substring → RU↔EN раскладка → pg_trgm fuzzy.

    Следующий уровень пробуется только при нуле результатов предыдущего —
    «похожее» не разбавляет точные совпадения.
    """

    def _ordered(s):
        return s.order_by(AdClient.updated_at.desc()).limit(limit)

    variants = query_variants(q) if q else []
    if not variants:
        return (await db.execute(_ordered(stmt))).all()

    for tokens in variants:
        rows = (await db.execute(_ordered(stmt.where(*_text_search_conditions(tokens))))).all()
        if rows:
            return rows

    if not _supports_trgm(db):
        return []

    qn = normalize_query(q)
    sim = func.greatest(
        func.similarity(AdClient.name, qn),
        func.similarity(func.coalesce(AdClient.contact, ""), qn),
    )
    fuzzy_stmt = stmt.where(sim > _FUZZY_SIMILARITY_THRESHOLD).order_by(sim.desc()).limit(limit)
    return (await db.execute(fuzzy_stmt)).all()


@router.get("/clients")
async def list_clients(
    stage: Optional[str] = None,
    region_id: Optional[int] = None,
    q: Optional[str] = None,
    debtors_only: bool = False,
    include_archived: bool = False,
    limit: int = 200,
    db: AsyncSession = Depends(get_db_session),
):
    """Список клиентов со свёрнутыми агрегатами (оплачено, число публикаций).

    Агрегаты считаются скалярными подзапросами — один проход, свежие сверху.
    ``debtors_only`` (С4) — только клиенты с неоплаченным счётом старше порога.
    """
    paid_sq = (
        select(func.coalesce(func.sum(AdPayment.amount), 0))
        .where(AdPayment.client_id == AdClient.id, AdPayment.status == "paid")
        .scalar_subquery()
    )
    awaiting_sq = (
        select(func.coalesce(func.sum(AdPayment.amount), 0))
        .where(AdPayment.client_id == AdClient.id, AdPayment.status == "awaiting")
        .scalar_subquery()
    )
    pay_count_sq = (
        select(func.count(AdPayment.id)).where(AdPayment.client_id == AdClient.id).scalar_subquery()
    )
    pub_count_sq = (
        select(func.count(AdPublication.id))
        .where(AdPublication.client_id == AdClient.id)
        .scalar_subquery()
    )
    # Расход нити (И1): Σ цены вышедших публикаций — для сигнала «нужна доплата»
    # прямо в списке клиентов, до раскрытия карточки.
    spent_sq = (
        select(func.coalesce(func.sum(AdPublication.price), 0))
        .where(
            AdPublication.client_id == AdClient.id,
            AdPublication.status == "published",
        )
        .scalar_subquery()
    )
    # Штучный баланс (И2): куплено пакетом (Σ units_paid у оплат) vs вышло
    # (число published-публикаций). Сигнал перерасхода в списке.
    paid_units_sq = (
        select(func.coalesce(func.sum(AdPayment.units_paid), 0))
        .where(AdPayment.client_id == AdClient.id, AdPayment.status == "paid")
        .scalar_subquery()
    )
    consumed_units_sq = (
        select(func.count(AdPublication.id))
        .where(
            AdPublication.client_id == AdClient.id,
            AdPublication.status == "published",
        )
        .scalar_subquery()
    )
    order_items_sq = (
        select(func.count(AdOrderItem.id))
        .where(AdOrderItem.client_id == AdClient.id)
        .scalar_subquery()
    )

    stmt = select(
        AdClient,
        paid_sq.label("total_paid"),
        awaiting_sq.label("total_awaiting"),
        pay_count_sq.label("payments_count"),
        pub_count_sq.label("publications_count"),
        spent_sq.label("total_spent"),
        paid_units_sq.label("paid_units"),
        consumed_units_sq.label("consumed_units"),
        order_items_sq.label("order_items_count"),
    )
    if not include_archived:
        stmt = stmt.where(AdClient.is_archived.is_(False))
    if stage:
        stmt = stmt.where(AdClient.stage == stage)
    if region_id is not None:
        stmt = stmt.where(AdClient.region_id == region_id)
    if debtors_only:
        from modules.ad_cabinet.debtors import DEBTOR_DAYS

        cutoff = datetime.utcnow() - timedelta(days=DEBTOR_DAYS)
        overdue_exists = (
            select(AdPayment.id)
            .where(
                AdPayment.client_id == AdClient.id,
                AdPayment.status == "awaiting",
                AdPayment.created_at <= cutoff,
            )
            .exists()
        )
        stmt = stmt.where(overdue_exists)

    rows = await _search_client_rows(db, stmt, q, limit)
    clients = []
    for row in rows:
        client = row[0]
        total_paid, total_awaiting, payments_count, publications_count = row[1:5]
        total_spent, paid_units, consumed_units, order_items_count = row[5:9]
        d = client.to_dict()
        d["total_paid"] = float(total_paid or 0)
        d["total_awaiting"] = float(total_awaiting or 0)
        d["payments_count"] = int(payments_count or 0)
        d["publications_count"] = int(publications_count or 0)
        d["order_items_count"] = int(order_items_count or 0)
        # Баланс-сигнал для списка (И1+И2): уровень/остаток той же логикой, что в
        # карточке. Цена части публикаций может быть NULL — в списке это не видно
        # (published_unpriced=0), точный «расход неполный» показывает карточка.
        d["balance"] = summarize(
            d["total_paid"],
            float(total_spent or 0),
            awaiting=d["total_awaiting"],
            paid_units=int(paid_units or 0),
            consumed_units=int(consumed_units or 0),
        )
        clients.append(d)
    return {"clients": clients}


@router.post("/clients")
async def create_client(
    payload: ClientCreateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Завести клиента вручную. Дубль по ``author_vk_id`` → 409 (только если vk_id задан)."""
    if payload.stage not in _VALID_STAGES:
        raise HTTPException(status_code=400, detail="invalid stage")

    if payload.author_vk_id is not None:
        existing = (
            await db.execute(
                select(AdClient).where(AdClient.author_vk_id == int(payload.author_vk_id))
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=409, detail="client already exists")

    client = AdClient(
        author_vk_id=int(payload.author_vk_id) if payload.author_vk_id is not None else None,
        author_is_group=payload.author_is_group,
        name=payload.name,
        vk_url=payload.vk_url,
        contact=payload.contact,
        region_id=payload.region_id,
        stage=payload.stage,
        notes=payload.notes,
        phone=payload.phone,
        telegram=payload.telegram,
        email=payload.email,
        postal_address=payload.postal_address,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return client.to_dict()


@router.get("/clients/{client_id}")
async def get_client(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Карточка клиента: профиль + оплаты + публикации + сводка по деньгам."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    payments = (
        (
            await db.execute(
                select(AdPayment)
                .where(AdPayment.client_id == client_id)
                .order_by(AdPayment.paid_at.desc())
            )
        )
        .scalars()
        .all()
    )
    publications = (
        (
            await db.execute(
                select(AdPublication)
                .where(AdPublication.client_id == client_id)
                .order_by(AdPublication.published_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # Баланс нити (И1): единый источник правды paid/spent/remaining.
    balance = compute_balance(payments, publications)

    # Группировка публикаций по сообществам для UI
    by_community: dict = {}
    for pub in publications:
        cid = pub.community_vk_id
        if cid not in by_community:
            by_community[cid] = {
                "community_vk_id": cid,
                "community_name": pub.community_name or None,
                "community_screen_name": pub.community_screen_name,
                "publications": [],
                "total_price": 0,
                "paid_price": 0,
                "unpaid_price": 0,
                "paid_count": 0,
                "unpaid_count": 0,
            }
        pd = pub.to_dict()
        by_community[cid]["publications"].append(pd)
        price = pd["price"] or 0
        by_community[cid]["total_price"] += price
        if pub.paid_status == "paid":
            by_community[cid]["paid_price"] += price
            by_community[cid]["paid_count"] += 1
        else:
            by_community[cid]["unpaid_price"] += price
            by_community[cid]["unpaid_count"] += 1

    # Клиентские тоталы (общее)
    total_published = len(publications)
    total_paid_pubs = sum(1 for p in publications if p.paid_status == "paid")
    total_unpaid_pubs = total_published - total_paid_pubs
    total_paid_amount = sum((p.price or 0) for p in publications if p.paid_status == "paid")
    total_unpaid_amount = sum((p.price or 0) for p in publications if p.paid_status != "paid")

    return {
        "client": client.to_dict(),
        "payments": [p.to_dict() for p in payments],
        "publications": [p.to_dict() for p in publications],
        "publications_by_community": sorted(
            by_community.values(), key=lambda c: c["community_name"] or ""
        ),
        "publications_totals": {
            "total_count": total_published,
            "total_price": sum((p.price or 0) for p in publications),
            "paid_count": total_paid_pubs,
            "unpaid_count": total_unpaid_pubs,
            "paid_amount": total_paid_amount,
            "unpaid_amount": total_unpaid_amount,
            "debt": total_unpaid_amount,
        },
        "total_paid": balance["paid"],
        "total_awaiting": balance["awaiting"],
        "publications_count": len(publications),
        "balance": balance,
    }


@router.get("/clients/{client_id}/balance")
async def client_balance(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Только баланс нити клиента (лёгкий endpoint для будущего beat-напоминания И2)."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    payments = (
        (await db.execute(select(AdPayment).where(AdPayment.client_id == client_id)))
        .scalars()
        .all()
    )
    publications = (
        (await db.execute(select(AdPublication).where(AdPublication.client_id == client_id)))
        .scalars()
        .all()
    )
    return compute_balance(payments, publications)


@router.patch("/clients/{client_id}")
async def update_client(
    client_id: int,
    payload: ClientUpdateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Правка карточки: применяются только явно переданные поля."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    fields = payload.dict(exclude_unset=True)
    if "stage" in fields and fields["stage"] not in _VALID_STAGES:
        raise HTTPException(status_code=400, detail="invalid stage")
    old_stage = client.stage
    for key, value in fields.items():
        setattr(client, key, value)
    if "stage" in fields and fields["stage"] != old_stage:
        log_interaction(
            db,
            kind="status_changed",
            client_id=client.id,
            summary=f"Стадия: {old_stage} → {fields['stage']}",
            meta={"from": old_stage, "to": fields["stage"]},
        )
    await db.commit()
    await db.refresh(client)
    return client.to_dict()


@router.delete("/clients/{client_id}")
async def delete_client(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Архивировать клиента (аудит 2026-09-05): раньше DELETE каскадом стирал
    оплаты, пакеты и чат, а осиротевшие отложки выходили бесплатно и пропадали
    из очереди модерации. Архивный клиент скрыт из списков, история цела;
    вернуть — ``POST /clients/{id}/unarchive``."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    client.is_archived = True
    log_interaction(db, kind="archived", client_id=client.id, summary="Клиент архивирован")
    await db.commit()
    return {"success": True, "archived": True}


@router.post("/clients/{client_id}/unarchive")
async def unarchive_client(client_id: int, db: AsyncSession = Depends(get_db_session)):
    """Вернуть клиента из архива."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    client.is_archived = False
    log_interaction(
        db, kind="unarchived", client_id=client.id, summary="Клиент возвращён из архива"
    )
    await db.commit()
    return {"success": True, "archived": False}


@router.post("/clients/upsert-from-request/{request_id}")
async def upsert_from_request(
    request_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Завести клиента из заявки (или привязать к существующему по ``author_vk_id``).

    Ключ — ``author_vk_id`` заявки (fallback ``peer_id``). Если клиент с таким
    VK-id уже есть — привязываем заявку к нему, не создавая дубль. Заявке
    проставляется ``client_id``.
    """
    ar = await db.get(AdRequest, request_id)
    if not ar:
        raise HTTPException(status_code=404, detail="ad request not found")

    key = ar.author_vk_id or ar.peer_id
    if not key:
        raise HTTPException(status_code=400, detail="заявка без author_vk_id/peer_id")

    existing = (
        await db.execute(select(AdClient).where(AdClient.author_vk_id == int(key)))
    ).scalar_one_or_none()

    created = False
    if existing:
        client = existing
    else:
        client = AdClient(
            author_vk_id=int(key),
            author_is_group=bool(ar.author_is_group),
            name=ar.author_name,
            region_id=ar.region_id,
            stage="contacted" if ar.status == "contacted" else "detected",
        )
        db.add(client)
        await db.flush()  # получить client.id для привязки заявки
        created = True

    ar.client_id = client.id
    # Бэкфилл: события, записанные по этой заявке до появления клиента
    # (client_id IS NULL), привязываем к клиенту — чтобы они попали в его таймлайн.
    await db.execute(
        update(AdInteraction)
        .where(
            AdInteraction.ad_request_id == request_id,
            AdInteraction.client_id.is_(None),
        )
        .values(client_id=client.id)
    )
    log_interaction(
        db,
        kind="linked" if existing else "detected",
        client_id=client.id,
        ad_request_id=request_id,
        summary=(
            "Заявка привязана к существующему клиенту" if existing else "Заведён клиент из заявки"
        ),
    )
    await db.commit()
    await db.refresh(client)
    return {
        "client": client.to_dict(),
        "created": created,
        "linked_request_id": request_id,
    }


# ---------------------------------------------------------------- payments


@router.post("/payments")
async def create_payment(
    payload: PaymentCreateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Записать оплату/ожидание. ``status='paid'`` продвигает клиента в ``paid``.

    ``status='awaiting'`` — деньги ещё не пришли (согласованная ``amount``);
    клиента в воронке не двигаем (это делает фактическая оплата).
    """
    client = await db.get(AdClient, payload.client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    if payload.amount is None or float(payload.amount) <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    status = payload.status or "paid"
    if status not in _PAYMENT_STATUSES:
        raise HTTPException(status_code=400, detail="invalid payment status")

    now = datetime.utcnow()
    pay = AdPayment(
        client_id=int(payload.client_id),
        amount=payload.amount,
        method=payload.method,
        status=status,
        units_paid=payload.units_paid,
        bank=payload.bank,
        ad_request_id=payload.ad_request_id,
        scheduled_post_id=payload.scheduled_post_id,
        note=payload.note,
        paid_at=_parse_dt(payload.paid_at) or now,
        paid_confirmed_at=now if status == "paid" else None,
    )
    db.add(pay)
    # В воронку «оплачено» двигаем только при фактической оплате.
    if status == "paid" and client.stage != "lost":
        client.stage = "paid"
    # Доплата перевзводит напоминание о перерасходе (И2): «доплатил → можно
    # напомнить снова, когда расход опять догонит оплату».
    if status == "paid":
        client.spend_alerted_at = None
    await db.flush()  # получить pay.id для ссылки в событии
    bank_part = f", {pay.bank}" if pay.bank else ""
    if status == "awaiting":
        summary = f"Ожидание оплаты: {float(pay.amount):g} ₽{bank_part}"
        kind = "payment_awaiting"
    else:
        summary = f"Оплата {float(pay.amount):g} ₽{bank_part}"
        kind = "payment_added"
    log_interaction(
        db,
        kind=kind,
        client_id=client.id,
        payment_id=pay.id,
        ad_request_id=payload.ad_request_id,
        scheduled_post_id=payload.scheduled_post_id,
        summary=summary,
        meta={
            "amount": float(pay.amount),
            "method": pay.method,
            "bank": pay.bank,
            "status": status,
        },
    )
    await db.commit()
    await db.refresh(pay)
    return pay.to_dict()


@router.patch("/payments/{payment_id}")
async def update_payment(
    payment_id: int,
    payload: PaymentUpdateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Правка оплаты (сумма/способ/банк/статус/дата/заметка) — всё редактируемо.

    Переход ``awaiting → paid`` ставит ``paid_confirmed_at``, двигает клиента в
    ``paid`` (если не ``lost``) и пишет событие ``payment_paid``.
    """
    pay = await db.get(AdPayment, payment_id)
    if not pay:
        raise HTTPException(status_code=404, detail="payment not found")
    fields = payload.dict(exclude_unset=True)
    if "status" in fields and fields["status"] not in _PAYMENT_STATUSES:
        raise HTTPException(status_code=400, detail="invalid payment status")
    if "amount" in fields and fields["amount"] is not None and float(fields["amount"]) <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")

    became_paid = "status" in fields and fields["status"] == "paid" and pay.status != "paid"
    for key, value in fields.items():
        if key == "paid_at":
            dt = _parse_dt(value)
            if dt is not None:
                pay.paid_at = dt
        else:
            setattr(pay, key, value)

    if became_paid:
        pay.paid_confirmed_at = datetime.utcnow()
        client = await db.get(AdClient, pay.client_id)
        if client:
            if client.stage != "lost":
                client.stage = "paid"
            client.spend_alerted_at = None  # доплата перевзводит напоминание (И2)
        log_interaction(
            db,
            kind="payment_paid",
            client_id=pay.client_id,
            payment_id=pay.id,
            summary=f"Оплата подтверждена: {float(pay.amount):g} ₽"
            + (f", {pay.bank}" if pay.bank else ""),
            meta={"amount": float(pay.amount), "bank": pay.bank},
        )
        if pay.client_id:
            await _thank_client(db, pay.client_id, float(pay.amount))
    await db.commit()
    await db.refresh(pay)
    return pay.to_dict()


async def _thank_client(db, client_id: int, amount: float) -> None:
    """«Оплата получена» клиенту в личку ВК (PR 1.7 аудита 2026-09-05). Без commit."""
    from modules.ad_cabinet.vk_bot import notify as vk_notify

    await vk_notify.notify_client(
        db,
        int(client_id),
        f"✅ Оплата {amount:g} ₽ получена — спасибо! Баланс обновлён. "
        "Нужен чек самозанятого — скажите, пришлём.",
    )


class ConfirmPaidIn(BaseModel):
    """Подтверждение пачки счетов: банк зачисления (опционально, фикс-список)."""

    bank: Optional[str] = None


@router.post("/orders/{order_ref}/confirm-paid")
async def order_confirm_paid(
    order_ref: str, payload: ConfirmPaidIn, db: AsyncSession = Depends(get_db_session)
):
    """Подтвердить оплату ВСЕГО заказа — одна кнопка вместо N строк (PR 1.7)."""
    from modules.ad_cabinet import payment_claims

    res = await payment_claims.confirm_order(db, order_ref, bank=payload.bank)
    if not res["confirmed"]:
        raise HTTPException(status_code=404, detail="В заказе нет ожидающих оплаты счетов")
    await _thank_client(db, res["client_id"], res["amount"])
    await db.commit()
    return res


@router.post("/clients/{client_id}/payments/confirm-all")
async def client_confirm_all_paid(
    client_id: int, payload: ConfirmPaidIn, db: AsyncSession = Depends(get_db_session)
):
    """Подтвердить ВСЕ ожидающие счета клиента («всё оплачено»)."""
    from modules.ad_cabinet import payment_claims

    if await db.get(AdClient, client_id) is None:
        raise HTTPException(status_code=404, detail="client not found")
    res = await payment_claims.confirm_client(db, client_id, bank=payload.bank)
    if not res["confirmed"]:
        raise HTTPException(status_code=404, detail="Ожидающих оплаты счетов нет")
    await _thank_client(db, client_id, res["amount"])
    await db.commit()
    return res


@router.delete("/payments/{payment_id}")
async def delete_payment(
    payment_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Удалить оплату. Платёж за пакет (provider='package') — только через пакет."""
    pay = await db.get(AdPayment, payment_id)
    if not pay:
        raise HTTPException(status_code=404, detail="payment not found")
    if pay.provider == "package":
        raise HTTPException(
            status_code=409,
            detail="Это оплата пакета — она следует за пакетом; закройте пакет, а не платёж",
        )
    amount = float(pay.amount) if pay.amount is not None else 0
    log_interaction(
        db,
        kind="payment_deleted",
        client_id=pay.client_id,
        summary=f"Удалена оплата {amount:g} ₽",
        meta={"amount": amount},
    )
    await db.delete(pay)
    await db.commit()
    return {"success": True}


@router.get("/banks")
async def list_banks(db: AsyncSession = Depends(get_db_session)):
    """Банки для дропдауна оплаты + частоты использования (куда чаще платят).

    Возвращает фикс-список ``AD_PAYMENT_BANKS`` и счётчики по фактическим оплатам
    (status='paid'), отсортированные по убыванию — чтобы видеть предпочитаемый банк.
    """
    rows = (
        await db.execute(
            select(
                AdPayment.bank,
                func.count(AdPayment.id),
                func.coalesce(func.sum(AdPayment.amount), 0),
            )
            .where(AdPayment.status == "paid", AdPayment.bank.isnot(None))
            .group_by(AdPayment.bank)
            .order_by(func.count(AdPayment.id).desc())
        )
    ).all()
    stats = [
        {"bank": bank, "count": int(cnt or 0), "total": float(total or 0)}
        for bank, cnt, total in rows
    ]
    return {"banks": list(AD_PAYMENT_BANKS), "stats": stats}


# ---------------------------------------------------------------- publications


@router.post("/publications")
async def create_publication(
    payload: PublicationCreateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Записать публикацию. Привязанного клиента продвигаем в ``published``."""
    if payload.status not in _PUBLICATION_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")

    client = None
    if payload.client_id is not None:
        client = await db.get(AdClient, payload.client_id)
        if not client:
            raise HTTPException(status_code=404, detail="client not found")

    pub = AdPublication(
        client_id=payload.client_id,
        community_vk_id=int(payload.community_vk_id),
        vk_post_id=payload.vk_post_id,
        region_id=payload.region_id,
        ad_request_id=payload.ad_request_id,
        scheduled_post_id=payload.scheduled_post_id,
        price=payload.price,
        status=payload.status,
        note=payload.note,
        published_at=_parse_dt(payload.published_at) or datetime.utcnow(),
    )
    db.add(pub)
    # Не понижаем стадию уже оплаченного клиента — published только вперёд по воронке.
    if client is not None and client.stage in ("detected", "contacted", "scheduled"):
        client.stage = "published"
    await db.flush()  # получить pub.id для ссылки в событии
    log_interaction(
        db,
        kind="published",
        client_id=payload.client_id,
        publication_id=pub.id,
        ad_request_id=payload.ad_request_id,
        scheduled_post_id=payload.scheduled_post_id,
        summary=f"Публикация в сообщество {payload.community_vk_id}"
        + (f" ({payload.price:g} ₽)" if payload.price else ""),
        meta={"community_vk_id": int(payload.community_vk_id), "price": payload.price},
    )
    await db.commit()
    await db.refresh(pub)
    return pub.to_dict()


@router.delete("/publications/{publication_id}")
async def delete_publication(
    publication_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Удалить запись о публикации."""
    pub = await db.get(AdPublication, publication_id)
    if not pub:
        raise HTTPException(status_code=404, detail="publication not found")
    log_interaction(
        db,
        kind="publication_deleted",
        client_id=pub.client_id,
        summary=f"Удалена запись о публикации в сообщество {pub.community_vk_id}",
    )
    await db.delete(pub)
    await db.commit()
    return {"success": True}


@router.patch("/publications/{publication_id}")
async def update_publication(
    publication_id: int,
    payload: PublicationUpdateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Обновить публикацию: цена, статус оплаты (paid/unpaid), заметка, дата."""
    pub = await db.get(AdPublication, publication_id)
    if not pub:
        raise HTTPException(status_code=404, detail="publication not found")

    changed = []
    if payload.price is not None:
        from config.ad_landing import PRICE_FLOOR_RUB

        # Пол цены (Этап 2): 0 — бесплатно, иначе не ниже 200 ₽ за размещение.
        if 0 < float(payload.price) < PRICE_FLOOR_RUB:
            raise HTTPException(
                status_code=400,
                detail=f"Цена размещения не ниже {PRICE_FLOOR_RUB} ₽ (0 — бесплатно)",
            )
        pub.price = payload.price
        changed.append(f"цена={payload.price:g} ₽")
    if payload.paid_status is not None:
        if payload.paid_status not in ("paid", "unpaid"):
            raise HTTPException(status_code=400, detail="paid_status must be 'paid' or 'unpaid'")
        pub.paid_status = payload.paid_status
        changed.append(f"оплата={'да' if payload.paid_status == 'paid' else 'нет'}")
    if payload.note is not None:
        pub.note = payload.note
        changed.append("заметка")
    if payload.published_at is not None:
        pub.published_at = _parse_dt(payload.published_at) or pub.published_at
        changed.append("дата")

    if changed:
        log_interaction(
            db,
            kind="publication_updated",
            client_id=pub.client_id,
            publication_id=pub.id,
            summary="Обновлена публикация: " + ", ".join(changed),
            meta={"price": payload.price, "paid_status": payload.paid_status},
        )
    await db.commit()
    await db.refresh(pub)
    return pub.to_dict()


# ---------------------------------------------------------------- community info


@router.post("/community-info")
async def community_info(
    payload: CommunityInfoIn,
):
    """Разрешить VK-сообщество по URL/screen_name/ID → {group_id, name, photo, ...}."""
    from modules.vk_monitor.vk_client import VKClient
    from modules.vk_token_router import get_healthy_read_token
    from utils.vk_url import parse_vk_group_url

    group_id, screen_name = parse_vk_group_url(payload.url)
    if group_id is None and screen_name is None:
        raise HTTPException(status_code=400, detail="не удалось распознать VK-ссылку")

    token = await get_healthy_read_token()
    if not token:
        raise HTTPException(status_code=503, detail="no healthy VK parse-token")
    client = VKClient(token=token)

    if group_id is None and screen_name is not None:
        import asyncio

        try:
            resolved = await asyncio.to_thread(
                client.vk.utils.resolveScreenName, screen_name=screen_name
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"VK resolveScreenName failed: {e}")
        if not resolved or resolved.get("type") != "group":
            raise HTTPException(
                status_code=404, detail=f"VK не нашёл группу с адресом '{screen_name}'"
            )
        group_id = int(resolved.get("object_id") or 0)

    if not group_id:
        raise HTTPException(status_code=400, detail="не удалось определить group_id")

    import asyncio

    try:
        infos = await asyncio.to_thread(
            client.get_groups_by_ids,
            [abs(group_id)],
            "screen_name,members_count,photo_200",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"VK groups.getById failed: {e}")
    if not infos:
        raise HTTPException(status_code=404, detail=f"VK group {group_id} не найден")
    info = infos[0]
    return {
        "group_id": -abs(group_id),
        "screen_name": info.get("screen_name") or screen_name,
        "name": info.get("name") or "",
        "members_count": info.get("members_count"),
        "photo_url": info.get("photo_200"),
    }


# ---------------------------------------------------------------- resolve profile


@router.post("/resolve-profile")
async def resolve_profile(
    payload: ResolveProfileIn,
):
    """Разрешить VK-ссылку в данные профиля (человек или сообщество).

    Принимает URL/screen_name/ID, возвращает тип (user/group) и поля:
    name, photo_url, vk_id, screen_name. Используется для авто-заполнения
    формы создания клиента.
    """
    import asyncio

    from modules.vk_monitor.vk_client import VKClient
    from modules.vk_token_router import get_healthy_read_token
    from utils.vk_url import parse_vk_group_url

    raw = payload.url.strip()
    token = await get_healthy_read_token()
    if not token:
        raise HTTPException(status_code=503, detail="no healthy VK parse-token")
    vk = VKClient(token=token)

    # Пробуем как числовой ID
    numeric_id = None
    try:
        numeric_id = int(raw)
    except (ValueError, TypeError):
        pass

    # Похоже на vk.com/idXXX, vk.ru/idXXX или vk.com/durov
    user_screen_name = None
    if not numeric_id and raw:
        # Извлекаем screen_name или id из URL вида vk.com/xxx
        import re

        m = re.search(r"vk\.(?:com|ru)/([a-zA-Z0-9_.]+)", raw)
        if m:
            part = m.group(1)
            # id12345 → числовой id пользователя
            id_match = re.match(r"^id(\d+)$", part)
            if id_match:
                numeric_id = int(id_match.group(1))
            else:
                user_screen_name = part
        elif raw.startswith("@"):
            user_screen_name = raw[1:]
        elif re.match(r"^[a-zA-Z][a-zA-Z0-9_.]{3,}$", raw):
            user_screen_name = raw

    # 1. Пробуем как сообщество
    group_id, group_sn = parse_vk_group_url(raw)
    if group_id is not None:
        try:
            infos = await asyncio.to_thread(
                vk.get_groups_by_ids, [abs(group_id)], "screen_name,members_count,photo_200"
            )
            if infos:
                info = infos[0]
                return {
                    "type": "group",
                    "vk_id": -abs(group_id),
                    "name": info.get("name") or "",
                    "screen_name": info.get("screen_name") or group_sn or "",
                    "photo_url": info.get("photo_200"),
                    "members_count": info.get("members_count"),
                }
        except Exception:
            pass

    # 2. Пробуем как пользователя — по числовому id
    if numeric_id and numeric_id > 0:
        try:
            users = await asyncio.to_thread(
                vk.vk.users.get, user_ids=[numeric_id], fields="photo_200"
            )
            if users:
                u = users[0]
                return {
                    "type": "user",
                    "vk_id": u["id"],
                    "first_name": u.get("first_name", ""),
                    "last_name": u.get("last_name", ""),
                    "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                    "photo_url": u.get("photo_200"),
                }
        except Exception:
            pass

    # 3. Пробуем разрешить screen_name через utils.resolveScreenName
    if user_screen_name:
        try:
            resolved = await asyncio.to_thread(
                vk.vk.utils.resolveScreenName, screen_name=user_screen_name
            )
            if resolved and resolved.get("object_id"):
                obj_type = resolved.get("type", "")
                obj_id = int(resolved["object_id"])
                if obj_type == "user":
                    users = await asyncio.to_thread(
                        vk.vk.users.get, user_ids=[obj_id], fields="photo_200"
                    )
                    if users:
                        u = users[0]
                        return {
                            "type": "user",
                            "vk_id": u["id"],
                            "first_name": u.get("first_name", ""),
                            "last_name": u.get("last_name", ""),
                            "name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                            "photo_url": u.get("photo_200"),
                        }
                elif obj_type == "group":
                    infos = await asyncio.to_thread(
                        vk.get_groups_by_ids, [obj_id], "screen_name,photo_200"
                    )
                    if infos:
                        info = infos[0]
                        return {
                            "type": "group",
                            "vk_id": -obj_id,
                            "name": info.get("name") or "",
                            "screen_name": info.get("screen_name") or user_screen_name,
                            "photo_url": info.get("photo_200"),
                        }
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="не удалось найти профиль по этой ссылке")


# ---------------------------------------------------------------- scan posts


@router.post("/clients/{client_id}/scan-posts")
async def scan_client_posts(
    client_id: int,
    payload: ScanPostsIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Сканировать стену VK-сообщества на посты заказчика.

    Поиск:
    - ``keywords`` — точная фраза (подстрока) в тексте поста, без учёта регистра.
    - ``author_name`` — имя автора предложенного поста (signer_id).
    - Логика ИЛИ: пост подходит, если совпало ЛИБО keywords ЛИБО author_name.
    - ``date_from`` / ``date_to`` — ограничение по дате (wall.get пагинируется
      до границы date_from или до max_pages страниц).
    """
    import asyncio
    import re
    from datetime import datetime as dt

    from modules.vk_monitor.vk_client import VKClient
    from modules.vk_token_router import get_healthy_read_token

    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    owner_id = int(payload.community_vk_id)
    keywords_lower = (payload.keywords or "").strip().lower()
    author_lower = (payload.author_name or "").strip().lower()
    if not keywords_lower and not author_lower:
        raise HTTPException(status_code=400, detail="укажите ключевые слова и/или имя автора")

    date_from = _parse_dt(payload.date_from) if payload.date_from else None
    date_to = _parse_dt(payload.date_to) if payload.date_to else dt.utcnow()
    if date_from and date_from > date_to:
        date_from, date_to = date_to, date_from

    token = await get_healthy_read_token()
    if not token:
        raise HTTPException(status_code=503, detail="no healthy VK parse-token")
    vk = VKClient(token=token)

    # Нормализуем кавычки в ключевых словах: «» → ""
    _quotes_map = str.maketrans("«»", '""')
    keywords_normalized = keywords_lower.translate(_quotes_map) if keywords_lower else ""

    # Если author_name похож на VK-ссылку — резолвим в числовой vk_id для точного
    # сравнения по signer_id (надёжнее, чем подстрока в имени).
    author_vk_id: Optional[int] = None
    if author_lower and re.search(r"vk\.(?:com|ru)/id(\d+)", author_lower):
        m = re.search(r"vk\.(?:com|ru)/id(\d+)", author_lower)
        if m:
            author_vk_id = int(m.group(1))

    # Пагинируем wall.get до границы date_from или max_pages
    all_posts = []
    page = 0
    max_pages = max(1, min(payload.max_pages, 20))
    while page < max_pages:
        try:
            chunk = await asyncio.to_thread(
                vk.get_wall_posts, owner_id, count=100, offset=page * 100
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"VK wall.get failed: {e}")
        if not chunk:
            break
        all_posts.extend(chunk)
        # Проверяем, не вышли ли за date_from
        if date_from:
            oldest = chunk[-1].get("date", 0)
            if oldest < date_from.timestamp():
                break
        if len(chunk) < 100:
            break
        page += 1

    # Собираем signer_id со всех постов для резолва имён
    signer_ids = set()
    for post in all_posts:
        sid = post.get("signer_id")
        if sid and sid > 0:
            signer_ids.add(sid)

    signer_names: dict[int, str] = {}
    if signer_ids and author_lower:
        try:
            uid_list = list(signer_ids)[:500]
            users = await asyncio.to_thread(
                vk.vk.users.get, user_ids=uid_list, fields="screen_name"
            )
            for u in users:
                full = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip().lower()
                signer_names[u["id"]] = full
        except Exception:
            pass

    # Проверяем, какие посты уже есть в БД (один запрос)
    existing = set()
    if all_posts:
        post_ids = [p["id"] for p in all_posts]
        rows = (
            (
                await db.execute(
                    select(AdPublication.vk_post_id).where(
                        AdPublication.community_vk_id == owner_id,
                        AdPublication.vk_post_id.in_(post_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing = set(rows)

    # Фильтруем
    matched = []
    for post in all_posts:
        if post["id"] in existing:
            continue
        # Фильтр по дате
        post_ts = post.get("date", 0)
        if date_from and post_ts < date_from.timestamp():
            continue
        if date_to and post_ts > date_to.timestamp():
            continue

        text = (post.get("text") or "").lower()
        text_normalized = text.translate(_quotes_map)
        kw_match = keywords_normalized and keywords_normalized in text_normalized
        auth_match = False
        if author_lower:
            sid = post.get("signer_id")
            if sid and sid > 0:
                if author_vk_id:
                    auth_match = sid == author_vk_id
                elif sid in signer_names:
                    auth_match = author_lower in signer_names[sid]
        if kw_match or auth_match:
            matched.append(post)

    # Имя сообщества
    community_name = None
    community_screen_name = None
    try:
        infos = await asyncio.to_thread(vk.get_groups_by_ids, [abs(owner_id)], "screen_name")
        if infos:
            community_name = infos[0].get("name")
            community_screen_name = infos[0].get("screen_name")
    except Exception:
        pass

    # Создаём
    added = []
    for post in matched:
        post_date = dt.utcfromtimestamp(post["date"]) if post.get("date") else dt.utcnow()
        pub = AdPublication(
            client_id=client_id,
            community_vk_id=owner_id,
            vk_post_id=post["id"],
            price=None,
            status="published",
            paid_status="unpaid",
            published_at=post_date,
            views=(
                post.get("views", {}).get("count") if isinstance(post.get("views"), dict) else None
            ),
            likes=(
                post.get("likes", {}).get("count") if isinstance(post.get("likes"), dict) else None
            ),
            reposts=(
                post.get("reposts", {}).get("count")
                if isinstance(post.get("reposts"), dict)
                else None
            ),
            comments=(
                post.get("comments", {}).get("count")
                if isinstance(post.get("comments"), dict)
                else None
            ),
            community_name=community_name,
            community_screen_name=community_screen_name,
        )
        db.add(pub)
        added.append(pub)

    if added:
        if client.stage in ("detected", "contacted", "scheduled"):
            client.stage = "published"
        await db.flush()
        for pub in added:
            log_interaction(
                db,
                kind="published",
                client_id=client_id,
                publication_id=pub.id,
                summary=(
                    f"Сканер: найден пост в {community_name or owner_id}"
                    f" (post {pub.vk_post_id})"
                ),
                meta={"community_vk_id": owner_id, "vk_post_id": pub.vk_post_id},
            )
    await db.commit()

    oldest_ts = all_posts[-1].get("date", 0) if all_posts else None
    return {
        "found": len(matched),
        "already_known": len(existing),
        "added": len(added),
        "scanned_posts": len(all_posts),
        "pages": page + 1,
        "oldest_scanned_date": (
            dt.utcfromtimestamp(oldest_ts).strftime("%Y-%m-%d") if oldest_ts else None
        ),
        "publications": [p.to_dict() for p in added],
        "community": {
            "group_id": owner_id,
            "name": community_name,
            "screen_name": community_screen_name,
        },
    }


# ---------------------------------------------------------------- report


@router.post("/clients/{client_id}/report")
async def client_report(
    client_id: int,
    payload: ReportFiltersIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Отчёт клиента: публикации с фильтрами по сообществам, датам и статусу оплаты."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    stmt = select(AdPublication).where(
        AdPublication.client_id == client_id,
        AdPublication.status == "published",
    )
    if payload.community_vk_ids:
        stmt = stmt.where(AdPublication.community_vk_id.in_(payload.community_vk_ids))
    if payload.date_from:
        stmt = stmt.where(AdPublication.published_at >= _parse_dt(payload.date_from))
    if payload.date_to:
        stmt = stmt.where(AdPublication.published_at <= _parse_dt(payload.date_to))
    if payload.paid_status:
        stmt = stmt.where(AdPublication.paid_status == payload.paid_status)

    stmt = stmt.order_by(AdPublication.published_at.desc())
    publications = (await db.execute(stmt)).scalars().all()

    # Платежи и баланс
    payments = (
        (await db.execute(select(AdPayment).where(AdPayment.client_id == client_id)))
        .scalars()
        .all()
    )
    balance = compute_balance(payments, publications)

    # Группировка по сообществам
    by_community: dict[int, dict] = {}
    for pub in publications:
        cid = pub.community_vk_id
        if cid not in by_community:
            by_community[cid] = {
                "community_vk_id": cid,
                "community_name": pub.community_name or f"Группа {cid}",
                "community_screen_name": pub.community_screen_name,
                "publications": [],
                "total_price": 0,
                "paid_price": 0,
                "unpaid_price": 0,
                "paid_count": 0,
                "unpaid_count": 0,
            }
        pd = pub.to_dict()
        by_community[cid]["publications"].append(pd)
        price = pd["price"] or 0
        by_community[cid]["total_price"] += price
        if pub.paid_status == "paid":
            by_community[cid]["paid_price"] += price
            by_community[cid]["paid_count"] += 1
        else:
            by_community[cid]["unpaid_price"] += price
            by_community[cid]["unpaid_count"] += 1

    return {
        "client": client.to_dict(),
        "balance": balance,
        "publications": [p.to_dict() for p in publications],
        "by_community": sorted(by_community.values(), key=lambda c: c["community_name"] or ""),
        "totals": {
            "published_count": len(publications),
            "total_price": sum((p.price or 0) for p in publications),
            "paid_count": sum(1 for p in publications if p.paid_status == "paid"),
            "unpaid_count": sum(1 for p in publications if p.paid_status != "paid"),
            "paid_amount": sum((p.price or 0) for p in publications if p.paid_status == "paid"),
            "unpaid_amount": sum((p.price or 0) for p in publications if p.paid_status != "paid"),
        },
    }


# ---------------------------------------------------------------- funnel


# ---------------------------------------------------------------- timeline


@router.get("/clients/{client_id}/timeline")
async def client_timeline(
    client_id: int,
    limit: int = 200,
    db: AsyncSession = Depends(get_db_session),
):
    """Хронология событий клиента (свежие сверху) — из ``ad_interactions``.

    ``cabinet_visit`` исключён: до 24 строк «открыл кабинет» в сутки вытеснили
    бы из окна заказы и оплаты — то, ради чего оператор открывает карточку
    (should-fix ревью 2026-08-26). Визиты видны в сводной ленте вкладки.
    """
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    rows = (
        (
            await db.execute(
                select(AdInteraction)
                .where(
                    AdInteraction.client_id == client_id,
                    AdInteraction.kind != "cabinet_visit",
                )
                .order_by(AdInteraction.created_at.desc(), AdInteraction.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"timeline": [r.to_dict() for r in rows]}


@router.post("/interactions")
async def create_interaction(
    payload: InteractionCreateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Добавить ручную заметку/событие в таймлайн клиента."""
    client = await db.get(AdClient, payload.client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    if not (payload.summary or "").strip():
        raise HTTPException(status_code=400, detail="summary required")
    rec = log_interaction(
        db,
        kind=(payload.kind or "note").strip(),
        client_id=payload.client_id,
        summary=payload.summary.strip(),
    )
    dt = _parse_dt(payload.created_at)
    if dt is not None:
        rec.created_at = dt
    await db.commit()
    await db.refresh(rec)
    return rec.to_dict()


@router.patch("/interactions/{interaction_id}")
async def update_interaction(
    interaction_id: int,
    payload: InteractionUpdateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Правка события таймлайна (текст/вид/время) — для исправления опечаток."""
    rec = await db.get(AdInteraction, interaction_id)
    if not rec:
        raise HTTPException(status_code=404, detail="interaction not found")
    fields = payload.dict(exclude_unset=True)
    if "summary" in fields and fields["summary"] is not None:
        rec.summary = fields["summary"]
    if "kind" in fields and fields["kind"]:
        rec.kind = fields["kind"]
    if "created_at" in fields:
        dt = _parse_dt(fields["created_at"])
        if dt is not None:
            rec.created_at = dt
    await db.commit()
    await db.refresh(rec)
    return rec.to_dict()


@router.delete("/interactions/{interaction_id}")
async def delete_interaction(
    interaction_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Удалить ошибочное событие таймлайна."""
    rec = await db.get(AdInteraction, interaction_id)
    if not rec:
        raise HTTPException(status_code=404, detail="interaction not found")
    await db.delete(rec)
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------- client chat (PR-5)


class ClientReplyIn(BaseModel):
    """Ответ клиенту из чата карточки."""

    message: str


async def _resolve_client_dialog(db: AsyncSession, client_id: int):
    """(community_vk_id, peer_id) для чата клиента — из его свежей заявки с peer.

    Берём последнюю заявку клиента с положительным ``peer_id`` (предпочитая ЛС,
    но и у предложки peer есть). Возвращает (None, None), если диалога нет.
    """
    ar = (
        (
            await db.execute(
                select(AdRequest)
                .where(
                    AdRequest.client_id == client_id,
                    AdRequest.peer_id.isnot(None),
                    AdRequest.peer_id > 0,
                )
                .order_by(
                    AdRequest.origin.desc(),
                    AdRequest.detected_at.desc(),
                    AdRequest.id.desc(),
                )
            )
        )
        .scalars()
        .first()
    )
    if not ar:
        return None, None, None
    return int(ar.community_vk_id), int(ar.peer_id), ar


@router.get("/clients/{client_id}/thread")
async def client_thread(
    client_id: int,
    count: int = 30,
    db: AsyncSession = Depends(get_db_session),
):
    """Двусторонняя переписка с клиентом (вход + наши ответы) — для чата в карточке.

    ``messages.getHistory`` уже содержит и входящие, и наши исходящие (``out``),
    поэтому видно «я ему уже отвечал». Никогда не 500-ит на флапе VK.
    """
    import asyncio

    from modules.notifications.vk_dialogs_checker import VKDialogsChecker
    from modules.vk_token_router import load_vk_routing

    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    community_vk_id, peer_id, _ = await _resolve_client_dialog(db, client_id)
    if not peer_id:
        return {"messages": [], "reason": "no_dialog"}

    user_token, community_tokens = await load_vk_routing()
    if not user_token:
        return {"messages": [], "reason": "no_token"}

    try:
        checker = VKDialogsChecker(user_token, community_tokens=community_tokens)
        messages = await asyncio.to_thread(checker.fetch_history, community_vk_id, peer_id, count)
    except Exception as e:  # pragma: no cover - защита от неожиданного
        logger.warning("client thread fetch failed for %s: %s", client_id, e)
        return {"messages": [], "reason": "error", "error": str(e)}

    dialog_url = f"https://vk.com/gim{abs(community_vk_id)}?sel={peer_id}"
    return {
        "messages": messages,
        "community_vk_id": community_vk_id,
        "peer_id": peer_id,
        "dialog_url": dialog_url,
    }


@router.post("/clients/{client_id}/reply")
async def client_reply(
    client_id: int,
    payload: ClientReplyIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Отправить ответ клиенту от имени сообщества (полу-авто, как в кабинете).

    Цель (community, peer) — из свежей заявки клиента. VK 901/нет доступа →
    возвращаем ``personal_deeplink`` (оператор пишет с личного). Успех пишется в
    таймлайн (kind=reply_sent), чтобы видеть «я ему уже отвечал».
    """
    from modules.notifications.vk_actions import send_message
    from modules.vk_token_router import load_vk_routing

    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    if not (payload.message or "").strip():
        raise HTTPException(status_code=400, detail="empty message")

    community_vk_id, peer_id, _ = await _resolve_client_dialog(db, client_id)
    if not peer_id:
        raise HTTPException(status_code=400, detail="нет диалога с клиентом")

    user_token, community_tokens = await load_vk_routing()
    if not user_token:
        return {"success": False, "error": "VK token not found"}

    # Синхронный HTTP в ВК (до десятков секунд при таймаутах) — в поток, чтобы
    # не держать event loop всего web (PR 1.8 аудита 2026-09-05).
    import asyncio

    res = await asyncio.to_thread(
        send_message,
        group_id=community_vk_id,
        peer_id=peer_id,
        message=payload.message.strip(),
        user_token=user_token,
        community_tokens=community_tokens,
        random_id=0,
    )

    if res.get("success"):
        log_interaction(
            db,
            kind="reply_sent",
            client_id=client_id,
            summary="Ответ клиенту (чат): " + payload.message.strip()[:120],
            meta={"via": res.get("via")},
        )
        await db.commit()
        return {"success": True, "via": res.get("via")}

    if res.get("allowed") is False:
        return {
            "allowed": False,
            "personal_deeplink": res.get("personal_deeplink") or f"https://vk.com/im?sel={peer_id}",
            "error_code": res.get("error_code"),
        }
    return {
        "success": False,
        "error": res.get("error"),
        "error_code": res.get("error_code"),
    }


# ---------------------------------------------------------------- stats (С3)


@router.post("/clients/{client_id}/refresh-stats")
async def refresh_client_stats(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Обновить метрики (просмотры/лайки/репосты) публикаций клиента сейчас (С3).

    Кнопка «Обновить» в карточке. Тянет свежие цифры из VK для публикаций этого
    клиента; UI затем перечитывает карточку. Фон раз в день делает то же по всем.
    """
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    from modules.ad_cabinet.publication_stats import run_collect_stats

    result = await run_collect_stats(only_client_id=client_id)
    return result


@router.get("/clients/{client_id}/stats-report")
async def client_stats_report(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Готовый текст отчёта клиенту по метрикам его размещений (С3).

    Собирается из уже сохранённых метрик публикаций (status='published'). UI
    предзаполняет этим текстом чат — оператор правит и отправляет клиенту.
    """
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")

    pubs = (
        (
            await db.execute(
                select(AdPublication)
                .where(
                    AdPublication.client_id == client_id,
                    AdPublication.status == "published",
                )
                .order_by(AdPublication.published_at.desc())
            )
        )
        .scalars()
        .all()
    )
    measured = [p for p in pubs if p.stats_updated_at is not None]
    total_views = sum(int(p.views or 0) for p in measured)

    if not measured:
        report = "Статистика по вашим размещениям ещё собирается — заглянем чуть позже."
    else:
        lines = []
        for p in measured:
            when = p.published_at.strftime("%d.%m.%Y") if p.published_at else ""
            url = p.to_dict().get("vk_post_url") or f"сообщество {p.community_vk_id}"
            lines.append(
                f"• {when}: 👁 {int(p.views or 0)} просмотров, "
                f"❤ {int(p.likes or 0)}, 🔁 {int(p.reposts or 0)}\n  {url}"
            )
        report = (
            "📊 Статистика ваших рекламных размещений:\n\n"
            + "\n".join(lines)
            + f"\n\nИтого просмотров: {total_views}."
        )
    return {
        "report": report,
        "publications_measured": len(measured),
        "total_views": total_views,
    }


# ---------------------------------------------------------------- order items


@router.get("/clients/{client_id}/order-items")
async def list_order_items(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Позиции заказа клиента (что и сколько реклам заказано/будет опубликовано)."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    rows = (
        (
            await db.execute(
                select(AdOrderItem)
                .where(AdOrderItem.client_id == client_id)
                .order_by(AdOrderItem.created_at.desc(), AdOrderItem.id.desc())
            )
        )
        .scalars()
        .all()
    )
    total_qty = sum(int(r.quantity or 0) for r in rows)
    return {"order_items": [r.to_dict() for r in rows], "total_quantity": total_qty}


# ----------------------------------------------------- заявки клиента (И5)


@router.get("/clients/{client_id}/requests")
async def list_client_requests(
    client_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Все заявки (``ad_requests``) этого клиента — для overlay-модалки «всё в карточке» (И5).

    Сводит предложку и входящие ЛС, привязанные к клиенту через ``client_id``
    (привязка возникает при заведении/привязке клиента из заявки). Read-only:
    отдаёт снимки постов + VK-ссылки (``to_dict`` уже даёт ``vk_post_url`` /
    ``author_url`` / ``dialog_url``), чтобы оператор видел историю обращений
    клиента прямо из карточки, не уходя в общий инбокс.
    """
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    rows = (
        (
            await db.execute(
                select(AdRequest)
                .where(AdRequest.client_id == client_id)
                .order_by(AdRequest.detected_at.desc(), AdRequest.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"requests": [r.to_dict() for r in rows], "count": len(rows)}


@router.post("/order-items")
async def create_order_item(
    payload: OrderItemCreateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Добавить позицию заказа вручную."""
    client = await db.get(AdClient, payload.client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    if payload.status not in _ORDER_ITEM_STATUSES:
        raise HTTPException(status_code=400, detail="invalid order item status")
    if payload.quantity is not None and int(payload.quantity) < 1:
        raise HTTPException(status_code=400, detail="quantity must be >= 1")

    item = AdOrderItem(
        client_id=int(payload.client_id),
        description=payload.description,
        quantity=int(payload.quantity or 1),
        period_start=_parse_date(payload.period_start),
        period_end=_parse_date(payload.period_end),
        status=payload.status,
        ad_request_id=payload.ad_request_id,
        scheduled_post_id=payload.scheduled_post_id,
        publication_id=payload.publication_id,
        note=payload.note,
    )
    db.add(item)
    log_interaction(
        db,
        kind="order_item",
        client_id=client.id,
        ad_request_id=payload.ad_request_id,
        summary="Позиция заказа: "
        + ((payload.description or "без описания")[:80])
        + f" ×{item.quantity}",
    )
    await db.commit()
    await db.refresh(item)
    return item.to_dict()


@router.post("/order-items/from-request/{request_id}")
async def create_order_item_from_request(
    request_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Подтянуть позицию заказа из заявки предложки/ЛС (описание из текста заявки).

    Заявка должна быть привязана к клиенту (``client_id``) — иначе 400 с просьбой
    сначала завести/привязать клиента (кнопка «В CRM» на карточке заявки).
    """
    ar = await db.get(AdRequest, request_id)
    if not ar:
        raise HTTPException(status_code=404, detail="ad request not found")
    if not ar.client_id:
        raise HTTPException(status_code=400, detail="заявка не привязана к клиенту")

    description = (ar.text_snapshot or ar.community_name or "").strip()[:500]
    item = AdOrderItem(
        client_id=int(ar.client_id),
        ad_request_id=request_id,
        description=description or "реклама из заявки",
        quantity=1,
        status="planned",
    )
    db.add(item)
    log_interaction(
        db,
        kind="order_item",
        client_id=int(ar.client_id),
        ad_request_id=request_id,
        summary="Позиция заказа из заявки: " + (description[:80] or "реклама"),
    )
    await db.commit()
    await db.refresh(item)
    return item.to_dict()


@router.patch("/order-items/{item_id}")
async def update_order_item(
    item_id: int,
    payload: OrderItemUpdateIn,
    db: AsyncSession = Depends(get_db_session),
):
    """Правка позиции заказа (описание/кол-во/период/статус/заметка)."""
    item = await db.get(AdOrderItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="order item not found")
    fields = payload.dict(exclude_unset=True)
    if "status" in fields and fields["status"] not in _ORDER_ITEM_STATUSES:
        raise HTTPException(status_code=400, detail="invalid order item status")
    if "quantity" in fields and fields["quantity"] is not None and int(fields["quantity"]) < 1:
        raise HTTPException(status_code=400, detail="quantity must be >= 1")
    for key, value in fields.items():
        if key in ("period_start", "period_end"):
            setattr(item, key, _parse_date(value))
        else:
            setattr(item, key, value)
    await db.commit()
    await db.refresh(item)
    return item.to_dict()


@router.delete("/order-items/{item_id}")
async def delete_order_item(
    item_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    """Удалить позицию заказа."""
    item = await db.get(AdOrderItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="order item not found")
    await db.delete(item)
    await db.commit()
    return {"success": True}


# ---------------------------------------------------------------- stats / charts


@router.get("/stats/timeseries")
async def stats_timeseries(
    days: int = 30,
    db: AsyncSession = Depends(get_db_session),
):
    """Динамика по дням за ``days`` дней: рост предложений и оплаченной рекламы.

    Возвращает непрерывные ряды (дни без данных = 0) для Chart.js:
      * ``offers`` — число заявок (``ad_requests``) по ``detected_at``;
      * ``paid``   — число и сумма оплат (``status='paid'``) по ``paid_at``.
    """
    days = max(1, min(int(days), 365))
    start = (datetime.utcnow() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    offer_rows = (
        await db.execute(
            select(func.date(AdRequest.detected_at), func.count(AdRequest.id))
            .where(AdRequest.detected_at >= start)
            .group_by(func.date(AdRequest.detected_at))
        )
    ).all()
    paid_rows = (
        await db.execute(
            select(
                func.date(AdPayment.paid_at),
                func.count(AdPayment.id),
                func.coalesce(func.sum(AdPayment.amount), 0),
            )
            .where(AdPayment.status == "paid", AdPayment.paid_at >= start)
            .group_by(func.date(AdPayment.paid_at))
        )
    ).all()

    def _key(d) -> str:
        return d.isoformat() if hasattr(d, "isoformat") else str(d)

    offers_by_day = {_key(d): int(c or 0) for d, c in offer_rows}
    paid_by_day = {_key(d): (int(c or 0), float(s or 0)) for d, c, s in paid_rows}

    labels, offers, paid_count, paid_amount = [], [], [], []
    for i in range(days):
        day = (start + timedelta(days=i)).date().isoformat()
        labels.append(day)
        offers.append(offers_by_day.get(day, 0))
        c, s = paid_by_day.get(day, (0, 0.0))
        paid_count.append(c)
        paid_amount.append(s)

    return {
        "days": days,
        "labels": labels,
        "offers": offers,
        "paid_count": paid_count,
        "paid_amount": paid_amount,
    }


# ---------------------------------------------------------------- funnel


@router.get("/funnel")
async def funnel(db: AsyncSession = Depends(get_db_session)):
    """Сводка воронки: число клиентов по стадиям + деньги/публикации итого."""
    stage_rows = (
        await db.execute(select(AdClient.stage, func.count(AdClient.id)).group_by(AdClient.stage))
    ).all()
    by_stage = {stage: 0 for stage in _VALID_STAGES}
    total_clients = 0
    for stage, count in stage_rows:
        by_stage[stage] = int(count or 0)
        total_clients += int(count or 0)

    total_paid = (
        await db.execute(
            select(func.coalesce(func.sum(AdPayment.amount), 0)).where(AdPayment.status == "paid")
        )
    ).scalar_one()
    total_awaiting = (
        await db.execute(
            select(func.coalesce(func.sum(AdPayment.amount), 0)).where(
                AdPayment.status == "awaiting"
            )
        )
    ).scalar_one()
    publications_count = (await db.execute(select(func.count(AdPublication.id)))).scalar_one()
    total_views = (
        await db.execute(select(func.coalesce(func.sum(AdPublication.views), 0)))
    ).scalar_one()

    # Должники (С4): неоплаченные счета старше порога.
    from modules.ad_cabinet.debtors import DEBTOR_DAYS

    cutoff = datetime.utcnow() - timedelta(days=DEBTOR_DAYS)
    debtors_amount = (
        await db.execute(
            select(func.coalesce(func.sum(AdPayment.amount), 0)).where(
                AdPayment.status == "awaiting", AdPayment.created_at <= cutoff
            )
        )
    ).scalar_one()
    debtors_count = (
        await db.execute(
            select(func.count(func.distinct(AdPayment.client_id))).where(
                AdPayment.status == "awaiting",
                AdPayment.created_at <= cutoff,
                AdPayment.client_id.isnot(None),
            )
        )
    ).scalar_one()

    # Достижимость инбокса (Раунд 4): среди ОТКРЫТЫХ заявок (route=ad_cabinet,
    # status='new' — ровно то, что видит оператор на вкладке «Входящие», урок G91)
    # сколько рекламодателей с ОТКРЫТОЙ vs ЗАКРЫТОЙ ЛС. Закрытым (can_message=False)
    # сообщество писать не может → оператору нужен deeplink-фолбэк «ответить из
    # личного VK». Метрика делает разрыв достижимости видимым неделя-к-неделе.
    _open_inbox = (
        AdRequest.route == "ad_cabinet",
        AdRequest.status == "new",
    )
    inbox_reachable = (
        await db.execute(
            select(func.count(AdRequest.id)).where(*_open_inbox, AdRequest.can_message.is_(True))
        )
    ).scalar_one()
    inbox_unreachable = (
        await db.execute(
            select(func.count(AdRequest.id)).where(*_open_inbox, AdRequest.can_message.is_(False))
        )
    ).scalar_one()

    return {
        "stages": _VALID_STAGES,
        "by_stage": by_stage,
        "total_clients": total_clients,
        "total_paid": float(total_paid or 0),
        "total_awaiting": float(total_awaiting or 0),
        "publications_count": int(publications_count or 0),
        "total_views": int(total_views or 0),
        "debtors_count": int(debtors_count or 0),
        "debtors_amount": float(debtors_amount or 0),
        "debtor_days": DEBTOR_DAYS,
        "inbox_reachable": int(inbox_reachable or 0),
        "inbox_unreachable": int(inbox_unreachable or 0),
    }


@router.get("/greeting-text")
async def greeting_text(db: AsyncSession = Depends(get_db_session)):
    """Готовый текст авто-приветствия — для кнопки «скопировать» в кабинете.

    Тот же источник, что и у рассылки (``resolve_greeting_text``), с живыми
    цифрами сети. Персональные плейсхолдеры не подставляются: текст уходит в
    буфер обмена для ручной вставки в другие каналы, где ни имени собеседника,
    ни названия паблика нет — ``render`` подчистит артефакты за них.
    """
    body = await resolve_greeting_text(db)
    if not body:
        raise HTTPException(status_code=404, detail="greeting template not found")

    stats = await get_network_stats(db)
    text = render(
        body,
        communities_count=stats["communities_count"],
        subscribers_count=stats["subscribers_count"],
    )
    return {
        "text": text,
        "mangled": looks_mangled(body),
        **stats,
    }


# ----------------------------------------------------------------------
# Пакеты постов клиентов кабинета (заказ владельца 2026-08-26)
# ----------------------------------------------------------------------


class PackageIn(BaseModel):
    """Новый пакет клиенту. ``monthly=True`` — на текущий календарный месяц
    (МСК); иначе бессрочный. ``free_promo`` — акция: цена 0, по умолчанию
    3 поста; малмыжским ``site_ad=True`` — плюс размещение на сайте (вручную)."""

    kind: str = Field(..., pattern=r"^(free_promo|prepaid|postpaid|unlimited)$")
    # ``unlimited`` — квоты нет, posts_total=0 (Этап 2, 2026-09-05).
    posts_total: int = Field(..., ge=0, le=100)
    # Верхняя граница спасает от Infinity и переполнения NUMERIC(10,2) — 500 на
    # flush превращается в валидационную 422 (ревью 2026-08-26).
    price: float = Field(0, ge=0, le=9_999_999)
    monthly: bool = False
    site_ad: bool = False
    paid: bool = False  # prepaid: галочка «уже оплачено» сразу при создании
    note: Optional[str] = None


def _msk_today():
    from datetime import timedelta as _td

    return (datetime.utcnow() + _td(hours=3)).date()


def _month_bounds(day):
    import calendar
    from datetime import date as _date

    last = calendar.monthrange(day.year, day.month)[1]
    return _date(day.year, day.month, 1), _date(day.year, day.month, last)


@router.get("/clients/{client_id}/packages")
async def client_packages(client_id: int, db: AsyncSession = Depends(get_db_session)):
    """Пакеты клиента (все, свежие сверху) + текущее состояние enforcement."""
    from modules.ad_cabinet import packages as pkgs

    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    rows = (
        (
            await db.execute(
                select(AdClientPackage)
                .where(AdClientPackage.client_id == client_id)
                .order_by(AdClientPackage.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    state = await pkgs.get_state(db, client_id)
    return {
        "packages": [r.to_dict() for r in rows],
        "active_package": state["package"].to_dict() if state["package"] else None,
        "block_reason": state["block_reason"],
    }


@router.post("/clients/{client_id}/packages")
async def create_package(
    client_id: int, payload: PackageIn, db: AsyncSession = Depends(get_db_session)
):
    """Выдать пакет: акционный бесплатный, предоплатный или постоплатный."""
    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    from modules.ad_cabinet import packages as _pkgs

    unlimited = payload.kind == "unlimited"
    if not unlimited and payload.posts_total < 1:
        raise HTTPException(status_code=400, detail="posts_total must be >= 1")
    period_start = period_end = None
    if unlimited:
        # 30 дней с даты оплаты: период ставится галочкой «оплачен» (здесь — если сразу).
        if payload.paid:
            period_start, period_end = _pkgs.unlimited_period(_msk_today())
    elif payload.monthly:
        period_start, period_end = _month_bounds(_msk_today())
    row = AdClientPackage(
        client_id=client_id,
        kind=payload.kind,
        posts_total=0 if unlimited else payload.posts_total,
        price=payload.price if payload.kind != "free_promo" else 0,
        period_start=period_start,
        period_end=period_end,
        paid_at=datetime.utcnow() if (payload.paid or payload.kind == "free_promo") else None,
        site_ad=payload.site_ad,
        note=payload.note,
    )
    db.add(row)
    await db.flush()
    # Деньги пакета — в ad_payments (097): иначе «оплачено» клиента не видит их.
    from modules.ad_cabinet import packages as _pkgs

    if await _pkgs.record_package_payment(db, row) is not None:
        if client.stage not in ("lost",):
            client.stage = "paid"
        client.spend_alerted_at = None
    log_interaction(
        db,
        kind="package_created",
        client_id=client_id,
        summary=(
            (
                f"Безлимит {_pkgs.UNLIMITED_DAYS} дн."
                if unlimited
                else f"Пакет {row.kind}: {row.posts_total} постов"
            )
            + (f", {float(row.price):.0f} ₽" if float(row.price or 0) else ", бесплатно")
            + (
                f", до {period_end.isoformat()}"
                if period_end
                else (", период с даты оплаты" if unlimited else ", бессрочно")
            )
            + (" + реклама на сайте" if row.site_ad else "")
        ),
    )
    await db.commit()
    await db.refresh(row)
    return row.to_dict()


async def _get_package(db, package_id: int) -> AdClientPackage:
    row = await db.get(AdClientPackage, package_id)
    if not row:
        raise HTTPException(status_code=404, detail="package not found")
    return row


@router.post("/packages/{package_id}/mark-paid")
async def package_mark_paid(package_id: int, db: AsyncSession = Depends(get_db_session)):
    """Галочка «оплачено»: prepaid открывается, postpaid-долг снимается."""
    row = await _get_package(db, package_id)
    if row.paid_at is None:
        row.paid_at = datetime.utcnow()
        from modules.ad_cabinet import packages as _pkgs

        if _pkgs.is_unlimited(row) and row.period_end is None:
            row.period_start, row.period_end = _pkgs.unlimited_period(_msk_today())
        pay = await _pkgs.record_package_payment(db, row)
        if pay is not None:
            client = await db.get(AdClient, row.client_id)
            if client is not None:
                if client.stage != "lost":
                    client.stage = "paid"
                client.spend_alerted_at = None
        log_interaction(
            db,
            kind="package_paid",
            client_id=row.client_id,
            summary=(
                f"Пакет #{row.id} отмечен оплаченным ({float(row.price or 0):.0f} ₽)"
                + (", в оплаты записано" if pay is not None else "")
            ),
        )
        await db.commit()
    await db.refresh(row)
    return row.to_dict()


@router.post("/packages/{package_id}/extend")
async def package_extend(package_id: int, db: AsyncSession = Depends(get_db_session)):
    """Продлить месячный пакет на СЛЕДУЮЩИЙ месяц (новая строка, оплата не
    проставляется — в долг по доверию либо до галочки). Только вручную —
    авто-продления нет (решение владельца)."""
    import calendar
    from datetime import date as _date
    from datetime import timedelta as _td

    from modules.ad_cabinet import packages as _pkgs

    row = await _get_package(db, package_id)
    if not row.period_end:
        raise HTTPException(status_code=400, detail="бессрочный пакет не продлевается")
    nxt = row.period_end + _td(days=1)
    if _pkgs.is_unlimited(row):
        # Безлимит: следующие 30 дней встык, оплата — галочкой (период уже задан).
        exists = (
            await db.execute(
                select(AdClientPackage.id).where(
                    AdClientPackage.client_id == row.client_id,
                    AdClientPackage.is_active.is_(True),
                    AdClientPackage.kind == "unlimited",
                    AdClientPackage.period_start == nxt,
                )
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="следующий период безлимита уже выдан")
        u_start, u_end = _pkgs.unlimited_period(nxt)
        new = AdClientPackage(
            client_id=row.client_id,
            kind="unlimited",
            posts_total=0,
            price=row.price,
            period_start=u_start,
            period_end=u_end,
            site_ad=False,
            note=f"продление безлимита #{row.id}",
        )
        db.add(new)
        await db.flush()
        log_interaction(
            db,
            kind="package_extended",
            client_id=row.client_id,
            summary=f"Безлимит продлён до {u_end.isoformat()} (#{row.id} → #{new.id})",
        )
        await db.commit()
        await db.refresh(new)
        return new.to_dict()
    # Идемпотентность: двойной клик «продлить» не выдаёт двойную квоту.
    exists = (
        await db.execute(
            select(AdClientPackage.id).where(
                AdClientPackage.client_id == row.client_id,
                AdClientPackage.is_active.is_(True),
                AdClientPackage.period_start == nxt.replace(day=1),
            )
        )
    ).first()
    if exists:
        raise HTTPException(status_code=409, detail="на этот месяц пакет уже выдан")
    last = calendar.monthrange(nxt.year, nxt.month)[1]
    new = AdClientPackage(
        client_id=row.client_id,
        kind=row.kind,
        posts_total=row.posts_total,
        price=row.price,
        period_start=_date(nxt.year, nxt.month, 1),
        period_end=_date(nxt.year, nxt.month, last),
        site_ad=False,
        note=f"продление пакета #{row.id}",
    )
    db.add(new)
    await db.flush()
    log_interaction(
        db,
        kind="package_extended",
        client_id=row.client_id,
        summary=f"Пакет продлён на {new.period_start.isoformat()[:7]} (#{row.id} → #{new.id})",
    )
    await db.commit()
    await db.refresh(new)
    return new.to_dict()


@router.post("/packages/{package_id}/deactivate")
async def package_deactivate(package_id: int, db: AsyncSession = Depends(get_db_session)):
    row = await _get_package(db, package_id)
    row.is_active = False
    log_interaction(
        db, kind="package_deactivated", client_id=row.client_id, summary=f"Пакет #{row.id} закрыт"
    )
    await db.commit()
    await db.refresh(row)
    return row.to_dict()


@router.post("/packages/{package_id}/site-ad-done")
async def package_site_ad_done(package_id: int, db: AsyncSession = Depends(get_db_session)):
    """Отметить выполненным размещение на сайте вМалмыже.рф (акция; руками)."""
    row = await _get_package(db, package_id)
    if row.site_ad_done_at is None:
        row.site_ad_done_at = datetime.utcnow()
        log_interaction(
            db,
            kind="package_site_ad_done",
            client_id=row.client_id,
            summary="Реклама на сайте вМалмыже.рф размещена (акция)",
        )
        await db.commit()
    await db.refresh(row)
    return row.to_dict()


# ----------------------------------------------------------------------
# Модерация клиентских постов (кабинет рекламодателя)
# ----------------------------------------------------------------------


class RejectIn(BaseModel):
    comment: str = ""


def _cabinet_publisher_factory(db):
    async def factory(gid: int):
        from modules.publisher.vk_publisher_extended import VKPublisher

        return await VKPublisher.create_with_policy(db, target_group_id=gid)

    return factory


class ApproveIn(BaseModel):
    """Тело одобрения: новая дата выхода (ISO, МСК), если старая прошла."""

    publish_at: Optional[str] = None


@router.get("/moderation")
async def moderation_queue(db: AsyncSession = Depends(get_db_session)):
    """Клиентские посты, ждущие одобрения владельца (``status='pending'``)."""
    # OUTER JOIN (аудит 2026-09-05): pending-пост без карточки клиента (сирота
    # после старого DELETE) раньше исчезал из очереди навсегда — теперь виден.
    # Название района и ссылки на фото клиента — чтобы владелец не одобрял вслепую.
    rows = (
        await db.execute(
            select(AdScheduledPost, AdClient, Region.name)
            .outerjoin(AdClient, AdClient.id == AdScheduledPost.client_id)
            .outerjoin(Region, Region.id == AdScheduledPost.region_id)
            .where(AdScheduledPost.status == "pending")
            .order_by(AdScheduledPost.publish_date.asc(), AdScheduledPost.id.asc())
        )
    ).all()
    out = []
    for post, client, region_name in rows:
        d = post.to_dict()
        d["client"] = client.to_dict() if client is not None else None
        d["region_name"] = region_name
        d["photo_urls"] = (
            [
                f"/api/ad-crm/clients/{int(post.client_id)}/photos/{n}"
                for n in (post.image_names or [])
            ]
            if post.client_id
            else []
        )
        out.append(d)
    return {"pending": out}


@router.get("/moderation/count")
async def moderation_count(db: AsyncSession = Depends(get_db_session)):
    """Счётчики для бейджа «Кабинеты»: ждут одобрения · непрочитанные · заявили оплату."""
    pending = (
        await db.execute(
            select(func.count(AdScheduledPost.id)).where(AdScheduledPost.status == "pending")
        )
    ).scalar_one()
    unread = (
        await db.execute(
            select(func.count(AdChatMessage.id)).where(
                AdChatMessage.sender == "client", AdChatMessage.read_at.is_(None)
            )
        )
    ).scalar_one()
    # «Заявили оплату» — клиенты с ожидающими счетами, по которым нажато
    # «Я оплатил» (098): владельцу надо подтвердить, бейдж это считает.
    claimed = (
        await db.execute(
            select(func.count(func.distinct(AdPayment.client_id))).where(
                AdPayment.status == "awaiting", AdPayment.claimed_at.isnot(None)
            )
        )
    ).scalar_one()
    return {
        "pending": int(pending or 0),
        "unread": int(unread or 0),
        "claimed": int(claimed or 0),
    }


@router.get("/clients/{client_id}/photos/{name}")
async def owner_client_photo(client_id: int, name: str):
    """Фото клиента для очереди модерации (владелец). Файлы вне /static — отдаём сами."""
    from pathlib import Path

    from fastapi.responses import FileResponse

    from web.api.advertiser_cabinet import _client_photo_dir

    base = Path(str(name or "")).name  # отсечь path-traversal
    p = _client_photo_dir(int(client_id)) / base
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return FileResponse(p)


@router.post("/moderation/order/{order_ref}/approve")
async def moderation_order_approve(order_ref: str, db: AsyncSession = Depends(get_db_session)):
    """Одобрить ВЕСЬ заказ (все pending-строки с этим order_ref) — один клик вместо N.

    Прошедшие даты не переносим: такие строки остаются pending, ответ несёт
    ``past_date`` — их владелец одобряет по одной с новой датой.
    """
    from modules.ad_cabinet import client_orders
    from modules.vk_token_router import load_photo_upload_token
    from web.api.advertiser_cabinet import _msk_to_unix, _real_attachment_builder

    rows = (
        (
            await db.execute(
                select(AdScheduledPost).where(
                    AdScheduledPost.order_ref == str(order_ref)[:36],
                    AdScheduledPost.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="В заказе нет постов на одобрении")
    # Фото клиента заливает аккаунт-владелец группы (docs/ops/photo-upload-path.md).
    photo_token = await load_photo_upload_token(db)
    ok = failed = past = 0
    errors: List[str] = []
    for row in rows:
        try:
            await client_orders.approve_post(
                db,
                row,
                publisher_factory=_cabinet_publisher_factory(db),
                attachment_builder=_real_attachment_builder(row.client_id, photo_token),
                msk_to_unix=_msk_to_unix,
            )
        except client_orders.OrderError:
            past += 1
            continue
        if row.status == "scheduled":
            ok += 1
        else:
            failed += 1
            if row.error_message:
                errors.append(row.error_message[:120])
        log_interaction(
            db,
            kind="moderation_approved" if row.status == "scheduled" else "moderation_failed",
            client_id=row.client_id,
            scheduled_post_id=row.id,
            summary=(
                "Пост клиента одобрен (весь заказ)"
                if row.status == "scheduled"
                else f"Одобрение не дошло до VK: {row.error_message}"
            ),
        )
    client_id = rows[0].client_id
    if ok and client_id:
        from modules.ad_cabinet.vk_bot import notify as vk_notify

        await vk_notify.notify_client(
            db, client_id, f"✅ Ваш заказ одобрен: {ok} размещений поставлены в очередь."
        )
    await db.commit()
    return {
        "total": len(rows),
        "approved": ok,
        "failed": failed,
        "past_date": past,
        "errors": errors,
    }


@router.post("/moderation/order/{order_ref}/reject")
async def moderation_order_reject(
    order_ref: str, payload: RejectIn, db: AsyncSession = Depends(get_db_session)
):
    """Отклонить ВЕСЬ заказ с одной причиной (обязательна — клиент должен понять, что править)."""
    from modules.ad_cabinet import client_orders

    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Укажите причину отказа — она видна клиенту")
    rows = (
        (
            await db.execute(
                select(AdScheduledPost).where(
                    AdScheduledPost.order_ref == str(order_ref)[:36],
                    AdScheduledPost.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="В заказе нет постов на одобрении")
    for row in rows:
        await client_orders.reject_post(db, row, comment=comment)
        log_interaction(
            db,
            kind="moderation_rejected",
            client_id=row.client_id,
            scheduled_post_id=row.id,
            summary=f"Пост клиента отклонён (весь заказ): {comment}",
        )
    client_id = rows[0].client_id
    if client_id:
        from modules.ad_cabinet.vk_bot import notify as vk_notify

        await vk_notify.notify_client(
            db, client_id, f"🚫 Ваш заказ не принят: {comment} Поправьте и отправьте снова."
        )
    await db.commit()
    return {"total": len(rows), "rejected": len(rows)}


@router.post("/moderation/{post_id}/approve")
async def moderation_approve(
    post_id: int,
    payload: Optional[ApproveIn] = None,
    db: AsyncSession = Depends(get_db_session),
):
    """Одобрить pending-пост: отправка в VK-отложку + счётчик доверия клиента.

    Идемпотентно (повторный клик не публикует второй раз — approve_post
    возвращает не-pending пост как есть). Прошедшая дата выхода → 409 с
    просьбой передать ``publish_at`` (аудит 2026-09-05: раньше пост молча
    выходил «через три минуты»).
    """
    from modules.ad_cabinet import client_orders
    from modules.vk_token_router import load_photo_upload_token
    from web.api.advertiser_cabinet import _msk_to_unix, _real_attachment_builder

    row = await db.get(AdScheduledPost, post_id)
    if not row:
        raise HTTPException(status_code=404, detail="post not found")
    was_pending = row.status == "pending"

    new_at = None
    if payload is not None and payload.publish_at:
        try:
            new_at = datetime.fromisoformat(payload.publish_at).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректная дата publish_at")

    # Фото клиента заливает аккаунт-владелец группы (docs/ops/photo-upload-path.md).
    photo_token = await load_photo_upload_token(db)
    try:
        await client_orders.approve_post(
            db,
            row,
            publisher_factory=_cabinet_publisher_factory(db),
            attachment_builder=_real_attachment_builder(row.client_id, photo_token),
            msk_to_unix=_msk_to_unix,
            new_publish_at=new_at,
        )
    except client_orders.OrderError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if was_pending:
        log_interaction(
            db,
            kind="moderation_approved" if row.status == "scheduled" else "moderation_failed",
            client_id=row.client_id,
            scheduled_post_id=row.id,
            summary=(
                "Пост клиента одобрен и отправлен в отложку"
                if row.status == "scheduled"
                else f"Одобрение не дошло до VK: {row.error_message}"
            ),
        )
        if row.status == "scheduled" and row.client_id:
            from modules.ad_cabinet.vk_bot import notify as vk_notify

            when = row.publish_date.strftime("%d.%m %H:%M") if row.publish_date else "—"
            await vk_notify.notify_client(
                db,
                row.client_id,
                f"✅ Ваш пост одобрен и выйдет {when} МСК.",
            )
    await db.commit()
    await db.refresh(row)
    return row.to_dict()


@router.post("/moderation/{post_id}/reject")
async def moderation_reject(
    post_id: int, payload: RejectIn, db: AsyncSession = Depends(get_db_session)
):
    """Отклонить pending-пост; причина видна клиенту в кабинете и обязательна
    (аудит 2026-09-05: Escape в prompt отклонял безвозвратно и без объяснения)."""
    from modules.ad_cabinet import client_orders

    if not (payload.comment or "").strip():
        raise HTTPException(status_code=400, detail="Укажите причину отказа — она видна клиенту")
    row = await db.get(AdScheduledPost, post_id)
    if not row:
        raise HTTPException(status_code=404, detail="post not found")
    was_pending = row.status == "pending"
    await client_orders.reject_post(db, row, comment=payload.comment)
    if was_pending:
        log_interaction(
            db,
            kind="moderation_rejected",
            client_id=row.client_id,
            scheduled_post_id=row.id,
            summary=f"Пост клиента отклонён: {payload.comment or 'без причины'}",
        )
        if row.client_id:
            from modules.ad_cabinet.vk_bot import notify as vk_notify

            reason = (payload.comment or "").strip()
            await vk_notify.notify_client(
                db,
                row.client_id,
                "🚫 Ваш пост не принят"
                + (f": {reason}" if reason else ".")
                + " Поправьте и отправьте снова.",
            )
    await db.commit()
    await db.refresh(row)
    return row.to_dict()


# События кабинета в сводной ленте активности: приход/визиты/действия клиентов
# и всё, что у них ломается. Операторские правки карточек сюда не входят —
# лента отвечает на вопрос «что делают КЛИЕНТЫ», а не «что делал я сам».
CABINET_ACTIVITY_KINDS = (
    "cabinet_signup",
    "cabinet_visit",
    "cabinet_js_error",
    "cabinet_order_refused",
    "client_order",
    "cancelled",
    "moderation_approved",
    "moderation_rejected",
    "moderation_failed",
    # Деньги и выход (аудит 2026-09-05): лента без них не отвечала на
    # «оплаты не теряются, посты выходят».
    "published",
    "payment_paid",
    "payment_claimed",
    "outreach_sent",
    "package_created",
    "package_paid",
    "package_extended",
)

# Ошибочные события — UI подсвечивает красным.
CABINET_ACTIVITY_ERROR_KINDS = ("cabinet_js_error", "cabinet_order_refused", "moderation_failed")


@router.get("/cabinet-activity")
async def cabinet_activity(limit: int = 60, db: AsyncSession = Depends(get_db_session)):
    """Сводная лента активности кабинетов — по всем клиентам, свежие сверху.

    Заказ владельца 2026-08-26: видеть, что клиенты ПРИХОДЯТ и что у них
    РАБОТАЕТ/ЛОМАЕТСЯ, не открывая карточку каждого. Источник — тот же
    ``ad_interactions``, что и per-client таймлайн.
    """
    limit = max(1, min(int(limit), 300))
    # «cancelled»/«client_order» пишет и операторский CRM (actor='operator') —
    # в ленту КЛИЕНТСКОЙ активности такие не идут: массовая операторская отмена
    # не должна выглядеть приходом клиентов (should-fix ревью 2026-08-26).
    ambiguous = ("cancelled", "client_order")
    rows = (
        await db.execute(
            select(AdInteraction, AdClient.name)
            .outerjoin(AdClient, AdClient.id == AdInteraction.client_id)
            .where(
                AdInteraction.kind.in_(CABINET_ACTIVITY_KINDS),
                or_(
                    AdInteraction.kind.notin_(ambiguous),
                    AdInteraction.actor == "client",
                ),
            )
            .order_by(AdInteraction.created_at.desc(), AdInteraction.id.desc())
            .limit(limit)
        )
    ).all()
    return {
        "activity": [
            {
                **rec.to_dict(),
                "client_name": client_name,
                "is_error": rec.kind in CABINET_ACTIVITY_ERROR_KINDS,
            }
            for rec, client_name in rows
        ]
    }


# ----------------------------------------------------------------------
# Чат с клиентами кабинета (владелец: слева список, справа переписка)
# ----------------------------------------------------------------------


class OwnerChatIn(BaseModel):
    body: str


@router.get("/cabinets")
async def cabinets_list(all: int = 0, db: AsyncSession = Depends(get_db_session)):
    """Список кабинетов клиентов: номер, имя, последнее движение, счётчики.

    Заказ владельца 2026-09-02: одна строка — один кабинет, кликабельная,
    с уникальным номером (чтобы в разговоре с клиентом понимать, в какой из
    кабинетов-дублей он зашёл), свежие движения сверху. ``all=1`` добавляет
    CRM-клиентов без аккаунта. Логика — ``modules.ad_cabinet.cabinet_list``.
    """
    from modules.ad_cabinet.cabinet_list import list_cabinets

    return {"cabinets": await list_cabinets(db, include_unlinked=bool(all))}


@router.get("/chat/overview")
async def chat_overview(db: AsyncSession = Depends(get_db_session)):
    """Список тредов: клиент, последнее сообщение, unread-счётчик владельца."""
    from modules.ad_cabinet import chat as cabinet_chat

    return {"threads": await cabinet_chat.owner_overview(db)}


@router.get("/chat/{client_id}")
async def chat_thread(
    client_id: int,
    after_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db_session),
):
    """Переписка с клиентом (чтение помечает входящие прочитанными)."""
    from modules.ad_cabinet import chat as cabinet_chat

    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    rows = await cabinet_chat.fetch_thread(
        db, client_id, reader=cabinet_chat.SENDER_OWNER, after_id=after_id
    )
    await db.commit()  # отметки read_at
    return {"client": client.to_dict(), "messages": [r.to_dict() for r in rows]}


@router.post("/chat/{client_id}")
async def chat_reply(
    client_id: int, payload: OwnerChatIn, db: AsyncSession = Depends(get_db_session)
):
    """Ответ владельца в тред клиента."""
    from modules.ad_cabinet import chat as cabinet_chat

    client = await db.get(AdClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="client not found")
    try:
        row = await cabinet_chat.post_message(
            db, client_id, cabinet_chat.SENDER_OWNER, payload.body
        )
    except cabinet_chat.ChatError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log_interaction(
        db,
        kind="chat_reply",
        client_id=client_id,
        summary="Ответ клиенту в чате кабинета",
    )
    # Ответ уходит и в личку ВК клиента (бот САРАФАНа) — клиент, который в
    # кабинет не заходит, иначе ответа не увидит. В журнал не пишем: сам
    # chat_reply уже записан строкой выше.
    from modules.ad_cabinet.vk_bot import notify as vk_notify

    await vk_notify.notify_client(db, client_id, f"💬 САРАФАН: {payload.body}", log=False)
    await db.commit()
    await db.refresh(row)
    return row.to_dict()
