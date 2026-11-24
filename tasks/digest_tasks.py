"""Celery-задача вечерней сводки «Короткой строкой» (P182, заказ 20.10).

Сборник выходит раз в сутки, в 21:40 МСК — последним постом дня. Материал —
посты, которые сегодня прошли фильтр, но не попали в ленту: их пересказы
собирает ``modules.publisher.daily_digest``, публикация идёт обычным путём
(``VKPublisher`` + журнал публикаций + курсор дедупа), поэтому завтрашние волны
эти посты уже не возьмут.

Kill-switch — ``DAILY_DIGEST_ENABLED=0``: рубрика выключается одной переменной,
не трогая сбор и ленту. Пустой сборник (мало материала) — штатный исход, а не
сбой: в лог уходит причина, в отчёт — ``skipped``.

**Веером (решение владельца 24.11, P234).** Прежде сводка была **одной** задачей
на 76 регионов: она занимала единственный слот воркера целиком, её снимал общий
лимит 3600 с, и сборник выходил примерно для трети сети (замер 23.11 — 21 регион
из 76), а волна ``admin`` (22:20) ждала конца обхода. Теперь ``beat`` зовёт
``publish_daily_digest_fanout`` — задачу на регион, разнесённую по минутам
(``DAILY_DIGEST_REGION_SPREAD_SECONDS``) и уходящую в отдельную очередь
(``SETKA_DIGEST_QUEUE``), ровно как веер волны (ADR-0020).

**День закреплён в задаче.** Кандидаты берутся «с полуночи» (``day_start_utc``),
поэтому задача, дошедшая после полуночи (фото в сборнике держат слот дольше
часа), собрала бы **чужой** день. Веер передаёт каждому региону ``since`` —
начало московского дня, за который выпускается сборник.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from tasks.celery_app import app
from utils.celery_asyncio import run_coro

logger = logging.getLogger(__name__)

DIGEST_THEME = "korotko"
DIGEST_KIND = "digest"

#: Потолок вложений на один пост ВК. Превышение отвергает весь пост вместе с
#: текстом — поэтому лимит дублируется в трёх местах (черновик, отбор, здесь).
VK_MAX_ATTACHMENTS = 10

#: Сколько задача сводки может ждать своей очереди, прежде чем протухнуть.
#: Счёт от **слота** (не от постановки): с фото сборник держит слот дольше часа
#: (замер 23.11 — 44 мин сна на троттлинге загрузки), и обычный час от
#: ``countdown`` выкинул бы хвост веера, не дав ему выйти вовсе.
DIGEST_TASK_EXPIRES_SEC = 4 * 3600


@app.task(name="tasks.digest_tasks.publish_daily_digest")
def publish_daily_digest(
    region_code: Optional[str] = None, dry_run: bool = False
) -> Dict[str, Any]:
    """Собрать и опубликовать вечернюю сводку (или показать превью при ``dry_run``)."""
    try:
        return run_coro(_publish_daily_digest(region_code, dry_run=dry_run))
    except Exception as e:  # noqa: BLE001 — задача не должна валить beat-цикл
        logger.error("daily digest: задача упала: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


@app.task(name="tasks.digest_tasks.publish_daily_digest_region")
def publish_daily_digest_region(
    region_code: str, since_iso: Optional[str] = None, dry_run: bool = False
) -> Dict[str, Any]:
    """Сборник **одного** региона — задача веера (P234).

    ``since_iso`` — начало московского дня, за который выпускается сборник
    (ISO-строка, naive UTC). Оно закреплено в задании, а не берётся «сейчас»:
    хвост веера с фото доходит после полуночи, и без закрепления собрал бы
    **чужой** день.
    """
    since: Optional[datetime] = None
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso)
        except ValueError:
            logger.warning("daily digest: регион=%s не разобран since=%r", region_code, since_iso)
    try:
        return run_coro(_publish_daily_digest(region_code, dry_run=dry_run, since=since))
    except Exception as e:  # noqa: BLE001 — один район не валит остальные
        logger.error("daily digest: регион=%s задача упала: %s", region_code, e, exc_info=True)
        return {"success": False, "region": region_code, "error": str(e)}


@app.task(name="tasks.digest_tasks.publish_daily_digest_fanout")
def publish_daily_digest_fanout(dry_run: bool = False) -> Dict[str, Any]:
    """Веером: задача на регион, регионы разнесены по минутам.

    Зачем (P234): прежде сводка была одной задачей на 76 регионов и занимала
    единственный слот воркера целиком — её снимал общий лимит 3600 с, и сборник
    выходил примерно для трети сети, а волна ``admin`` (22:20) ждала конца
    обхода. Теперь у каждого региона своя задача со своим сроком: хвост списка
    не выпадает, а периодика проходит между регионами.

    **Что веер НЕ чинит:** сон на троттлинге загрузки фото живёт внутри задачи
    (замер 23.11 — 38 окон × 70 с = 44 мин из 60). Фото в сборнике владелец
    оставил (решение 24.11), поэтому прогон с фото занимает больше часа — и
    веер растягивает его, а не сокращает.

    ``dry_run=True`` — прогон без публикации: превью соберётся и уйдёт в лог
    воркера (тот же путь, что у ``publish_daily_digest(dry_run=True)``).
    """
    from config.runtime import get_digest_queue, get_digest_region_spread_seconds
    from modules.publisher.daily_digest import day_start_utc

    try:
        region_codes = run_coro(_active_region_codes())
    except Exception as e:  # noqa: BLE001 — beat-цикл не должен падать
        logger.error("daily digest: список регионов не взят: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}

    # Порядок регионов фиксируем по коду: слоты тогда не прыгают день ото дня.
    region_codes = sorted(region_codes)
    spread = get_digest_region_spread_seconds()
    # День закрепляем **в момент постановки**: хвост веера (с фото) доходит
    # после полуночи, и «с полуночи» на месте запуска взяло бы уже чужой день.
    since_iso = day_start_utc().isoformat()

    results = []
    total = len(region_codes)
    for index, code in enumerate(region_codes):
        countdown = 0
        if spread > 0 and total > 1:
            countdown = int(round(index * spread / float(total - 1)))
        results.append(
            publish_daily_digest_region.apply_async(
                args=[code, since_iso],
                kwargs={"dry_run": bool(dry_run)},
                countdown=countdown,
                # Задача не должна протухнуть, пока ждёт слот и пока тянется
                # очередь из-за троттлинга фото.
                expires=countdown + DIGEST_TASK_EXPIRES_SEC,
                # Отдельная очередь (ADR-0020): 76 задач на общей очереди
                # занимают голову списка, и мелкая периодика уезжает за ними.
                queue=get_digest_queue(),
            )
        )
    logger.info(
        "daily digest: веер — регионов=%d, окно разведения=%s с, первый слот сразу",
        total,
        int(spread),
    )
    return {
        "success": True,
        "regions": region_codes,
        "tasks": [r.id for r in results],
        "spread": int(spread),
        "since": since_iso,
    }


async def _active_region_codes() -> List[str]:
    """Коды активных регионов, у которых есть своя группа ВК (как и раньше)."""
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Region

    async with AsyncSessionLocal() as session:
        stmt = select(Region.code).where(Region.is_active.is_(True), Region.vk_group_id.isnot(None))
        return [str(code) for code in (await session.execute(stmt)).scalars().all()]


def _enabled() -> bool:
    raw = (os.getenv("DAILY_DIGEST_ENABLED", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _photos_enabled() -> bool:
    """Фото в сборнике (P214). Kill-switch — ``DAILY_DIGEST_PHOTOS=0``.

    По умолчанию включено: это утверждённое решение владельца. Отключается
    одной переменной, без выката — на случай, если картинки поедут.
    """
    raw = (os.getenv("DAILY_DIGEST_PHOTOS", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


async def _publish_daily_digest(
    region_code: Optional[str] = None,
    *,
    dry_run: bool = False,
    since: Optional[datetime] = None,
) -> Dict[str, Any]:
    if not _enabled():
        return {"success": True, "skipped": "disabled"}

    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Region

    async with AsyncSessionLocal() as session:
        stmt = select(Region).where(Region.is_active.is_(True), Region.vk_group_id.isnot(None))
        if region_code:
            stmt = stmt.where(Region.code == region_code)
        regions = (await session.execute(stmt.order_by(Region.code))).scalars().all()

        reports: List[Dict[str, Any]] = []
        for region in regions:
            try:
                report = await _digest_for_region(session, region, dry_run=dry_run, since=since)
            except Exception as e:  # noqa: BLE001 — один район не валит остальные
                logger.warning("daily digest: регион=%s сбой: %s", region.code, e, exc_info=True)
                report = {"region": region.code, "success": False, "error": str(e)}
            if report.get("published") or report.get("preview") or report.get("skipped"):
                reports.append(report)

    published = sum(1 for r in reports if r.get("published"))
    logger.info(
        "daily digest: регионов=%d опубликовано=%d%s",
        len(regions),
        published,
        "" if not dry_run else " (dry-run)",
    )
    return {
        "success": True,
        "regions_total": len(regions),
        "published": published,
        "reports": reports,
    }


async def _digest_for_region(
    session: Any, region: Any, *, dry_run: bool, since: Optional[datetime] = None
) -> Dict[str, Any]:
    from sqlalchemy import select

    from database.models_extended import RegionConfig, WorkTable
    from modules.deduplication.bulletin_history import build_region_dedup_sets
    from modules.publisher import daily_digest
    from modules.publisher.postopus_bulletin_headers import resolve_bulletin_hashtags

    region_config = (
        (await session.execute(select(RegionConfig).where(RegionConfig.region_code == region.code)))
        .scalars()
        .first()
    )
    if region_config is None:
        logger.info("daily digest: регион=%s без RegionConfig — пропуск", region.code)
        return {"region": region.code, "skipped": "no_region_config"}

    work_tables = (
        (await session.execute(select(WorkTable).where(WorkTable.region_code == region.code)))
        .scalars()
        .all()
    )
    cursor_lips, _ = build_region_dedup_sets(work_tables)

    posts = await daily_digest.collect_candidates(
        session, region.code, since=since, exclude_lips=cursor_lips
    )
    # Посты без текста, но с фото: в сборнике они живут картинкой (решение
    # владельца 03.11). Строк в тексте не добавляют — иначе поехала бы
    # привязка «пункт ↔ источник» в фактчеке.
    photo_candidates = await daily_digest.collect_photo_candidates(
        session, region.code, since=since, exclude_lips=cursor_lips
    )
    if len(posts) < daily_digest.MIN_ITEMS:
        logger.info("daily digest: регион=%s кандидатов=%d — пропуск", region.code, len(posts))
        return {"region": region.code, "skipped": f"candidates={len(posts)}"}

    names = await _source_names(session, list(posts) + list(photo_candidates))
    theme_tags, local_hashtag = resolve_bulletin_hashtags(region_config, DIGEST_THEME, region)
    try:
        configured_max = int(getattr(region_config, "text_post_maxsize_simbols", 0) or 0)
    except (TypeError, ValueError):
        configured_max = 0
    max_total_chars = min(
        daily_digest.MAX_TOTAL_CHARS,
        configured_max if configured_max > 0 else daily_digest.MAX_TOTAL_CHARS,
    )
    draft = await daily_digest.build_digest_from_posts(
        posts,
        names=names,
        theme_tags=theme_tags,
        local_hashtag=local_hashtag,
        region_name=getattr(region, "name", "") or "",
        region_code=region.code,
        max_total_chars=max_total_chars,
        photo_candidates=photo_candidates,
        # Водяной знак на фото — название НАШЕЙ группы («МАЛМЫЖ - ИНФО»), куда
        # выходит сборник: это её марка, а не подпись чужого сообщества.
        photo_watermark=str(getattr(region, "name", "") or ""),
    )
    if draft is None:
        return {"region": region.code, "skipped": "empty_digest"}

    if dry_run:
        # Превью печатается в лог: на боксе скрипт требует env (его читает только
        # root), поэтому штатный путь приёмки — `celery call ... dry_run=true`,
        # а результат смотрят в журнале воркера.
        logger.info("daily digest preview: регион=%s\n%s", region.code, draft.text)
        return {"region": region.code, "preview": draft.text, "lips": draft.lips}

    return await _publish(session, region, draft)


async def _source_names(session: Any, posts: List[Dict[str, Any]]) -> Dict[str, str]:
    """Имена источников: из базы, поверх — официальные из VK (если доступен)."""
    from sqlalchemy import select

    from database.models import Community

    owner_ids = sorted({int(p.get("owner_id") or 0) for p in posts if p.get("owner_id")})
    if not owner_ids:
        return {}
    rows = (
        await session.execute(
            select(Community.vk_id, Community.name).where(
                Community.vk_id.in_([gid for gid in owner_ids] + [-gid for gid in owner_ids])
            )
        )
    ).fetchall()
    names: Dict[str, str] = {}
    for vk_id, name in rows:
        try:
            key = str(abs(int(vk_id)))
        except (TypeError, ValueError):
            continue
        if name:
            names[key] = str(name)

    # Официальные названия — тот же источник, что у ленты (ADR-0011). Сбой VK
    # не мешает сборнику: остаётся имя из базы.
    try:
        from modules.source_names import fetch_official_names
        from modules.vk_monitor.vk_client import VKClient
        from modules.vk_token_router import pick_healthy_read_token

        candidate = await pick_healthy_read_token(session)
        if candidate:
            official = fetch_official_names(VKClient(candidate.token), owner_ids)
            for group_id, name in (official or {}).items():
                if name:
                    names[str(abs(int(group_id)))] = str(name)
    except Exception:  # pragma: no cover - сеть/VK
        logger.debug("daily digest: официальные имена недоступны", exc_info=True)
    return names


async def _photo_attachments(publisher: Any, region: Any, draft: Any) -> List[str]:
    """Фотографии сборника → строки вложений ``photo<owner>_<id>``.

    Путь: скачать фото источника → обработать (водяной знак сообщества, шум,
    ресайз без кропа — спецификация владельца из P203) → залить в **нашу**
    группу и приложить уже свой id. Чужое фото приложить нельзя: VK не отдаст
    id чужого сообщества, а «как есть» — это и есть репост без обработки.

    В сборнике фото двух происхождений: посты без текста (у них нет никакого
    другого представления, кроме картинки) и, если остались места, фото
    пересказанных пунктов. Строк в тексте ни те, ни другие не добавляют —
    привязка «пункт ↔ источник» в фактчеке от этого не сдвигается.

    Всё это — надстройка: любая ошибка (фото не скачалось, не загрузилось,
    ВК отказал) оставляет сборник **без** картинки, но не роняет рубрику.
    """
    photos = list(getattr(draft, "photos", None) or [])
    if not photos or not _photos_enabled():
        return []

    from modules.publisher.photo_processing import download_photo, process_photo, upload_wall_photo

    # Потолок ВК — 10 вложений на пост. Черновик уже ограничен, но лимит
    # продублирован здесь: превышение отвергло бы весь пост вместе с текстом.
    photos = photos[:VK_MAX_ATTACHMENTS]

    attachments: List[str] = []
    for photo in photos:
        try:
            raw = await asyncio.to_thread(download_photo, photo.url)
            if not raw:
                continue
            processed = await asyncio.to_thread(process_photo, raw, photo.watermark)
            att = await upload_wall_photo(publisher, region.vk_group_id, processed)
            if att:
                attachments.append(att)
        except Exception:  # noqa: BLE001 — фото не обязано быть
            logger.warning(
                "daily digest: регион=%s фото не прикреплено (%s)",
                region.code,
                getattr(photo, "url", "")[:80],
                exc_info=True,
            )
    return attachments


async def _publish(session: Any, region: Any, draft: Any) -> Dict[str, Any]:
    from modules.publication_journal import record_publication
    from modules.publisher.vk_publisher_extended import VKPublisher

    publisher = await VKPublisher.create_with_policy(
        session, target_group_id=region.vk_group_id, test_polygon_mode=False
    )
    attachments = await _photo_attachments(publisher, region, draft)
    result = await publisher.publish_bulletin(
        group_id=region.vk_group_id,
        text=draft.text,
        attachments=attachments,
    )
    published = bool(isinstance(result, dict) and result.get("success"))

    if published:
        await record_publication(
            region_code=region.code,
            wave_theme=DIGEST_THEME,
            kind=DIGEST_KIND,
            posts_included=draft.lips,
            publish_result=result,
        )
        try:
            # Курсор дедупа — по пунктам И по фото-only постам: они тоже
            # израсходованы сборником. В ЖУРНАЛ публикаций их не пишем:
            # журнал — это сопоставление «пункт ↔ источник» для фактчека, и
            # лишние строки сдвинули бы сопоставление.
            photo_lips = [p.lip for p in (getattr(draft, "photos", None) or []) if p.lip]
            await _remember_published(session, region.code, list(draft.lips) + photo_lips)
        except Exception:  # noqa: BLE001 — публикация уже состоялась, курсор чинится потом
            logger.warning(
                "daily digest: регион=%s курсор дедупа не обновлён",
                region.code,
                exc_info=True,
            )

    try:
        from monitoring.metrics import publish_result_label, track_digest_published

        track_digest_published(
            region=region.code, topic=DIGEST_THEME, result=publish_result_label(result)
        )
    except Exception:  # pragma: no cover - метрики не валят публикацию
        logger.warning("daily digest: метрика не записалась", exc_info=True)

    logger.info(
        "daily digest: регион=%s опубликовано=%s пунктов=%d знаков=%d",
        region.code,
        published,
        len(draft.items),
        len(draft.text),
    )
    return {
        "region": region.code,
        "published": published,
        "items": len(draft.items),
        "chars": len(draft.text),
        "url": (result or {}).get("url"),
    }


async def _remember_published(session: Any, region_code: str, lips: List[str]) -> None:
    """Отметить вышедшие посты в глобальном курсоре дедупа района.

    Журнал публикаций (``published_posts``) закрывает дедуп внутри сборника,
    но утренние волны смотрят в ``work_tables`` — без этой строки вчерашний
    пост мог бы выйти в ленту ещё раз, пока не истекли его сутки свежести.
    """
    from sqlalchemy import select

    from database.models_extended import WorkTable
    from modules.deduplication.bulletin_history import (
        GLOBAL_REGION_WORK_THEME,
        append_unique_limited,
    )
    from tasks.parsing_scheduler_tasks import WORK_TABLE_LIP_LIMIT

    row = (
        (
            await session.execute(
                select(WorkTable).where(
                    WorkTable.region_code == region_code,
                    WorkTable.theme == GLOBAL_REGION_WORK_THEME,
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        row = WorkTable(
            region_code=region_code,
            theme=GLOBAL_REGION_WORK_THEME,
            lip=[],
            hash=[],
        )
        session.add(row)
    row.lip = append_unique_limited(row.lip or [], lips, WORK_TABLE_LIP_LIMIT)
    await session.commit()
