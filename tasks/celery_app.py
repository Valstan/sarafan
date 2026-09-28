"""
Celery Application

Главное приложение Celery для автоматизации SETKA.

Tasks:
- run_vk_monitoring: Запуск production workflow каждый час
- create_daily_bulletin: Создание сводки за день (18:00)
- cleanup_old_posts: Очистка старых постов (03:00)

Запуск:
    # Worker
    celery -A tasks.celery_app worker --loglevel=info

    # Beat scheduler
    celery -A tasks.celery_app beat --loglevel=info
"""

# Добавляем корневую директорию в PYTHONPATH — должно быть ДО любых проектных
# импортов, иначе flake8 ругается E402 на нижестоящие `from utils...` / `from
# config...`. Делаем самый минимум на верху, остальные импорты — ниже.
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta

from celery import Celery, signals
from celery.schedules import crontab

from utils.celery_asyncio import run_coro
from utils.json_logging import configure_json_logging

# Setup logging
# Plain-text по умолчанию (формат как раньше). При env LOG_FORMAT=json форматтер
# переустанавливается на структурированный JSON (см. utils/json_logging.py) —
# удобно грепать/парсить логи worker'а на инцидентах. Повторно дёргаем из
# worker_ready-хука ниже, т.к. Celery переинициализирует логгеры при старте.
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s - %(message)s")
configure_json_logging()
logger = logging.getLogger(__name__)

# Восстановление REQUIRED-секретов (DATABASE_URL/REDIS_URL) из vault КАРМАНа
# ДО импорта config.runtime: worker/beat стартуют из этой точки, и потеря
# /etc/setka/setka.env иначе убьёт процесс на `_require` при первом же
# импорте таска. В норме — no-op (все ключи на месте, ноль сетевых вызовов).
from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402
from utils.log_redaction import install_log_redaction  # noqa: E402

bootstrap_secrets()  # noqa: E402

# Маскирование секретов в логах — строго ПОСЛЕ bootstrap: значения секретов
# снимаются с окружения в момент установки, а из комнаты КАРМАНа они приезжают
# выше. Инцидент 2026-08-12 (угон Telegram-ботов) начался с токена, попавшего
# в celery-worker.log из текста исключения requests.
install_log_redaction()  # noqa: E402

# --- Telegram alerts for Notifications ---


def _pick_telegram_bot_token(telegram_tokens: dict) -> str | None:
    # Prefer historically used names, then fall back to any configured token.
    for key in ("VALSTANBOT", "ALERT", "AFONYA"):
        token = telegram_tokens.get(key)
        if token:
            return token
    # Any token is better than none
    return next(iter(telegram_tokens.values()), None)


def _compute_notifications_signature(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _maybe_send_telegram_notifications_alert() -> None:
    """
    Send Telegram alert if there are any notifications and the payload is NEW.

    Зовут ВСЕ ТРИ часовые проверки (предложка :15, сообщения :24, комментарии
    :57). «Последней в цепочке», от которой алёрт уходил раньше, больше нет:
    тройку разнесли по минутам ради памяти (2026-09-22), и привязка к одной
    задаче означала бы, что находка предложки ждёт оповещения 40 минут.

    Дублей от этого не будет: ниже стоит дедуп по sha1 полного payload'а из
    Redis — если с прошлого раза ничего не изменилось, сообщение не уходит.
    Три оповещения за час случатся ровно тогда, когда все три проверки нашли
    новое, то есть когда их и хочется получить.
    """
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules import telegram_http as tg_http
        from modules.notifications.storage import NotificationsStorage

        storage = NotificationsStorage()
        data = storage.get_all_notifications()

        # Nothing to notify about.
        if (data.get("total_count") or 0) <= 0:
            return

        bot_token = _pick_telegram_bot_token(TELEGRAM_TOKENS)
        chat_id = TELEGRAM_ALERT_CHAT_ID
        if not bot_token or not chat_id:
            logger.warning("Telegram credentials not configured; skipping notifications alert")
            return

        # Dedupe: do not spam the same alert every hour.
        signature_payload = {
            "suggested_posts": data.get("suggested_posts", []),
            "unread_messages": data.get("unread_messages", []),
            "recent_comments": data.get("recent_comments", []),
        }
        signature = _compute_notifications_signature(signature_payload)
        last_sig_key = f"{storage.key_prefix}:last_telegram_signature"
        last_sig = storage.redis_client.get(last_sig_key)
        if last_sig == signature:
            return
        storage.redis_client.setex(last_sig_key, 86400, signature)

        suggested = data.get("suggested_posts") or []
        messages = data.get("unread_messages") or []
        comments = data.get("recent_comments") or []

        # Build a compact message (HTML).
        lines: list[str] = []
        lines.append("<b>📬 Новые уведомления SETKA</b>")
        lines.append("")
        lines.append(f"📝 Предложенных постов: <b>{len(suggested)}</b>")
        for n in suggested[:5]:
            name = n.get("region_name", "?")
            cnt = n.get("suggested_count", 0)
            url = n.get("url", "")
            if url:
                lines.append(f"  • {name}: {cnt} — <a href='{url}'>проверить</a>")
            else:
                lines.append(f"  • {name}: {cnt}")
        if len(suggested) > 5:
            lines.append(f"  …и ещё {len(suggested) - 5} регион(ов)")

        lines.append("")
        lines.append(f"💬 Непрочитанных сообщений: <b>{len(messages)}</b>")
        for n in messages[:5]:
            name = n.get("region_name", "?")
            cnt = n.get("unread_count", 0)
            url = n.get("url", "")
            if url:
                lines.append(f"  • {name}: {cnt} — <a href='{url}'>открыть</a>")
            else:
                lines.append(f"  • {name}: {cnt}")
        if len(messages) > 5:
            lines.append(f"  …и ещё {len(messages) - 5} регион(ов)")

        lines.append("")
        lines.append(f"💭 Комментариев за сутки: <b>{len(comments)}</b>")
        for c in comments[:5]:
            name = c.get("region_name", "?")
            text = (c.get("text") or "").strip().replace("<", "&lt;").replace(">", "&gt;")
            post_url = c.get("post_url", "")
            preview = (text[:120] + "…") if len(text) > 120 else text
            if post_url:
                lines.append(f"  • {name}: {preview} — <a href='{post_url}'>пост</a>")
            else:
                lines.append(f"  • {name}: {preview}")
        if len(comments) > 5:
            lines.append(f"  …и ещё {len(comments) - 5} комментариев")

        message = "\n".join(lines)

        resp = tg_http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            logger.warning(f"Telegram sendMessage failed: {resp.status_code} {resp.text[:300]}")
        else:
            logger.info("Telegram notifications alert sent")

    except Exception as e:
        logger.warning(f"Failed to send Telegram notifications alert: {e}")


# Создаем Celery app
# IMPORTANT: keep a single Celery runtime and explicitly include tasks that are scheduled by beat.
app = Celery(
    "setka",
    include=[
        "tasks.parsing_tasks",
        "tasks.parsing_scheduler_tasks",  # Postopus migration
        "tasks.discovery_tasks",  # community discovery + weekly recheck
        "tasks.radar_tasks",  # content radar: fan-out source poller (Ф0.2)
        "tasks.vk_bot_tasks",  # ВК-бот кабинета: Long Poll сообщества САРАФАН
        "tasks.broadcast_tasks",  # сетевая рассылка: диспетчер-публикатор (brain 2026-06-14)
        "tasks.promo_tasks",  # раскрутка молодых сообществ (заказ владельца 2026-08-28)
    ],
)
app.config_from_object("config.celery_config")


# При выставленной ``PROMETHEUS_MULTIPROC_DIR`` worker пишет метрики в shared
# mmap-файл с именем по своему PID. Если процесс умер и mmap не подчистить,
# его counter'ы продолжат участвовать в агрегациях через MultiProcessCollector.
# ``mark_process_dead`` исключает PID из выдачи. Защита идемпотентна — если
# env-var не выставлена, prometheus_client поднимет ValueError, мы его глотаем.
@signals.worker_shutdown.connect  # type: ignore[has-type]
def _setka_mark_prom_process_dead(**_kwargs) -> None:
    import os

    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(os.getpid())
    except Exception:
        logger.debug("prometheus mark_process_dead failed", exc_info=True)


# Celery переинициализирует логирование при старте worker'а (хватает root-логгер
# через свой ``setup_logging`` / ``--loglevel``), затирая форматтер, выставленный
# на import-е модуля. Переустанавливаем JSON-форматтер уже после готовности
# worker'а, чтобы LOG_FORMAT=json реально действовал на его выводе.
@signals.worker_process_init.connect  # type: ignore[has-type]
def _setka_redact_logs_in_child(**_kwargs) -> None:
    """Маскирование секретов — в КАЖДОМ дочернем процессе prefork-пула.

    Инцидент 2026-09-02: в celery-worker.log нашлось 2834 строки httpx
    «HTTP Request: GET …access_token=<ключ сообщества>…» из ForkPoolWorker-N,
    хотя фабрика ставится на импорте и на worker_ready. В свежем процессе тот
    же код маскирует (проверено на боксе), то есть в детях пула фабрика не
    доживает до задач. worker_process_init срабатывает уже в ребёнке —
    ставим заново (идемпотентно).
    """
    try:
        install_log_redaction()
    except Exception:
        logger.debug("install_log_redaction (child) failed", exc_info=True)


@signals.worker_ready.connect  # type: ignore[has-type]
def _setka_apply_json_logging(**_kwargs) -> None:
    try:
        if configure_json_logging():
            logger.info("JSON logging enabled for Celery worker (LOG_FORMAT=json)")
    except Exception:
        logger.debug("configure_json_logging failed", exc_info=True)
    # Идемпотентно: если фабрика уже стоит с import-а — no-op. Повтор здесь на
    # случай, если чей-то setup_logging успел подменить фабрику записей.
    try:
        install_log_redaction()
    except Exception:
        logger.debug("install_log_redaction failed", exc_info=True)


# ---------------------------------------------------------------------------
# Прибор памяти: КАКАЯ задача поднимает потолок RSS дочернего процесса.
#
# Зачем он вообще. По P164 ядро убивало воркер примерно раз в сутки на 390–518
# МБ, причём в `journalctl` этих убийств нет ни одного — они видны только в
# `dmesg`. Оба порога переработки ребёнка (по числу задач и по памяти,
# `config/celery_config.py`) лечат НАКОПЛЕНИЕ, но виновника не называют: до сих
# пор про течь известно единственное — окно HH:17–19, то есть догадка по
# соседству с расписанием. Строкой в логе вопрос решается, состязательным
# чтением кода — нет.
#
# Что именно печатается. `ru_maxrss` — ПИК за жизнь процесса, и он не убывает.
# Поэтому «дельта» здесь читается как «эта задача подняла потолок», а не «эта
# задача столько занимает прямо сейчас». Для поиска того, кто растит память,
# нужен именно потолок; мгновенный снимок показал бы шум.
#
# Печатаем только при РОСТЕ потолка — иначе выше порога пола строка повторяла
# бы одно и то же число после каждой задачи. `ru_maxrss` монотонен, значит
# число строк на одного ребёнка ограничено числом настоящих ступеней роста.
# ---------------------------------------------------------------------------
_MEM_PROBE_STEP_KB = 32 * 1024  # ступень роста, с которой пишем в лог
_MEM_PROBE_FLOOR_KB = 300 * 1024  # …а выше этого пика пишем любой рост
_last_peak_rss_kb = 0


@signals.task_postrun.connect  # type: ignore[has-type]
def _setka_log_peak_rss(task=None, **_kwargs) -> None:
    """Пик RSS после каждой задачи + сборка мусора.

    Прибор не имеет права уронить задачу: всё внутри одного ``try``, отказ
    уходит в debug. ``resource`` есть только на POSIX — на Windows-машине
    разработчика импорт упадёт и хук промолчит.
    """
    global _last_peak_rss_kb
    try:
        import gc
        import resource

        # Циклы держит персистентный event loop (utils/celery_asyncio), и на
        # ~300 задачах в час полная сборка — это шум по времени.
        gc.collect()

        # На Linux ru_maxrss в килобайтах (на macOS — в байтах; прод Ubuntu).
        peak_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        delta_kb = peak_kb - _last_peak_rss_kb
        if peak_kb > _last_peak_rss_kb and (
            delta_kb >= _MEM_PROBE_STEP_KB or peak_kb >= _MEM_PROBE_FLOOR_KB
        ):
            logger.info(
                "mem: task=%s peak_rss_mb=%d delta_mb=%d",
                getattr(task, "name", None) or "unknown",
                peak_kb // 1024,
                delta_kb // 1024,
            )
        if peak_kb > _last_peak_rss_kb:
            _last_peak_rss_kb = peak_kb
    except Exception:
        logger.debug("peak rss probe failed", exc_info=True)


@app.task(name="tasks.celery_app.run_vk_monitoring")
def run_vk_monitoring():
    """
    Запуск production workflow для мониторинга VK.

    Выполняется каждый час в :05 минут.
    Сканирует все активные регионы, применяет фильтры,
    делает AI scoring и создает сводки.
    """
    logger.info("=" * 80)
    logger.info("Starting VK monitoring workflow...")
    logger.info("=" * 80)

    try:
        from scripts.run_production_workflow import ProductionWorkflow

        # Создаем и запускаем workflow
        workflow = ProductionWorkflow()
        result = run_coro(workflow.run())

        logger.info("VK monitoring completed successfully!")
        logger.info(f"Result: {result}")

        return {"success": True, "timestamp": datetime.now().isoformat(), "result": result}

    except Exception as e:
        logger.error(f"VK monitoring failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.create_daily_bulletin")
def create_daily_bulletin():
    """
    Создание дневной сводки для всех регионов.

    Выполняется каждый день в 18:00.
    Собирает топ-посты за день, создает сводки,
    готовит к публикации.
    """
    logger.info("=" * 80)
    logger.info("Creating daily bulletin...")
    logger.info("=" * 80)

    try:
        from sqlalchemy import and_, select

        from database.connection import AsyncSessionLocal
        from database.models import Post, Region
        from modules.aggregation.aggregator import NewsAggregator

        async def create_bulletin():
            async with AsyncSessionLocal() as session:
                # Получаем все активные регионы
                result = await session.execute(select(Region).where(Region.is_active.is_(True)))
                regions = list(result.scalars())

                aggregator = NewsAggregator(session)
                bulletins = []

                # Создаем сводка для каждого региона
                for region in regions:
                    logger.info(f"Creating bulletin for {region.name}...")

                    # Получаем посты за последние 24 часа
                    cutoff_time = datetime.now() - timedelta(hours=24)
                    posts_result = await session.execute(
                        select(Post)
                        .where(
                            and_(
                                Post.region_id == region.id,
                                Post.date_published >= cutoff_time,
                                Post.ai_analyzed.is_(True),
                            )
                        )
                        .order_by(Post.ai_score.desc())
                        .limit(10)
                    )
                    posts = list(posts_result.scalars())

                    if not posts:
                        logger.warning(f"No posts found for {region.name}")
                        continue

                    # Создаем сводка
                    bulletin = await aggregator.create_bulletin(
                        posts=posts, region=region, max_posts=5
                    )

                    if bulletin:
                        bulletins.append(
                            {
                                "region": region.name,
                                "posts_count": len(bulletin.source_posts),
                                "total_views": bulletin.total_views,
                                "text_length": len(bulletin.aggregated_text),
                            }
                        )
                        logger.info(
                            f"Bulletin created for {region.name}: "
                            f"{len(bulletin.source_posts)} posts"
                        )

                return bulletins

        bulletins = run_coro(create_bulletin())

        logger.info(f"Daily bulletin completed! Created {len(bulletins)} bulletins")

        return {"success": True, "timestamp": datetime.now().isoformat(), "bulletins": bulletins}

    except Exception as e:
        logger.error(f"Daily bulletin failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.check_suggested_posts")
def check_suggested_posts():
    """
    Проверка предложенных постов в главных группах регионов.

    Выполняется каждый час с 8:00 до 22:00.
    Проверяет все главные группы регионов (с префиксом ИНФО) на наличие
    предложенных постов от посетителей.

    Результаты сохраняются в Redis и отправляются в Telegram.
    """
    logger.info("=" * 80)
    logger.info("Checking suggested posts in region groups...")
    logger.info("=" * 80)

    try:
        from sqlalchemy import select

        from database.connection import AsyncSessionLocal
        from database.models import Region
        from modules.notifications.storage import NotificationsStorage
        from modules.notifications.vk_suggested_checker import VKSuggestedChecker
        from modules.vk_token_router import load_community_tokens, pick_healthy_read_token

        async def check():
            # Получаем все регионы с главными группами
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Region).where(
                        Region.vk_group_id.isnot(None),
                        # Уведомления должны проверяться независимо от статуса "пауза" региона.
                    )
                )
                regions = list(result.scalars())

                if not regions:
                    logger.warning("No regions with VK groups found")
                    return []

                logger.info(f"Checking {len(regions)} region groups...")

                # Подготавливаем данные для проверки
                region_groups = [
                    {
                        "region_id": r.id,
                        "region_name": r.name,
                        "region_code": r.code,
                        "vk_group_id": r.vk_group_id,
                    }
                    for r in regions
                ]

                # Проверяем предложенные посты — живой READ-токен из БД
                # (probe + self-heal; был хардкод env VALSTAN — инцидент 2026-07-12)
                cand = await pick_healthy_read_token(session)
                vk_token = cand.token if cand else None
                if not vk_token:
                    logger.error("No healthy VK READ token")
                    return []

                community_tokens = await load_community_tokens(session)
                checker = VKSuggestedChecker(vk_token, community_tokens=community_tokens)
                run_start = datetime.now()
                notifications = await checker.check_all_region_groups(region_groups)
                run_duration = (datetime.now() - run_start).total_seconds()

                # Сохраняем в Redis. keep_if_empty=True защищает от стирания
                # результата ручной проверки, если автопроверка вернула 0
                # из-за временной ошибки VK API (например, сломаны
                # community-tokens).
                storage = NotificationsStorage()
                storage.save_notifications(
                    notifications,
                    "suggested_posts",
                    keep_if_empty=True,
                )
                # История проверок (этап 3): для виджета «активность за 24ч».
                storage.save_run(
                    "suggested_posts",
                    count=len(notifications),
                    duration_seconds=run_duration,
                    success=True,
                )
                # Prometheus (etap 5)
                try:
                    from monitoring.metrics import (
                        notifications_check_duration_seconds,
                        notifications_check_total,
                        notifications_items_found_total,
                    )

                    result_label = "ok" if notifications else "empty"
                    notifications_check_total.labels(
                        check_type="suggested",
                        result=result_label,
                    ).inc()
                    notifications_check_duration_seconds.labels(
                        check_type="suggested",
                    ).observe(run_duration)
                    notifications_items_found_total.labels(
                        check_type="suggested",
                    ).inc(len(notifications))
                except Exception as _e:
                    logger.debug("metrics emit failed: %s", _e)

                logger.info(f"Found {len(notifications)} groups with suggested posts")

                return notifications

        notifications = run_coro(check())

        # Агрегированный алёрт — из каждой проверки, см. докстринг функции.
        _maybe_send_telegram_notifications_alert()

        # Сам список в результат не кладём: его никто не читает (UI берёт из
        # Redis, `web/api/notifications.py` дёргает задачу через `.delay()` и
        # результата не ждёт), а в result backend он уезжал целиком — до
        # тысяч словарей на задачу при `result_expires=3600`.
        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "notifications_count": len(notifications),
        }

    except Exception as e:
        logger.error(f"Failed to check suggested posts: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.scan_suggested_ads")
