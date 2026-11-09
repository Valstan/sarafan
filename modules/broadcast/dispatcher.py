"""Сетевая рассылка — диспетчер-публикатор (директива brain 2026-06-14).

Беат раз в минуту берёт кампании, у которых наступило время прогона
(``status='scheduled'`` ∧ ``next_run_at<=now`` ∧ ``runs_done<repeat_count``), и
публикует ``wall.post`` **немедленно** в каждую цель — НЕ в VK-отложку (канон
владельца: управление в программе).

Гарантии:
- **идемпотентность** под конкурентным беатом: per-(цель, прогон) защёлка
  ``broadcast_publications`` — claim через ``INSERT … ON CONFLICT DO NOTHING``
  ПЕРЕД публикацией и commit сразу, поэтому один пост в одну цель за прогон один
  раз (UNIQUE-индекс сериализует конкурентов);
- **throttle ≥5с** между реальными постами (анти-Captcha, probe-проверено на
  krugozor: 16@5с = 16/16 без капчи, бурст ловит капчу);
- **per-target изоляция**: ошибка в одну цель не валит остальные;
- **повтор**: после полного прогона ``runs_done++`` и перенос ``next_run_at`` на
  ``repeat_interval_hours`` вперёд, пока не исчерпан ``repeat_count`` → ``done``;
- **watchdog #018**: алёрт, если есть просроченные кампании (диспетчер молча встал).

Время: ``scheduled_at``/``next_run_at`` — МСК wall-clock naive (как
``AdScheduledPost.publish_date``); сравниваем с МСК-now. ``published_at`` — UTC.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database.models import BroadcastCampaign, BroadcastPublication, BroadcastTarget

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
TERMINAL = ("published", "error")

HEARTBEAT_KEY = "setka:broadcast_last_dispatch"
_HEARTBEAT_TTL = 14 * 24 * 3600
COOLDOWN_KEY = "setka:broadcast_dispatch_stale_cooldown"
ALERT_COOLDOWN_SECONDS = 6 * 3600
# Кампания «просрочена», если next_run_at позади более чем на это (диспетчер
# обязан был её взять). Запас на длинный прогон (16 целей @5с ≈ 80с).
OVERDUE_GRACE_SECONDS = 15 * 60
# 'pending'-строка старше этого = процесс умер между claim и записью результата
# (рестарт mid-run). Сильно больше максимального прогона (~80с) — реклеймим в error.
STALE_PENDING_SECONDS = 10 * 60


def _now_msk() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None)


def _redis():
    from modules.bulletin_heartbeat import _redis as _r

    return _r()


def touch_heartbeat(*, ts: Optional[float] = None) -> None:
    """Записать heartbeat «диспетчер живой» (best-effort, не падает)."""
    try:
        client = _redis()
        if client is None:
            logger.warning("broadcast heartbeat skipped: redis unavailable")
            return
        client.setex(HEARTBEAT_KEY, _HEARTBEAT_TTL, str(int(ts if ts is not None else time.time())))
    except Exception:  # pragma: no cover - наблюдаемость не валит публикацию
        logger.warning("broadcast heartbeat write failed", exc_info=True)


def compute_reschedule(
    *,
    runs_done: int,
    repeat_count: int,
    next_run_at: Optional[datetime],
    interval_hours: float,
    now: datetime,
):
    """После завершённого прогона → новые (runs_done, status, next_run_at). Чистая.

    Абсолютное значение (``runs_done+1``, а не in-place ++) — два конкурентных
    прогона вычислят одно и то же, double-increment невозможен."""
    new_runs = int(runs_done) + 1
    if new_runs >= int(repeat_count):
        return new_runs, "done", next_run_at
    base = next_run_at or now
    return new_runs, "scheduled", base + timedelta(hours=float(interval_hours or 0))


def _parse_attachments_map(raw: Optional[str]) -> Dict[int, str]:
    """Распарсить кэш ``campaign.attachments`` → ``{abs(group_id): 'photo..,photo..'}``.

    Формат — JSON-map (per-group: у каждой цели свои attachment'ы, владелец фото =
    эта группа). Пустая / legacy-строка (старый формат «одна строка на все») / битый
    JSON → ``{}`` (картинки перельются при следующей правке кампании, сбрасывающей
    кэш). Значение ``"{}"`` (заливка не дала ничего) тоже даёт ``{}`` — текст-онли."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[int, str] = {}
    for k, v in data.items():
        try:
            if v:
                out[abs(int(k))] = str(v)
        except (ValueError, TypeError):
            continue
    return out


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _claim(session, campaign_id: int, group_id: int, run_index: int) -> bool:
    """Атомарно заклеймить (campaign, group, run). True — мы первыми."""
    stmt = (
        pg_insert(BroadcastPublication)
        .values(campaign_id=campaign_id, group_id=group_id, run_index=run_index, status="pending")
        .on_conflict_do_nothing(index_elements=["campaign_id", "group_id", "run_index"])
    )
    res = await session.execute(stmt)
    return (res.rowcount or 0) == 1


