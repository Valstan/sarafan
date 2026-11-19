"""Отправка карточек новых рекламных заявок в MAX.

**Что делает.** Находит заявки, которым карточка ещё не уходила
(``max_notified_at IS NULL``), собирает по каждой карточку
(``modules/max_notifier.py``) и отправляет в чат «САРАФАН».

**Почему метка в БД, а не флаг в памяти.** Заявка живёт в Postgres, и «ей уже
сообщили» — свойство самой заявки. Ключ в Redis пережил бы перезапуск или
вытеснение, и тогда владелец получил бы повторные карточки — тот самый шум,
который эта задача убирает.

**Идемпотентность на частичной отправке.** Метка ставится **только после
успешной** отправки. Если MAX недоступен, заявка осталась без метки и уйдёт
следующим проходом — лучше повтор карточки, чем потерянная заявка. Повтор
безопасен и для рекламодателя: он видит уведомление у владельца, а не у себя.

**Канал не настроен — не ошибка.** Нет ``MAX_TOKEN``/``MAX_CHAT_ID`` — задача
молча выходит: канал включается владельцем, когда он создаст бота.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from database.models import AdRequest
from modules.max_notifier import card_for_request, send_card

logger = logging.getLogger(__name__)

#: Сколько карточек за один проход. MAX разрешает 2 сообщения в секунду, а
#: владельцу десяток заявок подряд не нужен — карточки копятся, а не теряются.
MAX_PER_RUN = 20


def cabinet_url() -> str:
    """Ссылка на страницу рекламного кабинета (куда ведёт карточка, если
    исходного поста нет — например, по входящему ЛС)."""
    try:
        from config.runtime import SERVER

        domain = (
            SERVER.get("domain")
            or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
        )
        return f"https://{domain}/ad"
    except Exception:  # pragma: no cover - конфиг без SERVER — не повод падать
        return ""


async def notify_new_requests(
    session,
    *,
    chat_id: Optional[Any] = None,
    limit: int = MAX_PER_RUN,
) -> Dict[str, Any]:
    """Разослать карточки по неотправленным заявкам.

    Возвращает ``{"sent": n, "skipped": n, "failed": n, "total": n}``.
    """
    rows: List[AdRequest] = (
        (
            await session.execute(
                select(AdRequest)
                .where(
                    AdRequest.status == "new",
                    AdRequest.max_notified_at.is_(None),
                )
                .order_by(AdRequest.detected_at.asc(), AdRequest.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"sent": 0, "skipped": 0, "failed": 0, "total": 0}

    sent = 0
    skipped = 0
    failed = 0
    for ar in rows:
        card = card_for_request(ar, cabinet_url=cabinet_url())
        result = send_card(card, chat_id=chat_id)
        if not result.get("ok"):
            # Канал не настроен — отправлять некуда, заявка ждёт.
            skipped += 1
            continue
        ar.max_notified_at = datetime.utcnow()
        sent += 1
        try:
            await session.commit()
        except Exception as e:  # noqa: BLE001 — отправлено-то, метку не записали
            logger.warning("MAX notify: не записана метка отправки заявки %s: %s", ar.id, e)
            await session.rollback()

    return {"sent": sent, "skipped": skipped, "failed": failed, "total": len(rows)}


__all__ = ("MAX_PER_RUN", "cabinet_url", "notify_new_requests")