def scan_suggested_ads():
    """Скан предложки на рекламу → заявки в ad_requests (рекламный кабинет).

    Каждые 30 минут 8:00-22:00 (на 25-й минуте — офсет от notification-задач).
    Детект через AdvertisementFilter + предложка-сигналы, дедуп по
    (community_vk_id, vk_post_id). Telegram-алерт только при НОВЫХ заявках
    (new_total>0 — дедуп уже на уровне БД, повторных алертов не будет).
    """
    logger.info("=" * 80)
    logger.info("Scanning предложка for advertisements (ad cabinet)...")
    logger.info("=" * 80)

    try:
        from modules.ad_cabinet.scanner import run_scan

        result = run_coro(run_scan())
        new_total = int(result.get("new_total", 0))
        if new_total > 0:
            _maybe_alert_new_ads(new_total, result.get("regions", []))

        logger.info("ad cabinet scan done: %d new ad requests", new_total)
        return {
            "success": result.get("success", False),
            "timestamp": datetime.now().isoformat(),
            "new_total": new_total,
            "regions": result.get("regions", []),
        }
    except Exception as e:
        logger.error(f"scan_suggested_ads failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


def _maybe_alert_new_ads(new_total: int, regions: list, source_label: str = "предложке") -> None:
    """Telegram-алерт о новых рекламных заявках (best-effort).

    ``source_label`` — откуда заявки (``"предложке"`` / ``"личке"``), для текста.
    """
    try:
        from config.runtime import SERVER, TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules import telegram_http as tg_http

        token = TELEGRAM_TOKENS.get("VALSTANBOT") or TELEGRAM_TOKENS.get("ALERT")
        chat_id = TELEGRAM_ALERT_CHAT_ID
        if not token or not chat_id:
            return
        domain = (
            SERVER.get("domain") or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
        )
        url = f"https://{domain}/ad"
        by_region = ", ".join(
            f"{r.get('region_code')}:{r.get('new')}" for r in (regions or []) if r.get("new")
        )
        text = (
            f"📢 Новых рекламных заявок в {source_label}: <b>{new_total}</b>\n"
            f"{by_region}\n\n{url}"
        )
        tg_http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    except Exception as e:
        logger.warning(f"ad cabinet telegram alert failed: {e}")


def _send_debtor_alert(text: str) -> bool:
    """Отправить Telegram-напоминание о должниках/перерасходе (С4/И2).

    Возвращает True только при HTTP 200 (аудит 2026-09-05): раньше ответ не
    проверялся, и перерасход помечался «доставленным» после 400 от Telegram —
    напоминание гасло на три дня.
    """
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules import telegram_http as tg_http

        token = TELEGRAM_TOKENS.get("VALSTANBOT") or TELEGRAM_TOKENS.get("ALERT")
        chat_id = TELEGRAM_ALERT_CHAT_ID
        if not token or not chat_id:
            return False
        resp = tg_http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if not resp.ok:
            logger.warning("ad cabinet alert: telegram answered %s", resp.status_code)
        return bool(resp.ok)
    except Exception as e:
        logger.warning(f"ad debtor telegram alert failed: {e}")
        return False


def _dominant_failure(failures) -> str:
    """Самая частая причина отказа чанков — одной строкой для статуса таски.

    Причины приходят кодами ``modules.deepseek_client`` (``no_api_key`` |
    ``network`` | ``http_<code>`` | ...). Нам нужна не гистограмма, а имя
    поломки: 26 одинаковых ``no_api_key`` — это «ключа нет», и статус таски
    обязан произнести это вслух.
    """
    if not failures:
        return ""
    counts: dict = {}
    for reason in failures:
        key = str(reason or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


@app.task(name="tasks.celery_app.classify_pending_posts")
def classify_pending_posts(limit: int = 0):
    """Headless-классификация собранных постов на DeepSeek (D-024).

    Замена облачной рутине-агенту: те же ``fetch_pending`` → вердикты →
    ``record_verdicts``, но вызов модели идёт прямо отсюда, без облака.

    **Гейт ``CLASSIFIER_HEADLESS_ENABLED`` выключен по умолчанию.** Пока жива
    рутина, оба источника пишут в одну таблицу, а запись идемпотентна по ``lip``
    — включённые одновременно, они воруют друг у друга посты и делают сверку
    невозможной. Включать только ВМЕСТО рутины.
    """
    try:
        from config.classifier import (
            classifier_disabled,
            get_headless_chunk_size,
            get_pending_max,
            get_region_allowlist,
            get_source_days,
            headless_enabled,
        )

        if classifier_disabled():
            return {"status": "skipped:classifier-disabled"}
        if not headless_enabled():
            return {"status": "skipped:headless-off"}

        from modules.classifier import headless, rules, service
        from modules.secrets_bootstrap import ensure_secret

        # Ключ живёт в комнате КАРМАНа и приезжает bootstrap'ом НА СТАРТЕ
        # процесса. Если в тот момент vault был недоступен, ключа не будет до
        # следующего рестарта — а рестартовать никто не станет, потому что
        # снаружи всё «ок» (инцидент 2026-08-19, трое суток нулевых прогонов).
        # До-тяг с кулдауном закрывает дыру там, где ключ реально нужен.
        ensure_secret("DEEPSEEK_API_KEY")

        async def _run():
            from database.connection import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                codes = get_region_allowlist()
                cap = get_pending_max()
                # text_only: бестекстовые движок не судит, а в батче они
                # занимали до 179 мест из 200 на каждом прогоне (2026-09-10).
                posts = await service.fetch_pending(
                    session,
                    region_codes=codes or None,
                    limit=min(limit or cap, cap),
                    days=get_source_days(),
                    text_only=True,
                )
                if not posts:
                    return {"status": "ok", "posts": 0}
                postulates = await rules.render_effective_postulates(session)
                run = headless.classify_posts(
                    posts, postulates=postulates, chunk_size=get_headless_chunk_size()
                )
                logger.info("classifier headless: %s", headless.summarize(run))
                if run["failures"]:
                    logger.warning("classifier headless: отказы чанков: %s", run["failures"])
                if run["problems"]:
                    logger.warning("classifier headless: замечания: %s", run["problems"][:10])
                if not run["verdicts"]:
                    # Ноль вердиктов при непустом батче — это ОТКАЗ, а не «ок».
                    # Прежний код возвращал status=ok, и таска трое суток
                    # рапортовала успех, забрав 200 постов и не разметив ни
                    # одного: единственным следом был WARNING в логе, на который
                    # никто не смотрит. Мониторинг успеха не видит отказа, если
                    # отказ рапортует успехом (пул #145, зеркало инцидента с
                    # ботами).
                    reason = _dominant_failure(run["failures"]) or "no-verdicts"
                    logger.error(
                        "classifier headless: батч %d постов, ноль вердиктов — причина %s",
                        len(posts),
                        reason,
                    )
                    return {
                        "status": f"error:{reason}",
                        "posts": len(posts),
                        "recorded": 0,
                        "failures": len(run["failures"]),
                    }
                stats = await service.record_verdicts(
                    session,
                    run["verdicts"],
                    source="headless",
                    region_codes_fallback=codes or None,
                )
                await session.commit()
                return {
                    "status": "ok",
                    "posts": len(posts),
                    "tokens": run["tokens"],
                    "actions": headless.actions_histogram(run["verdicts"]),
                    **stats,
                }

        # run_coro, а не asyncio.run: в prefork-воркере петля переиспользуется
        # процессом (utils/celery_asyncio) — общая идиома всех async-тасок здесь.
        return run_coro(_run())
    except Exception as e:
        # Классификатор — усилитель, не единая точка отказа: его сбой не должен
        # ронять beat-цепочку (та же политика, что у enforce.fail-open).
        logger.warning("classifier headless task failed: %s", e)
        return {"status": f"error:{type(e).__name__}"}


@app.task(name="tasks.celery_app.refresh_post_metrics")
def refresh_post_metrics(hours: int = 0):
    """Обновление метрик собранных постов — данные под рейтинг (звено 5, шаг 1).

    Метрики в момент сбора почти нулевые (пост собран через минуты после
    публикации). Рейтинг строится на том, что доросло за окно отсева, поэтому
    цифры надо догонять фоном.
    """
    try:
        from modules.classifier.audit_window import AUDIT_WINDOW_HOURS
        from modules.classifier.metrics_refresh import refresh_metrics

        async def _run():
            from database.connection import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                return await refresh_metrics(session, hours=hours or AUDIT_WINDOW_HOURS)

        # run_coro, а не asyncio.run: в prefork-воркере петля переиспользуется
        # процессом (utils/celery_asyncio) — общая идиома всех async-тасок здесь.
        return run_coro(_run())
    except Exception as e:
        # Обновление метрик — вспомогательная работа: её отказ не должен
        # ронять beat-цепочку. Но и молчать нельзя, иначе рейтинг тихо
        # застынет на старых числах (тот же класс, что инцидент 19.08,
        # где таска рапортовала успех, ничего не сделав).
        logger.error("refresh_post_metrics failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.task(name="tasks.celery_app.distill_classifier_rules")
def distill_classifier_rules(limit: int = 200, days: int = 30):
    """Дистилляция правок оператора в предложения правил — звено 7 (D-024).

    Замыкает петлю обучения: правки → обобщённые правила → постулаты → поведение
    ИИ-фильтра. Предложения ложатся как ``proposed`` и ждут оператора в ленте —
    автоприменения здесь нет и не будет (ADR-0005).

    Гейт — свой собственный ``CLASSIFIER_DISTILL_ENABLED``, по умолчанию выключен:
    пока правила чеканит ручная дистилляция из чата по памятке ``/distill``, две
    ветки предлагали бы правила по одному и тому же сырью.

    До 2026-09-04 здесь стоял ``CLASSIFIER_HEADLESS_ENABLED`` — флаг ИИ-фильтра
    потока, который на проде поднят. Условие читалось как развод веток, а работало
    как «всегда включено»: выключить дистилляцию, не выключив фильтр, было нельзя.
    Панель за неделю набрала 8 черновиков, каждый — пересказ утверждённого правила.
    """
    try:
        from config.classifier import classifier_disabled, distill_enabled

        if classifier_disabled():
            return {"status": "skipped:classifier-disabled"}
        if not distill_enabled():
            return {"status": "skipped:distill-off"}

        from modules.classifier import distill as distill_mod
        from modules.classifier import rules
        from modules.secrets_bootstrap import ensure_secret

        ensure_secret("DEEPSEEK_API_KEY")  # см. классификатор выше

        async def _run():
            from database.connection import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                corrections = await rules.fetch_corrections_for_distill(
                    session, limit=limit, days=days
                )
                postulates = await rules.render_effective_postulates(session)
                run = distill_mod.distill(corrections, postulates=postulates)
                logger.info("classifier distill: %s", distill_mod.summarize(run))
                if run.get("rejected"):
                    logger.info("classifier distill: отбраковано: %s", run["rejected"][:5])
                if not run.get("proposals"):
                    reason = run.get("reason") or "no-proposals"
                    # «Материала мало» — законный ноль, всё остальное — отказ.
                    # Без этого различия дистиллятор с 2026-08-17 рапортовал
                    # успех на отсутствующем ключе (reason=no_api_key, ok).
                    if reason in ("not-enough-material", "distilled"):
                        return {"status": "ok", "reason": reason, "proposals": 0}
                    logger.error("classifier distill: прогон впустую — причина %s", reason)
                    return {"status": f"error:{reason}", "reason": reason, "proposals": 0}
                counts = await rules.record_rule_proposals(
                    session, run["proposals"], source="headless"
                )
                await session.commit()
                return {"status": "ok", "tokens": run["tokens"], **counts}

        return run_coro(_run())
    except Exception as e:
        logger.warning("classifier distill task failed: %s", e)
        return {"status": f"error:{type(e).__name__}"}


@app.task(name="tasks.celery_app.check_classifier_heartbeat")
def check_classifier_heartbeat():
    """Сторож «ИИ-фильтр молчит»: алёрт, если вердиктов давно нет.

    Инцидент 2026-08-19: ключ DeepSeek не доехал до воркера, и трое суток
    ``classify_pending_posts`` возвращала ``ok`` с нулём вердиктов. Ни один
    существующий сигнал этого не показывал — сервисы ``active``, health 200,
    beat шлёт, worker принимает. Сторож смотрит на СЛЕД РАБОТЫ (свежесть
    последнего вердикта в БД), а не на то, отработала ли таска.
    """
    try:
        from config.classifier import get_source_days
        from config.runtime import SERVER, TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules.classifier import service
        from modules.classifier.heartbeat import maybe_alert_stale_classifier

        async def _run():
            from database.connection import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                return await service.health_stats(session, days=get_source_days())

        health = run_coro(_run())
        domain = (
            SERVER.get("domain") or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
        )
        status = maybe_alert_stale_classifier(
            last_verdict_at=health.get("last_verdict_at"),
            backlog=int(health.get("backlog") or 0),
            telegram_token=(TELEGRAM_TOKENS.get("VALSTANBOT") or TELEGRAM_TOKENS.get("ALERT")),
            chat_id=TELEGRAM_ALERT_CHAT_ID,
            dashboard_url=f"https://{domain}/classifier",
        )
        logger.info("classifier heartbeat watchdog: %s", status)
        return {"success": True, "status": status, "backlog": health.get("backlog")}
    except Exception as e:
        logger.error("check_classifier_heartbeat failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@app.task(name="tasks.celery_app.check_telegram_bot_identity")
def check_telegram_bot_identity():
    """Сторож целостности Telegram-ботов: имя, описание, вебхук (2026-08-12).

    Раз в час сверяет, как боты выглядят снаружи, с эталоном в
    ``modules/telegram_bot_guard.py``. Расхождение = либо владелец сменил имя и
    не обновил эталон, либо токен утёк и ботом распоряжается кто-то ещё —
    оба случая требуют человека, поэтому шлём алёрт с 6-часовым cooldown.
    """
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules.telegram_bot_guard import maybe_alert

        redis_client = None
        try:
            from modules.notifications.storage import NotificationsStorage

            redis_client = NotificationsStorage().redis_client
        except Exception:
            logger.debug("telegram bot guard: redis unavailable, cooldown off", exc_info=True)

        status = maybe_alert(
            tokens=dict(TELEGRAM_TOKENS),
            telegram_token=_pick_telegram_bot_token(TELEGRAM_TOKENS),
            chat_id=TELEGRAM_ALERT_CHAT_ID,
            redis_client=redis_client,
        )
        return {"status": status}
    except Exception as e:
        logger.warning("telegram bot guard task failed: %s", e)
        return {"status": f"error:{type(e).__name__}"}


@app.task(name="tasks.celery_app.scan_inbound_dm_ads")
def scan_inbound_dm_ads():
    """Скан входящих ЛС сообществ на рекламу → заявки в ad_requests (блок A).

    Каждые 30 минут 8:00-22:00 (на 35-й/05-й минуте — офсет от скана предложки
    в X:25/55). Источник — ``messages.getConversations`` главных групп; детект
    тем же AdvertisementFilter + предложка-сигналы; дедуп по (community_vk_id,
    peer_id) при origin='inbound_dm'. Telegram-алерт только при НОВЫХ заявках.
    """
    logger.info("=" * 80)
    logger.info("Scanning inbound DM for advertisements (ad cabinet, block A)...")
    logger.info("=" * 80)

    try:
        from modules.ad_cabinet.dm_scanner import run_dm_scan

        result = run_coro(run_dm_scan())
        new_total = int(result.get("new_total", 0))
        # Telegram-алерт — только про НОВУЮ рекламу в личке; не-рекламные ЛС видны в
        # разделе «Уведомления» (Этап 1), отдельно ими не спамим.
        new_ads_total = int(result.get("new_ads_total", 0))
        if new_ads_total > 0:
            _maybe_alert_new_ads(new_ads_total, result.get("regions", []), source_label="личке")

        logger.info(
            "ad cabinet DM scan done: %d new DM rows (%d ad → cabinet)",
            new_total,
            new_ads_total,
        )
        return {
            "success": result.get("success", False),
            "timestamp": datetime.now().isoformat(),
            "new_total": new_total,
            "new_ads_total": new_ads_total,
            "regions": result.get("regions", []),
        }
    except Exception as e:
        logger.error(f"scan_inbound_dm_ads failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.reconcile_scheduled_publications")
def reconcile_scheduled_publications():
    """Авто-фиксация публикаций отложки (рекламный кабинет, PR-6).

    Каждые 15 минут 8:00–23:45 МСК. Для отложек, чьё время прошло, проверяет
    через VK факт публикации и фиксирует в CRM: статус→published, AdPublication,
    awaiting-оплата (если есть client+price), событие в таймлайн. Идемпотентно.
    """
    logger.info("Reconciling scheduled ad publications (ad cabinet, PR-6)...")
    try:
        from modules.ad_cabinet.publish_reconciler import run_reconcile

        result = run_coro(run_reconcile())
        logger.info(
            "reconcile scheduled publications done: %s",
            result,
        )
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"reconcile_scheduled_publications failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.dispatch_ad_reposts")
def dispatch_ad_reposts():
    """Тик диспетчера планировщика предложки (каждую минуту, 24/7).

    Репосты в сообщества-дублёры после выхода оригинала + оригиналы в режиме
    ``queue``. No-op без due-строк или при AD_REPOST_DISABLED. Идемпотентно
    (lease-claim), throttle ≥5 с между VK-записями.
    """
    try:
        from modules.ad_cabinet.repost_dispatcher import run_repost_dispatch

        result = run_coro(run_repost_dispatch())
        if result.get("taken"):
            logger.info("ad repost dispatch: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"dispatch_ad_reposts failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.watch_ad_pending")
def watch_ad_pending():
    """Сторож pending-постов с прошедшей датой (аудит 2026-09-05): пометка,
    пинг владельцу (дедуп сутки на пост), одно уведомление клиенту."""
    try:
        from modules.ad_cabinet.pending_watch import run_pending_watch

        result = run_coro(run_pending_watch())
        if result.get("marked"):
            logger.info("ad pending watch: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"watch_ad_pending failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.dispatch_ad_outreach")
def dispatch_ad_outreach(campaign_id=None):
    """Тик рассылки рекламного оффера (Этап 4): лимиты, тихие часы, dry-run, стоп по 9/14.

    ``campaign_id`` — тик по одной кампании (кнопка «Тик сейчас» в /ad ставит
    задачу сюда же, чтобы не гнать отправку внутри web-запроса)."""
    try:
        from modules.ad_cabinet.outreach import run_outreach_tick

        result = run_coro(run_outreach_tick(alert=_send_debtor_alert, campaign_id=campaign_id))
        if result.get("sent") or result.get("dry_run") or result.get("stopped"):
            logger.info("ad outreach: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"dispatch_ad_outreach failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.unpin_ad_posts")
def unpin_ad_posts():
    """Снять закрепы рекламных постов, у которых прошли сутки (Этап 2, PR 2C)."""
    try:
        from modules.ad_cabinet.pinning import run_unpin

        result = run_coro(run_unpin())
        if result.get("due"):
            logger.info("ad unpin: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"unpin_ad_posts failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.ad_photo_retention")
def ad_photo_retention():
    """Ретенция фото рекламных клиентов (PR 1.8 аудита 2026-09-05): каталоги
    архивных клиентов — целиком, у живых — файлы старше AD_PHOTO_KEEP_DAYS без
    ссылок из активных постов."""
    try:
        from modules.ad_cabinet.photo_retention import run_photo_retention

        result = run_coro(run_photo_retention())
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"ad_photo_retention failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.check_ad_repost_heartbeat")
def check_ad_repost_heartbeat():
    """Watchdog планировщика предложки: алёрт, если просроченные строки не взяты."""
    try:
        from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules.ad_cabinet.repost_dispatcher import maybe_alert_stale

        token = TELEGRAM_TOKENS.get("VALSTANBOT") or TELEGRAM_TOKENS.get("ALERT")
        status = run_coro(maybe_alert_stale(telegram_token=token, chat_id=TELEGRAM_ALERT_CHAT_ID))
        logger.info("ad repost watchdog: %s", status)
        return {"success": True, "status": status, "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error(f"check_ad_repost_heartbeat failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.expire_ad_posts")
def expire_ad_posts():
    """Авто-снятие рекламных постов по истечении срока (С2, ad-CRM).

    Раз в сутки (03:30 MSK). Для вышедших публикаций с истёкшим expires_at —
    wall.delete + статус removed + removed_at + событие 'removed' в таймлайн.
    Срок опционален (без срока пост висит вечно). Идемпотентно (только
    status='published').
    """
    logger.info("Expiring ad posts past their term (ad cabinet, С2)...")
    try:
        from modules.ad_cabinet.post_expirer import run_expiry

        result = run_coro(run_expiry())
        logger.info("expire ad posts done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"expire_ad_posts failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.collect_ad_publication_stats")
def collect_ad_publication_stats():
    """Суточный сбор метрик рекламных публикаций (С3, ad-CRM).

    Раз в сутки (04:30 MSK). Для вышедших публикаций тянет просмотры/лайки/
    репосты через wall.getById и пишет снимок. Ручное обновление одного клиента —
    через API-кнопку (run_collect_stats(only_client_id=...)).
    """
    logger.info("Collecting ad publication stats (ad cabinet, С3)...")
    try:
        from modules.ad_cabinet.publication_stats import run_collect_stats

        result = run_coro(run_collect_stats())
        logger.info("collect ad publication stats done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"collect_ad_publication_stats failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.alert_ad_debtors")
def alert_ad_debtors():
    """Суточное Telegram-напоминание о должниках (С4, ad-CRM).

    Раз в день (10:00 MSK). Клиенты с awaiting-оплатами старше порога (3 дн.,
    AD_DEBTOR_DAYS) → один Telegram-список оператору. Полу-авто: оплату оператор
    отмечает руками, код лишь напоминает о просрочке.
    """
    logger.info("Alerting ad debtors (ad cabinet, С4)...")
    try:
        from config.runtime import SERVER
        from modules.ad_cabinet.debtors import run_debtor_alert

        domain = (
            SERVER.get("domain") or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
        )
        url = f"https://{domain}/ad#crm"
        result = run_coro(run_debtor_alert(send=_send_debtor_alert, url=url))
        logger.info("ad debtors alert done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"alert_ad_debtors failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.alert_ad_overspent")
def alert_ad_overspent():
    """Суточное Telegram-напоминание о перерасходе пакета публикаций (И2, ad-CRM).

    Раз в день (11:00 MSK, через час после должников). Клиенты, у кого вышло
    больше публикаций, чем оплачено пакетом (``units_paid``) → напомнить о проплате
    следующего периода. Дедуп через ``spend_alerted_at`` (сброс при новой оплате).
    """
    logger.info("Alerting ad overspent (ad cabinet, И2)...")
    try:
        from config.runtime import SERVER
        from modules.ad_cabinet.spend_balance import run_overspend_alert

        domain = (
            SERVER.get("domain") or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
        )
        url = f"https://{domain}/ad#crm"
        result = run_coro(run_overspend_alert(send=_send_debtor_alert, url=url))
        logger.info("ad overspent alert done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"alert_ad_overspent failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.auto_greet_ad_requests")
def auto_greet_ad_requests():
    """Авто-приветствие рекламодателю на новую заявку (улучшение отклика).

    Каждые 30 мин 8:00-22:00 (X:10/40, сразу после сканов). Свежим новым заявкам
    в разрешённых сообществах (env AD_AUTO_GREETING_COMMUNITIES) шлёт приветствие
    один раз. Off по умолчанию (пустой allowlist → no-op).
    """
    logger.info("Auto-greeting new ad requests (ad cabinet)...")
    try:
        from modules.ad_cabinet.auto_greeting import run_auto_greeting

        result = run_coro(run_auto_greeting())
        logger.info("auto-greeting done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"auto_greet_ad_requests failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.collect_member_snapshots")
def collect_member_snapshots():
    """Суточный снимок подписчиков ГЛАВНЫХ ИНФО-групп активных регионов (04:00 MSK).

    Тянет `groups.getById(fields=members_count)` по `regions.vk_group_id` и пишет
    по строке на (регион, день) в `region_member_snapshots` — фундамент графика
    роста подписчиков. Только главные группы (куда выпускаем сводки), не весь
    пул источников — экономия VK API. Идемпотентно за день (upsert).
    """
    logger.info("Collecting region member-count snapshots...")
    try:
        from modules.members_snapshot import collect_member_snapshots as _collect

        result = run_coro(_collect())
        logger.info("member snapshots done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"collect_member_snapshots failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.collect_oblast_unique_snapshots")
def collect_oblast_unique_snapshots():
    """Еженедельный снимок УНИКАЛЬНЫХ подписчиков области без дублей (ночь пн).

    Объединяет member-id главных ИНФО-групп каждой области (сама область +
    районы) через `groups.getMembers` и пишет уникальных в
    `oblast_unique_member_snapshots` — «чистый» охват области для сравнения
    областей на графике роста. Только ~16 главных групп (1000 id/запрос) →
    нагрузка ничтожна. Идемпотентно за день (upsert).
    """
    logger.info("Collecting oblast unique-member snapshots...")
    try:
        from modules.oblast_unique_members import collect_oblast_unique_snapshots as _collect

        result = run_coro(_collect())
        logger.info("oblast unique snapshots done: %s", result)
        return {"success": True, "timestamp": datetime.now().isoformat(), **result}
    except Exception as e:
        logger.error(f"collect_oblast_unique_snapshots failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.check_unread_messages")
def check_unread_messages():
    """Проверка непрочитанных сообщений в главных группах регионов.

    Окно 8:00-22:00 MSK гарантируется beat-расписанием
    `crontab(minute=28, hour='8-22')` в `beat_schedule`. Внутри таски доп.
    проверка часа не нужна — раньше она лишь дублировала фильтр.

    Минута 28, а не 16: тройка проверок шла подряд в :15/:16/:17, каждая одной
    задачей по всем регионам, и на одном ядре они наезжали друг на друга ровно
    в окне OOM-убийств (P164). Почему именно 28, а не любая свободная минута —
    в комментарии у beat-записи.
    """
    logger.info("=" * 80)
    logger.info("Checking unread messages in region groups...")
    logger.info("=" * 80)

    try:
        from sqlalchemy import select

        from database.connection import AsyncSessionLocal
        from database.models import Region
        from modules.notifications.storage import NotificationsStorage
        from modules.notifications.vk_messages_checker import VKMessagesChecker
        from modules.vk_token_router import load_community_tokens, pick_healthy_read_token

        async def check():
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Region).where(
                        Region.vk_group_id.isnot(None),
                        # Уведомления должны проверяться независимо от статуса "пауза" региона.
                    )
                )
                regions = list(result.scalars())

                if not regions:
                    logger.warning("No regions with VK groups found")
                    return []

                logger.info(f"Checking {len(regions)} region groups for unread messages...")

                region_groups = [
                    {
                        "region_id": r.id,
                        "region_name": r.name,
                        "region_code": r.code,
                        "vk_group_id": r.vk_group_id,
                    }
                    for r in regions
                ]

                # Живой READ-токен из БД (probe + self-heal; был env-хардкод VALSTAN)
                cand = await pick_healthy_read_token(session)
                vk_token = cand.token if cand else None
                if not vk_token:
                    logger.error("No healthy VK READ token")
                    return []

                # Community-токены для каждой группы (если есть) — checker предпочтёт их.
                community_tokens = await load_community_tokens(session)

                checker = VKMessagesChecker(vk_token, community_tokens=community_tokens)
                run_start = datetime.now()
                result = await checker.check_all_region_groups(region_groups)
                run_duration = (datetime.now() - run_start).total_seconds()
                notifications = result["notifications"]
                denied_groups = result["denied_groups"]

                storage = NotificationsStorage()
                storage.save_notifications(notifications, "unread_messages")
                storage.save_notifications(denied_groups, "unread_messages_denied")
                storage.save_run(
                    "unread_messages",
                    count=len(notifications),
                    denied_count=len(denied_groups),
                    duration_seconds=run_duration,
                    success=True,
                )
                try:
                    from monitoring.metrics import (
                        notifications_check_duration_seconds,
                        notifications_check_total,
                        notifications_items_found_total,
                    )

                    if denied_groups and not notifications:
                        result_label = "denied"
                    elif notifications:
                        result_label = "ok"
                    else:
                        result_label = "empty"
                    notifications_check_total.labels(
                        check_type="messages",
                        result=result_label,
                    ).inc()
                    notifications_check_duration_seconds.labels(
                        check_type="messages",
                    ).observe(run_duration)
                    notifications_items_found_total.labels(
                        check_type="messages",
                    ).inc(len(notifications))
                except Exception as _e:
                    logger.debug("metrics emit failed: %s", _e)

                logger.info(
                    "Found %d groups with unread messages (%d denied access)",
                    len(notifications),
                    len(denied_groups),
                )
                return notifications, denied_groups

        notifications, denied_groups = run_coro(check())

        # Агрегированный алёрт — из каждой проверки, см. докстринг функции.
        _maybe_send_telegram_notifications_alert()

        # Список в результат не кладём — см. `check_suggested_posts`.
        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "notifications_count": len(notifications),
            "denied_count": len(denied_groups),
        }

    except Exception as e:
        logger.error(f"Failed to check unread messages: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.check_recent_comments")
def check_recent_comments():
    """Проверка комментариев за последние 24 часа в главных ИНФО-группах регионов.

    Окно 8:00-22:00 MSK гарантируется beat-расписанием
    `crontab(minute=57, hour='8-22')` в `beat_schedule`. Доп. inside-task
    проверка часа удалена (этап 2 рефактора).

    Минута 57, а не 17: это самая тяжёлая из трёх проверок (все ИНФО-группы ×
    до 50 постов × все комментарии с ветками), и она уведена дальше всех — и
    от соседок по тройке, и от волн на :20 (P164, разнос 2026-09-22).
    """
    from datetime import datetime, timedelta

    logger.info("=" * 80)
    logger.info("Checking recent comments (last 24h) under posts of all communities...")
    logger.info("=" * 80)

    try:
        from sqlalchemy import select

        from database.connection import AsyncSessionLocal
        from database.models import Region
        from modules.notifications.storage import NotificationsStorage
        from modules.notifications.vk_comments_checker import VKCommentsChecker
        from modules.vk_token_router import get_healthy_read_token, load_community_tokens

        cutoff_dt = datetime.utcnow() - timedelta(hours=24)
        cutoff_ts = int(cutoff_dt.timestamp())

        async def check():
            # Живой READ-токен из БД (probe + self-heal; был env-хардкод VALSTAN)
            vk_token = await get_healthy_read_token()
            if not vk_token:
                logger.error("No healthy VK READ token")
                return []

            async with AsyncSessionLocal() as session:
                # Берём только главные ИНФО-группы регионов
                rows = await session.execute(
                    select(Region.id, Region.code, Region.name, Region.vk_group_id)
                    .where(
                        # Уведомления должны проверяться независимо от статуса "пауза" региона.
                        Region.vk_group_id.isnot(None),
                    )
                    .order_by(Region.id)
                )
                regions = rows.all()

                region_groups = []
                for region_id, region_code, region_name, vk_group_id in regions:
                    # Дополнительный фильтр по префиксу/маркеру ИНФО
                    if "ИНФО" not in (region_name or ""):
                        continue
                    region_groups.append(
                        {
                            "region_id": region_id,
                            "region_code": region_code,
                            "region_name": region_name,
                            "vk_group_id": vk_group_id,
                        }
                    )

                community_tokens = await load_community_tokens(session)
                checker = VKCommentsChecker(vk_token, community_tokens=community_tokens)
                run_start = datetime.now()
                notifications = await checker.check_recent_comments_for_region_groups(
                    region_groups=region_groups, cutoff_ts=cutoff_ts
                )
                run_duration = (datetime.now() - run_start).total_seconds()

                # keep_if_empty=True: ручной запуск из UI мог только что обнаружить
                # комментарии — не стираем их если автотаска вернула 0 из-за
                # community-token error 27.
                storage = NotificationsStorage()
                storage.save_notifications(
                    notifications,
                    "recent_comments",
                    keep_if_empty=True,
                )
                storage.save_run(
                    "recent_comments",
                    count=len(notifications),
                    duration_seconds=run_duration,
                    success=True,
                )
                try:
                    from monitoring.metrics import (
                        notifications_check_duration_seconds,
                        notifications_check_total,
                        notifications_items_found_total,
                    )

                    result_label = "ok" if notifications else "empty"
                    notifications_check_total.labels(
                        check_type="comments",
                        result=result_label,
                    ).inc()
                    notifications_check_duration_seconds.labels(
                        check_type="comments",
                    ).observe(run_duration)
                    notifications_items_found_total.labels(
                        check_type="comments",
                    ).inc(len(notifications))
                except Exception as _e:
                    logger.debug("metrics emit failed: %s", _e)

                logger.info(f"Found {len(notifications)} recent comments (main INFO groups only)")
                return notifications

        notifications = run_coro(check())

        # После обновления всех ключей (suggested/messages/comments) отправляем агрегированное
        # Telegram-уведомление (если есть новые элементы).
        _maybe_send_telegram_notifications_alert()

        # Health watchdog (этап 5): если последние N автопроверок подряд
        # вернули 0 — намёк на сломанный токен, шлём отдельный alert
        # (с собственным cooldown, чтобы не спамить).
        try:
            from config.runtime import SERVER, TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
            from modules.notifications.health import maybe_alert_broken_tokens

            telegram_token = TELEGRAM_TOKENS.get("VALSTANBOT")
            chat_id = TELEGRAM_ALERT_CHAT_ID
            domain = (
                SERVER.get("domain")
                or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
            )
            dashboard_url = f"https://{domain}/notifications"

            run_coro(
                maybe_alert_broken_tokens(
                    telegram_token=telegram_token,
                    chat_id=chat_id,
                    dashboard_url=dashboard_url,
                )
            )
        except Exception as _e:
            logger.debug("token health watchdog failed: %s", _e)

        # Список в результат не кладём — см. `check_suggested_posts`.
        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "comments_count": len(notifications),
        }

    except Exception as e:
        logger.error(f"Failed to check recent comments: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


async def _active_region_codes() -> set:
    """Коды активных регионов с VK-группой — те, что вообще ходят в волны."""
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Region

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Region.code).where(Region.is_active.is_(True), Region.vk_group_id.isnot(None))
        )
        return set(result.scalars().all())


@app.task(name="tasks.celery_app.check_bulletin_heartbeat")
def check_bulletin_heartbeat():
    """Watchdog «давно нет сводок»: алёрт, если novost давно не публиковался.

    Читает Redis-heartbeat (пишется из ``track_digest_published`` на всех путях
    публикации). Если тема ``novost`` протухла дольше порога — Telegram-алёрт
    с собственным cooldown (см. ``modules.bulletin_heartbeat``). Beat гоняет днём
    (10:00–22:00); ночью простой 20:40→6:40 легитимен и не проверяется.

    Вторая проверка — по регионам (P169): тематический ключ слеп к району, а
    район без единой сводки за все слоты суток — отказ, на который сторож
    обязан покраснеть. Список активных регионов берётся из БД — это
    единственное, что не даёт выключенному району кричать. Если БД недоступна,
    список не подставляется, и региональная проверка **молчит**
    (``skipped:no-active-regions``): нефильтрованный алёрт назвал бы давно
    погашенные районы, а при лежащей БД волна всё равно не идёт и тематический
    сторож покраснеет сам.
    """
    try:
        from config.runtime import SERVER, TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS
        from modules.bulletin_heartbeat import maybe_alert_stale_bulletin, maybe_alert_stale_regions

        token = TELEGRAM_TOKENS.get("VALSTANBOT") or TELEGRAM_TOKENS.get("ALERT")
        chat_id = TELEGRAM_ALERT_CHAT_ID
        domain = (
            SERVER.get("domain") or f"{SERVER.get('host', '127.0.0.1')}:{SERVER.get('port', 8000)}"
        )
        status = maybe_alert_stale_bulletin(
            topic="novost",
            telegram_token=token,
            chat_id=chat_id,
            dashboard_url=f"https://{domain}/",
        )
        logger.info("bulletin heartbeat watchdog: %s", status)

        active_regions = None
        try:
            active_regions = run_coro(_active_region_codes())
        except Exception:  # pragma: no cover - БД недоступна → сторож молчит
            logger.warning("active regions lookup failed, region watchdog silenced", exc_info=True)
        regions_status = maybe_alert_stale_regions(
            topic="novost",
            active_regions=active_regions,
            telegram_token=token,
            chat_id=chat_id,
            dashboard_url=f"https://{domain}/",
        )
        logger.info("bulletin heartbeat watchdog (regions): %s", regions_status)
        return {
            "success": True,
            "status": status,
            "regions_status": regions_status,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"check_bulletin_heartbeat failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.cleanup_old_posts")
def cleanup_old_posts():
    """
    Очистка старых постов из БД.

    Выполняется каждый день в 03:00.
    Удаляет посты старше 30 дней для освобождения места.
    """
    logger.info("=" * 80)
    logger.info("Cleaning up old posts...")
    logger.info("=" * 80)

    try:
        from sqlalchemy import delete

        from database.connection import AsyncSessionLocal
        from database.models import Post

        async def cleanup():
            async with AsyncSessionLocal() as session:
                # Удаляем посты старше 30 дней
                cutoff_date = datetime.now() - timedelta(days=30)

                result = await session.execute(
                    delete(Post).where(Post.date_published < cutoff_date)
                )

                deleted_count = result.rowcount
                await session.commit()

                return deleted_count

        deleted_count = run_coro(cleanup())

        logger.info(f"Cleanup completed! Deleted {deleted_count} old posts")

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "deleted_count": deleted_count,
        }

    except Exception as e:
        logger.error(f"Cleanup failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.prune_gateway_requests")
def prune_gateway_requests():
    """Ретеншн лога VK-шлюза: удалить строки ``gateway_requests`` старше N дней.

    Раз в сутки (03:40 MSK, после cleanup 03:00 и expire-ad 03:30). Порог — env
    ``GATEWAY_REQUESTS_RETENTION_DAYS`` (дефолт 90). Аналог ``cleanup_old_posts``.
    Лог шлюза растёт постоянно (включая 401/429) — без ретеншна таблица пухнет.
    """
    logger.info("Pruning old gateway_requests (VK-шлюз retention)...")
    try:
        from sqlalchemy import delete

        from config.gateway import get_gateway_requests_retention_days
        from database.connection import AsyncSessionLocal
        from database.models import GatewayRequest

        async def prune():
            cutoff = datetime.utcnow() - timedelta(days=get_gateway_requests_retention_days())
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    delete(GatewayRequest).where(GatewayRequest.created_at < cutoff)
                )
                await session.commit()
                return result.rowcount

        deleted = run_coro(prune())
        logger.info("gateway_requests prune done: deleted %s rows", deleted)
        return {"success": True, "timestamp": datetime.now().isoformat(), "deleted_count": deleted}
    except Exception as e:
        logger.error(f"prune_gateway_requests failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.prune_collected_post_audit")
def prune_collected_post_audit():
    """Ретеншн журнала аудита сбора: удалить ``collected_post_audit`` старше N дней.

    Раз в сутки (03:45 MSK, после cleanup 03:00 и gateway-prune 03:40). Порог — env
    ``COLLECTION_AUDIT_RETENTION_DAYS`` (дефолт 60). Аудит пишется на каждый собранный
    пост (ADR-0004) → без ретеншна таблица пухнет. Безопасно: реестр вердиктов
    ``content_classifications`` (дедуп по ``lip``) НЕ трогаем — уже классифицированный
    пост не воскреснет в ``/pending`` даже после чистки аудита.
    """
    logger.info("Pruning old collected_post_audit (classifier source retention)...")
    try:
        from sqlalchemy import delete

        from config.runtime import get_collection_audit_retention_days
        from database.connection import AsyncSessionLocal
        from database.models_extended import CollectedPostAudit

        async def prune():
            cutoff = datetime.utcnow() - timedelta(days=get_collection_audit_retention_days())
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    delete(CollectedPostAudit).where(CollectedPostAudit.collected_at < cutoff)
                )
                await session.commit()
                return result.rowcount

        deleted = run_coro(prune())
        logger.info("collected_post_audit prune done: deleted %s rows", deleted)
        return {"success": True, "timestamp": datetime.now().isoformat(), "deleted_count": deleted}
    except Exception as e:
        logger.error(f"prune_collected_post_audit failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.prune_skipped_duplicates")
def prune_skipped_duplicates_task():
    """Ретеншн журнала отсеянных дублей: удалить записи старше N дней.

    Раз в сутки (03:48 MSK). Порог — env ``SKIPPED_DUPLICATES_RETENTION_DAYS``
    (дефолт 7). Запись нужна ровно столько, сколько пост может ещё оказаться
    кандидатом (``max_post_age_hours``, дефолт 72 ч), поэтому неделя — это тот же
    срок с запасом. Побочный смысл ретеншена: ложное срабатывание текстового
    дедупа само рассасывается через неделю, а не запирает пост навсегда.
    """
    logger.info("Pruning old skipped_duplicates...")
    try:
        import os

        from database.connection import AsyncSessionLocal
        from modules.deduplication.skipped import prune_skipped

        try:
            keep_days = max(1, int(os.getenv("SKIPPED_DUPLICATES_RETENTION_DAYS", "7")))
        except ValueError:
            keep_days = 7

        async def prune():
            async with AsyncSessionLocal() as session:
                return await prune_skipped(session, keep_days=keep_days)

        deleted = run_coro(prune())
        logger.info("skipped_duplicates prune done: deleted %s rows", deleted)
        return {"success": True, "timestamp": datetime.now().isoformat(), "deleted_count": deleted}
    except Exception as e:
        logger.error(f"prune_skipped_duplicates failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.prune_published_posts")
def prune_published_posts_task():
    """Ретеншн журнала публикаций: удалить ``published_posts`` старше N дней.

    Раз в сутки (03:47 MSK, между аудитом сбора 03:45 и снапшотом правил 03:50).
    Порог — env ``PUBLISHED_POSTS_RETENTION_DAYS`` (дефолт 400). Журнал пишется на
    каждый опубликованный пост (~1200 строк в сутки по сети) и, в отличие от
    соседних, живёт без гейта — значит и расти будет всегда. Окно квоты всё равно
    сутки, так что удерживать больше года незачем; год с запасом оставлен, чтобы
    сравнивать сезоны.
    """
    logger.info("Pruning old published_posts (theme quota journal retention)...")
    try:
        import os

        from database.connection import AsyncSessionLocal
        from modules.publication_journal import prune_published_posts

        try:
            keep_days = max(1, int(os.getenv("PUBLISHED_POSTS_RETENTION_DAYS", "400")))
        except ValueError:
            keep_days = 400

        async def prune():
            async with AsyncSessionLocal() as session:
                return await prune_published_posts(session, keep_days=keep_days)

        deleted = run_coro(prune())
        logger.info("published_posts prune done: deleted %s rows", deleted)
        return {"success": True, "timestamp": datetime.now().isoformat(), "deleted_count": deleted}
    except Exception as e:
        logger.error(f"prune_published_posts failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.snapshot_learned_rules")
def snapshot_learned_rules():
    """Ежедневный снапшот выученных правил (overlay ADR-0005) в файл на диске.

    Пишет approved-правила из ``classification_rules`` в untracked-файл (дефолт
    ``logs/classifier_learned_rules_snapshot.md``) — durable-аудит слоя вне БД.
    Захват в git-историю — шагом dev-сессии (коммит с прода запрещён, PR-only
    ADR-0002); контент детерминированный (без timestamp генерации), поэтому
    git-дифф после захвата = реальное изменение слоя правил.
    """
    logger.info("Snapshotting learned classifier rules (ADR-0005 overlay)...")
    try:
        from config.classifier import get_rules_snapshot_path
        from database.connection import AsyncSessionLocal
        from modules.classifier.rules import render_learned_snapshot

        async def snap():
            async with AsyncSessionLocal() as session:
                return await render_learned_snapshot(session)

        content = run_coro(snap())
        path = get_rules_snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8"))
        logger.info("learned rules snapshot written: %s (%s bytes)", path, size)
        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "path": str(path),
            "bytes": size,
        }
    except Exception as e:
        logger.error(f"snapshot_learned_rules failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.run_content_conveyor")
def run_content_conveyor():
    """Прогон контент-конвейера ВК → сайты кластера (D-015, MANDATE brain 2026-08-08).

    Отбор из опубликованных сводок → один вызов DeepSeek на пост (рубрика,
    заголовок, лёгкая редактура) → POST на ingest сайта. Три звена одним
    прогоном: ссылки на медиа живут ровно столько, сколько живут у ВК-CDN
    (G56/G63), и отложенная доставка везла бы битые картинки.

    **Выключено по умолчанию.** Активность решает ``CONVEYOR_SITES``, и пустое
    значение означает «ни один сайт». Пустой список активных сайтов — не ошибка,
    а штатное состояние: таска молча возвращает ноль обработанных.

    Приёмник создаёт только черновики, публикует человек (shadow-фаза §D).
    """
    try:
        from config.content_conveyor import get_active_sites
        from database.connection import AsyncSessionLocal
        from modules.conveyor.rules import load_rules
        from modules.conveyor.runner import run_site

        sites = get_active_sites()
        if not sites:
            return {"success": True, "sites": 0, "note": "конвейер выключен (CONVEYOR_SITES пуст)"}

        async def go():
            out = []
            async with AsyncSessionLocal() as session:
                for site in sites:
                    # Правила читаются на каждом прогоне, а не кэшируются на
                    # процесс: файл правится человеком между прогонами, и
                    # правка не должна ждать рестарта worker'а.
                    rules = load_rules(str(site.get("key") or ""))
                    out.append(await run_site(session, site, rules=rules, sleep=time.sleep))
            return out

        stats = run_coro(go())
        for s in stats:
            logger.info(
                "конвейер %s: отобрано %s, доставлено %s, отклонено %s, задержано %s, сбоев %s",
                s.get("site"),
                s.get("selected"),
                s.get("delivered"),
                s.get("rejected"),
                s.get("held"),
                s.get("failed"),
            )
        return {"success": True, "timestamp": datetime.now().isoformat(), "stats": stats}
    except Exception as e:
        logger.error(f"run_content_conveyor failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


@app.task(name="tasks.celery_app.probe_token_capabilities")
def probe_token_capabilities():
    """Замер прав VK-токенов опытом — раз в месяц (владелец 2026-07-26).

    Только read-only пробы (``modules.vk_monitor.token_capabilities``): ничего
    не публикует, не комментирует, не шлёт сообщений. Результат — снапшот в
    Redis, виден на ``/tokens``; расхождения с прошлым замером уходят в лог и
    Telegram-алерт. Ежедневный прогон не нужен — права меняются редко, а
    ценность в том, чтобы дрейф («вчера читал стену, сегодня error 27») не
    прошёл незамеченным до сломанного сбора.

    Живёт здесь, а не в ``tasks/monitoring_tasks.py``, потому что тот модуль не
    входит в ``include`` этого приложения: beat позвал бы имя, для которого у
    воркера нет обработчика (поймано гейтом ``tests/test_celery_task_names``).
    """
    logger.info("Probing VK token capabilities (monthly)...")
    try:
        from database.connection import AsyncSessionLocal
        from modules.vk_monitor import token_capabilities as caps
        from modules.vk_token_router import (
            _send_telegram_alert_safe,
            get_active_parse_tokens,
            load_community_tokens,
        )
        from utils.timezone import now_moscow

        async def run():
            async with AsyncSessionLocal() as session:
                tokens = dict(await get_active_parse_tokens(session))
                for community_id, token in (await load_community_tokens(session)).items():
                    tokens[f"COMM_{community_id}"] = token

            selected = caps.select_tokens(tokens)
            if not selected:
                logger.warning("token capabilities: активных токенов нет — замер пропущен")
                return {"success": True, "tokens": 0, "changes": []}

            previous = caps.load_snapshot() or {}
            matrix = caps.measure(selected)
            measured_at = now_moscow().isoformat(timespec="seconds")
            caps.save_snapshot(matrix, measured_at)

            changes = caps.diff_snapshots(previous.get("matrix"), matrix)
            if changes:
                logger.warning("token capabilities drift:\n%s", "\n".join(changes))
                await _send_telegram_alert_safe(
                    "⚠️ Права VK-токенов изменились с прошлого замера:\n" + "\n".join(changes[:20])
                )
            return {
                "success": True,
                "tokens": len(matrix),
                "measured_at": measured_at,
                "changes": changes,
            }

        return run_coro(run())
    except Exception as e:
        logger.error(f"probe_token_capabilities failed: {e}", exc_info=True)
        return {"success": False, "timestamp": datetime.now().isoformat(), "error": str(e)}


# Расписания (Beat Schedule)
app.conf.beat_schedule = {
    # Проверка предложенных постов каждый час с 8:00 до 22:00 в X:15
    "check-suggested-hourly": {
        "task": "tasks.celery_app.check_suggested_posts",
        "schedule": crontab(minute=15, hour="8-22"),  # Каждый час 8-22 на 15-й минуте
        "options": {
            "expires": 3000,
            "catchup": False,
        },
    },
    # Скан предложки на рекламу (рекламный кабинет) каждые 30 мин 8:00-22:00 в X:25/55
    "scan-suggested-ads": {
        "task": "tasks.celery_app.scan_suggested_ads",
        "schedule": crontab(minute="25,55", hour="8-22"),
        "options": {
            "expires": 1500,
            "catchup": False,
        },
    },
    # Скан входящих ЛС на рекламу (рекламный кабинет, блок A) каждые 30 мин в X:05/35
    "scan-inbound-dm-ads": {
        "task": "tasks.celery_app.scan_inbound_dm_ads",
        "schedule": crontab(minute="5,35", hour="8-22"),
        "options": {
            "expires": 1500,
            "catchup": False,
        },
    },
    # Авто-приветствие рекламодателю на новую заявку — X:10/40 (сразу после сканов).
    # Off по умолчанию (env AD_AUTO_GREETING_COMMUNITIES пуст → no-op).
    "auto-greet-ad-requests": {
        "task": "tasks.celery_app.auto_greet_ad_requests",
        "schedule": crontab(minute="10,40", hour="8-22"),
        "options": {
            "expires": 1500,
            "catchup": False,
        },
    },
    # Авто-фиксация публикаций отложки (рекламный кабинет, PR-6): каждые 15 мин,
    # 8:00–23:45 МСК. До 2026-09-05 стояло `minute="45", hour="8-22"` — то есть
    # раз в час, а не «каждые 30 мин», как гласил комментарий; вместе с UTC-сдвигом
    # в реконсилере вечерние посты фиксировались только утром (аудит кабинета).
    "reconcile-scheduled-publications": {
        "task": "tasks.celery_app.reconcile_scheduled_publications",
        "schedule": crontab(minute="*/15", hour="8-23"),
        "options": {
            "expires": 600,
            "catchup": False,
        },
    },
    # Планировщик предложки (Этап 0, план 2026-09-05): диспетчер раз в минуту
    # 24/7 — репосты в дублёры после выхода оригинала и оригиналы в режиме
    # queue. No-op без due-строк или при AD_REPOST_DISABLED.
    "ad-repost-dispatch": {
        "task": "tasks.celery_app.dispatch_ad_reposts",
        "schedule": crontab(minute="*"),
        "options": {"expires": 55, "catchup": False},
    },
    # Сторож pending-постов с прошедшей датой (аудит 2026-09-05): раз в час :05, 8–23 МСК.
    "ad-pending-watch": {
        "task": "tasks.celery_app.watch_ad_pending",
        "schedule": crontab(minute=5, hour="8-23"),
        "options": {"expires": 1800, "catchup": False},
    },
    # Рассылка оффера: раз в 5 минут 9–20 МСК (тихие часы кампании — внутри тика).
    "ad-outreach-dispatch": {
        "task": "tasks.celery_app.dispatch_ad_outreach",
        "schedule": crontab(minute="*/5", hour="9-20"),
        "options": {"expires": 240, "catchup": False},
    },
    # Снятие суточных закрепов рекламных постов: раз в час на :50, 8–23 МСК.
    "unpin-ad-posts": {
        "task": "tasks.celery_app.unpin_ad_posts",
        "schedule": crontab(minute=50, hour="8-23"),
        "options": {"expires": 1800, "catchup": False},
    },
    # Ретенция фото кабинета — раз в неделю ночью (данные вне БД, 10-ГБ бокс).
    "ad-photo-retention": {
        "task": "tasks.celery_app.ad_photo_retention",
        "schedule": crontab(minute=20, hour=4, day_of_week="sunday"),
        "options": {"expires": 3600, "catchup": False},
    },
    # Watchdog планировщика предложки: раз в час на :27, алёрт только если
    # есть просроченные строки, которые диспетчер не взял.
    "ad-repost-watchdog": {
        "task": "tasks.celery_app.check_ad_repost_heartbeat",
        "schedule": crontab(minute=27),
        "options": {"expires": 1800, "catchup": False},
    },
    # Liveness демона ВК-бота (setka-vk-bot): heartbeat пишется демоном после
    # каждого ответа Long Poll; протух >15 мин → Telegram (cooldown 6 ч). 24/7 —
    # бот клиентский; минуты не пересекаются с соседними сторожами.
    "vk-bot-watchdog": {
        "task": "tasks.vk_bot_tasks.check_vk_bot_heartbeat",
        "schedule": crontab(minute="3,13,23,33,43,53"),
        "options": {"expires": 540, "catchup": False},
    },
    # Авто-снятие рекламных постов по истечении срока (С2, ad-CRM) — 03:30 MSK
    # (после cleanup 03:00 и radar-retention 03:20). Срок опционален.
    "expire-ad-posts-daily": {
        "task": "tasks.celery_app.expire_ad_posts",
        "schedule": crontab(minute=30, hour=3),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Суточное напоминание о должниках по рекламе (С4, ad-CRM) — 10:00 MSK
    "alert-ad-debtors-daily": {
        "task": "tasks.celery_app.alert_ad_debtors",
        "schedule": crontab(minute=0, hour=10),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Суточное напоминание о перерасходе пакета публикаций (И2, ad-CRM) — 11:00 MSK
    "alert-ad-overspent-daily": {
        "task": "tasks.celery_app.alert_ad_overspent",
        "schedule": crontab(minute=0, hour=11),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Контент-конвейер ВК → сайты кластера (D-015): 3 раза в сутки.
    # Часы выбраны в промежутках между публикацией сводок, чтобы конвейер брал
    # уже сформированную ленту района, а не догонял её в момент сборки.
    # Пока CONVEYOR_SITES пуст — таска отрабатывает как no-op.
    "run-content-conveyor": {
        "task": "tasks.celery_app.run_content_conveyor",
        "schedule": crontab(minute=20, hour="9,14,19"),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Суточный сбор метрик рекламных публикаций (С3, ad-CRM) — 04:30 MSK
    "collect-ad-publication-stats-daily": {
        "task": "tasks.celery_app.collect_ad_publication_stats",
        "schedule": crontab(minute=30, hour=4),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Второй слот тех же метрик — 17:30 MSK (кабинет рекламодателя: клиент
    # видит статистику «1-2 раза в день», заказ владельца 2026-08-25).
    # Та же задача, run_collect_stats идемпотентен — просто свежий снимок.
    "collect-ad-publication-stats-evening": {
        "task": "tasks.celery_app.collect_ad_publication_stats",
        "schedule": crontab(minute=30, hour=17),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Суточный снимок числа подписчиков сообществ (фундамент графика роста) — 04:00 MSK
    # Раскрутка: состав считается ПОСЛЕ суточного снимка подписчиков (04:00), иначе
    # зачисление шло бы по вчерашним числам. Минуты 8 и 38 в сетке свободны.
    "promo-sync-enrollments": {
        "task": "tasks.promo_tasks.sync_promo_enrollments",
        "schedule": crontab(minute=8, hour=4),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Диспетчер раскрутки: 10 тиков в дневном окне, публикует ≤ суточного потолка.
    # Минута :08 в сетке свободна — два поста подряд на стене донора невозможны.
    "promo-dispatch": {
        "task": "tasks.promo_tasks.dispatch_promo",
        "schedule": crontab(minute=8, hour="10-19"),
        "options": {
            "expires": 3000,
            "catchup": False,
        },
    },
    # Сторож: молчит, пока каналы в сухом прогоне или продвигать некого.
    "promo-watchdog": {
        "task": "tasks.promo_tasks.check_promo_heartbeat",
        "schedule": crontab(minute=28),
        "options": {
            "expires": 1800,
            "catchup": False,
        },
    },
    # Размеры донорских сообществ — 4 вызова groups.getById на всю сеть, раз в неделю.
    "promo-members-refresh-weekly": {
        "task": "tasks.promo_tasks.refresh_promo_community_members",
        "schedule": crontab(minute=38, hour=5, day_of_week=2),
        "options": {
            "expires": 6 * 3600,
            "catchup": False,
        },
    },
    "collect-member-snapshots-daily": {
        "task": "tasks.celery_app.collect_member_snapshots",
        "schedule": crontab(minute=0, hour=4),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Еженедельный снимок УНИКАЛЬНЫХ подписчиков области без дублей — пн 02:30 UTC
    # (05:30 MSK), пока сеть спит. groups.getMembers по ~16 главным группам дёшев.
    "collect-oblast-unique-snapshots-weekly": {
        "task": "tasks.celery_app.collect_oblast_unique_snapshots",
        "schedule": crontab(minute=30, hour=2, day_of_week=1),
        "options": {
            "expires": 6 * 3600,
            "catchup": False,
        },
    },
    # Проверка непрочитанных сообщений каждый час с 8:00 до 22:00 в X:28
    #
    # Минуты тройки проверок (:15 предложка, :28 сообщения, :57 комментарии)
    # разведены сознательно и НЕ должны сближаться: каждая ходит в ВК по всем
    # регионам одной задачей, а ядро на боксе одно — стоявшие подряд :15/:16/:17
    # выстраивались в очередь в одном дочернем процессе ровно в окне OOM-убийств
    # HH:17–19 (P164).
    #
    # Разводить надо не только тройку между собой, но и от чужих обходов по всем
    # регионам. :28 выбран по карте занятости расписания: ближайшие тяжёлые
    # соседи — :25 (`scan-suggested-ads`, свой обход предложки, замер 33 с) и
    # :30 (опрос радара плюс волны), то есть у задачи с замеренным максимумом
    # 51 с остаётся больше минуты чистого времени с обеих сторон. Минута :24,
    # стоявшая здесь в первой редакции, оставляла до :25 девять секунд — и
    # сводила на нет офсет, ради которого `scan-suggested-ads` и поставлен на
    # :25/:55. Гейт — tests/test_notifications/test_beat_stagger.py.
    "check-unread-messages-hourly": {
        "task": "tasks.celery_app.check_unread_messages",
        "schedule": crontab(minute=28, hour="8-22"),  # Каждый час 8-22 на 28-й минуте
        "options": {
            "expires": 3000,
            "catchup": False,
        },
    },
    # Проверка комментариев за сутки каждый час с 8:00 до 22:00 в X:57
    # (самая тяжёлая из тройки — уведена дальше всех, см. комментарий выше)
    "check-recent-comments-hourly": {
        "task": "tasks.celery_app.check_recent_comments",
        "schedule": crontab(minute=57, hour="8-22"),  # Каждый час 8-22 на 57-й минуте
        "options": {
            "expires": 3000,
            "catchup": False,
        },
    },
    # Дневная сводка в 18:00
    "bulletin-daily": {
        "task": "tasks.celery_app.create_daily_bulletin",
        "schedule": crontab(hour=18, minute=0),  # 18:00 каждый день
        "options": {
            "expires": 3000,
            "catchup": False,
        },
    },
    # Очистка старых постов в 03:00
    "cleanup-daily": {
        "task": "tasks.celery_app.cleanup_old_posts",
        "schedule": crontab(hour=3, minute=0),  # 03:00 каждый день
        "options": {
            "expires": 3000,
            "catchup": False,
        },
    },
    # Ретеншн лога VK-шлюза: удалить строки старше N дней (env, дефолт 90) — 03:40 MSK
    "prune-gateway-requests-daily": {
        "task": "tasks.celery_app.prune_gateway_requests",
        "schedule": crontab(minute=40, hour=3),
        "options": {
            # expires 6 ч, а не 1 ч: на одном ядре ночной затор волн регулярно
            # превышает час, и с expires=3600 таска молча протухала в очереди
            # (все четыре prune не исполнялись с 05.09.2026 — найдено 28.09).
            # Таски идемпотентны и длятся сантисекунды, залеживание безопасно.
            "expires": 21600,
            "catchup": False,
        },
    },
    # Ретеншн журнала аудита сбора классификатора: строки старше N дней (env, дефолт 60) — 03:45 MSK
    "prune-collected-post-audit-daily": {
        "task": "tasks.celery_app.prune_collected_post_audit",
        "schedule": crontab(minute=45, hour=3),
        "options": {
            # См. комментарий у prune-gateway-requests-daily: expires обязан
            # переживать ночной затор, иначе ретеншн молча не работает.
            "expires": 21600,
            "catchup": False,
        },
    },
    # Ретеншн журнала отсеянных дублей: старше N дней (env, дефолт 7) — 03:48 MSK
    "prune-skipped-duplicates-daily": {
        "task": "tasks.celery_app.prune_skipped_duplicates",
        "schedule": crontab(minute=48, hour=3),
        "options": {
            # См. комментарий у prune-gateway-requests-daily.
            "expires": 21600,
            "catchup": False,
        },
    },
    # Ретеншн журнала публикаций (квоты тем): строки старше N дней (env, дефолт 400) — 03:47 MSK
    "prune-published-posts-daily": {
        "task": "tasks.celery_app.prune_published_posts",
        "schedule": crontab(minute=47, hour=3),
        "options": {
            # См. комментарий у prune-gateway-requests-daily.
            "expires": 21600,
            "catchup": False,
        },
    },
    # Снапшот выученных правил классификатора (overlay ADR-0005) в untracked-файл — 03:50 MSK
    "snapshot-learned-rules-daily": {
        "task": "tasks.celery_app.snapshot_learned_rules",
        "schedule": crontab(minute=50, hour=3),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Watchdog «давно нет сводок»: раз в час 10:00–22:00 на :05. Если тема
    # novost не публиковалась дольше порога (6ч в самой задаче) — Telegram-алёрт
    # (с 6ч-cooldown). novost-волны идут 6×/сутки (макс дневной зазор ~5ч), порог
    # 6ч с запасом. Ночью не гоняем — простой 20:40→6:40 легитимен.
    "bulletin-heartbeat-watchdog": {
        "task": "tasks.celery_app.check_bulletin_heartbeat",
        "schedule": crontab(minute=5, hour="10-22"),
        "options": {
            "expires": 1800,
            "catchup": False,
        },
    },
    # ========================================================================
    # POSTOPUS MIGRATION: Crontab replacement → Celery Beat
    # Original crontab entries migrated from old_postopus
    # ========================================================================
    # Reklama (ads): 5 10,14,19 * * *
    "postopus-reklama-10": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=5, hour=10),
        "args": ("reklama",),
        "options": {"expires": 3600},
    },
    "postopus-reklama-14": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=5, hour=14),
        "args": ("reklama",),
        "options": {"expires": 3600},
    },
    "postopus-reklama-19": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=5, hour=19),
        "args": ("reklama",),
        "options": {"expires": 3600},
    },
    # Тема "sosed" (два слота 10:20 и 20:20) СНЯТА 2026-09-14 по решению владельца.
    #
    # Соседский обмен новостями существовал в двух механиках, и работала из них
    # одна. Замер за 30 дней перед снятием:
    #   * тема "sosed" (парсинг сообществ category="sosed" ВНУТРИ своего района):
    #     собрано 4109 постов, оставлено фильтром 25, опубликовано сводок — НОЛЬ;
    #     сообществ такой категории во всей сети всего два;
    #   * "bulletin-share-neighbors-daily" ниже (репост #Новости с главных групп
    #     соседних районов): 25 опубликованных сводок за то же окно.
    # Два слота в сутки тратили вызовы ВК и работу фильтров, не давая ничего.
    #
    # Владелец: «соседи обмениваются новостями с ближними соседями, по одной в
    # день, чтобы друг друга поддерживать; если это делается разными путями —
    # оставь один работающий, остальное удали». Оставлен тот, что и описывает
    # эту функцию: cross-region обмен раз в сутки.
    #
    # Имя темы нигде не удалено — оно остаётся валидным ключом переопределений
    # (POSTOPUS_BULLETIN_THEMES) и меткой классификатора. Снят только запуск.
    #
    # Соседский обмен новостями (cross-region): раз в сутки утром. Каждый регион
    # с непустым Region.neighbors репостит #Новости с главных групп соседей.
    # Движок — modules.cascaded_bulletin.run_neighbor_bulletin.
    "bulletin-share-neighbors-daily": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_neighbor_share",
        "schedule": crontab(minute=30, hour=8),
        "options": {"expires": 3600},
    },
    # Novost (news): 40 6,11,12,16,18,20 * * *
    "postopus-novost-6": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=40, hour=6),
        "args": ("novost",),
        "options": {"expires": 3600},
    },
    "postopus-novost-11": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=40, hour=11),
        "args": ("novost",),
        "options": {"expires": 3600},
    },
    "postopus-novost-12": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=40, hour=12),
        "args": ("novost",),
        "options": {"expires": 3600},
    },
    "postopus-novost-16": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=40, hour=16),
        "args": ("novost",),
        "options": {"expires": 3600},
    },
    "postopus-novost-18": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=40, hour=18),
        "args": ("novost",),
        "options": {"expires": 3600},
    },
    "postopus-novost-20": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=40, hour=20),
        "args": ("novost",),
        "options": {"expires": 3600},
    },
    # ───────────────────────────────────────────────────────────────────────
    # Областные тематические волны (community-mode oblast, 2026-05).
    # kirov_obl перешёл с каскада (сводка-сводок из районов) на свой пул
    # communities по темам — публикует тематические сводки как район.
    # Базовые темы (novost/sport/kultura/admin) область получает на ОБЩИХ волнах
    # postopus-<theme>-* (после снятия хардкод-исключения kirov_obl). Ниже —
    # волны под расширенную областную повестку. strict=True: волна берёт только
    # регионы с communities ИМЕННО этой темы (районы не затрагиваются — у них
    # таких сообществ нет). Слоты разнесены 7:30–22:00 на :50/:10 (свободны от
    # :40/:45/:20/:30 базовых тем). Старые postopus-kirov-oblast-* (каскад) сняты.
    "postopus-proisshestviya-8": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=8),
        "kwargs": {"theme": "proisshestviya", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-proisshestviya-14": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=14),
        "kwargs": {"theme": "proisshestviya", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-proisshestviya-20": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=20),
        "kwargs": {"theme": "proisshestviya", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-zhkh-7": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=7),
        "kwargs": {"theme": "zhkh", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-zhkh-15": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=15),
        "kwargs": {"theme": "zhkh", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-selhoz-9": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=9),
        "kwargs": {"theme": "selhoz", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-selhoz-16": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=16),
        "kwargs": {"theme": "selhoz", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-nauka-10": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=10),
        "kwargs": {"theme": "nauka", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-nauka-17": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=17),
        "kwargs": {"theme": "nauka", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-promyshlennost-11": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=11),
        "kwargs": {"theme": "promyshlennost", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-promyshlennost-19": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=19),
        "kwargs": {"theme": "promyshlennost", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-molodezh-12": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=12),
        "kwargs": {"theme": "molodezh", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-molodezh-18": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=18),
        "kwargs": {"theme": "molodezh", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-zdorovie-13": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=13),
        "kwargs": {"theme": "zdorovie", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-zdorovie-21": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=50, hour=21),
        "kwargs": {"theme": "zdorovie", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-priroda-12": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=10, hour=12),
        "kwargs": {"theme": "priroda", "strict": True},
        "options": {"expires": 3600},
    },
    "postopus-priroda-19": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=10, hour=19),
        "kwargs": {"theme": "priroda", "strict": True},
        "options": {"expires": 3600},
    },
    # Татарстан (областной каскадная сводка из главных групп bal/kukmor).
    # Детей всего 2 — 2 слота/сутки достаточно (чаще = повторы). minute=45 как
    # у kirov-oblast (после волн novost :40). Публикует при наличии
    # community-токена COMM_239149826 (см. миграцию 016).
    "postopus-tatarstan-oblast-9": {
        "task": "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        "schedule": crontab(minute=45, hour=9),
        "kwargs": {"region_code": "tatarstan_obl", "theme": "oblast"},
        "options": {"expires": 3600},
    },
    "postopus-tatarstan-oblast-19": {
        "task": "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        "schedule": crontab(minute=45, hour=19),
        "kwargs": {"region_code": "tatarstan_obl", "theme": "oblast"},
        "options": {"expires": 3600},
    },
    # Kultura (culture): 20 7,13,16,19,21 * * *
    "postopus-kultura-7": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=7),
        "args": ("kultura",),
        "options": {"expires": 3600},
    },
    "postopus-kultura-13": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=13),
        "args": ("kultura",),
        "options": {"expires": 3600},
    },
    "postopus-kultura-16": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=16),
        "args": ("kultura",),
        "options": {"expires": 3600},
    },
    "postopus-kultura-19": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=19),
        "args": ("kultura",),
        "options": {"expires": 3600},
    },
    "postopus-kultura-21": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=21),
        "args": ("kultura",),
        "options": {"expires": 3600},
    },
    # Sport: 30 12,19 * * *
    "postopus-sport-12": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=30, hour=12),
        "args": ("sport",),
        "options": {"expires": 3600},
    },
    "postopus-sport-19": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=30, hour=19),
        "args": ("sport",),
        "options": {"expires": 3600},
    },
    # Admin: 20 8,12,20 * * *
    "postopus-admin-8": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=8),
        "args": ("admin",),
        "options": {"expires": 3600},
    },
    "postopus-admin-12": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=12),
        "args": ("admin",),
        "options": {"expires": 3600},
    },
    "postopus-admin-20": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=20, hour=20),
        "args": ("admin",),
        "options": {"expires": 3600},
    },
    # Union: 30 11,17 * * *
    "postopus-union-11": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=30, hour=11),
        "args": ("union",),
        "options": {"expires": 3600},
    },
    "postopus-union-17": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=30, hour=17),
        "args": ("union",),
        "options": {"expires": 3600},
    },
    # Detsad: 30 13 * * *
    "postopus-detsad-13": {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=30, hour=13),
        "args": ("detsad",),
        "options": {"expires": 3600},
    },
    # Addons (roulette): 20 6,11,18,22 * * *
    # Тема "addons" (четыре слота 6:20, 11:20, 18:20, 22:20) СНЯТА 2026-09-22
    # по решению владельца.
    #
    # Замысел владельца дословно: «addons было задумано изначально как выпуск
    # новостей не из района, а извне — добавка: научпоп, путешествия, музыка,
    # кино, вообще какие-то общие развлечения».
    #
    # Замер за 7 дней перед снятием показал, что тема делала не это. Из 1752
    # её публикаций классификатор отнёс к «кругозору» лишь 139 — восемь
    # процентов. Остальные 92 %: новости 537, дети и образование 330,
    # администрация 297, культура 221, спорт 141, объявления 78, соседи 9 —
    # то есть обычный районный материал тех самых категорий, у которых есть
    # свои темы.
    #
    # Механизм объясняет перекос: собственных сообществ у темы не было НИ В
    # ОДНОМ из 57 регионов, поэтому её волна уходила в запасной путь «читать
    # весь пул района» и публиковала то, что не забрали волны остальных тем.
    # Получился не выпуск извне, а канал перелива районного контента.
    #
    # Исходную задачу при этом исправно делает другой канал — ежедневная
    # рассылка «Кругозор» (modules/krugozor_broadcast.py, 20:00): 21.09 она
    # разослала сводку в 54 региона из 54 из 12 активных сообществ.
    #
    # Цена снятия названа владельцу числом и принята им: 26 % публикаций сети.
    #
    # Имя темы нигде не удалено — оно остаётся валидным ключом переопределений
    # и меткой классификатора, как и у снятой 14.09 темы "sosed". Снят только
    # запуск. Разбор целиком — docs/pending/P180.
    # Discovery health-recheck — еженедельно (ПН 04:00 MSK).
    # Обходит все Region.is_active=True; для каждого Community.is_active=True
    # делает wall.get + AI-категоризацию, обновляет health_status / last_post_at
    # / checked_at / suggested_category. По итогам — Telegram-alert (если есть
    # dead / dormant / changed_category). См. modules/discovery/health_check.py.
    "discovery-recheck-weekly": {
        "task": "tasks.discovery_tasks.recheck_all_active_regions",
        "schedule": crontab(hour=4, minute=0, day_of_week="mon"),
        "options": {
            "expires": 3600,
            "catchup": False,
        },
    },
    # Dormant-политика, ре-проверка: авто-вынос выводит предмет ИЗ ОБЛАСТИ
    # НАБЛЮДЕНИЯ — вынесенное сообщество больше не опрашивается, и ошибка выноса
    # ненаблюдаема по построению. Пока вердикт ставил человек, «вынес и забыл»
    # было приемлемо; с июля его ставит автомат (рекомендация brain 2026-08-22,
    # пул #182). Ожившие возвращаются в парс автоматически — вердикт обязан быть
    # отзывным. Цена ~48 вызовов ВК в месяц.
    # Идёт ПЕРЕД digest'ом (09:30) намеренно: иначе digest отрапортует как
    # вынесенное то, что ре-проверка в тот же час вернёт.
    "dormant-recheck-disabled-monthly": {
        "task": "tasks.discovery_tasks.dormant_recheck_disabled",
        "schedule": crontab(minute=15, hour=9, day_of_month="1"),
        "options": {
            "expires": 6 * 3600,
            "catchup": False,
        },
    },
    # Dormant-политика: ежемесячный digest вынесенных (условие brain 2026-06-30
    # к auto-disable T1 — владелец видит и может возразить; soft-disable обратим).
    "dormant-disable-digest-monthly": {
        "task": "tasks.discovery_tasks.dormant_disable_digest",
        "schedule": crontab(minute=30, hour=9, day_of_month="1"),
        "options": {
            "expires": 6 * 3600,
            "catchup": False,
        },
    },
    # Замер прав токенов опытом — раз в месяц, 1-го в 04:10 (решение владельца
    # 2026-07-26). Только read-only пробы; ежедневный прогон не нужен — права
    # меняются редко, а ценность в том, чтобы дрейф («вчера читал стену, сегодня
    # error 27») не прошёл незамеченным до сломанного сбора. Изменения с
    # прошлого замера уходят в лог и Telegram-алерт, снапшот виден на /tokens.
    "probe-token-capabilities-monthly": {
        "task": "tasks.celery_app.probe_token_capabilities",
        "schedule": crontab(minute=10, hour=4, day_of_month="1"),
        "options": {"expires": 6 * 3600, "catchup": False},
    },
    # Rolling discovery — ОТКЛЮЧЕНО 2026-06-02 (по решению владельца).
    #
    # ⚠️ Причина отключения снята 2026-08-12: нейро-классификация переведена с
    # Groq (403, бюджета не было) на DeepSeek (D-024), и ключ есть. Условие
    # «вернуть, когда к discovery подключат нейро-фильтр» наступило. Слот
    # оставлен выключенным сознательно: включение меняет объём авто-подбора и
    # расход токенов, это решение владельца, а не следствие смены движка.
    #
    # Алгоритмический авто-подбор кандидатов БЕЗ нейро-классификации
    # даёт ~98% мусора: на Туже из 136 авто-кандидатов годных ≈0 — омонимы
    # («Тужа» ↔ фраза «не тужи(ть)»), чужие сёла (Ситки→Аляска,
    # Михайловское→Карелия), коммерция, Киров-wide паблики. Без нейронки в петле
    # подбор ведём ВРУЧНУЮ через скил `/discover_communities` (читаем посты,
    # классифицируем в чате). Вернуть, когда к discovery подключат нейро-фильтр.
    # См. tasks.discovery_tasks.discover_rolling_one_region.
    # "discovery-rolling-daily": {
    #     "task": "tasks.discovery_tasks.discover_rolling_one_region",
    #     "schedule": crontab(hour=3, minute=30),
    #     "options": {
    #         "expires": 3600,
    #         "catchup": False,
    #     },
    # },
    # Copy Setka (network repost): 7,37 * * * *
    "postopus-copy-setka-07": {
        "task": "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        "schedule": crontab(minute=7),
        "kwargs": {"region_code": "copy", "theme": "setka"},
        "options": {"expires": 1800},
    },
    "postopus-copy-setka-37": {
        "task": "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        "schedule": crontab(minute=37),
        "kwargs": {"region_code": "copy", "theme": "setka"},
        "options": {"expires": 1800},
    },
    # Поток «Кругозор» (научпоп → веером на районные паблики): раз в день 20:00 MSK
    # (вечер — пик чтения «умного»/lean-back контента). Гейт KRUGOZOR_BROADCAST_DISABLED
    # (OFF по умолчанию). Старт 1×/день; при хорошем охвате добавляется обед 13:00.
    "krugozor-broadcast-evening": {
        "task": "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        "schedule": crontab(minute=0, hour=20),
        "kwargs": {"region_code": "copy", "theme": "krugozor"},
        "options": {"expires": 3600},
    },
    # Контент-радар (Ф0.2): fan-out поллер источников каждые 10 мин круглосуточно
    # (личная лента — не публикация в сеть, ночное окно не нужно). Внутри прогона
    # поллятся только активные источники с ≥1 подпиской; пустой радар = no-op.
    "radar-poll-sources": {
        "task": "tasks.radar_tasks.poll_radar_sources",
        "schedule": crontab(minute="*/10"),
        "options": {"expires": 540, "catchup": False},
    },
    # Intake-бот «Карман» (приём форвардов каналов): каждую минуту getUpdates.
    # No-op, пока не заданы RADAR_BOT_NAME + токен (#008). Форвард поста канала
    # боту → канал в радар + подписка оператора.
    "radar-intake-bot": {
        "task": "tasks.radar_tasks.poll_radar_bot",
        "schedule": crontab(minute="*"),
        "options": {"expires": 55, "catchup": False},
    },
    # VK-интейк (приём кодов привязки VK-лички через Bots Long Poll сообщества):
    # каждую минуту. No-op, пока не задан RADAR_VK_COMMUNITY_ID + community-токен
    # (#008). Юзер пишет код сообществу-точке → ловим vk_id → vk_dm-вывод в личку.
    "radar-vk-intake": {
        "task": "tasks.radar_tasks.poll_radar_vk_intake",
        "schedule": crontab(minute="*"),
        "options": {"expires": 55, "catchup": False},
    },
    # ВК-бот кабинета: Long Poll крутит отдельный демон setka-vk-bot
    # (scripts/vk_bot_daemon.py) — в beat его нет намеренно: воркер на проде
    # однопроцессный, минутный тик давал клиенту минуту ожидания на каждый шаг.
    # Задача tasks.vk_bot_tasks.poll_sarafan_vk_bot оставлена для ручного тика;
    # за живостью демона следит vk-bot-watchdog (выше, рядом с ad-repost-watchdog).
    # Ретенция ленты радара: элементы старше 30 дней (RADAR_ITEMS_RETENTION_DAYS)
    # удаляются ночью в 03:20 (после cleanup-daily в 03:00). Сохранёнки —
    # снимки, не страдают (FK SET NULL).
    "radar-items-retention-daily": {
        "task": "tasks.radar_tasks.cleanup_old_radar_items",
        "schedule": crontab(minute=20, hour=3),
        "options": {"expires": 3600, "catchup": False},
    },
    # Headless-классификация постов на DeepSeek (D-024): каждые 3 часа на :35.
    # Частота подобрана под фактический поток облачной рутины, которую эта таска
    # заменяет: ~1600 вердиктов в сутки при потолке CLASSIFIER_PENDING_MAX=200 —
    # это 8 прогонов. No-op, пока CLASSIFIER_HEADLESS_ENABLED=0 (по умолчанию):
    # включать только ВМЕСТО рутины, иначе оба источника воруют друг у друга
    # посты — запись идемпотентна по lip.
    "classifier-headless": {
        "task": "tasks.celery_app.classify_pending_posts",
        "schedule": crontab(minute=35, hour="*/3"),
        "options": {"expires": 3 * 3600, "catchup": False},
    },
    # Метрики постов под рейтинг отбора (звено 5, шаг 1): каждые 3 часа на :05.
    # Сдвиг от classifier-headless (:35) — сознательный: обе таски ходят за
    # живым READ-токеном, и толкаться за ним в одну минуту незачем.
    # Объём круга посчитан на проде 19.08: окно 72 часа = 7774 поста = 78
    # батчей wall.getById, ~620 вызовов в сутки.
    "post-metrics-refresh": {
        "task": "tasks.celery_app.refresh_post_metrics",
        "schedule": crontab(minute=5, hour="*/3"),
        "options": {"expires": 3 * 3600, "catchup": False},
    },
    # Дистилляция правок оператора в правила — звено 7 петли (D-024): раз в
    # неделю, ночь понедельника. Частота продиктована сырьём, а не удобством:
    # правила рождаются из НАКОПЛЕННЫХ правок, и ежедневный прогон предлагал бы
    # обобщения по двум-трём случаям — ровно то, что порог MIN_CORRECTIONS и
    # запрещает. Меньше 10 правок за окно → прогон возвращает
    # not-enough-material и не тратит ни токена.
    # Сторож «ИИ-фильтр молчит» — раз в 2 часа на :05. Смотрит на свежесть
    # последнего вердикта в БД, а не на исход таски: в инциденте 2026-08-19
    # таска рапортовала успех, ничего не сделав, и отказ прожил трое суток.
    "classifier-stale-watchdog": {
        "task": "tasks.celery_app.check_classifier_heartbeat",
        "schedule": crontab(minute=5, hour="*/2"),
        "options": {"expires": 3600, "catchup": False},
    },
    "classifier-distill-weekly": {
        "task": "tasks.celery_app.distill_classifier_rules",
        "schedule": crontab(minute=40, hour=4, day_of_week=1),
        "options": {"expires": 6 * 3600, "catchup": False},
    },
    # Сторож целостности Telegram-ботов (инцидент 2026-08-12): раз в час на :47.
    # Смотрит на бота ГЛАЗАМИ ПОДПИСЧИКА — имя, описание, вебхук, — а не на то,
    # прошла ли отправка: угон не ломает публикацию, он ей пользуется, и
    # переименование бота в рекламу казино прожило больше суток незамеченным.
    "telegram-bot-identity-guard": {
        "task": "tasks.celery_app.check_telegram_bot_identity",
        "schedule": crontab(minute=47),
        "options": {"expires": 3000, "catchup": False},
    },
    # Watchdog поллера радара (#018): раз в час на :12. Алёртит только если есть
    # активные подписанные источники, а heartbeat протух (>40 мин) — retired≠dead.
    "radar-poll-watchdog": {
        "task": "tasks.radar_tasks.check_radar_poll_heartbeat",
        "schedule": crontab(minute=12),
        "options": {"expires": 1800, "catchup": False},
    },
    # Flow B: зеркало стены ВК-сообщества «Гоньба» → Telegram @gonba_life.
    # Каждые ~20 мин в активные часы; cap постов/run в самой задаче (анти-флуд).
    "telegram-gonba-mirror": {
        "task": "tasks.parsing_scheduler_tasks.mirror_community_to_telegram",
        "schedule": crontab(minute="10,40", hour="7-23"),
        "options": {"expires": 1200, "catchup": False},
    },
    # Сетевая рассылка (директива brain 2026-06-14): диспетчер-публикатор раз в
    # минуту публикует wall.post немедленно в назревшие кампании (НЕ в VK-отложку).
    # No-op, пока нет запланированных кампаний или BROADCAST_DISABLED.
    "broadcast-dispatch": {
        "task": "tasks.broadcast_tasks.dispatch_broadcasts",
        "schedule": crontab(minute="*"),
        "options": {"expires": 55, "catchup": False},
    },
    # Watchdog рассылки (#018): раз в час на :22. Алёртит только если есть
    # просроченные кампании, а диспетчер их не разослал (молча встал).
    "broadcast-watchdog": {
        "task": "tasks.broadcast_tasks.check_broadcast_heartbeat",
        "schedule": crontab(minute=22),
        "options": {"expires": 1800, "catchup": False},
    },
}


if __name__ == "__main__":
    # Для отладки
    logger.info("Celery app configured successfully!")
    logger.info(f"Broker: {app.conf.broker_url}")
    logger.info(f"Backend: {app.conf.result_backend}")
    logger.info(f"Timezone: {app.conf.timezone}")
    logger.info(f"Beat schedule: {list(app.conf.beat_schedule.keys())}")
