"""API кабинета рекламодателя (`/api/advertiser`) — клиентская половина ad-CRM.

Клиент входит через ЕСА (роль ``advertiser``, зона гейта ``ADVERTISER_PREFIXES``)
и видит ТОЛЬКО СВОЁ: каждый хендлер начинается с ``_current_client()`` — карточка
резолвится из сессии (``modules/ad_cabinet/advertiser_link``), ``client_id`` из
тела запроса не читается нигде. Цену считает только сервер (``quote_price``).

Публикация — существующим циклом операторского планировщика
(``modules/ad_cabinet/client_orders``): N строк ``AdScheduledPost`` → VK-отложка;
фиксацию выхода и ``AdPayment(awaiting)`` делает ``publish_reconciler``. Клиент
своих платежей не создаёт; «оплачено» подтверждает владелец в CRM.

Фото клиента живут в ``web/uploads/advertiser/<client_id>/`` — СОЗНАТЕЛЬНО вне
``/static`` (тот примонтирован публично): отдача только владельцу карточки.
"""

from __future__ import annotations

import logging
import shutil  # noqa: F401 — тесты патчат ac.shutil.disk_usage (модуль общий)
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.ad_landing import PAYMENTS, RECEIPT_NOTE, build_price_table
from database.connection import get_db_session
from database.models import AdClient, AdPayment, AdPublication, AdScheduledPost, Region
from modules.ad_cabinet import advertiser_link, chat, client_orders, client_photos, impersonation
from modules.ad_cabinet.balance import compute_balance
from modules.ad_cabinet.interaction_log import log_interaction

logger = logging.getLogger(__name__)
router = APIRouter()

from web.api.ad_cabinet import _msk_to_unix  # noqa: E402

# Лимиты и хранение фото — одна точка правды с ВК-ботом (modules/ad_cabinet/client_photos).
_ALLOWED_IMG_EXT = client_photos.ALLOWED_IMG_EXT
_MAX_IMG_BYTES = client_photos.MAX_IMG_BYTES
MAX_PHOTOS_PER_POST = client_photos.MAX_PHOTOS_PER_POST
MAX_PHOTOS_PER_CLIENT = client_photos.MAX_PHOTOS_PER_CLIENT


def _current_user(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def _current_client(request: Request, db: AsyncSession):
    """Карточка клиента текущей сессии. 403 — юзер ещё не рекламодатель.

    **Единственная дверь для входа владельца в чужой кабинет.** Если в запросе
    есть ``?as_client=<id>`` и запрашивающий — владелец, отдаём ЕГО карточку и
    пишем запись в журнал (``modules/ad_cabinet/impersonation``). Инвариант
    модуля при этом не ослаблен: ``client_id`` по-прежнему не читается ни в
    одном из 16 хендлеров — читает его только impersonation, и только он же
    проверяет владельца. Не-владельцу параметр отвечает 403, а не игнорируется:
    молчаливое игнорирование выглядит как «работает» и прячет дыру изоляции.
    """
    user = _current_user(request)

    target, impersonated = await impersonation.resolve(db, user, request)
    if impersonated:
        await db.commit()  # журнал входа не должен зависеть от исхода хендлера
        return user, target

    client = await advertiser_link.resolve_client(db, user)
    if client is None:
        raise HTTPException(status_code=403, detail="Не рекламодатель — пройдите онбординг")
    return user, client


# ----------------------------------------------------------------------
# Онбординг и профиль
# ----------------------------------------------------------------------


class OnboardingIn(BaseModel):
    name: Optional[str] = Field(None, max_length=300)
    phone: Optional[str] = Field(None, max_length=50)


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(get_db_session)):
    """Статус текущего юзера в кабинете. Доступен ЛЮБОМУ аутентифицированному
    (гейт: ``ADVERTISER_ONBOARDING_EXACT``) — не-рекламодателю отвечает
    ``is_advertiser=False``, страница показывает онбординг."""
    user = _current_user(request)
    owner = impersonation.is_owner(user)

    target, impersonated = await impersonation.resolve(db, user, request)
    if impersonated:
        await db.commit()  # запись входа в журнал не зависит от исхода страницы
        return {
            "is_advertiser": True,
            "role": getattr(user, "role", None),
            "display_name": getattr(user, "display_name", None) or getattr(user, "login", None),
            "client": target.to_dict(),
            "is_owner": True,
            "impersonating": {"client_id": target.id, "name": target.name},
        }

    client = await advertiser_link.resolve_client(db, user)
    if client is not None:
        await db.commit()  # fallback-линковка могла записать FK (self-healing)
    return {
        # Владелец «рекламодателем» не становится: своей карточки у него нет и
        # заводить её не надо — он входит в чужие через ?as_client. Страница
        # покажет ему не онбординг, а выбор кабинета (см. is_owner ниже).
        "is_advertiser": client is not None,
        "role": getattr(user, "role", None),
        "display_name": getattr(user, "display_name", None) or getattr(user, "login", None),
        "client": client.to_dict() if client is not None else None,
        "is_owner": owner,
        "impersonating": None,
    }


