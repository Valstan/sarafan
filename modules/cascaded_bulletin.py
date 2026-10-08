"""Каскадная сводка для регионов типа ``oblast`` и ``strana``.

Простыми словами
================

В SETKA регионы организованы в три уровня (см. ``docs/REGIONS_HIERARCHY.md``):

    strana  (например, rf) ─┐
                            │ источники = главные сообщества детей-областей
    oblast  (например, kirov_obl) ─┐
                                   │ источники = главные сообщества детей-районов
    raion   (например, mi, vp, tuzha) ─┐
                                       │ источники = записи в `communities`
                                       │ (партнёрские VK-паблики района)
                                       v
                                  публикация в region.vk_group_id

Каждый регион публикует свою сводку в **своё** главное сообщество
(``region.vk_group_id``). Для **района** источники — сообщества-партнёры
(старая логика в ``tasks/parsing_scheduler_tasks.parse_and_publish_theme``).

Для **области** и **страны** работает этот модуль — каскадная сборка:

    1. Берём всех активных детей региона (где ``parent_region_id = region.id``)
       у которых есть ``vk_group_id``.
    2. Для каждого ребёнка читаем ``cascade_posts_per_child`` свежих постов
       со стены ``child.vk_group_id`` (по умолчанию **5**).
    3. Все собранные посты прогоняем через общий filter-pipeline
       (``AdvancedVKParser.filter_posts_list``) — он отсеивает дубли,
       рекламу, повторы по hash/lip.
    4. Дополнительно hard-exclude'им рекламу / addons / религиозные посты
       (как в старом kirov_oblast — список маркеров ниже).
    5. Складываем итоговая сводка через ``BulletinBuilder`` и публикуем
       в ``region.vk_group_id`` через ``VKPublisher.create_with_policy``.

Параметры в ``RegionConfig.bulletin_filters.defaults``
====================================================

* ``cascade_posts_per_child`` — сколько свежих постов брать с каждого ребёнка
  (default ``5``). Безопасный диапазон 1-50.
* ``cascade_lookback_hours`` — максимальный возраст поста (default ``72.0``).
  Слишком старые отсекаются — каскад должен быть свежим.
* ``cascade_source_region_codes`` — список явных кодов детей (override). Если
  пуст или не задан — берутся **все** активные регионы с
  ``parent_region_id = region.id`` и ``vk_group_id IS NOT NULL``.

Backward-compat
================

Старый ``modules.kirov_oblast_bulletin.run_kirov_oblast_bulletin`` остаётся как
тонкий wrapper и просто вызывает ``run_cascaded_bulletin(region_code="kirov_obl")``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.curation.recorder import record_curation_run
from modules.publication_journal import record_publication

logger = logging.getLogger(__name__)

DEFAULT_POSTS_PER_CHILD = 5
DEFAULT_LOOKBACK_HOURS = 72.0
WORK_TABLE_LIP_LIMIT = 1000
WORK_TABLE_HASH_LIMIT = 5000

_BANNED_BULLETIN_MARKERS = (
    "реклама",
    "объявлен",
    "дополнительно",
    "addons",
)

_RELIGIOUS_MARKERS = (
    "православ",
    "церков",
    "храм",
    "молитв",
    "епарх",
    "богослуж",
    "священ",
    "митрополит",
    "монастыр",
    "пасх",
    "крещен",
    "ислам",
    "мечет",
    "намаз",
)


def _defaults_dict(region_config: Any) -> Dict[str, Any]:
    df = getattr(region_config, "bulletin_filters", None) or {}
    if not isinstance(df, dict):
        return {}
    d = df.get("defaults") or {}
    return d if isinstance(d, dict) else {}


def _post_age_hours(post_data: Dict[str, Any]) -> Optional[float]:
    raw = post_data.get("date")
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    now_ts = datetime.now(tz=timezone.utc).timestamp()
    return max(0.0, (now_ts - ts) / 3600.0)


def _is_recent_enough(post_data: Dict[str, Any], max_age_hours: float) -> bool:
    age = _post_age_hours(post_data)
    if age is None:
        return False
    return age <= max_age_hours


def _is_religious_text(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _RELIGIOUS_MARKERS)


def _has_banned_marker(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _BANNED_BULLETIN_MARKERS)


async def _resolve_child_regions(
    session: AsyncSession,
    region_id: int,
    region_code: str,
    region_config: Any,
) -> List[Any]:
    """Возвращает список ребёнков-регионов (Region objects) с непустым ``vk_group_id``.

    Приоритет:
    1. ``RegionConfig.bulletin_filters.defaults.cascade_source_region_codes`` —
       явный override (список кодов).
    2. Иначе — все active regions с ``parent_region_id = region_id``
       и ``vk_group_id IS NOT NULL``.
    """
    from database.models import Region

    d = _defaults_dict(region_config)
    override = d.get("cascade_source_region_codes")
    if isinstance(override, list) and override:
        codes = [
            str(x).strip() for x in override if str(x).strip() and str(x).strip() != region_code
        ]
        if not codes:
            return []
        r = await session.execute(
            select(Region).where(
                Region.code.in_(codes),
                Region.is_active.is_(True),
                Region.vk_group_id.isnot(None),
            )
        )
        return list(r.scalars().all())

    r = await session.execute(
        select(Region).where(
            Region.parent_region_id == region_id,
            Region.is_active.is_(True),
            Region.vk_group_id.isnot(None),
        )
    )
    return list(r.scalars().all())


async def _resolve_neighbor_regions(session: AsyncSession, region: Any) -> List[Any]:
    """Возвращает список регионов-соседей (Region objects) с непустым ``vk_group_id``.

    Источник — ``Region.neighbors`` (запятая/точка-с-запятой-список кодов
    соседних регионов). Это движок «обмена новостями между соседями» (бывший
    ``modules/publisher/neighbor_sharing.py``, переписан под текущую иерархию):
    регион подтягивает посты с главных групп тех регионов, что отмечены его
    соседями. Сам регион из списка исключается (защита от само-репоста).
    """
    from database.models import Region

    raw = (getattr(region, "neighbors", None) or "").strip()
    if not raw:
        return []
    codes = [
        c.strip()
        for c in raw.replace(";", ",").split(",")
        if c.strip() and c.strip() != region.code
    ]
    if not codes:
        return []
    r = await session.execute(
        select(Region).where(
            Region.code.in_(codes),
            Region.is_active.is_(True),
            Region.vk_group_id.isnot(None),
        )
    )
    return list(r.scalars().all())


async def run_cascaded_bulletin(
    session: AsyncSession,
    *,
    region_code: str,
    theme: str = "oblast",
    test_mode: bool = False,
    source_mode: str = "children",
    require_hashtag: Optional[str] = None,
    dry_run: bool = False,
    footer: str = "",
) -> Dict[str, Any]:
    """Собрать и опубликовать каскадная сводка для региона.

    Два режима источников (``source_mode``):

    * ``"children"`` (default) — каскад вниз по иерархии. Источники = главные
      сообщества детей (``parent_region_id = region.id``). Только для регионов
      ``kind in {'oblast', 'strana'}``.
    * ``"neighbors"`` — обмен новостями между соседями. Источники = главные
      сообщества регионов из ``Region.neighbors``. Применим к любому региону
      (обычно ``raion``). Так реализован бывший ``neighbor_sharing.py`` без
      дублирования пайплайна — тот же сбор/фильтр/публикация.

    ``require_hashtag`` — если задан (например ``"#Новости"``), в кандидаты
    попадают только посты, содержащие этот хэштег (гейт для соседского обмена:
    репостим лишь то, что сосед явно пометил как новость).
    """
    from types import SimpleNamespace

    from database.models import Community, Region
    from database.models_extended import RegionConfig, WorkTable
    from modules.bulletin_pipeline_settings import get_effective_pipeline_settings
    from modules.deduplication.bulletin_history import (
        GLOBAL_REGION_WORK_THEME,
        TARGET_GROUP_POSTS_SCAN_LIMIT,
        append_unique_limited,
        build_region_dedup_sets,
        extract_source_lips_from_target_group_posts,
        partition_included_lips,
    )
    from modules.deduplication.fingerprints import (
        create_media_fingerprint,
        create_text_core_fingerprint,
        create_text_fingerprint,
        create_text_simhash,
        text_to_rafinad,
    )
    from modules.publisher.bulletin_builder import BulletinBuilder
    from modules.publisher.bulletin_splitter import BulletinSplitter
    from modules.publisher.postopus_bulletin_headers import (
        resolve_bulletin_hashtags,
        resolve_bulletin_header,
    )
    from modules.publisher.vk_publisher_extended import VKPublisher
    from modules.vk_monitor.advanced_parser import AdvancedVKParser
    from modules.vk_monitor.vk_client import VKClient
    from utils.post_utils import lip_of_post
    from utils.text_utils import is_advertisement

    result = await session.execute(select(Region).where(Region.code == region_code))
    region = result.scalars().first()
    if not region:
        return {
            "success": False,
            "error": f"Region {region_code} not found",
        }
    if not region.vk_group_id:
        return {
            "success": False,
            "error": f"Region {region_code} has no vk_group_id",
        }
    if source_mode == "children" and (region.kind or "raion") not in ("oblast", "strana"):
        return {
            "success": False,
            "error": (
                f"Region {region_code} kind={region.kind!r} — cascaded bulletin "
                "is for oblast/strana only"
            ),
        }

    cfg_result = await session.execute(
        select(RegionConfig).where(RegionConfig.region_code == region_code)
    )
    region_config = cfg_result.scalars().first()
    if not region_config:
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

    wt_result = await session.execute(
        select(WorkTable).where(
            WorkTable.region_code == region_code,
            WorkTable.theme == theme,
        )
    )
    work_table = wt_result.scalars().first()
    if not work_table:
        work_table = WorkTable(region_code=region_code, theme=theme, lip=[], hash=[])
        if not dry_run:
            session.add(work_table)
            await session.commit()
            await session.refresh(work_table)

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
            await session.refresh(global_work_table)

    ddef = _defaults_dict(region_config)
    posts_per_child = int(ddef.get("cascade_posts_per_child", DEFAULT_POSTS_PER_CHILD))
    posts_per_child = max(1, min(posts_per_child, 50))
    lookback_hours = float(ddef.get("cascade_lookback_hours", DEFAULT_LOOKBACK_HOURS))
    lookback_hours = max(1.0, min(lookback_hours, 168.0))

    # `children` держит регионы-источники: дети (parent_region_id) в режиме
    # "children" либо соседи (Region.neighbors) в режиме "neighbors". Имя
    # переменной историческое — остальной пайплайн идентичен для обоих режимов.
    if source_mode == "neighbors":
        children = await _resolve_neighbor_regions(session, region)
        no_sources_msg = (
            f"no active neighbors for region {region_code} "
            "(Region.neighbors empty or all inactive/without vk_group_id)"
        )
    else:
        children = await _resolve_child_regions(session, region.id, region.code, region_config)
        no_sources_msg = (
            f"no active children for region {region_code} "
            "(parent_region_id link or cascade_source_region_codes override)"
        )
    if not children:
        return {
            "success": True,
            "message": no_sources_msg,
            "posts_published": 0,
            "bulletins_count": 0,
            "stats": {},
        }

    # Выбор READ-токена с фильтром disabled_until (миграция 014).
    # Без фильтра парсинг падал на первом токене (VALSTAN) когда тот в
    # cooldown — см. инцидент 2026-05-27 с VK error 5 «User authorization
    # failed: user is blocked».
    from modules.vk_token_router import get_active_parse_tokens

    parse_tokens = await get_active_parse_tokens(session)
    if not parse_tokens:
        return {"success": False, "error": "No active VK READ tokens (all in cooldown?)"}
    vk = VKClient(next(iter(parse_tokens.values())))

    # Dedup-наборы по всем темам этого региона — чтобы тот же пост, который мы
    # уже включали в предыдущий oblast-выпуск, второй раз не пошёл.
    all_wt_result = await session.execute(
        select(WorkTable).where(WorkTable.region_code == region_code)
    )
    region_lips, region_hashes = build_region_dedup_sets(all_wt_result.scalars().all())

    # И ещё посмотрим в свою target-группу: если мы там что-то публиковали,
    # выдернуть lip'ы исходных постов — чтобы повторов не было.
    debug_counters: Dict[str, int] = {
        "children_total": len(children),
        "children_scanned": 0,
        "child_posts_scanned": 0,
        "child_posts_too_old": 0,
        "posts_without_required_hashtag": 0,
        "candidate_posts": 0,
        "filtered_posts_after_pipeline": 0,
        "filtered_posts_non_news_ads": 0,
        "filtered_posts_non_news_religious": 0,
        "filtered_posts_mourning": 0,
        "regular_posts_ready": 0,
    }
    try:
        # Своя стена — только ради текста вышедших сводок. Кэшируется результат
        # (список lip'ов), а не снимок стены. Как в районной волне.
        from modules.vk_monitor.wall_cache import get_cached_history_lips, store_history_lips

        own_wall_owner = -abs(int(region.vk_group_id))
        cached_lips = await asyncio.to_thread(get_cached_history_lips, own_wall_owner)
        if cached_lips is not None:
            region_lips.update(cached_lips)
        else:
            target_group_posts = await asyncio.to_thread(
                vk.get_wall_posts,
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
            "Cascaded bulletin %s: failed to load target group history: %s",
            region_code,
            e,
        )

    candidate_posts: List[Dict[str, Any]] = []
    for child in children:
        debug_counters["children_scanned"] += 1
        owner = -abs(int(child.vk_group_id))
        try:
            wall_posts = await asyncio.to_thread(vk.get_wall_posts, owner, posts_per_child, 0)
        except Exception as e:
            logger.warning(
                "Cascaded bulletin %s: wall.get failed for child %s: %s",
                region_code,
                child.code,
                e,
            )
            continue
        for wp in wall_posts or []:
            debug_counters["child_posts_scanned"] += 1
            if not _is_recent_enough(wp, lookback_hours):
                debug_counters["child_posts_too_old"] += 1
                continue
            if require_hashtag and require_hashtag.lower() not in (wp.get("text") or "").lower():
                debug_counters["posts_without_required_hashtag"] += 1
                continue
            candidate_posts.append(wp)

    debug_counters["candidate_posts"] = len(candidate_posts)

    if not candidate_posts:
        return {
            "success": True,
            "message": "no fresh posts collected from children",
            "posts_published": 0,
            "bulletins_count": 0,
            "stats": {},
            "debug": debug_counters,
        }

    pipeline_eff = get_effective_pipeline_settings(region_config, theme)
    parser = AdvancedVKParser(vk)
    # Нейро-вердикты классификатора (delete/hold) выключают посты из каскадной
    # сводки так же, как из районной (enforce 2026-07-13). Fail-open внутри.
    from modules.classifier.enforce import fetch_blocked_lips

    blocked_lips = await fetch_blocked_lips(session, region_code)
    posts = await parser.filter_posts_list(
        candidate_posts,
        theme=theme,
        region_config=region_config,
        work_table_lip=list(region_lips),
        work_table_hash=list(region_hashes),
        recent_text_fingerprints=[],
        pipeline_settings=pipeline_eff,
        blocked_lips=blocked_lips,
    )
    debug_counters["filtered_posts_after_pipeline"] = len(posts)

    # Нейро-фильтр ДО публикации — то же, что в районной волне (заказ владельца
    # 2026-08-19): свежесобранный пост вердикта ещё не имеет, и `blocked_lips`
    # выше про него ничего не знает. Гейт CLASSIFIER_PREPUBLISH_ENABLED, fail-open.
    from modules.classifier.prepublish import blocked_lips_before_publish, post_lip

    prepublish_blocked = await blocked_lips_before_publish(session, posts, region_code=region_code)
    if prepublish_blocked:
        before_prepublish = len(posts)
        posts = [p for p in posts if post_lip(p) not in prepublish_blocked]
        debug_counters["filtered_posts_classifier_prepublish"] = before_prepublish - len(posts)

    # Отбор В сводку по меткам (звено 5, шаг 2) — симметрично районной волне
    # (прецедент симметрии — prepublish выше). Строго ПОСЛЕ prepublish: тот
    # записал вердикты, которые отбор читает. Гейт CLASSIFIER_SELECTION_ENABLED.
    from modules.classifier import selection as classifier_selection

    posts, selection_mode, selection_removed = await classifier_selection.apply_wave_selection(
        session, posts, region_code=region_code, theme=theme
    )
    if selection_removed:
        debug_counters["filtered_posts_selection"] = selection_removed

    # Срочное вперёд рейтинга — тем же правилом, что в районной волне
    # (решение владельца 2026-09-22). Хедлайнера в каскаде нет, поэтому
    # передавать множество нужно только сборщику. Fail-open внутри.
    from utils.post_utils import post_lip as _post_lip

    urgent_lips = await classifier_selection.fetch_urgent_lips(session, region_code=region_code)
    urgent_in_wave = {_post_lip(p) for p in posts} & urgent_lips if urgent_lips else set()
    if urgent_in_wave:
        debug_counters["posts_urgent"] = len(urgent_in_wave)

    if selection_mode == classifier_selection.MODE_SKIP_WAVE:
        return {
            "success": True,
            "message": (
                "classifier selection: вердиктов нет, каскадная волна пропущена "
                "(политика деградации, шаг 2 звена 5)"
            ),
            "posts_published": 0,
            "bulletins_count": 0,
            "stats": {},
            "debug": debug_counters,
        }

    parser_stats = parser.get_stats()
    # Сбор кончился, счётчики сняты — отпускаем кэш стен и множества дедупа до
    # фильтрации и вёрстки. Каскад области идёт в том же единственном дочернем
    # процессе, что и районные волны (P164).
    parser.release_wave_state()

    # Hard-exclude'им рекламу + addons + религию (требование пользователя
    # для каскадных сводок; для районных сводок это идёт через
    # обычные фильтры).
    cascade_news_posts: List[Dict[str, Any]] = []
    for p in posts:
        txt = (p.get("text") or "").strip()
        if _has_banned_marker(txt):
            debug_counters["filtered_posts_non_news_ads"] += 1
            continue
        if is_advertisement(txt, skip_for_reklama=False, theme="novost"):
            debug_counters["filtered_posts_non_news_ads"] += 1
            continue
        if _is_religious_text(txt):
            debug_counters["filtered_posts_non_news_religious"] += 1
            continue
        cascade_news_posts.append(p)

    comm_meta = await session.execute(
        select(Community.vk_id, Community.name).where(Community.is_active.is_(True))
    )
    group_names = {str(abs(row[0])): row[1] for row in comm_meta.fetchall()}
    # Дополнительно — имена самих регионов-детей (по abs(vk_group_id)), чтобы
    # ссылки «источник» отображались как «МАЛМЫЖ - ИНФО», а не как голый id.
    for child in children:
        if child.vk_group_id:
            group_names[str(abs(int(child.vk_group_id)))] = child.name

    splitter = BulletinSplitter()
    mourning_posts, regular_posts = splitter.split_posts(cascade_news_posts)
    debug_counters["regular_posts_ready"] = len(regular_posts)
    if mourning_posts:
        debug_counters["filtered_posts_mourning"] = len(mourning_posts)
    # Для каскадных сводок траурные посты исключаем — у области/страны нет
    # своей траурной повестки, а посты из районов в их раздел не подходят.

    header = resolve_bulletin_header(region_config, theme, region)
    theme_tags, local_hashtag = resolve_bulletin_hashtags(region_config, theme)

    results = []
    selected_by_lip: Dict[str, Dict[str, Any]] = {}
    if regular_posts:
        builder = BulletinBuilder(
            header=header,
            hashtags=theme_tags,
            local_hashtag=local_hashtag,
            max_text_length=region_config.text_post_maxsize_simbols or 4096,
            repost_mode=region_config.setka_regim_repost,
            max_posts_per_bulletin=pipeline_eff.get("max_posts_per_bulletin"),
            footer=footer,
            urgent_lips=urgent_in_wave,
        )
        bulletin = builder.build_bulletin(regular_posts, group_names=group_names)
        if bulletin.post_count == 0 or not bulletin.text.strip():
            logger.warning(
                "Cascaded bulletin %s: empty regular bulletin, skipping publish "
                "(theme=%s candidates=%d)",
                region.code,
                theme,
                len(regular_posts),
            )
            debug_counters["filtered_posts_empty_bulletin"] = len(regular_posts)
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
                # read-only прогон: ничего не публикуем, отдаём превью.
                return {
                    "success": True,
                    "dry_run": True,
                    "region_code": region_code,
                    "theme": theme,
                    "regular_posts": len(regular_posts),
                    "mourning_posts": len(mourning_posts),
                    "would_publish": [
                        {
                            "kind": "regular",
                            "post_count": bulletin.post_count,
                            "char_count": len(bulletin.text or ""),
                            "attachments_count": len(bulletin.attachments_list or []),
                            "text_preview": (bulletin.text or "")[:1500],
                        }
                    ],
                    "bulletins_count": 1,
                    "stats": parser_stats,
                    "children_scanned": debug_counters["children_scanned"],
                    "candidate_posts": len(candidate_posts),
                    "debug": debug_counters,
                }
            vk_pub = await VKPublisher.create_with_policy(
                session,
                target_group_id=region.vk_group_id,
                test_polygon_mode=test_mode,
            )
            pub = await vk_pub.publish_bulletin(
                group_id=region.vk_group_id,
                text=bulletin.text,
                attachments=bulletin.attachments_list,
            )
            results.append(("regular", bulletin, pub))
            try:
                from monitoring.metrics import publish_result_label, track_digest_published

                track_digest_published(
                    region=region.code,
                    topic=theme,
                    result=publish_result_label(pub),
                )
            except Exception:  # pragma: no cover
                logger.warning("track_digest_published failed", exc_info=True)

    # Dedup cursor — only with actually published lips. A failed bulletin
    # was never posted: recording its lips would mute the posts for later
    # waves (silent loss).
    all_included, failed_included = partition_included_lips(results)
    if failed_included:
        logger.warning(
            "dedup cursor skips %d lips from failed bulletins " "(region=%s theme=%s): %s",
            len(failed_included),
            region.code,
            theme,
            ",".join(failed_included[:10]),
        )
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
        for included_lip in all_included:
            p = selected_by_lip.get(included_lip)
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

    # Shadow LLM-курация (PoC, письмо brain 2026-06-07): паркуем уже
    # опубликованные посты для пост-фактум вердикта /curate. Best-effort,
    # изолировано — на публикацию не влияет (см. modules/curation/recorder.py).
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

    total_published = sum(d.post_count for _, d, _ in results)
    first_url = results[0][2].get("url") if results else None

    return {
        "success": (all(r[2].get("success", False) for r in results) if results else True),
        "posts_published": total_published,
        "published_url": first_url,
        "regular_posts": len(regular_posts),
        "bulletins_count": len(results),
        "stats": parser_stats,
        "children_scanned": debug_counters["children_scanned"],
        "candidate_posts": len(candidate_posts),
        "debug": debug_counters,
    }


# Гейт #Новости снят 2026-08-29 (этап 4 ребрендинга): замер прод-логов показал
# posts_published=0 у ВСЕХ соседских прогонов — стена соседа-ИНФО состоит из
# сводок (их режет фильтр neighbor_bulletin, и это правильно), а гейт добивал
# остальное (18 из 25 постов). С появлением хедлайнеров (этап 3) на стенах
# соседей есть что перепечатывать нативно — без гейта. Region.config
# ['neighbor_hashtag'] оставлен как персональный гейт, если понадобится.
DEFAULT_NEIGHBOR_HASHTAG = None


async def run_neighbor_bulletin(
    session: AsyncSession,
    *,
    region_code: str,
    test_mode: bool = False,
    require_hashtag: Optional[str] = None,
) -> Dict[str, Any]:
    """Обмен новостями между соседями: репост `#Новости` с главных групп соседей.

    Тонкая обёртка над :func:`run_cascaded_bulletin` с ``source_mode="neighbors"``,
    ``theme="neighbors"`` и гейтом по хэштегу. Источники соседей — ``Region.neighbors``.
    Это единственный модуль соседского обмена (бывший ``neighbor_sharing.py`` удалён).

    ``require_hashtag`` по умолчанию берётся из ``region.config['neighbor_hashtag']``
    (если задан), иначе :data:`DEFAULT_NEIGHBOR_HASHTAG` (с 2026-08-29 — ``None``,
    гейт снят: см. комментарий у константы).
    """
    from database.models import Region

    res = await session.execute(select(Region).where(Region.code == region_code))
    region = res.scalars().first()

    gate = require_hashtag
    if gate is None:
        cfg = getattr(region, "config", None) if region else None
        if isinstance(cfg, dict):
            gate = cfg.get("neighbor_hashtag")
        if not gate:
            gate = DEFAULT_NEIGHBOR_HASHTAG

    # CTA-футер витрины соседа: «Ленты соседей: Имя url · …» — прямые ссылки
    # на группы, из которых собрана сводка (заказ владельца 2026-08-29:
    # «хотите узнать больше — идите в группу соседа»).
    footer = ""
    try:
        from tasks.parsing_scheduler_tasks import _build_neighbor_footer

        if region is not None:
            footer = await _build_neighbor_footer(session, region)
    except Exception:  # pragma: no cover - CTA не имеет права ронять канал
        logger.warning("neighbor CTA footer build failed", exc_info=True)

    return await run_cascaded_bulletin(
        session,
        region_code=region_code,
        theme="neighbors",
        test_mode=test_mode,
        source_mode="neighbors",
        require_hashtag=gate,
        footer=footer,
    )
