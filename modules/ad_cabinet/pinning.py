"""Закреп рекламного поста на сутки (Этап 2, PR 2C; решение владельца 2026-09-05).

Прайс: «закреп на сутки +200 ₽» за сообщество, пакетом и скидками не
покрывается (``config.ad_landing.PIN_PRICE_RUB``). В сообществе закреплён
может быть только один пост, поэтому:

- :func:`pin_conflicts` — сообщества из ``targets``, где на окно ±сутки от
  даты выхода уже есть закреп (любого клиента): заказ с закрепом туда — отказ;
- :func:`pin_after_publish` — после фиксации выхода делает ``wall.pin`` и
  запоминает ``pinned_at``/``pinned_until`` (UTC naive); неудача — событие
  ``pin_failed`` в таймлайне, деньги за закреп остаются вопросом владельца;
- :func:`run_unpin` — снимает закрепы, у которых ``pinned_until`` прошёл
  (``wall.unpin``), ставит ``unpinned_at``. Всё сетевое инъектируется.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select

from database.models import AdPublication, AdScheduledPost
from modules.ad_cabinet.interaction_log import log_interaction

logger = logging.getLogger(__name__)

PIN_HOURS = 24
_PIN_STATUSES = ("pending", "draft", "scheduled", "published")

Pinner = Callable[[int, int], Awaitable[Dict[str, Any]]]


async def pin_conflicts(
    session,
    targets: Sequence[Tuple[int, int]],
    publish_at: datetime,
) -> List[int]:
    """Сообщества из ``targets``, где закреп на окно ±сутки уже занят.

    Правило: занят закреп **любого** клиента — в сообществе может висеть
    только один закреп, и второй рекламный пост туда не влезает.

    ⚠️ Раньше у функции был параметр ``exclude_client_id`` («не считать
    конфликтом свои закрепы»), но тело его **не читало**: молчаливый no-op,
    который выглядел как работающая фича. Ни одного вызова с этим
    параметром не было, а правило «любого клиента» — сознательное, поэтому
    параметр убран (P019, прогон 19.11). Если владелец захочет разрешить
    клиенту второй закреп в своём же сообществе — это продуктовое решение,
    и параметр вернётся вместе с ним.
    """
    gids = [int(t[1]) for t in targets]
    if not gids:
        return []
    lo = publish_at - timedelta(hours=PIN_HOURS)
    hi = publish_at + timedelta(hours=PIN_HOURS)
    q = select(AdScheduledPost.community_vk_id).where(
        AdScheduledPost.community_vk_id.in_(gids),
        AdScheduledPost.pinned.is_(True),
        AdScheduledPost.status.in_(_PIN_STATUSES),
        AdScheduledPost.publish_date > lo,
        AdScheduledPost.publish_date < hi,
    )
    rows = (await session.execute(q)).scalars().all()
    return sorted({int(g) for g in rows})


async def pin_after_publish(
    session,
    row: AdScheduledPost,
    pub: AdPublication,
    *,
    pinner: Optional[Pinner],
    now: Optional[datetime] = None,
) -> bool:
    """Закрепить только что вышедший пост. ``True`` — закреплён. Без commit."""
    if not row.pinned or not pub.vk_post_id or pinner is None:
        return False
    now = now or datetime.utcnow()
    try:
        res = await pinner(int(row.community_vk_id), int(pub.vk_post_id))
    except Exception as e:  # noqa: BLE001 - сеть не роняет фиксацию выхода
        res = {"success": False, "error": str(e)}
    if res.get("success"):
        pub.pinned_at = now
        pub.pinned_until = now + timedelta(hours=PIN_HOURS)
        log_interaction(
            session,
            kind="pinned",
            client_id=row.client_id,
            scheduled_post_id=row.id,
            publication_id=pub.id,
            summary=f"Пост закреплён на сутки (сообщество {row.community_vk_id})",
            actor="system",
        )
        return True
    log_interaction(
        session,
        kind="pin_failed",
        client_id=row.client_id,
        scheduled_post_id=row.id,
        publication_id=pub.id,
        summary=f"Закреп не удался ({row.community_vk_id}): {res.get('error') or 'VK'}"[:300],
        actor="system",
    )
    return False


async def run_unpin(
    *,
    session_factory: Optional[Callable] = None,
    unpinner: Optional[Pinner] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Снять закрепы с истёкшим ``pinned_until``. Возвращает счётчики."""
    if session_factory is None:
        from database.connection import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    now = now or datetime.utcnow()
    stats = {"due": 0, "unpinned": 0, "failed": 0}
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AdPublication).where(
                        AdPublication.pinned_until.isnot(None),
                        AdPublication.pinned_until <= now,
                        AdPublication.unpinned_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        stats["due"] = len(rows)
        if rows and unpinner is None:
            unpinner = await build_default_unpinner(session)
        for pub in rows:
            try:
                res = await unpinner(int(pub.community_vk_id), int(pub.vk_post_id or 0))
            except Exception as e:  # noqa: BLE001
                res = {"success": False, "error": str(e)}
            if res.get("success"):
                pub.unpinned_at = now
                stats["unpinned"] += 1
                log_interaction(
                    session,
                    kind="unpinned",
                    client_id=pub.client_id,
                    publication_id=pub.id,
                    summary=f"Закреп снят через сутки (сообщество {pub.community_vk_id})",
                    actor="system",
                )
            else:
                stats["failed"] += 1
                logger.warning(
                    "unpin failed for %s_%s: %s",
                    pub.community_vk_id,
                    pub.vk_post_id,
                    res.get("error"),
                )
        await session.commit()
    return stats


async def build_default_pinner(session) -> Pinner:  # pragma: no cover - сеть
    from modules.publisher.vk_publisher_extended import VKPublisher

    async def pin(owner_id: int, post_id: int) -> Dict[str, Any]:
        publisher = await VKPublisher.create_with_policy(session, target_group_id=int(owner_id))
        return await publisher.pin_post(int(owner_id), int(post_id))

    return pin


async def build_default_unpinner(session) -> Pinner:  # pragma: no cover - сеть
    from modules.publisher.vk_publisher_extended import VKPublisher

    async def unpin(owner_id: int, post_id: int) -> Dict[str, Any]:
        publisher = await VKPublisher.create_with_policy(session, target_group_id=int(owner_id))
        return await publisher.unpin_post(int(owner_id), int(post_id))

    return unpin


__all__ = [
    "PIN_HOURS",
    "pin_conflicts",
    "pin_after_publish",
    "run_unpin",
    "build_default_pinner",
    "build_default_unpinner",
]
