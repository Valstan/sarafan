"""Celery-таски контент-радара (Ф0.2): fan-out поллер + watchdog #018."""

from __future__ import annotations

import logging
from datetime import datetime

from tasks.celery_app import app
from utils.celery_asyncio import run_coro

logger = logging.getLogger(__name__)


@app.task(name="tasks.radar_tasks.poll_radar_sources")
def poll_radar_sources():
    """Поллинг всех активных подписанных источников радара (каждые 10 мин).

    Источник поллится один раз на всех подписчиков (fan-out); дедуп — на
    уровне БД (uniq source_id+external_id). Успешный прогон пишет heartbeat
    ``setka:radar_last_polled``.
    """
    try:
        from modules.radar.poller import poll_all_sources

        result = run_coro(poll_all_sources())
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"poll_radar_sources failed: {e}", exc_info=True)
        return {
            "success": False,
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }


@app.task(name="tasks.radar_tasks.cleanup_old_radar_items")
def cleanup_old_radar_items():
    """Ретенция ленты радара: элементы старше N дней удаляются (03:20).

    Сохранёнки не страдают: radar_saved — снимок контента, его item_id
    гаснет в NULL (FK ON DELETE SET NULL). Порог — env
    RADAR_ITEMS_RETENTION_DAYS (дефолт 30, план Ф0).
    """
    try:
        from modules.radar.poller import cleanup_old_items

        result = run_coro(cleanup_old_items())
        logger.info("radar items cleanup: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"cleanup_old_radar_items failed: {e}", exc_info=True)
        return {
            "success": False,
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }


async def _run_radar_bot():
    """Один тик intake-бота «Карман»: getUpdates → добавить каналы из форвардов.

    Сборка зависимостей живёт в ``modules.radar.intake_runners``: тот же тик
    крутит демон ``setka-radar-intake``, которому нельзя импортировать Celery.
    """
    from modules.radar.intake_runners import telegram_intake_once

    return await telegram_intake_once()


@app.task(name="tasks.radar_tasks.poll_radar_bot")
def poll_radar_bot():
    """Intake-бот «Карман»: приём форвардов каналов (каждую минуту).

    Гейт #008: no-op, пока не заданы RADAR_BOT_NAME + токен. Форвард поста канала
    боту → канал добавляется в радар + подписка оператора (RADAR_BOT_RADAR_USER_ID).
    """
    try:
        result = run_coro(_run_radar_bot())
        if not result.get("skipped"):
            logger.info("radar intake-bot: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"poll_radar_bot failed: {e}", exc_info=True)
        return {
            "success": False,
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }


async def _run_vk_intake():
    """Один тик VK-интейка: Bots Long Poll сообщества → привязка VK-лички по коду.

    Сборка зависимостей — в ``modules.radar.intake_runners`` (её же берёт демон
    ``setka-radar-intake``, который импортировать Celery не может).
    """
    from modules.radar.intake_runners import vk_intake_once

    return await vk_intake_once()


@app.task(name="tasks.radar_tasks.poll_radar_vk_intake")
def poll_radar_vk_intake():
    """VK-интейк радара: приём кодов привязки VK-лички (каждую минуту).

    No-op, пока не задан RADAR_VK_COMMUNITY_ID + community-токен (#008). Юзер пишет
    код сообществу-точке → ловим его vk_id через Bots Long Poll → vk_dm-вывод.
    """
    try:
        result = run_coro(_run_vk_intake())
        if not result.get("skipped"):
            logger.info("radar vk-intake: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"poll_radar_vk_intake failed: {e}", exc_info=True)
        return {
            "success": False,
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }


@app.task(name="tasks.radar_tasks.check_radar_poll_heartbeat")
def check_radar_poll_heartbeat():
    """Watchdog: алёрт в Telegram, если поллер радара молчит при живых подписках."""
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules.radar.poller import maybe_alert_stale_radar_poll

        token = TELEGRAM_TOKENS.get("VALSTANBOT") or TELEGRAM_TOKENS.get("ALERT")
        status = run_coro(
            maybe_alert_stale_radar_poll(
                telegram_token=token,
                chat_id=TELEGRAM_ALERT_CHAT_ID,
            )
        )
        logger.info("radar poll watchdog: %s", status)
        return {
            "success": True,
            "status": status,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"check_radar_poll_heartbeat failed: {e}", exc_info=True)
        return {
            "success": False,
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
        }