@router.get("/clients")
async def owner_client_list(request: Request, db: AsyncSession = Depends(get_db_session)):
    """Список кабинетов для переключателя владельца. **Только владельцу.**

    Отдаёт минимум, нужный для выбора (id, имя, есть ли привязанный аккаунт), а
    не карточку целиком: операторский взгляд на данные клиента живёт в
    ``/api/ad-crm/clients/{id}``, дублировать его здесь незачем.
    """
    user = _current_user(request)
    if not impersonation.is_owner(user):
        raise HTTPException(status_code=403, detail="Недостаточно прав")

    rows = (
        (
            await db.execute(
                select(AdClient).order_by(AdClient.radar_user_id.is_(None), AdClient.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "clients": [
            {
                "id": c.id,
                "name": c.name,
                "stage": c.stage,
                "has_account": c.radar_user_id is not None,
            }
            for c in rows
        ]
    }


@router.post("/onboarding")
async def onboarding(
    payload: OnboardingIn, request: Request, db: AsyncSession = Depends(get_db_session)
):
    """Стать рекламодателем: линковка/создание карточки + роль ``advertiser``.

    Доступен любому аутентифицированному (гейт), идемпотентен: повторный вызов
    возвращает уже привязанную карточку.
    """
    user = _current_user(request)
    was_new = await advertiser_link.resolve_client(db, user) is None
    client = await advertiser_link.onboard_client(db, user, name=payload.name, phone=payload.phone)
    if getattr(user, "role", None) not in ("advertiser", "operator"):
        # Роль читается из БД на каждом запросе — вступает в силу сразу,
        # без перевыпуска сессионной куки.
        user.role = "advertiser"
        db.add(user)
    if was_new:
        await db.flush()  # id новой карточки нужен журналу до commit
        log_interaction(
            db,
            kind="cabinet_signup",
            client_id=client.id,
            summary=f"Новый клиент в кабинете: {client.name or getattr(user, 'login', '?')}",
            actor="client",
        )
    await db.commit()
    await db.refresh(client)
    if was_new:
        # Дедуп ПО КЛИЕНТУ, а не глобальный бюджет 5/час (аудит 2026-09-05: после
        # рассылки шестой настоящий клиент за час владельцу не показывался).
        # Скриптовые регистрации всё равно режет ЕСА (инвайт / VK ID). Оба
        # канала — как у регистрации из ВК-бота.
        from modules.ad_cabinet.vk_bot import notify as vk_notify

        await vk_notify.notify_owner(
            f"🆕 Кабинет №{client.id}: новый клиент «{_telemetry_identity(user, client)}» "
            "прошёл онбординг — карточка в /ad → Кабинеты",
            dedup_key=f"signup:{client.id}",
        )
    return {"is_advertiser": True, "client": client.to_dict()}


class TelemetryIn(BaseModel):
    """Событие из браузера клиента: визит либо JS-ошибка.

    Пишется в тот же таймлайн ``ad_interactions`` — владелец видит, что клиенты
    ПРИХОДЯТ и что у них ЛОМАЕТСЯ, не дожидаясь жалобы (заказ владельца
    2026-08-26: «клиенты заходили, ничего не работало, а я не знал»).
    """

    kind: str = Field(..., pattern=r"^(visit|js_error)$")
    message: str = Field("", max_length=500)
    source: str = Field("", max_length=300)


def _telemetry_identity(user, client) -> str:
    """Имя для сводной ленты: ВК-аккаунт без login не должен светиться «None»."""
    return (
        (client.name if client is not None else None)
        or getattr(user, "display_name", None)
        or getattr(user, "login", None)
        or f"user#{getattr(user, 'id', '?')}"
    )


# Потолок записей js_error на аккаунт в час — контент-независимый (ротация
# текста не выбивает новые вёдра, блокер ревью 2026-08-26).
JS_ERROR_BUDGET_PER_HOUR = 10


@router.post("/telemetry")
async def telemetry(
    payload: TelemetryIn, request: Request, db: AsyncSession = Depends(get_db_session)
):
    """Принять событие браузера клиента (доступен ЛЮБОМУ аутентифицированному —
    ошибка на странице онбординга ценнее прочих: до неё клиент ещё «никто»).

    Анти-шум: ``visit`` пишется не чаще раза в час на аккаунт; ``js_error`` —
    бюджет 10/час на аккаунт + дедуп одинакового текста на 10 минут; пинг
    владельцу — не чаще раза в час на аккаунт. Троттлы зовутся в thread'е
    (синхронный Redis не блокирует event loop). Ответ фиксированный — состояние
    серверных окон клиенту не раскрывается.
    """
    import asyncio

    user = _current_user(request)
    client = await advertiser_link.resolve_client(db, user)
    client_id = client.id if client is not None else None
    who = _telemetry_identity(user, client)

    from modules.ad_cabinet import owner_ping

    logged = False
    ping_text = None
    if payload.kind == "visit":
        if await asyncio.to_thread(owner_ping.ping_dedup_pass, f"visit:{user.id}", ttl=3600):
            log_interaction(
                db,
                kind="cabinet_visit",
                client_id=client_id,
                summary=f"Клиент открыл кабинет: {who}",
                actor="client",
            )
            logged = True
    else:  # js_error
        detail = " ".join(x for x in (payload.message.strip(), payload.source.strip()) if x)
        budget_ok = await asyncio.to_thread(
            owner_ping.event_budget_pass,
            f"js_error:{user.id}",
            limit=JS_ERROR_BUDGET_PER_HOUR,
            ttl=3600,
        )
        fresh = budget_ok and await asyncio.to_thread(
            owner_ping.ping_dedup_pass,
            f"js_error_log:{user.id}:{owner_ping.stable_digest(detail[:200])}",
            ttl=600,
        )
        if fresh:
            log_interaction(
                db,
                kind="cabinet_js_error",
                client_id=client_id,
                summary=f"Ошибка в браузере клиента {who}: {detail[:400]}",
                meta={"message": payload.message, "source": payload.source},
                actor="client",
            )
            logged = True
        ping_text = f"⚠️ Кабинет: у клиента «{who}» ошибка в браузере — {detail[:200]}"
    if logged:
        await db.commit()
    if ping_text:
        # После commit: висящий Telegram не держит открытую транзакцию.
        await asyncio.to_thread(owner_ping.notify_owner, ping_text, dedup_key=f"js_error:{user.id}")
    return {"ok": True}


@router.get("/summary")
async def summary(request: Request, db: AsyncSession = Depends(get_db_session)):
    """Баланс клиента — тем же ``compute_balance``, что у оператора (единый
    источник «сколько должен»), плюс производная «запланировано на сумму»."""
    _user, client = await _current_client(request, db)
    payments = (
        (await db.execute(select(AdPayment).where(AdPayment.client_id == client.id)))
        .scalars()
        .all()
    )
    publications = (
        (await db.execute(select(AdPublication).where(AdPublication.client_id == client.id)))
        .scalars()
        .all()
    )
    scheduled = (
        (
            await db.execute(
                select(AdScheduledPost).where(
                    AdScheduledPost.client_id == client.id,
                    AdScheduledPost.status.in_(client_orders.ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    planned = sum(float(p.price) for p in scheduled if p.price is not None)
    unread = await chat.unread_count(db, client.id, reader=chat.SENDER_CLIENT)
    from modules.ad_cabinet import packages as pkgs

    pkg_state = await pkgs.get_state(db, client.id)
    return {
        "client": client.to_dict(),
        "balance": compute_balance(payments, publications),
        "planned_total": round(planned, 2),
        "planned_posts": len(scheduled),
        "chat_unread": unread,
        "package": pkg_state["package"].to_dict() if pkg_state["package"] else None,
        "package_block": pkg_state["block_reason"],
        "packages": [p.to_dict() for p in pkg_state["packages"]],
    }


# ----------------------------------------------------------------------
# Прайс и районы
# ----------------------------------------------------------------------


@router.get("/price-table")
async def price_table(request: Request, db: AsyncSession = Depends(get_db_session)):
    """Прайс + районы, доступные для размещения (активные с группой)."""
    await _current_client(request, db)
    rows = (
        await db.execute(
            select(Region.id, Region.name, Region.center_city)
            .where(Region.is_active.is_(True), Region.vk_group_id.isnot(None))
            .order_by(Region.name.asc())
        )
    ).all()
    return {
        "regions": [{"id": rid, "name": name, "center": center} for rid, name, center in rows],
        "price_table": build_price_table(),
        "payments": PAYMENTS,
    }


class QuoteIn(BaseModel):
    region_ids: List[int] = Field(default_factory=list)
    pinned: bool = False  # закреп на сутки (+PIN_PRICE_RUB за сообщество)


@router.post("/quote")
async def quote(payload: QuoteIn, request: Request, db: AsyncSession = Depends(get_db_session)):
    """Предварительная цена выбора — тем же ``quote_price``, что и при заказе.

    Пакет учитывается ДО сабмита (should-fix ревью 2026-08-26): клиент с
    промо/пакетом видит «в счёт пакета, 0 ₽», с долгом — блокировку, при выборе
    сверх остатка — предупреждение, а не сюрприз-400 после заполнения формы.
    """
    _user, client = await _current_client(request, db)
    targets = await client_orders.resolve_targets(db, payload.region_ids)
    from modules.ad_cabinet import packages as pkgs
    from modules.ad_cabinet.pricing import quote_for_client

    base = await quote_for_client(db, client.id, len(targets), pinned=payload.pinned)

    state = await pkgs.get_state(db, client.id)
    if state["block_reason"]:
        return {**base, "blocked": state["block_reason"]}
    pkg = state["package"]
    if pkg is not None:
        left = pkgs.remaining(pkg)  # безлимит — без квоты (Этап 2)
        return {
            "n": len(targets),
            "price": base["pin_price"],  # пакет закреп не покрывает
            "base_price": base["base_price"],
            "pinned": payload.pinned,
            "pin_price": base["pin_price"],
            "package": pkg.to_dict(),
            "over_limit": len(targets) > left,
        }
    return base


# ----------------------------------------------------------------------
# Фото клиента (вне /static — отдача только владельцу карточки)
# ----------------------------------------------------------------------


# Тела живут в client_photos (общие с ВК-ботом); имена здесь сохранены — их
# импортируют ad_crm, photo_retention, intake и патчат тесты.
_upload_root = client_photos.upload_root
_client_photo_dir = client_photos.client_photo_dir
_fits_disk = client_photos.fits_disk
_client_photo_paths = client_photos.client_photo_paths


@router.get("/photos")
async def list_photos(request: Request, db: AsyncSession = Depends(get_db_session)):
    _user, client = await _current_client(request, db)
    return {
        "photos": [
            {"name": p.name, "size": p.stat().st_size} for p in _client_photo_paths(client.id)
        ]
    }


@router.post("/photos")
async def upload_photo(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db_session),
):
    _user, client = await _current_client(request, db)
    suffix = Path(file.filename or "").suffix.lower()
    data = await file.read()
    try:
        name = client_photos.store_client_photo(client.id, data, suffix)
    except client_photos.PhotoError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"name": name, "size": len(data)}


@router.get("/photos/{name}")
async def get_photo(name: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    _user, client = await _current_client(request, db)
    base = Path(str(name or "")).name  # отсечь path-traversal
    p = _client_photo_dir(client.id) / base
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return FileResponse(p)


@router.delete("/photos/{name}")
async def delete_photo(name: str, request: Request, db: AsyncSession = Depends(get_db_session)):
    _user, client = await _current_client(request, db)
    base = Path(str(name or "")).name
    # Файл ещё нужен активному посту (аудит 2026-09-05): иначе при одобрении
    # пост ушёл бы без картинок, а теперь — упал бы в failed.
    used_by = await photo_in_use(db, client.id, base)
    if used_by is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Фото используется в посте №{used_by}, который ещё не вышел — "
                "сначала отмените пост"
            ),
        )
    p = _client_photo_dir(client.id) / base
    if p.is_file():
        p.unlink()
    return {"success": True}


# ----------------------------------------------------------------------
# Заказы и посты
# ----------------------------------------------------------------------


class OrderIn(BaseModel):
    text: str = Field("", max_length=client_orders.TEXT_MAX)
    photos: List[str] = Field(default_factory=list)
    region_ids: List[int] = Field(default_factory=list)
    whole_network: bool = False
    publish_now: bool = False
    publish_at: Optional[str] = None  # "YYYY-MM-DDTHH:MM", МСК wall-clock
    pinned: bool = False  # закреп на сутки после выхода (+200 ₽ за сообщество)


def _real_publisher_factory(db):
    async def factory(gid: int):
        from modules.publisher.vk_publisher_extended import VKPublisher

        return await VKPublisher.create_with_policy(db, target_group_id=gid)

    return factory


class AttachmentError(RuntimeError):
    """Фото поста не собрать: пост НЕ должен уйти текстом молча (аудит 2026-09-05).

    Раньше пропавший файл или сбой заливки давали ``[]``, и оплаченный пост с
    картинками выходил голым текстом без следа в журнале. Теперь ``_send_one``
    ловит исключение → строка ``failed`` с причиной, слот возвращается в пакет,
    владелец видит ошибку в очереди/списке.
    """


def _real_attachment_builder(client_id: int, photo_token):
    """Заливка клиентских фото на стену группы (owner-specific, на каждую свою).

    ``photo_token`` — токен аккаунта-владельца группы: заливать фото, которое
    потом примет community-токен, может только он (см.
    ``docs/ops/photo-upload-path.md``).
    """

    def build(gid: int, image_names) -> List[str]:
        if not image_names:
            return []
        if not photo_token:
            raise AttachmentError("нет токена владельца группы для заливки фото на стену")
        wanted = {Path(n).name for n in image_names}
        paths = [p for p in _client_photo_paths(client_id) if p.name in wanted]
        missing = wanted - {p.name for p in paths}
        if missing:
            raise AttachmentError(f"фото не найдены на диске: {', '.join(sorted(missing))}")
        try:
            import vk_api

            from modules.publisher.vk_wall_photo_upload import upload_wall_images

            api = vk_api.VkApi(token=photo_token).get_api()
            images = [p.read_bytes() for p in paths[:MAX_PHOTOS_PER_POST]]
            out = upload_wall_images(api, images, group_id=gid)
        except AttachmentError:
            raise
        except Exception as e:  # noqa: BLE001 — но не молча
            raise AttachmentError(f"заливка фото в VK не удалась: {e}") from e
        if not out:
            raise AttachmentError("VK не вернул ни одного вложения для фото")
        return out

    return build


async def photo_in_use(session, client_id: int, name: str) -> Optional[int]:
    """id активного (pending/scheduled) поста клиента, который ссылается на файл."""
    from modules.ad_cabinet.client_orders import ACTIVE_STATUSES

    rows = (
        (
            await session.execute(
                select(AdScheduledPost.id, AdScheduledPost.image_names).where(
                    AdScheduledPost.client_id == int(client_id),
                    AdScheduledPost.status.in_(ACTIVE_STATUSES),
                )
            )
        )
        .tuples()
        .all()
    )
    base = Path(str(name or "")).name
    for pid, names in rows:
        if base in {Path(str(n)).name for n in (names or [])}:
            return int(pid)
    return None


def _parse_publish_at(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {raw}")


@router.post("/orders")
async def create_order(
    payload: OrderIn, request: Request, db: AsyncSession = Depends(get_db_session)
):
    """Создать заказ: районы галочками или «вся сеть», сразу или по расписанию.

    Не-trusted клиент получает ``pending`` (в VK не уходит ничего до одобрения
    владельцем) — UI честно показывает «после одобрения».
    """
    user, client = await _current_client(request, db)
    region_ids = payload.region_ids
    if payload.whole_network:
        region_ids = await client_orders.all_target_region_ids(db)

    from modules.vk_token_router import load_photo_upload_token

    # Фото клиента заливает аккаунт-владелец группы: community-токен залить не
    # может ([27]), а фото администратора community-токен в записи не принимает —
    # молча (docs/ops/photo-upload-path.md).
    photo_token = await load_photo_upload_token(db)

    try:
        result = await client_orders.submit_order(
            db,
            client=client,
            user_id=user.id,
            text=payload.text,
            image_paths=[Path(n).name for n in payload.photos][:MAX_PHOTOS_PER_POST],
            region_ids=region_ids,
            publish_at=_parse_publish_at(payload.publish_at),
            publish_now=payload.publish_now,
            publisher_factory=_real_publisher_factory(db),
            attachment_builder=_real_attachment_builder(client.id, photo_token),
            msk_to_unix=_msk_to_unix,
            pinned=payload.pinned,
        )
    except client_orders.OrderError as e:
        # Отказ формы — тоже событие для владельца: клиент ПЫТАЛСЯ заказать и
        # упёрся. Пишется не чаще раза в 10 минут на клиента (should-fix ревью:
        # зацикленный невалидный запрос не заливает ленту строкой на каждый 400).
        import asyncio

        from modules.ad_cabinet import owner_ping

        if await asyncio.to_thread(
            owner_ping.ping_dedup_pass, f"order_refused:{client.id}", ttl=600
        ):
            log_interaction(
                db,
                kind="cabinet_order_refused",
                client_id=client.id,
                summary=f"Заказ не прошёл валидацию: {e}",
                actor="client",
            )
            await db.commit()
        raise HTTPException(status_code=400, detail=str(e))

    log_interaction(
        db,
        kind="client_order",
        client_id=client.id,
        summary=f"Заказ из кабинета: {len(result['posts'])} районов, "
        f"{result['price_total']:.0f} ₽" + (" (на модерации)" if result["moderation"] else ""),
        actor="client",
    )
    await db.commit()

    # Пинг владельцу — ВСЕГДА (аудит 2026-09-05): заказ trusted-клиента уходил в
    # VK молча, а «заявка видна владельцу» ломалась ровно на самых активных.
    from modules.ad_cabinet.vk_bot import notify as vk_notify

    await vk_notify.notify_owner(_pending_text(client, result))

    return {
        "order_ref": result["order_ref"],
        "price_total": result["price_total"],
        "quote": result["quote"],
        "moderation": result["moderation"],
        "posts": [p.to_dict() for p in result["posts"]],
    }


def _pending_text(client, result) -> str:
    """Текст пинга владельцу о заказе (без дедупа — каждый заказ важен). Уходит в
    Telegram и ВК (``vk_bot.notify.notify_owner``). Различает «ждёт одобрения»
    (с причиной, если это долговой гейт) и «уже в VK-отложке» (trusted)."""
    head = (
        f"🛎 Кабинет №{client.id}: клиент «{client.name or client.id}» создал заказ на "
        f"{len(result['posts'])} районов ({result['price_total']:.0f} ₽)"
    )
    if result.get("moderation"):
        reason = result.get("debt_hold")
        return head + " — ждёт одобрения в /ad" + (f" (долг: {reason})" if reason else "")
    return head + " — trusted, уже в VK-отложке; отменить можно в /ad → Кабинеты"


@router.get("/posts")
async def my_posts(request: Request, db: AsyncSession = Depends(get_db_session)):
    """Мои посты: отложки (по заказам) + вышедшие публикации с метриками."""
    _user, client = await _current_client(request, db)
    scheduled = (
        (
            await db.execute(
                select(AdScheduledPost)
                .where(AdScheduledPost.client_id == client.id)
                .order_by(AdScheduledPost.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    publications = (
        (
            await db.execute(
                select(AdPublication)
                .where(AdPublication.client_id == client.id)
                .order_by(AdPublication.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return {
        "scheduled": [r.to_dict() for r in scheduled],
        "publications": [r.to_dict() for r in publications],
    }


@router.post("/posts/{post_id}/cancel")
async def cancel_post(post_id: int, request: Request, db: AsyncSession = Depends(get_db_session)):
    """Отменить СВОЙ неопубликованный пост.

    ``pending``/``draft`` — просто отмена; ``scheduled`` — плюс снятие из
    VK-отложки (паттерн операторского cancel: VK-удаление не удалось → статус
    не меняем, клиент видит реальное состояние).
    """
    _user, client = await _current_client(request, db)
    # Движок общий с ВК-ботом (client_orders.cancel_own_post): блокировка
    # строки, терминальные статусы и снятие из VK-отложки живут там в одном
    # месте — второй копии этих трёх условий в проекте быть не должно.
    res = await client_orders.cancel_own_post(db, client, post_id, source="cabinet")
    if res["reason"] == "not_found":
        raise HTTPException(status_code=404, detail="Пост не найден")
    row = res["row"]
    if res["cancel_error"]:
        return {**row.to_dict(), "cancel_error": res["cancel_error"]}
    await db.commit()
    await db.refresh(row)
    return row.to_dict()


# ----------------------------------------------------------------------
# Оплаты
# ----------------------------------------------------------------------


@router.get("/payments")
async def my_payments(request: Request, db: AsyncSession = Depends(get_db_session)):
    """Мои оплаты (awaiting/paid) + реквизиты для перевода (MVP: вручную)."""
    _user, client = await _current_client(request, db)
    rows = (
        (
            await db.execute(
                select(AdPayment)
                .where(AdPayment.client_id == client.id)
                .order_by(AdPayment.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return {
        "payments": [r.to_dict() for r in rows],
        "requisites": PAYMENTS,
        "receipt_note": RECEIPT_NOTE,
    }


class ClaimIn(BaseModel):
    """«Я оплатил»: ``payment_ids`` — какие счета; пусто/None — все ожидающие."""

    payment_ids: Optional[List[int]] = None


@router.post("/payments/claim")
async def claim_payments(
    payload: ClaimIn, request: Request, db: AsyncSession = Depends(get_db_session)
):
    """Клиент сообщает, что перевёл деньги (PR 1.7 аудита 2026-09-05).

    Ставит ``claimed_at`` на ожидающие счета и пингует владельца; повторное
    нажатие ничего не плодит (уже заявленные строки не трогаются).
    """
    from modules.ad_cabinet import payment_claims
    from modules.ad_cabinet.vk_bot import notify as vk_notify

    _user, client = await _current_client(request, db)
    res = await payment_claims.claim_payments(db, client, payment_ids=payload.payment_ids)
    await db.commit()
    if res["claimed"]:
        await vk_notify.notify_owner(
            f"💳 Кабинет №{client.id}: «{client.name or client.id}» сообщил об оплате "
            f"{res['amount']:g} ₽ ({res['claimed']} сч.) — подтвердить в /ad → Кабинеты",
            dedup_key=f"claim:{client.id}",
            dedup_ttl=600,
        )
    return res


# ----------------------------------------------------------------------
# Чат с владельцем
# ----------------------------------------------------------------------


class ChatIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=chat.BODY_MAX)


@router.get("/chat")
async def my_chat(
    request: Request,
    after_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db_session),
):
    _user, client = await _current_client(request, db)
    rows = await chat.fetch_thread(db, client.id, reader=chat.SENDER_CLIENT, after_id=after_id)
    await db.commit()  # отметки read_at
    return {"messages": [r.to_dict() for r in rows]}


@router.post("/chat")
async def send_chat(payload: ChatIn, request: Request, db: AsyncSession = Depends(get_db_session)):
    _user, client = await _current_client(request, db)
    try:
        row = await chat.post_message(db, client.id, chat.SENDER_CLIENT, payload.body)
    except chat.ChatError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    await db.refresh(row)
    from modules.ad_cabinet.vk_bot import notify as vk_notify

    # Пинг о новом сообщении — не чаще раза в 10 минут на клиента (аудит
    # 2026-09-05: час дедупа съедал «алло, я оплатил» после «когда выйдет?»), с
    # началом текста, чтобы владелец понял, что пришло, не открывая /ad. Уходит
    # в оба канала владельца — Telegram и личка ВК (бот САРАФАНа).
    preview = (row.body or "").strip().replace("\n", " ")[:80]
    await vk_notify.notify_owner(
        f"💬 Кабинет №{client.id}: «{client.name or client.id}»: {preview} — "
        "ответить в /ad → Кабинеты",
        dedup_key=f"chat:{client.id}",
        dedup_ttl=600,
    )
    return row.to_dict()
