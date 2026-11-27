"""Публичный приём заявок с лендинга ``/regions/links``.

Заказ владельца 2026-11-27: витрина сети дублируется на публичный домен
(валентин.вмалмыже.рф), и клиент должен **нажать кнопку и создать заявку**,
а не скопировать текст в буфер (до этого «Заказать» только клала текст в буфер
обмена, а заявка доезжала до владельца через ВК/Телеграм вручную).

Эндпоинт **публичный** (точный путь в ``middleware/auth_gate.py`` → PUBLIC_EXACT):
у анонимного посетителя нет ни сессии, ни аккаунта. Поэтому всё, что приходит
из браузера, считается недоверенным:

* **цена считается на сервере** по ``config/ad_landing.py`` — клиент присылает
  только коды сообществ, а цену и охват считаем мы (клиент может прислать
  что угодно, вплоть до «вам 0 ₽»);
* **названия и подписчики берём из БД**, а не из тела запроса: список районов
  меняется, а заявка должна описывать ту картину, которая была на экране;
* **анти-спам**: honeypot + лимит на IP + суточный потолок на все заявки +
  дедуп по токену отправки (двойной клик не должен плодить заявки).

Уведомление владельцу — Telegram + личка ВК (готовый ``vk_bot.notify``) и
MAX (``max_notifier.send_card``). Обе отправки best-effort: упавшее
уведомление не должно ронять приём заявки.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.models import LandingOrder, Region, RegionMemberSnapshot

logger = logging.getLogger(__name__)

router = APIRouter()

#: Сколько сообществ клиент может выбрать за одну заявку. Больше — явно не
#: человек (или скрипт, который забыл ограничить себя).
MAX_REGIONS = 200
#: Лимиты приёма на один IP: в минуту и в сутки.
MAX_PER_IP_PER_MIN = 3
MAX_PER_IP_PER_DAY = 10
#: Суточный потолок на ВСЕ заявки. Нужен на случай, если IP посетителей не
#: различимы (edge без X-Forwarded-For) — тогда лимит «на IP» стал бы лимитом
#: «на всех» и заявки перестали бы доходить.
MAX_PER_DAY_ALL = 50


class LandingOrderIn(BaseModel):
    """Тело запроса формы заявки."""

    name: str = Field(..., min_length=2, max_length=300)
    contact: str = Field(..., min_length=3, max_length=300)
    #: Выбранные сообщества: список словарей с ключом ``code``. Названия и
    #: подписчики из тела запроса НЕ используются — они пересобираются из БД.
    regions: List[Dict[str, Any]] = Field(default_factory=list)
    comment: Optional[str] = Field(default=None, max_length=2000)
    #: Honeypot. Скрытое поле: человек его не видит и не заполняет, бот —
    #: заполняет. Непустое значение → заявка не сохраняется.
    website: Optional[str] = Field(default=None, max_length=200)
    #: Токен одной отправки (генерируется страницей). Двойной клик и F5 после
    #: отправки несут тот же токен — по нему заявка не дублируется.
    request_token: Optional[str] = Field(default=None, max_length=64)
    #: С какой страницы пришли (для разбора трафика).
    page: Optional[str] = Field(default=None, max_length=500)


def _client_ip(request: Request) -> str:
    """IP посетителя.

    Тот же порядок, что у ``middleware/rate_limiter.py``: X-Forwarded-For →
    X-Real-IP → socket. Через edge-прокси (Джино) реальный IP клиента приходит
    именно в X-Forwarded-For, поэтому nginx обязан передавать его дальше как
    есть, без ``$proxy_add_x_forwarded_for`` (иначе первым окажется IP edge'а
    и все посетители склеятся в одного).
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    if request.client:
        return request.client.host
    return "unknown"


def _quota(key: str, per_min: int, per_day: int):
    """Квота приёма заявок (fail-open при недоступном Redis)."""
    from modules.gateway.quota import GatewayQuota
    from modules.vk_monitor.rate_limiter import _build_redis_client

    return GatewayQuota(_build_redis_client(), per_min, per_day)