async def _record_result(
    session, *, campaign_id: int, group_id: int, run_index: int, res: Dict[str, Any]
) -> bool:
    """Записать исход публикации в заклеймленную строку. Возвращает ok."""
    res = res or {}
    ok = bool(res.get("success"))
    if ok:
        values = dict(
            status="published",
            vk_post_id=res.get("post_id"),
            post_url=res.get("url"),
            error=None,
            published_at=datetime.utcnow(),
        )
    else:
        values = dict(
            status="error",
            error=str(res.get("error") or "publish failed")[:500],
            published_at=datetime.utcnow(),
        )
    await session.execute(
        sa_update(BroadcastPublication)
        .where(
            BroadcastPublication.campaign_id == campaign_id,
            BroadcastPublication.group_id == group_id,
            BroadcastPublication.run_index == run_index,
        )
        .values(**values)
    )
    return ok


async def dispatch_campaign(
    session,
    campaign: BroadcastCampaign,
    *,
    publish: Callable[[int, str, Optional[List[str]]], Any],
    build_attachments: Optional[Callable] = None,
    interval: float = 0.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Один прогон кампании: публикация во все цели + перенос расписания."""
    now = now or _now_msk()
    run_index = int(campaign.runs_done)

    targets = (
        (
            await session.execute(
                select(BroadcastTarget).where(BroadcastTarget.campaign_id == campaign.id)
            )
        )
        .scalars()
        .all()
    )
    if not targets:
        campaign.status = "done"  # слать некуда → закрываем, чтобы не висела
        await session.commit()
        return {"campaign_id": campaign.id, "targets": 0, "published": 0, "skipped": "no_targets"}

    existing = (
        (
            await session.execute(
                select(BroadcastPublication).where(
                    BroadcastPublication.campaign_id == campaign.id,
                    BroadcastPublication.run_index == run_index,
                )
            )
        )
        .scalars()
        .all()
    )
    # Реклейм зависшего 'pending' (процесс умер между claim и записью результата —
    # рестарт/деплой mid-run): помечаем error (терминально), чтобы прогон мог
    # завершиться и кампания не зависла навечно (иначе done_groups скрывает цель,
    # а terminal_groups её не считает → completion недостижим, +вечный watchdog).
    # НЕ перепубликовываем (статус публикации неизвестен → избегаем дубля); цель
    # видна как error, оператор при желании дожмёт через retry.
    stale_cut = datetime.utcnow() - timedelta(seconds=STALE_PENDING_SECONDS)
    reclaimed = False
    for p in existing:
        if p.status == "pending" and p.published_at and p.published_at < stale_cut:
            await session.execute(
                sa_update(BroadcastPublication)
                .where(BroadcastPublication.id == p.id)
                .values(
                    status="error", error="stuck pending (рестарт?) — статус публикации неизвестен"
                )
            )
            p.status = "error"
            reclaimed = True
    if reclaimed:
        await session.commit()
    done_groups = {int(p.group_id) for p in existing}
    terminal_groups = {int(p.group_id) for p in existing if p.status in TERMINAL}

    # Медиа: залить картинки ПО КАЖДОЙ цели (владелец фото = эта группа) и
    # кэшировать карту {gid: 'photo..'} в JSON. Переиспользовать одну строку на все
    # цели НЕЛЬЗЯ — owner-mismatch, VK дропает вложение (см.
    # vk_wall_photo_upload). Грузим user-токеном: community-токен → error 27. "{}"
    # (пустую карту) кэшируем тоже, чтобы не дёргать заливку каждый прогон.
    if campaign.attachments is None and (campaign.image_names or []):
        built: Dict[int, str] = {}
        if build_attachments is not None:
            built = await _maybe_await(build_attachments(campaign, targets)) or {}
        campaign.attachments = json.dumps({str(k): v for k, v in built.items()})
        await session.commit()
    att_map = _parse_attachments_map(campaign.attachments)
    text = campaign.body or ""

    published = 0
    posted_any = False
    for t in targets:
        gid = int(t.group_id)
        if gid in done_groups:
            continue
        if not await _claim(session, campaign.id, gid, run_index):
            await session.commit()  # ON CONFLICT no-op — снять лок/закрыть txn
            continue
        await session.commit()  # claim виден конкурентам, лок UNIQUE-индекса снят
        if posted_any and interval > 0:
            await asyncio.sleep(interval)  # throttle между реальными постами
        posted_any = True
        att_str = att_map.get(abs(gid))
        attachments = att_str.split(",") if att_str else None
        try:
            res = await _maybe_await(publish(gid, text, attachments))
        except Exception as e:  # noqa: BLE001 — per-target изоляция
            logger.warning("broadcast publish failed c=%s g=%s: %s", campaign.id, gid, e)
            res = {"success": False, "error": str(e)[:500]}
        ok = await _record_result(
            session, campaign_id=campaign.id, group_id=gid, run_index=run_index, res=res
        )
        await session.commit()
        terminal_groups.add(gid)
        if ok:
            published += 1

    complete = len(terminal_groups) >= len(targets)
    if complete:
        new_runs, new_status, new_next = compute_reschedule(
            runs_done=campaign.runs_done,
            repeat_count=campaign.repeat_count,
            next_run_at=campaign.next_run_at,
            interval_hours=campaign.repeat_interval_hours,
            now=now,
        )
        campaign.runs_done = new_runs
        campaign.status = new_status
        campaign.next_run_at = new_next
        await session.commit()

    return {
        "campaign_id": campaign.id,
        "targets": len(targets),
        "published": published,
        "run_index": run_index,
        "complete": complete,
    }


async def _default_publisher(session) -> Callable[[int, str, Optional[List[str]]], Awaitable]:
    """Публикатор по умолчанию — один на прогон (как krugozor), под токен-полиси."""
    from modules.publisher.vk_publisher_extended import VKPublisher

    publisher = await VKPublisher.create_with_policy(session, target_group_id=None)

    async def _pub(group_id: int, text: str, attachments: Optional[List[str]]):
        return await publisher.publish_bulletin(
            group_id=group_id, text=text, attachments=attachments
        )

    return _pub


async def _default_build_attachments(campaign, targets) -> Dict[int, str]:
    """Залить картинки кампании на стену КАЖДОЙ цели → ``{abs(group_id): 'photo..'}``.

    Грузим **user-токеном владельца группы**: VK запрещает
    ``photos.getWallUploadServer`` под community-auth (error 27 — probe
    ``scripts/probe_wall_upload_token.py`` 2026-06-19: 16/16 user-ok, 0/16
    community), а фото администратора community-токен в записи не принимает —
    молча (``docs/ops/photo-upload-path.md``). Фото грузится в контекст КАЖДОЙ
    группы (owner = эта группа) — одну строку на все цели переиспользовать нельзя
    (owner-mismatch → VK дропает фото).
    Per-target изоляция: сбой заливки в одну цель не валит остальные (та уйдёт
    текстом — graceful degrade)."""
    from modules.broadcast.service import broadcast_image_paths

    paths = broadcast_image_paths(campaign.image_names or [])
    if not paths:
        return {}
    try:
        from modules.vk_token_router import load_photo_upload_token

        # Заливает аккаунт-владелец группы: community-токен залить не может
        # ([27]), а фото администратора community-токен в записи не принимает —
        # молча (docs/ops/photo-upload-path.md).
        photo_token = await load_photo_upload_token()
    except Exception as e:  # pragma: no cover - инфраструктурный сбой
        logger.warning("broadcast: load_photo_upload_token failed: %s", e)
        return {}
    if not photo_token:
        logger.warning("broadcast: нет токена владельца для заливки → посты уйдут текстом")
        return {}

    import vk_api

    from modules.publisher.vk_wall_photo_upload import upload_wall_images

    api = vk_api.VkApi(token=photo_token).get_api()
    images = [p.read_bytes() for p in paths]
    out: Dict[int, str] = {}
    for t in targets:
        gid = abs(int(t.group_id))
        try:
            atts = upload_wall_images(api, images, group_id=gid)
        except Exception as e:  # pragma: no cover - сеть/VK; per-target изоляция
            logger.warning("broadcast attachment build failed g=%s: %s", gid, e)
            continue
        if atts:
            out[gid] = ",".join(atts)
    return out


async def run_broadcast_dispatch(
    *,
    session_factory: Optional[Callable] = None,
    publish: Optional[Callable] = None,
    build_attachments: Optional[Callable] = None,
    interval: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Тик диспетчера: опубликовать все назревшие кампании. Возвращает сводку."""
    from config.runtime import broadcast_disabled, get_broadcast_post_interval_seconds

    if broadcast_disabled():
        return {"skipped": "disabled", "dispatched": 0}
    if session_factory is None:
        from database.connection import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if now is None:
        now = _now_msk()
    if interval is None:
        interval = get_broadcast_post_interval_seconds()

    results: List[Dict[str, Any]] = []
    async with session_factory() as session:
        due = (
            (
                await session.execute(
                    select(BroadcastCampaign).where(
                        BroadcastCampaign.status == "scheduled",
                        BroadcastCampaign.next_run_at.isnot(None),
                        BroadcastCampaign.next_run_at <= now,
                        BroadcastCampaign.runs_done < BroadcastCampaign.repeat_count,
                    )
                )
            )
            .scalars()
            .all()
        )
        if due:
            if publish is None:
                publish = await _default_publisher(session)
            if build_attachments is None:
                build_attachments = _default_build_attachments
        for camp in due:
            try:
                results.append(
                    await dispatch_campaign(
                        session,
                        camp,
                        publish=publish,
                        build_attachments=build_attachments,
                        interval=interval,
                        now=now,
                    )
                )
            except Exception:  # noqa: BLE001 — изолируем сбой одной кампании
                logger.exception("broadcast dispatch failed for campaign %s", camp.id)
                try:
                    await session.rollback()
                except Exception:  # pragma: no cover
                    pass

    touch_heartbeat()
    return {"dispatched": len(results), "campaigns": results}


async def _has_overdue_campaigns(
    now: datetime, *, session_factory: Optional[Callable] = None
) -> bool:
    """Есть ли просроченные scheduled-кампании (next_run_at < now - grace)?"""
    if session_factory is None:
        from database.connection import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    cutoff = now - timedelta(seconds=OVERDUE_GRACE_SECONDS)
    async with session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(BroadcastCampaign)
                .where(
                    BroadcastCampaign.status == "scheduled",
                    BroadcastCampaign.next_run_at.isnot(None),
                    BroadcastCampaign.next_run_at < cutoff,
                    BroadcastCampaign.runs_done < BroadcastCampaign.repeat_count,
                )
            )
        ).scalar() or 0
    return int(count) > 0


