"""Вебхук Bot API MAX — приём нажатий на кнопку «Ответить автору».

**Как это работает.** MAX шлёт боту события на наш HTTPS-эндпоинт
(``POST /subscriptions`` на стороне MAX). В теле — ``Update``; нас интересует
``type == "message_callback"``: в нём ``callback.callback_id`` (на что отвечать)
и ``callback.payload`` (что нажали).

**Аутентификация.** MAX подписывает запрос заголовком ``X-Max-Bot-Api-Secret``
— тем самым секретом, который владелец задал при подписке. Без проверки
эндпоинт был бы открытой дверью: любой, кто знает URL, мог бы нажимать
кнопки от имени владельца и слать рекламодателям ответы.

**Отвечать MAX надо всегда и быстро.** MAX ждёт ``200 OK`` до 30 секунд, а
после трёх неудачных попыток (60 с, 150 с, 375 с) **отключает подписку**. Поэтому
обработка не бросает исключений наружу: любая ошибка внутри превращается в
``200`` с честным телом — иначе одна битая заявка отключила бы весь канал.

**Что делает нажатие.** По id заявки (payload ``ad:<id>``) поднимаем её из БД,
готовим текст ответа (шаблон владельца + ссылка на услуги) и отправляем автору
от имени сообщества. Если VK не даёт сообществу писать (ошибка 901) — отдаём
владельцу ссылку на его личный диалог: писать от своего имени через API
нельзя (у user-токена нет scope ``messages``), но можно открыть диалог и
дописать самому.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request

from modules import max_http
from modules.max_notifier import ad_request_id_from_payload

logger = logging.getLogger(__name__)
router = APIRouter()


def webhook_secret() -> str:
    """Секрет вебхука (``MAX_WEBHOOK_SECRET``). Пусто — вебхук выключен."""
    return (os.getenv("MAX_WEBHOOK_SECRET") or "").strip()


def _check_secret(provided: Optional[str]) -> None:
    """Сверить ``X-Max-Bot-Api-Secret``.

    Секрет не задан → 503 (фича не настроена, а не «ваш запрос неверен»).
    Задан, но не совпал → 401.
    """
    expected = webhook_secret()
    if not expected:
        raise HTTPException(status_code=503, detail="MAX webhook не настроен")
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="bad or missing X-Max-Bot-Api-Secret")


def _updates(payload: Any) -> List[Dict[str, Any]]:
    """Список обновлений из тела вебхука.

    MAX шлёт один ``Update`` или список — оба формата встречаются, поэтому
    разбираем оба.
    """
    if isinstance(payload, list):
        return [u for u in payload if isinstance(u, dict)]
    if isinstance(payload, dict):
        updates = payload.get("updates")
        if isinstance(updates, list):
            return [u for u in updates if isinstance(u, dict)]
        return [payload]
    return []


async def _handle_callback(update: Dict[str, Any]) -> Dict[str, Any]:
    """Обработать одно нажатие кнопки. Никогда не бросает наружу."""
    callback = update.get("callback") or {}
    callback_id = str(callback.get("callback_id") or "")
    payload = str(callback.get("payload") or "")
    request_id = ad_request_id_from_payload(payload)

    if not request_id:
        logger.info("MAX webhook: нажатие с чужим payload %r — игнорируем", payload)
        return {"handled": False, "reason": "foreign_payload"}

    from database.connection import AsyncSessionLocal
    from database.models import AdRequest
    from modules.ad_cabinet.send_reply import build_reply_text, send_reply_to_author

    async with AsyncSessionLocal() as session:
        ar = await session.get(AdRequest, request_id)
        if not ar:
            logger.warning("MAX webhook: заявка %s не найдена", request_id)
            return {"handled": False, "reason": "request_not_found"}

        text = ar.prepared_message or await build_reply_text(session, ar)
        if not text:
            logger.warning("MAX webhook: для заявки %s нет текста ответа", request_id)
            return {"handled": False, "reason": "no_template"}

        result = await send_reply_to_author(session, ar, message=text)

    if result.get("success"):
        note = "Ответ отправлен автору от имени сообщества."
    elif result.get("allowed") is False:
        link = result.get("personal_deeplink")
        note = (
            "Сообщество не может писать этому автору первым — VK так не даёт. "
            "Ответьте из своего личного диалога ВК:"
            f"\n{link}"
            if link
            else "Сообщество не может писать этому автору первым — откройте диалог ВК и ответьте сами."
        )
    else:
        note = f"Ответ не отправлен: {result.get('error') or 'неизвестная причина'}"

    if callback_id:
        try:
            max_http.answer_callback(callback_id, note)
        except Exception as e:  # noqa: BLE001 — подтверждение не должно ронять обработку
            logger.warning("MAX webhook: не удалось подтвердить callback: %s", e)

    return {"handled": True, "request_id": request_id, "result": result, "note": note}


@router.post("/max/webhook")
async def max_webhook(
    request: Request,
    x_max_bot_api_secret: Optional[str] = Header(default=None, alias="X-Max-Bot-Api-Secret"),
):
    """Приём событий MAX.

    Всегда отвечает ``200``: неотвеченный или 5xx-ответ отключает подписку
    после трёх попыток, а ошибка в одной заявке не должна закрывать канал
    целиком.
    """
    _check_secret(x_max_bot_api_secret)
    try:
        payload = await request.json()
    except Exception:
        logger.warning("MAX webhook: тело не разобрано")
        return {"ok": True, "handled": 0}

    handled = 0
    for update in _updates(payload):
        if update.get("type") != "message_callback":
            continue
        try:
            result = await _handle_callback(update)
        except Exception as e:  # noqa: BLE001 — ответ 200 в любом случае
            logger.exception("MAX webhook: обработка нажатия не удалась: %s", e)
            continue
        handled += 1 if result.get("handled") else 0

    return {"ok": True, "handled": handled}


__all__ = ("router", "webhook_secret")