def _spam_check(client_ip: str) -> bool:
    """True — заявку можно принять; False — клиент превысил лимит.

    Два предела: на IP (в минуту и в сутки) и на все заявки (в сутки). Второй
    — страховка на случай, когда IP посетителей не различимы: тогда первый
    предел срабатывал бы на всех сразу.
    """
    try:
        allowed, retry_after = _quota(
            f"landing:ip:{client_ip}", MAX_PER_IP_PER_MIN, MAX_PER_IP_PER_DAY
        ).check_and_consume(f"landing:ip:{client_ip}")
        if not allowed:
            logger.info("landing: IP %s исчерпал лимит заявок", client_ip)
            return False

        allowed, _retry = _quota(
            "landing:all", MAX_PER_DAY_ALL * 10, MAX_PER_DAY_ALL
        ).check_and_consume("landing:all")
        if not allowed:
            logger.warning("landing: суточный потолок заявок исчерпан (%s)", MAX_PER_DAY_ALL)
            return False
    except Exception:  # noqa: BLE001 — не блокируем заявку из-за счётчика
        logger.warning("landing: проверка спама не удалась, заявку принимаем", exc_info=True)
    return True


async def _load_regions(db: AsyncSession, codes: List[str]) -> List[Dict[str, Any]]:
    """Собрать список сообществ по кодам — из БД, а не из тела запроса.

    Возвращает ``[{code, name, members, url}]``. Регион, которого нет в БД или
    у которого нет группы, пропускается: клиент мог прислать что угодно.
    """
    clean_codes: List[str] = []
    for code in codes:
        code = str(code or "").strip()
        if code and code not in clean_codes:
            clean_codes.append(code)
    if not clean_codes:
        return []

    result = await db.execute(select(Region).where(Region.code.in_(clean_codes)))
    regions = {r.code: r for r in result.scalars().all()}

    # Подписчики — последний дневной снимок по каждому региону.
    members: Dict[int, int] = {}
    if regions:
        subq = select(
            RegionMemberSnapshot.region_id,
            RegionMemberSnapshot.members_count,
            func.row_number()
            .over(
                partition_by=RegionMemberSnapshot.region_id,
                order_by=RegionMemberSnapshot.snapshot_date.desc(),
            )
            .label("rn"),
        ).where(RegionMemberSnapshot.region_id.in_([r.id for r in regions.values()]))
        rows = (await db.execute(subq)).all()
        for region_id, count, _rn in rows:
            members[int(region_id)] = int(count or 0)

    from modules.region_links import community_url

    items: List[Dict[str, Any]] = []
    for code in clean_codes:
        region = regions.get(code)
        if region is None:
            continue
        screen_name = (region.config or {}).get("screen_name") if region.config else None
        items.append(
            {
                "code": region.code,
                "name": getattr(region, "name", code),
                "members": members.get(region.id),
                "url": community_url(region.vk_group_id, screen_name),
            }
        )
    return items


def _order_ref(order_id: int) -> str:
    """Короткий номер заявки для показа клиенту (``З-1234``)."""
    return f"З-{order_id}"


def _notify_text(order: LandingOrder, regions: List[Dict[str, Any]]) -> str:
    """Текст уведомления владельцу (Telegram/ВК/MAX)."""
    lines = [
        "🆕 Заявка с лендинга САРАФАНА",
        f"Номер: {_order_ref(order.id)}",
        f"Имя: {order.name}",
        f"Контакт: {order.contact}",
        f"Сообществ: {order.regions_count}"
        + (f" · охват {order.reach:,}".replace(",", " ") if order.reach else ""),
    ]
    if order.price_estimate:
        lines.append(f"Ориентир по расценкам: {order.price_estimate:,} ₽".replace(",", " "))
    if order.source_host:
        lines.append(f"Откуда: {order.source_host}")
    if regions:
        names = ", ".join(r["name"] for r in regions[:12])
        if len(regions) > 12:
            names += f" и ещё {len(regions) - 12}"
        lines.append(f"Выбраны: {names}")
    if order.comment:
        lines.append(f"Комментарий: {order.comment}")
    return "\n".join(lines)