async def maybe_alert_stale_broadcast(
    *,
    telegram_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
    now: Optional[datetime] = None,
    session_factory: Optional[Callable] = None,
) -> str:
    """Watchdog #018: алёрт, если есть просроченные кампании (диспетчер встал).

    Нет просроченных = не инцидент (нет кампаний/всё разослано → молчим). С
    cooldown, чтобы не спамить. Возвращает статус-строку."""
    now = now or _now_msk()
    if not await _has_overdue_campaigns(now, session_factory=session_factory):
        return "no-overdue"
    if not telegram_token or not chat_id:
        return "skipped:no-telegram-config"

    client = _redis()
    try:
        if client is not None and client.get(COOLDOWN_KEY):
            return "skipped:cooldown"
    except Exception:  # pragma: no cover
        pass

    last = None
    try:
        if client is not None:
            v = client.get(HEARTBEAT_KEY)
            last = int(v) if v else None
    except Exception:  # pragma: no cover
        last = None
    hb = f"{(time.time() - last) / 3600:.1f} ч назад" if last else "никогда (диспетчер не писал)"
    message = (
        "⚠️ <b>SETKA: сетевая рассылка встала</b>\n\n"
        "Есть запланированные кампании, чьё время публикации давно прошло, а "
        "диспетчер их не разослал.\n"
        f"Последний тик диспетчера: <b>{hb}</b>.\n\n"
        "Проверь: <code>systemctl status setka-celery-beat setka-celery-worker</code>."
    )
    if dashboard_url:
        message += f"\n🔗 <a href='{dashboard_url}'>Открыть SETKA</a>"

    try:
        from modules import telegram_http as tg_http

        resp = tg_http.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            logger.warning("stale-broadcast alert failed: %s %s", resp.status_code, resp.text[:200])
            return "error:http-" + str(resp.status_code)
        if client is not None:
            client.setex(COOLDOWN_KEY, ALERT_COOLDOWN_SECONDS, "1")
        logger.info("Sent stale-broadcast alert")
        return "alert-sent"
    except Exception as exc:  # pragma: no cover - сеть
        logger.error("Failed to send stale-broadcast alert: %s", exc)
        return "error:" + str(exc)
