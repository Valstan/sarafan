"""
Celery tasks for Postopus migration - replaces crontab scheduling

Migrated from old_postopus crontab entries to Celery Beat schedule.
Each theme/region combination gets its own scheduled task.
"""

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

from celery import shared_task

from utils.celery_asyncio import run_coro

logger = logging.getLogger(__name__)

WORK_TABLE_LIP_LIMIT = 1000
WORK_TABLE_HASH_LIMIT = 5000


def _parse_vk_post_id(url: str | None) -> int | None:
    """Достать post_id из VK-ссылки ``…/wall-123_456`` → ``456`` (для истории
    публикаций). Возвращает None, если url пуст / не распознан."""
    if not url:
        return None
    import re

    m = re.search(r"wall-?\d+_(\d+)", str(url))
    return int(m.group(1)) if m else None


def _use_cascade_bulletin(region_kind: str | None, region_config: Any) -> bool:
    """Решает, собирать ли сводка каскадом (из главных групп детей/соседей)
    или обычным путём (из собственных ``communities`` региона).

    Каскад — для ``kind in {'oblast','strana'}`` ПО УМОЛЧАНИЮ. Но если у региона
    в ``config['bulletin_mode'] == 'communities'`` — он ведёт себя как район:
    собирает тематические сводки из своего пула communities (см.
    `/discover_communities`). Так ``kirov_obl`` (2026-05) перешёл с каскада на
    собственный пул из 50+ областных источников, не варясь в новостях своих же
    районов. ``tatarstan_obl`` / ``rf`` без флага остаются на каскаде.
    """
    if region_kind not in ("oblast", "strana"):
        return False
    mode = region_config.get("bulletin_mode") if isinstance(region_config, dict) else None
    return mode != "communities"


async def _build_neighbor_footer(session, region) -> str:
    """Футер сводки: «Ленты соседей: Имя url · Имя url» (этап 4 ребрендинга).

    Соседи ротируются по дню (``toordinal``): каждый день сводки региона
    показывают следующую пару — за неделю прокручивается весь список, а футер
    остаётся коротким (лимиты — в ``render_footer_line``). Откат per-region:
    ``regions.config['neighbor_footer'] = false``. Ошибка сборки не должна
    ронять публикацию — снаружи вызов обёрнут try/except.
    """
    from datetime import date as _date

    from sqlalchemy import select as _select

    from database.models import Region as _R
    from modules.promotion.copy import render_footer_line
    from modules.region_links import base_title, community_url, parse_neighbors

    cfg = getattr(region, "config", None)
    if isinstance(cfg, dict) and not cfg.get("neighbor_footer", True):
        return ""
    codes = parse_neighbors(getattr(region, "neighbors", None))
    if not codes:
        return ""
    rows = (
        await session.execute(
            _select(_R.code, _R.name, _R.vk_group_id, _R.config).where(
                _R.code.in_(codes),
                _R.is_active.is_(True),
                _R.vk_group_id.isnot(None),
            )
        )
    ).fetchall()
    items = []
    for row in sorted(rows, key=lambda r: r.code):
        screen = None
        if isinstance(row.config, dict):
            screen = row.config.get("screen_name")
        items.append(
            {
                "name": f"{base_title(row.name, None)} ИНФО",
                "url": community_url(row.vk_group_id, screen),
            }
        )
    if not items:
        return ""
    start = _date.today().toordinal() % len(items)
    rotated = items[start:] + items[:start]
    return render_footer_line(rotated)