async def _notify_owner(order: LandingOrder, regions: List[Dict[str, Any]]) -> bool:
    """Уведомить владельца: Telegram + личка ВК + MAX. True — канал принял.

    Обе отправки синхронные внутри (сокеты) — в потоке приложения их запускать
    нельзя, поэтому уходят в отдельный поток.
    """
    import asyncio

    text = _notify_text(order, regions)
    sent = False

    try:
        from modules.ad_cabinet.vk_bot import notify as vk_notify

        res = await vk_notify.notify_owner(text, dedup_key=f"landing-order-{order.id}")
        sent = sent or any(bool(v) for v in (res or {}).values())
    except Exception:  # noqa: BLE001 — уведомление не роняет приём заявки
        logger.warning("landing: уведомление в Telegram/ВК не удалось", exc_info=True)

    try:
        from modules.max_notifier import send_card

        res = await asyncio.to_thread(send_card, {"text": text})
        sent = sent or bool(res.get("ok"))
    except Exception:  # noqa: BLE001 — уведомление не роняет приём заявки
        logger.warning("landing: уведомление в MAX не удалось", exc_info=True)

    return sent


@router.post("/orders")
async def create_landing_order(
    payload: LandingOrderIn,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    """Принять заявку с лендинга. Публичный эндпоинт.

    Возвращает номер заявки, который клиент видит на экране («Заявка принята,
    номер З-123»). Владелец видит заявку в разделе «Заявки с сайта» на /ad и
    получает уведомление в Telegram и MAX.
    """
    # Honeypot: поле скрыто от человека, бот его заполнит. Отвечаем «принято»,
    # чтобы бот не подбирал другой способ, но ничего не пишем и не шлём.
    if (payload.website or "").strip():
        logger.info("landing: сработал honeypot, заявка не сохранена")
        return {"ok": True, "order_id": 0, "ref": "З-0", "saved": False}

    client_ip = _client_ip(request)
    if not _spam_check(client_ip):
        raise HTTPException(status_code=429, detail="Слишком много заявок, попробуйте позже")

    if len(payload.regions) > MAX_REGIONS:
        raise HTTPException(status_code=400, detail="Слишком много сообществ в заявке")

    # Дедуп: тот же токен отправки — та же заявка (двойной клик, F5).
    token = (payload.request_token or "").strip() or None
    if token:
        existing = (
            (await db.execute(select(LandingOrder).where(LandingOrder.request_token == token)))
            .scalars()
            .first()
        )
        if existing is not None:
            return {
                "ok": True,
                "order_id": existing.id,
                "ref": _order_ref(existing.id),
                "regions_count": existing.regions_count,
                "price_estimate": existing.price_estimate,
                "saved": True,
                "duplicate": True,
            }

    regions = await _load_regions(db, [str(r.get("code", "")) for r in payload.regions])

    from config.ad_landing import quote_price

    price_estimate = None
    if regions:
        quote = quote_price(len(regions))
        price_estimate = quote.get("price")
    reach = sum(int(r["members"]) for r in regions if r.get("members") is not None) or None

    order = LandingOrder(
        name=payload.name.strip(),
        contact=payload.contact.strip(),
        regions_json=regions,
        regions_count=len(regions),
        reach=reach,
        price_estimate=price_estimate,
        comment=(payload.comment or "").strip() or None,
        request_token=token,
        source_host=request.headers.get("host"),
        source_page=(payload.page or request.headers.get("referer")) or None,
        status="new",
    )
    db.add(order)
    try:
        await db.commit()
    except IntegrityError:
        # Гонка двух отправок с одним токеном — отдаём уже созданную заявку.
        await db.rollback()
        if token:
            existing = (
                (await db.execute(select(LandingOrder).where(LandingOrder.request_token == token)))
                .scalars()
                .first()
            )
            if existing is not None:
                return {
                    "ok": True,
                    "order_id": existing.id,
                    "ref": _order_ref(existing.id),
                    "regions_count": existing.regions_count,
                    "price_estimate": existing.price_estimate,
                    "saved": True,
                    "duplicate": True,
                }
        raise HTTPException(status_code=409, detail="Заявка не сохранена, попробуйте ещё раз")
    await db.refresh(order)

    logger.info(
        "landing: заявка %s принята (ip=%s, сообществ=%s, охват=%s)",
        _order_ref(order.id),
        client_ip,
        order.regions_count,
        order.reach,
    )

    # Уведомление — после коммита: заявка уже в базе, даже если канал молчит.
    if await _notify_owner(order, regions):
        order.notified_at = datetime.utcnow()
        db.add(order)
        await db.commit()

    return {
        "ok": True,
        "order_id": order.id,
        "ref": _order_ref(order.id),
        "regions_count": order.regions_count,
        "price_estimate": order.price_estimate,
        "saved": True,
    }
