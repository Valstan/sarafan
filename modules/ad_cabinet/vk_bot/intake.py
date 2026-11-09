"""Входящее бота: Bots Long Poll сообщества САРАФАН → диалог → ответы (этап 2).

Постоянный Long Poll ведёт демон ``scripts/vk_bot_daemon.py`` (systemd
``setka-vk-bot``); ``poll_once`` — ручной диагностический тик с тем же
``handle_one``. У каждого собеседника есть состояние диалога (Redis, ключ на
``peer_id``, сутки), обработчик — :mod:`.dialog`.

Всё сетевое инъектируется (``api_call``/``lp_get``/``sender``/``photo_fetch``),
поэтому тик целиком гоняется в тестах без ВК и без Redis.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from modules.ad_cabinet import client_photos
from modules.ad_cabinet.vk_bot import dialog, notify

logger = logging.getLogger(__name__)

TS_KEY = "setka:vkbot:lp_ts"
STATE_KEY = "setka:vkbot:state:{peer}"
STATE_TTL = 86400  # сутки: недописанный заказ не висит вечно
PHOTO_FETCH_TIMEOUT = 20.0  # секунд на скачивание одного фото с CDN ВК

StateGet = Callable[[int], Optional[Dict[str, Any]]]
StateSet = Callable[[int, Optional[Dict[str, Any]]], None]


def extract_incoming(update: Dict[str, Any]) -> Optional[dialog.Incoming]:
    """Событие Long Poll → :class:`dialog.Incoming` или ``None``. Чистая.

    Только ``message_new`` от людей (положительный ``from_id``); сообщения
    самого сообщества и события чатов не наши.
    """
    if not isinstance(update, dict) or update.get("type") != "message_new":
        return None
    obj = update.get("object") or {}
    msg = obj.get("message") if isinstance(obj, dict) else None
    if not isinstance(msg, dict):
        return None
    from_id = msg.get("from_id")
    if not isinstance(from_id, int) or from_id <= 0:
        return None
    return dialog.Incoming(
        peer_id=from_id,
        text=(msg.get("text") or "").strip(),
        payload=dialog.parse_payload(msg.get("payload")),
        attachments=[a for a in (msg.get("attachments") or []) if isinstance(a, dict)],
    )


def redis_state_store(r) -> Tuple[StateGet, StateSet]:
    """Пара (get, set) поверх Redis-клиента с ``decode_responses``."""

    def get(peer_id: int) -> Optional[Dict[str, Any]]:
        raw = r.get(STATE_KEY.format(peer=peer_id))
        if not raw:
            return None
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except ValueError:
            return None

    def set_(peer_id: int, state: Optional[Dict[str, Any]]) -> None:
        key = STATE_KEY.format(peer=peer_id)
        if state is None:
            r.delete(key)
        else:
            r.set(key, json.dumps(state, ensure_ascii=False, default=str), ex=STATE_TTL)

    return get, set_


def make_name_fetch(token: str, api_call) -> dialog.NameFetch:
    """``users.get`` community-токеном → «Имя Фамилия» для новой карточки."""

    async def fetch(vk_id: int) -> Optional[str]:
        resp = api_call(token, "users.get", user_ids=int(vk_id))
        rows = resp.get("response") if isinstance(resp, dict) else None
        if not rows:
            return None
        u = rows[0] or {}
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
        return name or None

    return fetch


def order_kwargs(draft: Dict[str, Any]) -> Dict[str, Any]:
    """Черновик диалога → именованные аргументы ``submit_order``. Чистая.

    ``photos`` черновика — имена файлов библиотеки клиента (basename, как кладёт
    кабинет); их и ждёт ``_real_attachment_builder`` при заливке на стену.
    """
    names = [Path(str(n)).name for n in (draft.get("photos") or []) if n]
    return {
        "text": draft.get("text") or "",
        "image_paths": [n for n in names if n][: client_photos.MAX_PHOTOS_PER_POST],
        "region_ids": list(draft.get("region_ids") or []),
        "publish_at": (
            datetime.fromisoformat(draft["publish_at"]) if draft.get("publish_at") else None
        ),
        "publish_now": bool(draft.get("publish_now")),
    }


def make_real_submitter() -> dialog.Submitter:
    """Заказ из бота — тем же ``submit_order``, что и кабинет.

    Для не-trusted клиента (все новые из бота) заказ ложится в ``pending`` без
    единого вызова ВК; фабрики нужны только trusted-клиентам.
    """

    async def submit(session, client, draft: Dict[str, Any]) -> Dict[str, Any]:
        from modules.ad_cabinet import client_orders
        from modules.vk_token_router import load_photo_upload_token
        from web.api.ad_crm import _cabinet_publisher_factory
        from web.api.advertiser_cabinet import _msk_to_unix, _real_attachment_builder

        # Фото заливает аккаунт-владелец группы (docs/ops/photo-upload-path.md).
        photo_token = await load_photo_upload_token(session)
        return await client_orders.submit_order(
            session,
            client=client,
            user_id=client.radar_user_id,
            publisher_factory=_cabinet_publisher_factory(session),
            attachment_builder=_real_attachment_builder(client.id, photo_token),
            msk_to_unix=_msk_to_unix,
            **order_kwargs(draft),
        )

    return submit


def make_photo_fetch(timeout: float = PHOTO_FETCH_TIMEOUT) -> dialog.PhotoFetch:
    """Скачать фото по ссылке из ``photo.sizes`` → bytes или ``None``.

    Ссылки ВК публичные и подписанные (токен не нужен), живут недолго — качаем
    в том же тике. Не картинка, пусто, тяжелее лимита, сеть — ``None`` с
    WARNING без query-строки (в ней подпись).
    """

    async def fetch(url: str) -> Optional[bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
                ctype = (r.headers.get("content-type") or "").lower()
                if not ctype.startswith("image/"):
                    return None
                data = r.content
        except Exception as e:  # noqa: BLE001 - сеть; фото просто не добавится
            # Не str(e): httpx.HTTPStatusError печатает полный URL с подписью.
            status = getattr(getattr(e, "response", None), "status_code", "")
            logger.warning(
                "vk_bot photo fetch failed: %s %s %s",
                str(url).split("?", 1)[0],
                type(e).__name__,
                status,
            )
            return None
        if not data or len(data) > client_photos.MAX_IMG_BYTES:
            return None
        return bytes(data)

    return fetch


async def handle_one(
    incoming: dialog.Incoming,
    *,
    session_factory,
    state_get: StateGet,
    state_set: StateSet,
    submit: dialog.Submitter,
    name_fetch: Optional[dialog.NameFetch],
    sender: Callable[[int, str, Optional[str]], Awaitable[Dict[str, Any]]],
    photo_fetch: Optional[dialog.PhotoFetch] = None,
) -> List[str]:
    """Одно входящее: диалог в своей транзакции → ответы → пинг владельцу.

    Возвращает список событий (``chat``/``order``) — для лога тика.
    """
    async with session_factory() as session:
        replies, new_state, events = await dialog.handle(
            session,
            incoming,
            state_get(incoming.peer_id),
            submit=submit,
            name_fetch=name_fetch,
            photo_fetch=photo_fetch,
        )
        await session.commit()
        client = await dialog.find_client(session, incoming.peer_id)
    state_set(incoming.peer_id, new_state)

    for text, kb in replies:
        ok, code = notify.send_outcome(await sender(incoming.peer_id, text, kb))
        if not ok:
            logger.warning("vk_bot reply to %s failed: vk error %s", incoming.peer_id, code)

    label = f"№{client.id} «{client.name or client.id}»" if client else f"vk id {incoming.peer_id}"
    for ev in events:
        if ev == "chat":
            preview = (incoming.text or "").strip().replace("\n", " ")[:80]
            await notify.notify_owner(
                f"💬 ВК-бот: клиент {label}: {preview} — ответить в /ad → Кабинеты",
                dedup_key=f"chat:{client.id if client else incoming.peer_id}",
                dedup_ttl=600,
            )
        elif ev == "order":
            await notify.notify_owner(
                f"🛎 ВК-бот: клиент {label} сделал заказ — ждёт одобрения в /ad"
            )
        elif ev == "order_direct":
            await notify.notify_owner(
                f"🛎 ВК-бот: клиент {label} (trusted) сделал заказ — уже в VK-отложке, "
                "отменить можно в /ad → Кабинеты"
            )
        elif ev == "payment_claimed":
            await notify.notify_owner(
                f"💳 ВК-бот: клиент {label} сообщил об оплате — подтвердить в /ad → Кабинеты",
                dedup_key=f"claim:{client.id if client else incoming.peer_id}",
                dedup_ttl=600,
            )
        elif ev == "order_cancelled":
            # Отмена — не мелочь для владельца: освободился слот в отложке ВК и,
            # если пост шёл в счёт пакета, вернулся слот пакета.
            await notify.notify_owner(
                f"🚫 ВК-бот: клиент {label} отменил размещение — слот освободился"
            )
        elif ev == "cancel_failed":
            # Бот сказал клиенту «передал владельцу» — эта строка и есть то
            # обещание. Без неё клиент ждал бы разбирательства, которого нет.
            await notify.notify_owner(
                f"⚠️ ВК-бот: у клиента {label} не снялось размещение из отложки ВК — "
                "снять вручную в /ad → Кабинеты"
            )
        elif ev == "signup":
            await notify.notify_owner(
                f"🆕 ВК-бот: новый клиент {label} — карточка заведена автоматически",
                dedup_key=f"vkbot_signup:{incoming.peer_id}",
            )
    return events


async def poll_once(
    *,
    token: str,
    group_id: int,
    session_factory,
    state_get: StateGet,
    state_set: StateSet,
    ts_get: Callable[[], Optional[str]],
    ts_set: Callable[[Optional[str]], None],
    submit: Optional[dialog.Submitter] = None,
    api_call=None,
    lp_get=None,
    sender=None,
    photo_fetch: Optional[dialog.PhotoFetch] = None,
) -> Dict[str, Any]:
    """Один тик: getLongPollServer → a_check → диалоги → ts."""
    from modules.radar.vk_intake import lp_fetch, vk_api_call

    api_call = api_call or vk_api_call
    lp_get = lp_get or lp_fetch
    submit = submit or make_real_submitter()
    sender = sender or notify._make_sender(token, group_id)
    photo_fetch = photo_fetch or make_photo_fetch()
    name_fetch = make_name_fetch(token, api_call)

    srv = api_call(token, "groups.getLongPollServer", group_id=group_id)
    resp = srv.get("response") if isinstance(srv, dict) else None
    if not resp or not resp.get("server"):
        err = (srv or {}).get("error") or {}
        return {"ok": False, "error": err.get("error_msg", "getLongPollServer failed")}

    server, key, fresh_ts = resp["server"], resp["key"], resp["ts"]
    ts = ts_get() or fresh_ts
    data = lp_get(server, key, ts)
    if "failed" in data:
        if data.get("failed") == 1 and data.get("ts") is not None:
            ts_set(str(data["ts"]))
        else:
            ts_set(None)
        return {"ok": True, "reinit": data.get("failed"), "processed": 0}

    updates = data.get("updates") or []
    processed, events_total = 0, 0
    for upd in updates:
        inc = extract_incoming(upd)
        if inc is None:
            continue
        processed += 1
        try:
            events = await handle_one(
                inc,
                session_factory=session_factory,
                state_get=state_get,
                state_set=state_set,
                submit=submit,
                name_fetch=name_fetch,
                sender=sender,
                photo_fetch=photo_fetch,
            )
            events_total += len(events)
        except Exception:  # noqa: BLE001 - один битый диалог не валит остальные
            logger.exception("vk_bot: handle_one failed for peer %s", inc.peer_id)
            try:
                await sender(
                    inc.peer_id,
                    "Что-то пошло не так, попробуйте ещё раз или напишите владельцу.",
                    dialog.MAIN_KEYBOARD,
                )
            except Exception:  # noqa: BLE001
                pass

    if data.get("ts") is not None:
        ts_set(str(data["ts"]))
    return {"ok": True, "updates": len(updates), "processed": processed, "events": events_total}


__all__ = [
    "extract_incoming",
    "redis_state_store",
    "poll_once",
    "handle_one",
    "order_kwargs",
    "make_photo_fetch",
    "TS_KEY",
]
