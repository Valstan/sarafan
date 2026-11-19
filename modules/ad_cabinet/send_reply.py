"""Ответ рекламодателю: отправить подготовленный текст автору заявки.

**Зачем отдельный модуль.** Ответ на заявку отправляют два места: веб-кабинет
(кнопка «Отправить») и **MAX** (кнопка «Ответить автору» в карточке заявки).
Логика одна и та же — и она непростая: что именно можно отправить, кто может
писать, что делать, когда VK не даёт сообществу писать первым. Дублировать её
в двух местах — значит через месяц получить два разных поведения, и разница
всплывёт на живом рекламодателе.

**Жёсткое ограничение VK, которое тут действует.** У **user-токена нет scope
``messages``** (замер 2026-09-05, ``modules/notifications/vk_actions.py``), а
сообщество не может написать пользователю первым (ошибка 901). Поэтому
«отправить от своего имени Валентина Савиных» через API **невозможно** — и
обещать это рекламодателю нельзя. Что можно:

1. **сообщество пишет само** — если автор разрешил сообществу писать
   (``messages.isMessagesFromGroupAllowed``; для входящих ЛС разрешение есть
   всегда, потому что человек написал первым);
2. **иначе — диалог с владельцем**: возвращаем ``personal_deeplink`` на личную
   страницу ВК с уже открытым диалогом, и владелец пишет сам, от своего имени.
   Это ровно то, что уже делает кабинет.

**Идемпотентность.** ``random_id`` = id заявки: повтор той же отправки на стороне
VK схлопывается, а не дублирует сообщение рекламодателю.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Сколько дней считать свежим precheck ``can_message`` (сообщество могло
#: открыть/закрыть разрешение). Дальше — переспрашиваем VK.
CAN_MESSAGE_TTL_DAYS = 7


def personal_deeplink(peer_id: Any) -> str:
    """Ссылка на личный диалог ВК с автором (fallback «от своего имени»)."""
    try:
        return f"https://vk.com/im?sel={int(peer_id)}"
    except (TypeError, ValueError):
        return "https://vk.com/im"


async def build_reply_text(session, request) -> Optional[str]:
    """Текст ответа автору: шаблон владельца + гарантированная ссылка на услуги.

    **Шаблон — один на все каналы.** Берётся тот же активный ``ad_greeting``,
    что уходит в авто-приветствии и показывается в кабинете (см.
    ``auto_greeting.resolve_greeting_text``): источник должен быть один, иначе
    владелец копирует не тот текст, который реально ушёл людям.

    **Ссылка на услуги добавляется всегда.** Владелец просил, чтобы в ответе
    было видно, какие услуги мы предлагаем, и куда смотреть прайс. Если в его
    шаблоне ссылки нет — дописываем стандартную строку; если есть — не
    трогаем (иначе будет две ссылки подряд).

    **Битый шаблон не отправляем.** ``looks_mangled`` ловит текст, потерявший
    кодировку (кириллица → «????») — такой ответ рекламодателю хуже, чем
    никакого.
    """
    from modules.ad_cabinet.auto_greeting import resolve_greeting_text
    from modules.ad_cabinet.message_builder import looks_mangled, render
    from modules.max_notifier import services_url

    template_text = await resolve_greeting_text(session)
    if not template_text:
        return None
    if looks_mangled(template_text):
        logger.error("MAX reply: шаблон потерял кодировку (кириллица → '?') — ответ не отправляем")
        return None

    text = render(
        template_text,
        author_name=request.author_name,
        community_name=request.community_name,
        region_name=request.community_name,
    )
    link = services_url()
    if link and link not in text:
        text = f"{text}\n\nУслуги и прайс по размещению рекламы: {link}"
    return text


async def send_reply_to_author(
    session,
    request,
    *,
    message: Optional[str] = None,
    attachment: Optional[str] = None,
) -> Dict[str, Any]:
    """Отправить ответ автору заявки.

    Возвращает словарь с исходами (те же формы, что у кабинета, чтобы UI и
    MAX читали одинаково):

    - ``{"success": True, "vk_message_id": ..., "via": ...}`` — отправлено;
    - ``{"allowed": False, "personal_deeplink": ..., "prepared_message": ...}`` —
      писать нельзя, отдаём диалог владельцу;
    - ``{"success": False, "error": ..., "error_code": ...}`` — прочие отказы.

    Никогда не бросает: вызывающий ( Celery-задача, вебхух MAX) сам решает, что
    делать с отказом.
    """
    from modules.ad_cabinet.interaction_log import log_interaction
    from modules.notifications.vk_actions import messages_allowed, send_message
    from modules.vk_token_router import load_vk_routing

    if message and message.strip():
        request.prepared_message = message.strip()
    if not (request.prepared_message or "").strip():
        return {"success": False, "error": "Сначала подготовьте сообщение"}

    # Автор-группа или нерезолвимый peer — ЛС невозможно.
    if request.author_is_group or not request.peer_id or int(request.peer_id) <= 0:
        return {
            "allowed": False,
            "reason": "author_is_group",
            "personal_deeplink": None,
            "prepared_message": request.prepared_message,
        }

    user_token, community_tokens = await load_vk_routing()
    if not user_token:
        return {"success": False, "error": "VK token not found"}

    group_id = int(request.community_vk_id)
    peer_id = int(request.peer_id)

    # Свежий precheck со скана (modules.ad_cabinet.scanner) — не дёргаем VK
    # повторно: messageability меняется редко.
    if (
        request.can_message is not None
        and request.can_message_checked_at
        and (datetime.utcnow() - request.can_message_checked_at)
        < timedelta(days=CAN_MESSAGE_TTL_DAYS)
    ):
        allowed = request.can_message
    else:
        allowed = await asyncio.to_thread(
            messages_allowed,
            group_id=group_id,
            user_id=peer_id,
            user_token=user_token,
            community_tokens=community_tokens,
        )
    if allowed is False:
        request.can_message = False
        request.can_message_checked_at = datetime.utcnow()
        await session.commit()
        return {
            "allowed": False,
            "personal_deeplink": personal_deeplink(peer_id),
            "prepared_message": request.prepared_message,
        }

    # allowed True или None (неизвестно) — пробуем отправить; 901 отработает
    # внутри send_message.
    res = await asyncio.to_thread(
        send_message,
        group_id=group_id,
        peer_id=peer_id,
        message=request.prepared_message,
        user_token=user_token,
        community_tokens=community_tokens,
        random_id=int(request.id),  # стабильный → идемпотентность на стороне VK
        attachment=attachment or None,
    )

    if res.get("success"):
        request.status = "contacted"
        request.contacted_at = datetime.utcnow()
        request.via = res.get("via")
        request.vk_message_id = res.get("message_id")
        request.can_message = True
        request.can_message_checked_at = datetime.utcnow()
        if attachment and not request.message_attachments:
            request.message_attachments = attachment
        log_interaction(
            session,
            kind="reply_sent",
            client_id=request.client_id,
            ad_request_id=request.id,
            summary="Отправлен ответ клиенту: " + (request.prepared_message or "")[:120],
            meta={"via": res.get("via"), "source": "max"},
        )
        await session.commit()
        return {
            "success": True,
            "vk_message_id": res.get("message_id"),
            "via": res.get("via"),
        }

    # Не отправлено. 900/901/902 → фолбэк на личный диалог.
    if res.get("allowed") is False:
        request.can_message = False
        request.can_message_checked_at = datetime.utcnow()
        await session.commit()
        return {
            "allowed": False,
            "personal_deeplink": res.get("personal_deeplink") or personal_deeplink(peer_id),
            "prepared_message": request.prepared_message,
            "error_code": res.get("error_code"),
        }

    return {
        "success": False,
        "error": res.get("error"),
        "error_code": res.get("error_code"),
    }


__all__ = ("CAN_MESSAGE_TTL_DAYS", "personal_deeplink", "send_reply_to_author")
