"""Один тик интейков радара: сборка зависимостей, отдельно от способа запуска.

Зачем отдельный модуль. Раньше эти две сборки жили внутри Celery-тасок
(``tasks/radar_tasks.py``), потому что запускались только из beat. Теперь у них
два способа запуска: тик из beat и непрерывный демон
(``scripts/radar_intake_daemon.py``). Демон не должен импортировать
``tasks.celery_app``: тот тянет за собой все таски, весь Celery и ~150 МБ
памяти — за это он и выносится из воркера.

**Почему вообще демон.** Оба поллинга почти всё время ЖДУТ (a_check Long Poll,
getUpdates у Telegram), но в однопроцессном воркере ожидание занимает
единственный слот: замер 21.10 — 1.7 ч + 1.6 ч в сутки (13 % слота) на пустое
ожидание, а при переполненной очереди волн эти тики ещё и истекают по
``expires`` и не выполняются вовсе (за день 1 526 выброшенных периодических
задач). Демон ждёт бесплатно: событие приходит за секунды, слот воркера свободен.

Сборки возвращают ровно то, что возвращали таски, — включая ``skipped``, когда
интейк выключен конфигом (нет ``RADAR_VK_COMMUNITY_ID``/токена, пустой
allowlist). Демон по ``skipped`` спит дольше, а не крутит цикл.

Третья сборка — ``sources_poll_once`` (поллер источников, 21.10): та же причина,
но без «ожидания события». Она крутится по фиксированному интервалу и не имеет
состояния, поэтому ``skipped`` у неё нет: пустой радар — это no-op, а не выключка.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def sources_poll_once() -> Dict[str, Any]:
    """Один прогон поллера источников радара — без Celery.

    Третий цикл демона. Раньше это была beat-таска ``poll_radar_sources``
    (каждые 10 минут), и она стоила слоту воркера 187 мин в сутки: пять
    источников поллились по очереди, а у телеграм-релейного фетчера таймаут
    45 с и две попытки. В демоне прогон не занимает слот, а сами фетчи идут
    параллельно (``modules.radar.poller``).

    Возвращает сводку поллера: ``{ok, sources, new_items, failed}``.
    """
    from modules.radar.poller import poll_all_sources

    summary = await poll_all_sources()
    return {"ok": True, **summary}


async def vk_intake_once(*, lp_wait: Optional[int] = None) -> Dict[str, Any]:
    """Один тик VK-интейка: Bots Long Poll сообщества → привязка VK-лички по коду.

    ``lp_wait`` — секунды ожидания ``a_check``. ``None`` — модульный дефолт
    (``vk_intake.LP_WAIT``, короткий, для beat-тика). Демон передаёт 25: там
    ожидание не стоит ничего.
    """
    from config.runtime import get_radar_vk_community_id

    community_id = get_radar_vk_community_id()
    if not community_id:
        return {"skipped": "RADAR_VK_COMMUNITY_ID not set"}

    from modules.vk_token_router import load_vk_routing

    _user_token, community_tokens = await load_vk_routing()
    token = (community_tokens or {}).get(community_id)
    if not token:
        return {"skipped": f"no community token for {community_id}"}

    from modules.bulletin_heartbeat import _redis

    r = _redis()
    if r is None:
        return {"skipped": "no redis (ts persistence required)"}
    ts_key = "setka:radar_vk_intake_ts"

    def ts_get():
        v = r.get(ts_key)
        if v is None:
            return None
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)

    def ts_set(v):
        if v is None:
            r.delete(ts_key)
        else:
            r.set(ts_key, str(v))

    async def link_account(code, vk_user_id, display_name):
        from modules.radar.account_link import link_vk

        return await link_vk(code, vk_user_id, display_name=display_name, group_id=community_id)

    async def reply(peer_id, text):
        # Тот же транспорт, что у бота САРАФАНа: пауза канала на 9/14 + лимитер.
        from modules.ad_cabinet.vk_bot import notify as vk_notify

        await vk_notify.vk_send(token, community_id, peer_id, text)

    from modules.radar.vk_intake import lp_fetch, poll_vk_intake_once

    lp_get = None
    if lp_wait is not None:

        def _lp(server, key, ts):
            return lp_fetch(server, key, ts, wait=int(lp_wait))

        lp_get = _lp

    return await poll_vk_intake_once(
        token=token,
        group_id=community_id,
        link_account=link_account,
        reply=reply,
        ts_get=ts_get,
        ts_set=ts_set,
        lp_get=lp_get,
    )


async def telegram_intake_once(*, long_poll: Optional[int] = None) -> Dict[str, Any]:
    """Один тик intake-бота «Карман»: getUpdates → добавить каналы из форвардов.

    ``long_poll`` — секунды серверного ожидания ``getUpdates``; ``None`` —
    модульный дефолт (0, beat-тик). Демон передаёт 25.
    """
    from config.runtime import (
        TELEGRAM_TOKENS,
        get_radar_bot_allowed_users,
        get_radar_bot_name,
        get_radar_bot_radar_user_id,
    )

    name = get_radar_bot_name()
    if not name:
        return {"skipped": "RADAR_BOT_NAME not set"}
    token = TELEGRAM_TOKENS.get(name)
    if not token:
        return {"skipped": f"no TELEGRAM_TOKEN for {name}"}

    allowed = get_radar_bot_allowed_users()
    if not allowed:
        # Без allowlist приёмник никому не отвечает (молчит чужим) — не поллим
        # вовсе, чтобы зря не тревожить общий бот. Владелец задаёт свой tg-id.
        return {"skipped": "RADAR_BOT_ALLOWED_USERS empty"}

    from modules.bulletin_heartbeat import _redis

    r = _redis()
    if r is None:
        return {"skipped": "no redis (offset persistence required)"}
    offset_key = "setka:radar_bot_offset"

    def offset_get():
        v = r.get(offset_key)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def offset_set(v):
        r.set(offset_key, int(v))

    radar_user_id = get_radar_bot_radar_user_id()

    async def add_channel(username):
        from database.connection import AsyncSessionLocal
        from modules.radar.sources.tg import resolve_source
        from modules.radar.subscriptions import upsert_subscription

        try:
            meta = await resolve_source(username)
        except Exception as e:  # noqa: BLE001 - канал недоступен/приватный
            return {"status": "error", "error": str(e)[:160]}
        async with AsyncSessionLocal() as session:
            res = await upsert_subscription(
                session, user_id=radar_user_id, source_type="tg", meta=meta
            )
        return {
            "status": "added" if res["created"] else "exists",
            "title": meta.get("title"),
        }

    async def link_account(code, chat_id, display_name):
        from modules.radar.account_link import link_telegram

        return await link_telegram(code, chat_id, display_name=display_name, bot_name=name)

    from modules.radar.bot_intake import poll_radar_bot_once

    return await poll_radar_bot_once(
        token=token,
        allowed_users=allowed,
        add_channel=add_channel,
        offset_get=offset_get,
        offset_set=offset_set,
        link_account=link_account,
        long_poll=long_poll,
    )