@shared_task(bind=True, max_retries=3)
def parse_and_publish_theme(
    self,
    region_code: str,
    theme: str,
    test_mode: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Main parsing and publishing task for a region/theme.
    Celery-compatible: uses run_coro (one event loop per worker process).

    ``dry_run=True`` — «прогон без публикации»: парсинг → фильтр → сборка
    сводки выполняются как обычно, но публикация в VK/Telegram, инкремент
    метрик и запись work-table/ParsingStats ПРОПУСКАЮТСЯ. Возвращает что было
    бы опубликовано (counts + превью текста). Используется страницей
    ``/regions/<code>/diagnostics``. В отличие от ``test_mode`` (публикует на
    тест-полигон) — это полностью read-only прогон.
    """
    from sqlalchemy import select

    # NB: ранее тут импортировался ``get_parse_tokens`` — теперь
    # parsing использует ``get_active_parse_tokens(session)`` (с фильтром
    # disabled_until), see use site below.
    from database.connection import AsyncSessionLocal
    from database.models import Community, Region
    from database.models_extended import ParsingStats, RegionConfig, WorkTable
    from modules.bulletin_pipeline_settings import get_effective_pipeline_settings
    from modules.curation.recorder import record_curation_run
    from modules.deduplication.bulletin_history import (
        GLOBAL_REGION_WORK_THEME,
        TARGET_GROUP_POSTS_SCAN_LIMIT,
        append_unique_limited,
        build_region_dedup_sets,
        extract_source_lips_from_target_group_posts,
    )
    from modules.deduplication.fingerprints import (
        create_media_fingerprint,
        create_text_core_fingerprint,
        create_text_fingerprint,
        create_text_simhash,
        text_to_rafinad,
    )
    from modules.publication_journal import record_publication
    from modules.publisher.bulletin_builder import BulletinBuilder
    from modules.publisher.bulletin_splitter import BulletinSplitter
    from modules.publisher.postopus_bulletin_headers import (
        resolve_bulletin_hashtags,
        resolve_bulletin_header,
        resolve_mourning_bulletin_format,
    )
    from modules.publisher.vk_publisher_extended import VKPublisher
    from modules.vk_monitor.advanced_parser import AdvancedVKParser
    from modules.vk_monitor.vk_client import VKClient
    from utils.post_utils import lip_of_post

    start_time = datetime.now()

    async def _execute():
        """Execute parsing and publishing pipeline."""
        async with AsyncSessionLocal() as session:
            # Псевдо-регион «copy» + тема «setka» — отдельный сетевой хаб
            # (env COPY_SETKA_*), без RegionConfig.
            if region_code == "copy" and theme == "setka":
                from modules.copy_setka_network import execute_copy_setka_network

                return await execute_copy_setka_network(session, test_mode=test_mode)

            # Псевдо-регион «copy» + тема «krugozor» — поток научпопа «Кругозор»
            # (env KRUGOZOR_*): ротация krugozor-источников → копи-веером на стены
            # регионов. Отдельный движок, без RegionConfig.
            if region_code == "copy" and theme == "krugozor":
                from modules.krugozor_broadcast import execute_krugozor_broadcast

                return await execute_krugozor_broadcast(session, test_mode=test_mode)

            # Каскадная сводка для регионов kind in {'oblast','strana'} —
            # ловим по типу региона, а не по жёсткому коду, чтобы новые
            # oblast/strana работали без правки кода (см. ``docs/REGIONS_HIERARCHY.md``).
            # ИСКЛЮЧЕНИЕ: если region.config['bulletin_mode']=='communities' — область
            # ведёт себя как район (собирает из своего пула communities, а не каскадом).
            from database.models import Region as _Region

            kind_row = (
                await session.execute(
                    select(_Region.kind, _Region.config).where(_Region.code == region_code)
                )
            ).first()
            region_kind = kind_row[0] if kind_row else None
            region_kind_config = kind_row[1] if kind_row else None
            if _use_cascade_bulletin(region_kind, region_kind_config):
                from modules.cascaded_bulletin import run_cascaded_bulletin

                return await run_cascaded_bulletin(
                    session,
                    region_code=region_code,
                    theme=theme,
                    test_mode=test_mode,
                    dry_run=dry_run,
                )

            # Сюда дошла либо обычный район, либо community-mode область/страна
            # (bulletin_mode='communities'). Для области важно НЕ публиковать тему,
            # которой у неё нет источников: иначе fallback «все communities» ниже
            # сделает, напр., «Объявления» из новостных пабликов. Поэтому для
            # community-mode oblast/strana fallback отключаем (см. шаг 4).
            is_community_sourced_oblast = region_kind in ("oblast", "strana")

            # 1. Get region config
            result = await session.execute(
                select(RegionConfig).where(RegionConfig.region_code == region_code)
            )
            region_config = result.scalars().first()
            if not region_config:
                logger.warning(f"RegionConfig not found for {region_code}; using safe defaults")
                region_config = SimpleNamespace(
                    region_code=region_code,
                    zagolovki={},
                    heshteg={},
                    heshteg_local={},
                    black_id=[],
                    delete_msg_blacklist=[],
                    filter_group_by_region_words={},
                    text_post_maxsize_simbols=4096,
                    setka_regim_repost=False,
                    bulletin_filters=None,
                )

            # 2. Get work table
            result = await session.execute(
                select(WorkTable).where(
                    WorkTable.region_code == region_code, WorkTable.theme == theme
                )
            )
            work_table = result.scalars().first()
            if not work_table:
                work_table = WorkTable(region_code=region_code, theme=theme, lip=[], hash=[])
                if not dry_run:
                    session.add(work_table)
                    await session.commit()

            global_wt_result = await session.execute(
                select(WorkTable).where(
                    WorkTable.region_code == region_code,
                    WorkTable.theme == GLOBAL_REGION_WORK_THEME,
                )
            )
            global_work_table = global_wt_result.scalars().first()
            if not global_work_table:
                global_work_table = WorkTable(
                    region_code=region_code,
                    theme=GLOBAL_REGION_WORK_THEME,
                    lip=[],
                    hash=[],
                )
                if not dry_run:
                    session.add(global_work_table)
                    await session.commit()

            # 3. Get region
            region_result = await session.execute(select(Region).where(Region.code == region_code))
            region = region_result.scalars().first()
            if not region or not region.vk_group_id:
                return {
                    "success": False,
                    "error": f"No VK group ID for region {region_code}",
                }

            # 4. Get communities for this theme
            communities_result = await session.execute(
                select(Community.vk_id).where(
                    Community.region_id == region.id,
                    Community.category == theme,
                    Community.is_active.is_(True),
                )
            )
            community_ids = [row[0] for row in communities_result.fetchall()]

            if not community_ids and not is_community_sourced_oblast:
                # Запасной путь: у темы нет своих сообществ — читаем весь пул.
                # Выглядит аварийным, а кормит вторую по объёму тему сети
                # (``addons``: своих сообществ нет ни у одного района, 1752
                # публикации за 7 дней). Флаг — для замера и аварии, дефолт ON;
                # цену повтора снимает кэш стен, а не отключение (P180).
                #
                # ИСКЛЮЧЕНИЕ для тем из THEMES_WITHOUT_ALL_COMMUNITIES_FALLBACK:
                # для ``reklama`` фолбэк опасен тем, что рекламный фильтр в этой
                # теме выключен, а ВК банит аккаунта админа за контент (P023,
                # замер и решение — P188). Замер 01.10.2026 показал, что фолбэк по
                # reklama не срабатывал ни разу за 30 суток, так что запрет стоит
                # ноль потерь сегодня и закрывает класс на будущее.
                from config.runtime import theme_fallback_all_communities_enabled
                from modules.bulletin_pipeline_settings import (
                    FALLBACK_ALL,
                    FALLBACK_DISABLED,
                    FALLBACK_FORBIDDEN,
                    fallback_decision,
                )

                decision = fallback_decision(
                    theme, global_enabled=theme_fallback_all_communities_enabled()
                )
                if decision == FALLBACK_FORBIDDEN:
                    # Тема, для которой фолбэк запрещён осознанно (см.
                    # THEMES_WITHOUT_ALL_COMMUNITIES_FALLBACK). Своё сообщение,
                    # а не «волна пропущена» вместе с выключенным флагом: иначе
                    # запрет и поломка настройки выглядели бы одинаково.
                    logger.warning(
                        "No communities found for %s/%s; тема исключена из fallback "
                        "«все communities» — волна пропущена",
                        region_code,
                        theme,
                    )
                elif decision == FALLBACK_ALL:
                    logger.warning(
                        f"No communities found for {region_code}/{theme}; "
                        "falling back to all active communities in region"
                    )
                    fallback_result = await session.execute(
                        select(Community.vk_id).where(
                            Community.region_id == region.id, Community.is_active.is_(True)
                        )
                    )
                    community_ids = [row[0] for row in fallback_result.fetchall()]
                else:
                    assert decision == FALLBACK_DISABLED, decision
                    logger.warning(
                        "No communities found for %s/%s; запасной путь выключен "
                        "(PARSE_THEME_FALLBACK_ALL_COMMUNITIES=0) — волна пропущена",
                        region_code,
                        theme,
                    )
            if not community_ids:
                # Для community-mode области это норма: просто нет источников
                # этой темы (не публикуем «не свою» тему из общих пабликов).
                msg = "No communities found"
                if is_community_sourced_oblast:
                    msg = f"No '{theme}' communities for community-mode region {region_code}"
                return {"success": True, "message": msg, "posts_published": 0}

            # Имена сообществ для кликабельных ссылок «источник» в сводке
            comm_meta = await session.execute(
                select(Community.vk_id, Community.name).where(Community.region_id == region.id)
            )
            group_names = {str(abs(row[0])): row[1] for row in comm_meta.fetchall()}

            # 5. Parse — живой READ-токен с probe'ом (инциденты 2026-05-27 и
            # 2026-07-12): «первый из активных» без перебора заклинивал парсинг
            # на мёртвом-но-включённом токене (error 5 на каждом вызове, 0
            # постов, 0 сводок — 4 дня тишины). pick_healthy_read_token сам
            # кладёт мёртвый токен в cooldown (report_error) и берёт следующий.
            from modules.vk_token_router import pick_healthy_read_token

            parse_cand = await pick_healthy_read_token(session)
            if not parse_cand:
                return {
                    "success": False,
                    "error": "No healthy VK READ tokens (all dead or in cooldown?)",
                }
            vk_client = VKClient(parse_cand.token)
            parser = AdvancedVKParser(vk_client)
            pipeline_eff = get_effective_pipeline_settings(region_config, theme)

            all_wt_result = await session.execute(
                select(WorkTable).where(WorkTable.region_code == region_code)
            )
            region_lips, region_hashes = build_region_dedup_sets(all_wt_result.scalars().all())

            # Отсеянные дубли (миграция 093, заказ владельца 2026-08-30) — тот же
            # курсор для парсера, что и опубликованное, но хранится отдельно:
            # складывать неопубликованное в work_tables.lip значило бы врать
            # статистике публикаций (прямое возражение владельца).
            from modules.deduplication.skipped import fetch_skipped_lips, merge_dedup_lips

            skipped_lips = await fetch_skipped_lips(session, region_code)
            try:
                # Своя ИНФО-стена: из неё берётся ТОЛЬКО текст уже вышедших
                # сводок — ради lip'ов источников для дедупа. Без кэша эта сотня
                # постов перечитывалась на каждом из ~26 прогонов темы в сутки.
                #
                # Кэшируется РЕЗУЛЬТАТ (список lip'ов, килобайты), а не снимок
                # стены (около мегабайта): снимок упирался в потолок записи и
                # молча не кэшировался вовсе. Сам вызов ВК идёт мимо общего кэша
                # стен (``cache_ttl=0``) — хранить его незачем.
                from modules.vk_monitor.wall_cache import (
                    get_cached_history_lips,
                    store_history_lips,
                )

                own_wall_owner = -abs(int(region.vk_group_id))
                cached_lips = await asyncio.to_thread(get_cached_history_lips, own_wall_owner)
                if cached_lips is not None:
                    region_lips.update(cached_lips)
                else:
                    target_group_posts = await asyncio.to_thread(
                        vk_client.get_wall_posts,
                        own_wall_owner,
                        TARGET_GROUP_POSTS_SCAN_LIMIT,
                        0,
                        cache_ttl=0,
                    )
                    history_lips = extract_source_lips_from_target_group_posts(target_group_posts)
                    region_lips.update(history_lips)
                    await asyncio.to_thread(store_history_lips, own_wall_owner, history_lips)
            except Exception as e:
                logger.warning(
                    "Failed to load target group bulletin history for %s: %s",
                    region_code,
                    e,
                )

            # Нейро-вердикты классификатора (delete/hold) выключают посты из
            # сводки (enforce, заказ владельца 2026-07-13). Fail-open внутри.
            from modules.classifier.enforce import fetch_blocked_lips

            blocked_lips = await fetch_blocked_lips(session, region_code)

            posts = await parser.parse_posts_from_communities(
                community_ids=community_ids,
                theme=theme,
                region_config=region_config,
                work_table_lip=merge_dedup_lips(region_lips, skipped_lips),
                work_table_hash=list(region_hashes),
                count_per_community=20,
                pipeline_settings=pipeline_eff,
                blocked_lips=blocked_lips,
            )
            parser_stats = parser.get_stats()

            # Дубли этой волны — в журнал, чтобы проигравший не вернулся свежим
            # кандидатом через пару часов. Best-effort: журнал не важнее волны.
            wave_skipped = list(getattr(parser, "_skipped_duplicates", []) or [])
            if wave_skipped and not dry_run:
                from modules.deduplication.skipped import record_skipped

                await record_skipped(
                    session,
                    region_code=region_code,
                    wave_theme=theme,
                    entries=wave_skipped,
                )

            # Фаза сбора кончилась: у парсера прочитано всё, что нам от него
            # нужно (`parser_stats` выше, `wave_skipped` только что). Дальше
            # идут классификатор, вёрстка и публикация — держать под ними кэш
            # стен и множества дедупа незачем, а районы идут в одном дочернем
            # процессе подряд (P164).
            parser.release_wave_state()
            del region_lips, region_hashes, skipped_lips

            # 5b. Нейро-фильтр ВНУТРИ волны (заказ владельца 2026-08-19).
            #     `blocked_lips` выше применяет вердикты, вынесенные РАНЬШЕ, а по
            #     свежесобранным постам их не бывает по построению: волна сама их
            #     парсит и сама же публикует. Измерено: за 14 дней ноль вердиктов
            #     раньше сбора и 10 743 позже. Здесь кандидаты доклассифицируются
            #     до публикации. Гейт CLASSIFIER_PREPUBLISH_ENABLED, fail-open.
            from modules.classifier.prepublish import blocked_lips_before_publish, post_lip

            prepublish_blocked = await blocked_lips_before_publish(
                session, posts, region_code=region_code
            )
            if prepublish_blocked:
                before_prepublish = len(posts)
                posts = [p for p in posts if post_lip(p) not in prepublish_blocked]
                dropped_now = before_prepublish - len(posts)
                if dropped_now:
                    parser_stats["posts_filtered_classifier_prepublish"] = dropped_now
                    logger.info(
                        "Нейро-фильтр до публикации (%s/%s): убрано %d из %d кандидатов",
                        region_code,
                        theme,
                        dropped_now,
                        before_prepublish,
                    )

            # 5c. Отбор В сводку по меткам (звено 5, шаг 2): в волне остаются
            #     ТОЛЬКО publish-посты; при молчании фильтра — политика
            #     деградации владельца (пропуск волны → алгоритмический
            #     fallback). Строго ПОСЛЕ prepublish: тот записал вердикты,
            #     которые отбор читает. Гейт CLASSIFIER_SELECTION_ENABLED.
            from modules.classifier import selection as classifier_selection

            posts, selection_mode, selection_removed = (
                await classifier_selection.apply_wave_selection(
                    session, posts, region_code=region_code, theme=theme
                )
            )
            if selection_removed:
                parser_stats["posts_filtered_selection"] = selection_removed

            # Срочное вперёд рейтинга (решение владельца 2026-09-22): отключения
            # света и воды, перекрытия, аварии на сетях, предупреждения МЧС.
            # Читается СТРОГО после `apply_wave_selection`: вердикты по свежим
            # постам записал prepublish парой шагов выше, до него их не было.
            # Fail-open внутри: пустое множество = прежний порядок.
            urgent_lips = await classifier_selection.fetch_urgent_lips(
                session, region_code=region_code
            )
            urgent_in_wave = {post_lip(p) for p in posts} & urgent_lips if urgent_lips else set()
            if urgent_in_wave:
                parser_stats["posts_urgent"] = len(urgent_in_wave)
                logger.info(
                    "Срочное в волне %s/%s: %d пост(ов) идут вперёд рейтинга",
                    region_code,
                    theme,
                    len(urgent_in_wave),
                )
            if selection_mode == classifier_selection.MODE_SKIP_WAVE:
                return {
                    "success": True,
                    "message": (
                        "classifier selection: вердиктов нет, волна пропущена "
                        "(политика деградации, шаг 2 звена 5)"
                    ),
                    "posts_published": 0,
                    "stats": parser_stats,
                }

            # 6. Split by sentiment
            splitter = BulletinSplitter()
            mourning_posts, regular_posts = splitter.split_posts(posts)
            logger.info(f"Split: {len(mourning_posts)} mourning, {len(regular_posts)} regular")

            # 7. Build bulletins (заголовки/хештеги как в old_postopus)
            header = resolve_bulletin_header(region_config, theme, region)
            theme_tags, local_hashtag = resolve_bulletin_hashtags(region_config, theme)

            # Community access tokens + publish-кандидаты подбираются внутри
            # ``VKPublisher.create_with_policy`` (см. modules.vk_token_router.TokenPolicy).
            results = []
            dry_previews: List[Dict[str, Any]] = []  # что было бы опубликовано (dry_run)
            selected_by_lip: Dict[str, Dict[str, Any]] = {}
            mourning_header = ""  # captured for Telegram mirror (Flow A)

            def _preview(kind: str, d) -> Dict[str, Any]:
                txt = d.text or ""
                return {
                    "kind": kind,
                    "post_count": d.post_count,
                    "char_count": len(txt),
                    "attachments_count": len(d.attachments_list or []),
                    "text_preview": txt[:1500],
                }

            # 7.5 Хедлайнер: сильнейший пост волны — отдельной короткой
            # публикацией (этап 3 ребрендинга). Замер Кирса 26.08: тот же
            # контент одиночным постом — 437 тыс. показов через рекомендации,
            # внутри сводки — 167. Сводка при этом остаётся (гибрид, решение
            # владельца 2026-08-29); откат — regions.config['headliner']=false.
            from modules.publisher.headliner import (
                build_headliner,
                headliner_enabled,
                pick_headliner,
            )

            headliner_post = None
            if regular_posts and headliner_enabled(getattr(region, "config", None)):
                headliner_post = pick_headliner(regular_posts, exclude_lips=urgent_in_wave)
                if headliner_post is not None:
                    regular_posts = [p for p in regular_posts if p is not headliner_post]

            # Футер со ссылками на соседей (этап 4): перекрёстная видимость
            # сети в каждой сводке; сборка не имеет права ронять публикацию.
            neighbor_footer = ""
            try:
                neighbor_footer = await _build_neighbor_footer(session, region)
            except Exception:  # pragma: no cover - footer никогда не валит волну
                logger.warning("neighbor footer build failed", exc_info=True)

            # Regular bulletin
            if regular_posts:
                builder = BulletinBuilder(
                    header=header,
                    hashtags=theme_tags,
                    local_hashtag=local_hashtag,
                    max_text_length=region_config.text_post_maxsize_simbols or 4096,
                    repost_mode=region_config.setka_regim_repost,
                    max_posts_per_bulletin=pipeline_eff.get("max_posts_per_bulletin"),
                    footer=neighbor_footer,
                    urgent_lips=urgent_in_wave,
                )
                bulletin = builder.build_bulletin(regular_posts, group_names=group_names)
                if bulletin.post_count == 0 or not bulletin.text.strip():
                    logger.warning(
                        "Empty regular bulletin after build, skipping publish "
                        "(region=%s theme=%s candidates=%d)",
                        region.code,
                        theme,
                        len(regular_posts),
                    )
                else:
                    selected_by_lip.update(
                        {
                            lip_of_post(
                                p.get("owner_id", p.get("from_id", 0)),
                                p.get("id", 0),
                            ): p
                            for p in regular_posts
                        }
                    )

                    if dry_run:
                        dry_previews.append(_preview("regular", bulletin))
                    else:
                        vk_publisher = await VKPublisher.create_with_policy(
                            session,
                            target_group_id=region.vk_group_id,
                            test_polygon_mode=test_mode,
                        )
                        publish_result = await vk_publisher.publish_bulletin(
                            group_id=region.vk_group_id,
                            text=bulletin.text,
                            attachments=bulletin.attachments_list,
                        )
                        results.append(("regular", bulletin, publish_result))
                        try:
                            from monitoring.metrics import (
                                publish_result_label,
                                track_digest_published,
                            )

                            track_digest_published(
                                region=region.code,
                                topic=theme,
                                result=publish_result_label(publish_result),
                            )
                        except (
                            Exception
                        ):  # pragma: no cover - metrics никогда не должны валить публикацию
                            # WARNING (не debug): прод LOG_LEVEL=INFO глушил debug,
                            # из-за чего сбой heartbeat #018 был невидим (2026-06-05).
                            logger.warning("track_digest_published failed", exc_info=True)

            # 7.6 Публикация хедлайнера — ПОСЛЕ сводки, чтобы на стене стоял
            # выше неё. Без шапки-заголовка: он должен выглядеть обычным
            # постом, а не сводкой (это и есть формат, который берут
            # рекомендации). Лип уходит в selected_by_lip тем же путём.
            if headliner_post is not None:
                hl_owner = headliner_post.get("owner_id", headliner_post.get("from_id", 0))
                hl_gid = headliner_post.get("community_vk_id", hl_owner)
                try:
                    hl_aid = abs(int(hl_gid if hl_gid is not None else hl_owner))
                except (TypeError, ValueError):
                    hl_aid = abs(int(hl_owner)) if hl_owner else 0
                hl_text, hl_attachments = build_headliner(
                    headliner_post,
                    group_name=group_names.get(str(hl_aid), "") if hl_aid else "",
                    local_hashtag=local_hashtag,
                )
                hl_lip = lip_of_post(hl_owner, headliner_post.get("id", 0))
                selected_by_lip[hl_lip] = headliner_post
                # Для work table / Telegram-зеркала / курации хедлайнер ходит
                # тем же интерфейсом, что сводка: posts_included + post_count.
                hl_bulletin = SimpleNamespace(
                    posts_included=[hl_lip],
                    post_count=1,
                    text=hl_text,
                    attachments_list=hl_attachments,
                )
                if dry_run:
                    dry_previews.append(
                        {
                            "kind": "headliner",
                            "post_count": 1,
                            "char_count": len(hl_text),
                            "attachments_count": len(hl_attachments),
                            "text_preview": hl_text[:1500],
                        }
                    )
                else:
                    hl_publisher = await VKPublisher.create_with_policy(
                        session,
                        target_group_id=region.vk_group_id,
                        test_polygon_mode=test_mode,
                    )
                    hl_result = await hl_publisher.publish_bulletin(
                        group_id=region.vk_group_id,
                        text=hl_text,
                        attachments=hl_attachments,
                    )
                    results.append(("headliner", hl_bulletin, hl_result))
                    # Heartbeat и с этого пути тоже. Хедлайнер пишет строку в
                    # published_posts (kind='headliner'), но ключ сторожа до
                    # 2026-09-22 не обновлял: волна, где прошёл ТОЛЬКО он,
                    # оставляла живой район без свежего ключа, и сторож краснел
                    # на работающем — ложная тревога, не пропуск. При MIN_POOL=3
                    # (modules/publisher/headliner.py) сценарий почти
                    # недостижим, но цена строки нулевая, а режим отказа реален.
                    try:
                        from monitoring.metrics import publish_result_label, track_digest_published

                        track_digest_published(
                            region=region.code,
                            topic=theme,
                            result=publish_result_label(hl_result),
                        )
                    except (
                        Exception
                    ):  # pragma: no cover - metrics никогда не должны валить публикацию
                        logger.warning("track_digest_published failed (headliner)", exc_info=True)
                    logger.info(
                        "Headliner published (region=%s theme=%s chars=%d atts=%d)",
                        region.code,
                        theme,
                        len(hl_text),
                        len(hl_attachments),
                    )

            # Mourning bulletin
            if mourning_posts:
                mourning_header, mourning_tags, mourning_local_hashtag = (
                    resolve_mourning_bulletin_format()
                )
                mourning_builder = BulletinBuilder(
                    header=mourning_header,
                    hashtags=mourning_tags,
                    local_hashtag=mourning_local_hashtag,
                    max_text_length=region_config.text_post_maxsize_simbols or 4096,
                    max_posts_per_bulletin=pipeline_eff.get("max_posts_per_bulletin"),
                )
                mourning_bulletin = mourning_builder.build_bulletin(
                    mourning_posts, group_names=group_names
                )
                if mourning_bulletin.post_count == 0 or not mourning_bulletin.text.strip():
                    logger.warning(
                        "Empty mourning bulletin after build, skipping publish "
                        "(region=%s theme=%s candidates=%d)",
                        region.code,
                        theme,
                        len(mourning_posts),
                    )
                else:
                    selected_by_lip.update(
                        {
                            lip_of_post(
                                p.get("owner_id", p.get("from_id", 0)),
                                p.get("id", 0),
                            ): p
                            for p in mourning_posts
                        }
                    )

                    if dry_run:
                        dry_previews.append(_preview("mourning", mourning_bulletin))
                    else:
                        vk_pub = await VKPublisher.create_with_policy(
                            session,
                            target_group_id=region.vk_group_id,
                            test_polygon_mode=test_mode,
                        )
                        mourning_pub = await vk_pub.publish_bulletin(
                            group_id=region.vk_group_id,
                            text=mourning_bulletin.text,
                            attachments=mourning_bulletin.attachments_list,
                        )
                        results.append(("mourning", mourning_bulletin, mourning_pub))
                        try:
                            from monitoring.metrics import (
                                publish_result_label,
                                track_digest_published,
                            )

                            track_digest_published(
                                region=region.code,
                                topic="mourning",
                                result=publish_result_label(mourning_pub),
                            )
                        except Exception:  # pragma: no cover
                            logger.warning("track_digest_published failed", exc_info=True)

            # dry_run: read-only прогон — ничего не публикуем и не пишем в БД.
            # Возвращаем что было бы опубликовано (см. /regions/<code>/diagnostics).
            if dry_run:
                return {
                    "success": True,
                    "dry_run": True,
                    "region_code": region_code,
                    "theme": theme,
                    "communities_count": len(community_ids),
                    "posts_parsed": len(posts),
                    "regular_posts": len(regular_posts),
                    "mourning_posts": len(mourning_posts),
                    "would_publish": dry_previews,
                    "bulletins_count": len(dry_previews),
                    "stats": parser_stats,
                }

            # 8. Update work table
            all_included = []
            for _, d, _ in results:
                all_included.extend(d.posts_included)
            if all_included:
                work_table.lip = append_unique_limited(
                    work_table.lip or [],
                    all_included,
                    WORK_TABLE_LIP_LIMIT,
                )
                global_work_table.lip = append_unique_limited(
                    global_work_table.lip or [],
                    all_included,
                    WORK_TABLE_LIP_LIMIT,
                )

                new_hash_entries: List[str] = []
                for lip in all_included:
                    p = selected_by_lip.get(lip)
                    if not isinstance(p, dict):
                        continue
                    text = (p.get("text") or "").strip()
                    if text:
                        tfp = create_text_fingerprint(text)
                        if tfp:
                            new_hash_entries.append(f"txtfp:{tfp}")
                        cfp = create_text_core_fingerprint(text)
                        if cfp:
                            new_hash_entries.append(f"txtcore:{cfp}")
                        rafinad_len = len(text_to_rafinad(text))
                        if rafinad_len >= 80:
                            simhash = create_text_simhash(text)
                            if simhash:
                                new_hash_entries.append(f"txtsim:{rafinad_len // 20}:{simhash}")

                    atts = p.get("attachments")
                    media_ids = create_media_fingerprint(atts if isinstance(atts, list) else [])
                    new_hash_entries.extend(media_ids)

                work_table.hash = append_unique_limited(
                    work_table.hash or [],
                    new_hash_entries,
                    WORK_TABLE_HASH_LIMIT,
                )
                global_work_table.hash = append_unique_limited(
                    global_work_table.hash or [],
                    new_hash_entries,
                    WORK_TABLE_HASH_LIMIT,
                )
                await session.commit()

            # 8.5 Mirror published bulletins to Telegram (Flow A — e.g. Малмыж @malmyzh_info).
            # Data-driven: only regions with telegram_channel set AND config.telegram_bot.
            # Wrapped so a Telegram failure NEVER breaks VK publishing.
            try:
                from modules.publisher.telegram_repost import mirror_bulletin_to_telegram
                from modules.publisher.telegram_repost_config import (
                    get_telegram_extra_hashtags,
                    telegram_repost_disabled,
                )

                tg_bot = (region.config or {}).get("telegram_bot")
                if (
                    region.telegram_channel
                    and tg_bot
                    and results
                    and not telegram_repost_disabled()
                ):
                    from modules.vk_monitor.vk_client_async import VKClientAsync

                    extra_tags = get_telegram_extra_hashtags(region.telegram_channel)
                    async with VKClientAsync(parse_cand.token) as tg_vk:
                        for kind, d, pub in results:
                            if not pub.get("success", False):
                                continue
                            tg_header = header if kind == "regular" else mourning_header
                            posts_for = [
                                selected_by_lip[lip]
                                for lip in d.posts_included
                                if lip in selected_by_lip
                            ]
                            if not posts_for:
                                continue
                            await mirror_bulletin_to_telegram(
                                tg_bot,
                                region.telegram_channel,
                                tg_header,
                                posts_for,
                                tg_vk,
                                extra_hashtags=extra_tags,
                                test_mode=test_mode,
                            )
            except Exception:
                logger.exception(
                    "Telegram mirror (Flow A) failed for %s/%s; VK publish unaffected",
                    region_code,
                    theme,
                )

            # 8.6 Shadow LLM-курация (PoC, письмо brain 2026-06-07): паркуем уже
            # опубликованные посты для пост-фактум вердикта /curate. Best-effort,
            # изолировано (своя сессия) — на публикацию не влияет.
            for kind, d, pub in results:
                await record_curation_run(
                    region_code=region.code,
                    theme=theme,
                    kind=kind,
                    selected_by_lip=selected_by_lip,
                    posts_included=d.posts_included,
                    publish_result=pub,
                )

            # Журнал публикаций (миграция 091): что и когда реально вышло на
            # стену. Врезан в тот же цикл, что и курация, — одна строка покрывает
            # районную волну, каскад, соседский канал, траур и хедлайнер. Без
            # env-гейта, в отличие от курации: доли тем, посчитанные по одному
            # району из двадцати девяти, описывали бы не сеть, а этот район.
            for kind, d, pub in results:
                await record_publication(
                    region_code=region.code,
                    wave_theme=theme,
                    kind=kind,
                    posts_included=d.posts_included,
                    publish_result=pub,
                )

            # 9. Return result
            total_published = sum(d.post_count for _, d, _ in results)
            first_url = results[0][2].get("url") if results else None
            return {
                "success": (all(r[2].get("success", False) for r in results) if results else True),
                "posts_published": total_published,
                "published_url": first_url,
                "mourning_posts": len(mourning_posts),
                "regular_posts": len(regular_posts),
                "bulletins_count": len(results),
                "stats": parser_stats,
            }

    try:
        # Same persistent loop as other Celery tasks (see utils/celery_asyncio).
        result = run_coro(_execute())

        # dry_run — полностью read-only: не пишем ParsingStats в БД.
        if dry_run:
            return result

        # Save stats (sync-friendly)
        try:

            async def _save_stats():
                async with AsyncSessionLocal() as session:
                    record = ParsingStats(
                        region_code=region_code,
                        theme=theme,
                        run_date=start_time,
                        run_type="scheduled",
                        duration_seconds=(datetime.now() - start_time).total_seconds(),
                        success=result.get("success", False),
                        total_groups_checked=result.get("stats", {}).get("total_groups_checked", 0),
                        total_posts_scanned=result.get("stats", {}).get("total_posts_scanned", 0),
                        posts_filtered_old=result.get("stats", {}).get("posts_filtered_old", 0),
                        posts_filtered_duplicate_lip=result.get("stats", {}).get(
                            "posts_filtered_duplicate_lip", 0
                        ),
                        posts_filtered_duplicate_text=result.get("stats", {}).get(
                            "posts_filtered_duplicate_text", 0
                        ),
                        posts_filtered_duplicate_foto=result.get("stats", {}).get(
                            "posts_filtered_duplicate_foto", 0
                        ),
                        posts_filtered_black_id=result.get("stats", {}).get(
                            "posts_filtered_black_id", 0
                        ),
                        posts_filtered_no_region_words=result.get("stats", {}).get(
                            "posts_filtered_no_region_words", 0
                        ),
                        posts_filtered_advertisement=result.get("stats", {}).get(
                            "posts_filtered_advertisement", 0
                        ),
                        posts_filtered_no_attachments=result.get("stats", {}).get(
                            "posts_filtered_no_attachments", 0
                        ),
                        posts_final_count=result.get("stats", {}).get("posts_final_count", 0),
                        published_to_test_polygon=test_mode,
                        published_url=result.get("published_url"),
                        published_post_id=_parse_vk_post_id(result.get("published_url")),
                    )
                    session.add(record)
                    await session.commit()

            run_coro(_save_stats())
        except Exception as stats_err:
            logger.warning(f"Failed to save stats: {stats_err}")
        return result

    except Exception as e:
        logger.error(f"Task failed for {region_code}/{theme}: {e}")
        # Save failure stats
        try:

            async def _save_failure():
                async with AsyncSessionLocal() as session:
                    record = ParsingStats(
                        region_code=region_code,
                        theme=theme,
                        run_date=start_time,
                        run_type="scheduled",
                        duration_seconds=(datetime.now() - start_time).total_seconds(),
                        success=False,
                        error_message=str(e),  # noqa: F821 — closed over outer except clause
                    )
                    session.add(record)
                    await session.commit()

            run_coro(_save_failure())
        except Exception:
            pass
        raise self.retry(exc=e, countdown=60 * (2**self.request.retries))


@shared_task
def parse_reklama(region_code: str):
    return parse_and_publish_theme.delay(region_code, "reklama")


@shared_task
def parse_novost(region_code: str):
    return parse_and_publish_theme.delay(region_code, "novost")


@shared_task
def parse_kultura(region_code: str):
    return parse_and_publish_theme.delay(region_code, "kultura")


@shared_task
def parse_sport(region_code: str):
    return parse_and_publish_theme.delay(region_code, "sport")


@shared_task
def parse_sosed(region_code: str):
    return parse_and_publish_theme.delay(region_code, "sosed")


@shared_task
def share_neighbor_news(region_code: str):
    """Соседский обмен новостями: репост ``#Новости`` с главных групп соседей.

    Источники — регионы из ``Region.neighbors``. Единый движок —
    ``modules.cascaded_bulletin.run_neighbor_bulletin`` (source_mode=neighbors,
    theme=neighbors, гейт по хэштегу). Не путать с темой ``sosed`` (парсинг
    сообществ с ``category="sosed"`` внутри региона) — это разные механики.
    """
    from database.connection import AsyncSessionLocal
    from modules.cascaded_bulletin import run_neighbor_bulletin

    async def _run():
        async with AsyncSessionLocal() as session:
            return await run_neighbor_bulletin(session, region_code=region_code)

    return run_coro(_run())


@shared_task
def run_all_regions_neighbor_share():
    """Запустить соседский обмен по всем регионам с непустым ``Region.neighbors``."""
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import Region

    async def _get_regions():
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Region.code).where(
                    Region.is_active.is_(True),
                    Region.vk_group_id.isnot(None),
                    Region.neighbors.isnot(None),
                    Region.neighbors != "",
                )
            )
            return list(result.scalars().all())

    regions = run_coro(_get_regions())
    results = []
    for rc in regions:
        r = share_neighbor_news.delay(rc)
        results.append(r)
    return {"task": "neighbor_share", "regions": regions, "tasks": [r.id for r in results]}


@shared_task
def run_all_regions_theme(theme: str, strict: bool = False):
    """Run parsing for specific theme across all regions.

    ``strict=False`` (дефолт, исторический): регион попадает в волну, если у него
    есть communities этой темы **ИЛИ** вообще любые (fallback на «все communities»
    в parse_and_publish_theme). Нужен для агрегатных тем (addons и т.п.).

    ``strict=True``: только регионы с communities именно этой темы. Используется
    для новых областных тем (proisshestviya/molodezh/nauka/promyshlennost/selhoz/
    zdorovie/zhkh/priroda), чтобы волна не затягивала районы (у них таких
    communities нет) и не плодила «не свои» сводки.
    """
    from sqlalchemy import exists, select

    from database.connection import AsyncSessionLocal
    from database.models import Community, Region
    from database.models_extended import RegionConfig

    async def _get_regions():
        async with AsyncSessionLocal() as session:
            has_theme_communities = (
                select(Community.id)
                .where(
                    Community.region_id == Region.id,
                    Community.category == theme,
                    Community.is_active.is_(True),
                )
                .exists()
            )
            has_any_communities = (
                select(Community.id)
                .where(
                    Community.region_id == Region.id,
                    Community.is_active.is_(True),
                )
                .exists()
            )
            # NB: раньше тут был хардкод ``Region.code != "kirov_obl"`` — область
            # держалась вне тематических волн (жила на каскад-слотах). С переходом
            # kirov_obl на bulletin_mode='communities' (2026-05) исключение снято:
            # каскадные регионы (tatarstan_obl/rf) и так отсекаются проверкой
            # наличия communities ниже (у них пул пуст).
            community_gate = (
                has_theme_communities if strict else (has_theme_communities | has_any_communities)
            )
            # Регион допускается в волну, если выполнено ЛЮБОЕ из:
            #   1. есть строка RegionConfig (легаси-путь, мигрировано из Mongo);
            #   2. он community-mode (config.bulletin_mode='communities') — области/
            #      страны на собственном пуле (kirov_obl/tatarstan_obl, 2026-05);
            #   3. есть хотя бы одно активное community (``has_any_communities``).
            # Пункт 3 (2026-06) чинит онбординг районов: новый РАЙОН из визарда
            # `/regions/new` не получает строку region_configs (её создавала только
            # Mongo-миграция) и без bulletin_mode молча выпадал из ВСЕХ волн, хотя пул
            # источников у него есть (Тужа: 49 communities, 0 публикаций). Теперь
            # регион публикует сразу после засева пула. ``parse_and_publish_theme``
            # при отсутствии RegionConfig подставляет safe-defaults; заголовки/хэштеги
            # имеют fallback по теме+имени региона.
            # ``Region.config`` — generic JSON-колонка (без .astext), поэтому
            # достаём bulletin_mode PG-оператором ``->>`` (возвращает text).
            config_gate = (
                exists().where(RegionConfig.region_code == Region.code)
                | (Region.config.op("->>")("bulletin_mode") == "communities")
                | has_any_communities
            )
            result = await session.execute(
                select(Region.code).where(
                    Region.is_active.is_(True),
                    Region.vk_group_id.isnot(None),
                    config_gate,
                    community_gate,
                )
            )
            return list(result.scalars().all())

    regions = run_coro(_get_regions())
    results = []
    for rc in regions:
        r = parse_and_publish_theme.delay(rc, theme)
        results.append(r)
    return {"theme": theme, "regions": regions, "tasks": [r.id for r in results]}


@shared_task(bind=True, max_retries=2)
def mirror_community_to_telegram(self, community_id: int = None) -> Dict[str, Any]:
    """Flow B: mirror a VK community wall to its Telegram channel (e.g. Гоньба).

    Beat-scheduled. Defaults to the community configured via GONBA_COMMUNITY_ID.
    Idempotent per run (WorkTable lip-dedup); failures are logged, not raised.
    """

    async def _run():
        from database.connection import AsyncSessionLocal
        from modules.telegram_gonba_mirror import execute_gonba_telegram_mirror

        async with AsyncSessionLocal() as session:
            return await execute_gonba_telegram_mirror(session, community_id=community_id)

    return run_coro(_run())
