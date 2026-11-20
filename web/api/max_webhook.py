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

**Событие ``bot_started`` — отдельный случай.** Когда владелец нажимает
«Старт» в диалоге с ботом, MAX присылает ``bot_started`` с ``chat_id``. Мы его
запоминаем (Redis), и с этого момента уведомления идут в этот чат сами —
**идентификатор чата копировать руками не нужно**.

**Но «Старт» может и не сработать как страховка.** События приходят только
после подписки, а ``bot_started`` платформа шлёт **один раз — на первом
запуске диалога**. Если владелец открыл бота раньше, чем появилась подписка,
событие потерялось, а повторное нажатие «Старт» может и не дать его снова.
Поэтому вторым источником чата служит **любое сообщение владельца боту**
(``message_created``): MAX шлёт его всегда, и в нём чат тоже есть. Этот же
приём применяет соседний проект «Матрица РМЗ» — там адресат вычитывается
из входящего сообщения (у MAX ``GET /chats`` отключён, иного способа узнать
чат нет). Новый чат подтверждаем сообщением «САРАФАН на связи» — владелец
видит, что канал ожил, а не просто молчит.
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

#: Ответ владельцу, когда канал ожил: чат получен — значит, карточки заявок
#: пойдут сюда. Короткий: MAX-карточка и так читается на телефоне.
CONFIRMATION_TEXT = (
    "САРАФАН на связи: канал рекламных заявок включён.\n"
    "Этот чат получен — новые заявки будут приходить сюда, отвечать можно "
    "одним нажатием."
)


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


def _event_type(update: Dict[str, Any]) -> str:
    """Тип события MAX.

    **Ключ называется ``update_type``, а не ``type``** — это выяснилось живьём
    20.11, когда события от MAX приходили, а канал молчал: обработчик искал
    ``type``, получал ``None`` и тихо пропускал всё. Старый ключ ``type``
    оставлен как запасной: смена формы события не должна снова выключить канал
    молча.
    """
    for key in ("update_type", "type"):
        value = update.get(key)
        if value:
            return str(value)
    return ""


def _chat_id_from_update(update: Dict[str, Any]) -> Optional[str]:
    """Идентификатор чата из события MAX — в любой из известных форм.

    Формы событий у MAX менялись и ещё поменяются: у ``bot_started`` чат лежит
    **на верхнем уровне** события, у ``message_created`` — внутри
    ``message.recipient`` (``chat_id`` рядом с ``user_id``). Поэтому ищем по
    нескольким местам сразу, а не в одном: иначе событие сменит форму — и канал
    молча останется без адресата.

    Порядок — от надёжного к сомнительному: сначала ``chat_id`` (идентификатор
    чата), и только потом ``user_id`` (идентификатор человека). Адресат для
    отправки — это чат, поэтому ``user_id`` берём только если чата в событии
    нет вовсе.
    """
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    recipient = message.get("recipient") or {}
    top_chat = update.get("chat") or {}

    chat_candidates = (
        update.get("chat_id"),
        message.get("chat_id"),
        chat.get("chat_id"),
        chat.get("id"),
        top_chat.get("chat_id"),
        top_chat.get("id"),
        recipient.get("chat_id"),
    )
    for value in chat_candidates:
        if value not in (None, ""):
            return str(value)

    user_candidates = (recipient.get("user_id"), (message.get("user") or {}).get("user_id"))
    for value in user_candidates:
        if value not in (None, ""):
            return str(value)
    return None


async def _handle_bot_started(update: Dict[str, Any]) -> Dict[str, Any]:
    """Владелец нажал «Старт» в диалоге с ботом — запоминаем chat_id.

    Зачем это отдельным обработчиком: идентификатор чата узнаётся **только**
    из события, и копировать его руками не нужно. MAX присылает ``bot_started``
    с ``chat_id`` — мы его запоминаем, и с этого момента уведомления идут в
    этот чат сами.
    """
    chat_id = update.get("chat_id")
    if chat_id in (None, ""):
        logger.info("MAX webhook: bot_started без chat_id")
        return {"handled": False, "reason": "no_chat_id"}
    # Строка везде: MAX присылает идентификатор то числом, то строкой, а в Redis
    # он должен лежать одним и тем же — иначе «новый чат» не отличить от прежнего.
    chat_id = str(chat_id)
    saved = max_http.remember_chat_id(chat_id)
    logger.info(
        "MAX webhook: владелец запустил бота, chat_id=%s (%s)",
        chat_id,
        "запомнен" if saved else "запомнить не удалось (проверьте Redis)",
    )
    return {"handled": True, "chat_id": chat_id, "saved": saved}


async def _handle_message_created(update: Dict[str, Any]) -> Dict[str, Any]:
    """Владелец написал боту — узнаём из этого чат и подтверждаем, что канал ожил.

    Зачем это второй источник чата: ``bot_started`` платформа шлёт **один раз**
    — на первом запуске диалога. Если владелец открыл бота до появления
    подписки, событие потерялось, и нажатие «Старт» заново может его не
    породить. Любое сообщение боту событие приносит всегда, и в нём чат есть
    — так же, как в «Матрице РМЗ».

    Подтверждение («САРАФАН на связи») отправляется только когда чат новый:
    иначе каждое сообщение владельца порождало бы ответ.
    """
    chat_id = _chat_id_from_update(update)
    if not chat_id:
        logger.info("MAX webhook: message_created без chat_id")
        return {"handled": False, "reason": "no_chat_id"}

    known = max_http.learned_chat_id()
    saved = max_http.remember_chat_id(chat_id)
    logger.info(
        "MAX webhook: сообщение владельца, chat_id=%s (%s)",
        chat_id,
        "запомнен" if saved else "запомнить не удалось (проверьте Redis)",
    )
    if not saved:
        return {"handled": False, "reason": "not_saved", "chat_id": chat_id}

    if known and str(known) == str(chat_id):
        return {"handled": True, "chat_id": chat_id, "saved": True, "confirmation": False}

    confirmed = False
    try:
        max_http.send_message(chat_id, CONFIRMATION_TEXT)
        confirmed = True
    except Exception as e:  # noqa: BLE001 — подтверждение не должно ронять обработку
        logger.warning("MAX webhook: не удалось подтвердить подключение: %s", e)
    return {"handled": True, "chat_id": chat_id, "saved": True, "confirmation": confirmed}


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
            else (
                "Сообщество не может писать этому автору первым — "
                "откройте диалог ВК и ответьте сами."
            )
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
        kind = _event_type(update)
        try:
            if kind == "bot_started":
                result = await _handle_bot_started(update)
            elif kind == "message_created":
                result = await _handle_message_created(update)
            elif kind == "message_callback":
                result = await _handle_callback(update)
            else:
                logger.info("MAX webhook: событие типа %r — игнорируем", kind)
                continue
        except Exception as e:  # noqa: BLE001 — ответ 200 в любом случае
            logger.exception("MAX webhook: обработка %s не удалась: %s", kind, e)
            continue
        handled += 1 if result.get("handled") else 0

    return {"ok": True, "handled": handled}


__all__ = ("router", "webhook_secret")
