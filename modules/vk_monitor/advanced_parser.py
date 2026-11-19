"""
Advanced VK Parser - Main parsing orchestration module

Migrated from old_postopus bin/control/parser.py
Core parsing logic with full filtering pipeline.

This is the heart of the parsing system - fetches posts from VK communities,
applies all filters, and returns cleaned posts ready for bulletin building.
"""

import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from config.classifier import advertisement_filter_enabled
from config.runtime import (
    bulletin_jaccard_dedup_enabled,
    execute_batch_enabled,
    get_bulletin_jaccard_min_tokens,
    get_bulletin_jaccard_threshold,
    get_bulletin_simhash_bucket_gate,
    get_bulletin_similarity_threshold,
)
from modules.curation.collection_audit import record_collection_audit, should_audit
from modules.deduplication.fingerprints import (
    create_media_fingerprint,
    create_text_core_fingerprint,
    create_text_fingerprint,
    create_text_simhash,
    jaccard_similarity,
    simhash_hamming_distance,
    text_to_rafinad,
    text_token_set,
)
from modules.filters.ads_filter import is_marked_advertisement
from modules.vk_monitor.vk_client import VKClient
from utils.post_utils import clear_copy_history, lip_of_post, post_popularity
from utils.text_utils import check_blacklist, is_advertisement, is_hard_spam, is_neighbor_bulletin
from utils.vk_attachments import extract_vk_attachments, has_attachments

logger = logging.getLogger(__name__)

# Не считать «ядро» текста для near-dup, если rafinad слишком короткий (меньше ложных срабатываний)
_MIN_RAFINAD_LEN_FOR_CORE_DEDUP = 50
_MIN_RAFINAD_LEN_FOR_SIMILARITY_DEDUP = 80
_TEXT_SIMILARITY_THRESHOLD = 0.90
_SIMHASH_BUCKET_SIZE = 20

# Сводки: не брать посты старше СУТОК с момента публикации (оригинала при репосте).
#
# Решение владельца 2026-09-22, дословно: «событие получается вчера прошло, мы
# только сегодня о нём печатаем — нужно дать шанс сегодняшним постам пробиться в
# повестку». Прежние 72 часа работали против свежести вдвойне: старый пост не
# только доживал до следующего дня, но и выигрывал отбор, потому что за сутки
# успевал набрать просмотров и лайков больше, чем сегодняшний. Рейтинг слеп к
# возрасту (см. ``utils.post_utils.post_rating``), так что ограничение по
# возрасту — единственное место, где это лечится.
BULLETIN_MAX_POST_AGE_HOURS = 24


def _post_age_hours_utc(
    post_data: Dict[str, Any],
    now_ts: Optional[float] = None,
) -> Optional[float]:
    """Возраст поста в часах по полю date (Unix UTC). None — нет валидной даты."""
    raw = post_data.get("date")
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if now_ts is None:
        now_ts = datetime.now(tz=timezone.utc).timestamp()
    return max(0.0, (now_ts - ts) / 3600.0)


class AdvancedVKParser:
    """
    Advanced VK post parser with full filtering pipeline.

    Migrated from old_postopus parser.py with all filtering logic:
    1. Unwrap репостов, возраст, дедуп lip (work_table + батч)
    2. Фильтры black_id, реклама, blacklist, вложения, темы
    3. Дедуп текста и медиа внутри одного прогона

    Returns filtered posts ready for bulletin building.
    """

    def __init__(self, vk_client: VKClient):
        """
        Args:
            vk_client: VK API client instance
        """
        self.vk_client = vk_client
        # Отсеянные дубли волны и карта «ключ дедупа → lip первоисточника».
        # Здесь, а не только при старте волны: тесты и диагностика зовут
        # _filter_post напрямую, минуя parse_posts_from_communities.
        self._skipped_duplicates: List[Dict[str, Any]] = []
        self._batch_lip_by_key: Dict[str, str] = {}
        self._max_post_age_hours = float(BULLETIN_MAX_POST_AGE_HOURS)
        self._min_rafinad_core = int(_MIN_RAFINAD_LEN_FOR_CORE_DEDUP)
        self._min_rafinad_similarity = int(_MIN_RAFINAD_LEN_FOR_SIMILARITY_DEDUP)
        # near-dup параметры из env (дефолты = прежние хардкоды → нулевая регрессия)
        self._env_similarity_threshold = get_bulletin_similarity_threshold()
        self._simhash_bucket_gate = get_bulletin_simhash_bucket_gate()
        self._jaccard_enabled = bulletin_jaccard_dedup_enabled()
        self._jaccard_threshold = get_bulletin_jaccard_threshold()
        self._jaccard_min_tokens = get_bulletin_jaccard_min_tokens()
        self._text_similarity_threshold = float(self._env_similarity_threshold)
        self._max_simhash_hamming = self._compute_max_simhash_hamming(
            self._text_similarity_threshold
        )
        self._historical_text_simhashes: List[tuple[int, str]] = []
        self._batch_text_simhashes: Set[str] = set()
        # Множества слов постов текущего батча (intra-batch Jaccard near-dup).
        self._batch_token_sets: List[tuple[int, frozenset]] = []

        # Parsing statistics (stat_mode)
        self.stats = {
            "total_groups_checked": 0,
            "total_posts_scanned": 0,
            "posts_filtered_old": 0,
            "posts_filtered_duplicate_lip": 0,
            "posts_filtered_duplicate_text": 0,
            "posts_filtered_duplicate_foto": 0,
            # Диагностика near-dup (входят в posts_filtered_duplicate_text):
            "near_dup_simhash": 0,
            "near_dup_jaccard": 0,
            "posts_filtered_black_id": 0,
            "posts_filtered_no_region_words": 0,
            "posts_filtered_advertisement": 0,
            # Чужая размеченная реклама (метка ВК / erid / #реклама) — считается
            # отдельно от posts_filtered_advertisement: тот отсев работает по
            # commercial-scoring и в теме reklama выключен, а этот — надёжные
            # маркеры рекламодателя, режущие во всех темах.
            "posts_filtered_marked_ad": 0,
            "posts_filtered_hard_spam": 0,
            "posts_filtered_neighbor_bulletin": 0,
            "posts_filtered_classifier": 0,
            "posts_filtered_no_attachments": 0,
            "posts_filtered_blacklist_text": 0,
            # Тема sosed берёт только посты с #новости. До 2026-08-21 этот отсев
            # был ЕДИНСТВЕННЫМ в цепочке без счётчика — молчаливый `return None`
            # между двумя считающими соседями. Невидимый отсев нельзя ни measure-
            # before-promote, ни заметить, когда он начнёт резать лишнее.
            "posts_filtered_sosed_no_hashtag": 0,
            "posts_final_count": 0,
            "groups_with_posts": 0,
        }

    async def parse_posts_from_communities(
        self,
        community_ids: List[int],
        theme: str = "novost",
        region_config: Any = None,
        work_table_lip: List[str] = None,
        work_table_hash: List[str] = None,
        recent_text_fingerprints: List[str] = None,
        count_per_community: int = 20,
        shuffle_communities: bool = True,
        pipeline_settings: Optional[Dict[str, Any]] = None,
        blocked_lips: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Parse posts from multiple communities with full filtering.

        This is the main entry point, migrated from old_postopus parser().

        Args:
            community_ids: List of VK community IDs to scan
            theme: Theme (novost, kultura, sport, etc.)
            region_config: RegionConfig object for filtering
            work_table_lip: List of published post lips (for dedup)
            work_table_hash: List of photo hashes (for dedup)
            recent_text_fingerprints: Recent text fingerprints (for dedup)
            count_per_community: Posts to fetch per community
            shuffle_communities: Randomize community order
            pipeline_settings: Слитые настройки из bulletin_filters (возраст, дедуп, лимит fetch)
            blocked_lips: lip'ы, выключенные из публикации нейро-вердиктом
                классификатора (delete/hold; правка оператора главнее ИИ) —
                см. modules/classifier/enforce.fetch_blocked_lips

        Returns:
            List of filtered post data dicts
        """
        if work_table_lip is None:
            work_table_lip = []
        if work_table_hash is None:
            work_table_hash = []
        if recent_text_fingerprints is None:
            recent_text_fingerprints = []
        recent_text_set: Set[str] = (
            set(recent_text_fingerprints) if recent_text_fingerprints else set()
        )
        work_hash_set: Set[str] = set(work_table_hash or [])

        # Дедупликация внутри одного вызова parse_posts_from_communities
        self._batch_lips: Set[str] = set()
        # Отсеянные дубли волны и карта «ключ дедупа → lip первоисточника».
        # Инициализируются здесь тоже, а не только при старте волны: get_stats и
        # тесты трогают парсер до первого прогона.
        self._skipped_duplicates: List[Dict[str, Any]] = []
        self._batch_lip_by_key: Dict[str, str] = {}
        self._batch_text_fps: Set[str] = set()
        self._batch_core_fps: Set[str] = set()
        self._batch_media_sigs: Set[str] = set()
        self._batch_text_simhashes: Set[str] = set()
        self._batch_token_sets = []
        self._blocked_lips: Set[str] = set(blocked_lips or [])

        if pipeline_settings is None:
            self._max_post_age_hours = float(BULLETIN_MAX_POST_AGE_HOURS)
            self._min_rafinad_core = int(_MIN_RAFINAD_LEN_FOR_CORE_DEDUP)
            self._min_rafinad_similarity = int(_MIN_RAFINAD_LEN_FOR_SIMILARITY_DEDUP)
            self._text_similarity_threshold = float(self._env_similarity_threshold)
            effective_count = count_per_community
        else:
            self._max_post_age_hours = float(
                pipeline_settings.get("max_post_age_hours", BULLETIN_MAX_POST_AGE_HOURS)
            )
            self._min_rafinad_core = int(
                pipeline_settings.get("min_rafinad_len_core_dedup", _MIN_RAFINAD_LEN_FOR_CORE_DEDUP)
            )
            self._min_rafinad_similarity = int(
                pipeline_settings.get(
                    "min_rafinad_len_similarity_dedup",
                    _MIN_RAFINAD_LEN_FOR_SIMILARITY_DEDUP,
                )
            )
            self._text_similarity_threshold = float(
                pipeline_settings.get("text_similarity_threshold", self._env_similarity_threshold)
            )
            effective_count = int(
                pipeline_settings.get("posts_per_community_fetch", count_per_community)
            )
        self._max_simhash_hamming = self._compute_max_simhash_hamming(
            self._text_similarity_threshold
        )
        self._historical_text_simhashes = self._extract_text_simhashes(work_hash_set)

        # Порядок чтения стен больше не влияет на исход дубля: посты волны
        # сортируются по времени публикации ДО фильтрации (см. ниже). Shuffle
        # оставлен как есть — он распределяет нагрузку на ВК, а не решает споры.
        if shuffle_communities:
            random.shuffle(community_ids)

        # Пакетное чтение стен ДО цикла (execute, 2026-08-20): один запрос на
        # 20 групп вместо запроса на группу. Отказ пакета оставляет кэш пустым,
        # и цикл ниже читает поштучно, как раньше.
        await self._prefetch_walls(community_ids, effective_count)

        all_posts = []

        # Аудит сбора (ADR-0004, shadow): под гейтом копим сырой собранный батч,
        # чтобы после фильтрации записать обе стороны (kept + content-дропы). Ноль
        # оверхеда/риска, когда выключено.
        _audit_on = should_audit(getattr(region_config, "region_code", "") or "")
        _audit_collected: List[Dict[str, Any]] = []

        # Сбор со всех стен ДО фильтрации: чтобы решить спор двух похожих постов
        # по времени выхода, надо видеть обоих сразу.
        collected: List[Dict[str, Any]] = []
        for community_id in community_ids:
            self.stats["total_groups_checked"] += 1

            try:
                # Fetch posts from VK
                posts = await self._fetch_community_posts(community_id, effective_count)

                if not posts:
                    continue

                self.stats["groups_with_posts"] += 1
                if _audit_on:
                    _audit_collected.extend(posts)
                collected.extend(posts)

            except Exception as e:
                logger.error(f"❌ Failed to parse community {community_id}: {e}")
                continue

        # ПЕРВОИСТОЧНИК — ТОТ, КТО ВЫШЕЛ РАНЬШЕ (решение владельца 2026-08-30).
        # Дедуп в _filter_post устроен потоково: побеждает тот, кого просмотрели
        # первым, а второй отбрасывается. Раньше «первым» оказывался случайный —
        # порядок задавал random.shuffle по сообществам. Теперь вход упорядочен по
        # дате публикации, и «первый увиденный» означает «вышел раньше», то есть
        # сеть публикует первоисточник, а не того, кто переписал его быстрее.
        # Пост без даты уезжает в начало и отваливается на шаге 2 (возраст).
        collected.sort(key=lambda p: int(p.get("date") or 0))

        for post_data in collected:
            self.stats["total_posts_scanned"] += 1
            try:
                filtered = await self._filter_post(
                    post_data,
                    theme=theme,
                    region_config=region_config,
                    work_table_lip=work_table_lip,
                    work_hash_set=work_hash_set,
                    recent_text_fingerprints=recent_text_set,
                )
            except Exception as e:
                logger.error(f"❌ Failed to filter post: {e}")
                continue
            if filtered:
                all_posts.append(filtered)

        if _audit_on:
            await record_collection_audit(
                region_code=getattr(region_config, "region_code", "") or "",
                theme=theme,
                region_config=region_config,
                collected=_audit_collected,
                kept=all_posts,
            )

        # Sort by popularity
        all_posts.sort(
            key=lambda p: post_popularity(
                views=(
                    p.get("views", {}).get("count", 0)
                    if isinstance(p.get("views"), dict)
                    else p.get("views", 0)
                ),
                likes=p.get("likes", {}).get("count", 0),
                comments=p.get("comments", {}).get("count", 0),
                reposts=p.get("reposts", {}).get("count", 0),
            ),
            reverse=True,
        )

        self.stats["posts_final_count"] = len(all_posts)

        logger.info(
            f"📊 Parsing complete: {len(all_posts)} posts from "
            f"{self.stats['total_groups_checked']} groups"
        )

        return all_posts

    async def filter_posts_list(
        self,
        posts: List[Dict[str, Any]],
        theme: str,
        region_config: Any,
        work_table_lip: List[str],
        work_table_hash: List[str],
        recent_text_fingerprints: Optional[List[str]] = None,
        pipeline_settings: Optional[Dict[str, Any]] = None,
        blocked_lips: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Тот же пайплайн _filter_post, что и при обходе сообществ, но для уже загруженных постов
        (например областная сводка из ссылок на источники в районных сводках).
        """
        if work_table_lip is None:
            work_table_lip = []
        if work_table_hash is None:
            work_table_hash = []
        recent_text_set: Set[str] = set(recent_text_fingerprints or [])
        work_hash_set: Set[str] = set(work_table_hash or [])

        self._batch_lips = set()
        self._batch_text_fps = set()
        self._batch_core_fps = set()
        self._batch_media_sigs = set()
        self._batch_text_simhashes = set()
        self._batch_token_sets = []
        self._blocked_lips = set(blocked_lips or [])

        if pipeline_settings is None:
            self._max_post_age_hours = float(BULLETIN_MAX_POST_AGE_HOURS)
            self._min_rafinad_core = int(_MIN_RAFINAD_LEN_FOR_CORE_DEDUP)
            self._min_rafinad_similarity = int(_MIN_RAFINAD_LEN_FOR_SIMILARITY_DEDUP)
            self._text_similarity_threshold = float(self._env_similarity_threshold)
        else:
            self._max_post_age_hours = float(
                pipeline_settings.get("max_post_age_hours", BULLETIN_MAX_POST_AGE_HOURS)
            )
            self._min_rafinad_core = int(
                pipeline_settings.get("min_rafinad_len_core_dedup", _MIN_RAFINAD_LEN_FOR_CORE_DEDUP)
            )
            self._min_rafinad_similarity = int(
                pipeline_settings.get(
                    "min_rafinad_len_similarity_dedup",
                    _MIN_RAFINAD_LEN_FOR_SIMILARITY_DEDUP,
                )
            )
            self._text_similarity_threshold = float(
                pipeline_settings.get("text_similarity_threshold", self._env_similarity_threshold)
            )
        self._max_simhash_hamming = self._compute_max_simhash_hamming(
            self._text_similarity_threshold
        )
        self._historical_text_simhashes = self._extract_text_simhashes(work_hash_set)
        self._skipped_duplicates = []
        self._batch_lip_by_key = {}

        all_posts: List[Dict[str, Any]] = []
        # Тот же порядок, что и в районном пути: спор двух похожих постов решает
        # время выхода, а не порядок, в котором их принёс вызывающий.
        for post_data in sorted(posts, key=lambda p: int(p.get("date") or 0)):
            self.stats["total_posts_scanned"] += 1
            filtered = await self._filter_post(
                post_data,
                theme=theme,
                region_config=region_config,
                work_table_lip=work_table_lip,
                work_hash_set=work_hash_set,
                recent_text_fingerprints=recent_text_set,
            )
            if filtered:
                all_posts.append(filtered)

        all_posts.sort(
            key=lambda p: post_popularity(
                views=(
                    p.get("views", {}).get("count", 0)
                    if isinstance(p.get("views"), dict)
                    else p.get("views", 0)
                ),
                likes=p.get("likes", {}).get("count", 0),
                comments=p.get("comments", {}).get("count", 0),
                reposts=p.get("reposts", {}).get("count", 0),
            ),
            reverse=True,
        )
        self.stats["posts_final_count"] = len(all_posts)

        # Аудит сбора (ADR-0004, shadow): весь входной батч = собранное, all_posts = kept.
        if should_audit(getattr(region_config, "region_code", "") or ""):
            await record_collection_audit(
                region_code=getattr(region_config, "region_code", "") or "",
                theme=theme,
                region_config=region_config,
                collected=list(posts),
                kept=all_posts,
            )
        return all_posts

    async def _prefetch_walls(self, community_ids: List[int], count: int) -> None:
        """Прочитать стены ВСЕХ доноров волны пакетом (VK ``execute``).

        Измерено 2026-08-20: весь расход трёх user-токенов — это ``wall.get`` по
        донорским стенам, один вызов на группу на волну (5282 сканирования против
        5361 вызова за те же часы, при 1520 активных донорах). ``execute``
        исполняет до 25 вызовов на стороне ВК за один запрос, и волна из 13-14
        групп укладывается в один вызов вместо четырнадцати.

        **Отказ пакета не ломает волну.** Кэш просто остаётся пустым, и цикл
        читает группы поштучно, как раньше. Это единственная причина, по которой
        врезка сделана предзагрузкой, а не заменой чтения: путь «как было»
        остаётся живым и рабочим, а не аварийным.
        """
        self._wall_cache = {}
        if not community_ids or not execute_batch_enabled():
            return
        if not hasattr(self.vk_client, "get_wall_posts_batch"):
            return

        owner_ids = [-abs(int(cid)) for cid in community_ids]
        try:
            import asyncio

            self._wall_cache = await asyncio.to_thread(
                self.vk_client.get_wall_posts_batch, owner_ids, count
            )
        except Exception as e:  # noqa: BLE001 — пакет не обязан быть точкой отказа
            # WARNING, а не debug: прод гоняет LOG_LEVEL=INFO, и тихий откат на
            # поштучное чтение выглядел бы как «батчинг работает», хотя он не
            # работает ни дня. Ровно так однажды уже потеряли трое суток.
            logger.warning("execute-батч стен не удался (%s) — волна читает группы поштучно", e)
            self._wall_cache = {}

    async def _fetch_community_posts(self, community_id: int, count: int) -> List[Dict]:
        """Fetch posts from a single VK community."""
        # Use VK client to get wall posts
        # Implementation depends on your VK client setup

        # Пакетная предзагрузка (execute): группа уже прочитана — берём оттуда.
        # pop, а не get: стена нужна ровно один раз за волну, и держать её в
        # памяти до конца разбора незачем.
        cached = getattr(self, "_wall_cache", None)
        if cached is not None and -abs(community_id) in cached:
            posts = cached.pop(-abs(community_id)) or []
            for post in posts:
                if "owner_id" not in post:
                    post["owner_id"] = -abs(community_id)
            return posts

        if hasattr(self.vk_client, "get_wall_posts"):
            # VKClient is synchronous, run in thread
            import asyncio

            posts = await asyncio.to_thread(
                self.vk_client.get_wall_posts, -abs(community_id), count
            )
            # Add owner_id to each post (VK API may not include it for wall.get)
            for post in posts:
                if "owner_id" not in post:
                    post["owner_id"] = -abs(community_id)
            return posts
        elif hasattr(self.vk_client, "api_call"):
            response = await self.vk_client.api_call(
                "wall.get",
                {
                    "owner_id": -abs(community_id),  # Negative for groups
                    "count": count,
                },
            )
            return response.get("items", [])
        else:
            raise NotImplementedError("VK client doesn't support wall.get")

    async def _filter_post(
        self,
        post_data: Dict[str, Any],
        theme: str,
        region_config: Any,
        work_table_lip: List[str],
        work_hash_set: Set[str],
        recent_text_fingerprints: Set[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Apply full filtering pipeline to a single post.

        Важно: сначала unwrap репоста, иначе lip разный у одного и того же
        оригинала на разных стенах.
        """
        # 1. Развернуть репост → owner_id/post_id/text от оригинала
        post_data = clear_copy_history(post_data)

        owner_id = post_data.get("owner_id", post_data.get("from_id", 0))
        post_id = post_data.get("id", 0)
        text = (post_data.get("text") or "").strip()

        # 2. Возраст публикации
        age_h = _post_age_hours_utc(post_data)
        if age_h is None:
            self.stats["posts_filtered_old"] += 1
            return None
        if age_h > self._max_post_age_hours:
            self.stats["posts_filtered_old"] += 1
            return None

        # 3. Lip: уже в сводках / уже отобран в этом прогоне (один оригинал = один раз)
        lip = lip_of_post(owner_id, post_id)
        if lip in work_table_lip or lip in self._batch_lips:
            self.stats["posts_filtered_duplicate_lip"] += 1
            return None

        # 3a. Нейро-вердикт классификатора (enforce, заказ владельца 2026-07-13):
        #     пост с эффективным действием delete/hold выключен из публикации.
        #     Правка оператора главнее ИИ (см. modules/classifier/enforce).
        if lip in getattr(self, "_blocked_lips", set()):
            self.stats["posts_filtered_classifier"] += 1
            return None

        # 4. Black ID
        if region_config and region_config.black_id:
            if abs(owner_id) in [abs(x) for x in region_config.black_id]:
                self.stats["posts_filtered_black_id"] += 1
                return None

        # 5a. Hard spam / scam — режем ДАЖЕ для темы reklama (доска объявлений):
        #     VK банит за это аккаунт-админа паблика (инцидент Уржум 2026-07-08 —
        #     рекламная сводка ре-транслировала «удалённую работу / рассылку рекламы»
        #     → бан 4 дня). Легальные частные объявления (продам/куплю) сюда НЕ
        #     попадают — is_hard_spam узкий, ловит только скам/увод в обход VK.
        if is_hard_spam(text):
            self.stats["posts_filtered_hard_spam"] += 1
            return None

        # 5b. Своя же соседская сводка «Новости соседей …» — конечный продукт,
        #     обратно в оборот не берём НИКОГДА (каким бы популярным пост ни был):
        #     иначе матрёшка «сводка в сводке» (инцидент Малмыж 2026-07-27).
        if is_neighbor_bulletin(text):
            self.stats["posts_filtered_neighbor_bulletin"] += 1
            return None

        # 5c. Чужая РАЗМЕЧЕННАЯ реклама — режем во всех темах, включая reklama.
        #     Рубрика «объявления» строится из чужих агрегаторов, и ради частных
        #     объявлений рекламный фильтр там выключен целиком (шаг 5 ниже). Но
        #     выключен он был заодно и для коммерческой рекламы с легальной
        #     маркировкой — а именно за её ре-трансляцию ВК банит аккаунт админа
        #     (Уржум 2026-07-08, G151). Официальная метка ВК `marked_as_ads` при
        #     этом не проверялась НИ В ОДНОЙ теме: is_advertisement смотрит
        #     только текст.
        #     Маркеры узкие (`erid:`/`#реклама`/метка ВК) — их носит рекламодатель,
        #     частное «продам мёд» не носит, поэтому рубрика не обнуляется.
        if is_marked_advertisement(post_data):
            self.stats["posts_filtered_marked_ad"] += 1
            return None

        # 5. Advertisement filter — редакционная отсечка, «слепой второй
        #    редактор» (P091 §1: резала «Продам помидоры», которые правила
        #    16, 22 и 26 велят публиковать). Шаги 5a и 5c выше остаются
        #    ВСЕГДА: это защита от бана, а не редакция.
        #    Снимается флагом CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED=0 — решение
        #    владельца (шаг 3 звена 5). По умолчанию отсечка включена.
        #    Регион из CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS отсечку не
        #    получает — это «проба на одном районе» (P091, рекомендация):
        #    цена шага мерится на одном районе, а не разом на сети.
        if advertisement_filter_enabled(getattr(region_config, "region_code", "") or ""):
            is_reklama_theme = theme == "reklama"
            if is_advertisement(text, skip_for_reklama=is_reklama_theme, theme=theme):
                if not is_reklama_theme:
                    self.stats["posts_filtered_advertisement"] += 1
                    return None

        # 6. Blacklist text
        if region_config and region_config.delete_msg_blacklist:
            matched = check_blacklist(text, region_config.delete_msg_blacklist)
            if matched:
                self.stats["posts_filtered_blacklist_text"] += 1
                return None

        # 7. Region words filter (for communities that require regional relevance)
        if region_config and getattr(region_config, "filter_group_by_region_words", None):
            community_vk_id = post_data.get("community_vk_id", owner_id)
            try:
                community_key = str(abs(int(community_vk_id)))
            except (TypeError, ValueError):
                community_key = str(abs(int(owner_id))) if owner_id else ""

            group_words = None
            for group_id, words in (region_config.filter_group_by_region_words or {}).items():
                try:
                    if str(abs(int(group_id))) == community_key:
                        group_words = words
                        break
                except (TypeError, ValueError):
                    continue

            if group_words is not None:
                text_lower = text.lower()
                matched_words = [w for w in group_words if w and w.lower() in text_lower]
                if not matched_words:
                    self.stats["posts_filtered_no_region_words"] += 1
                    return None

                # Hide source attribution for posts accepted by regional keyword filter
                post_data["hide_attribution"] = True

        # 8. No-attachments filter (for non-novost/non-reklama themes; oblast = как новости)
        if theme not in ("novost", "reklama", "oblast"):
            attachments = extract_vk_attachments(post_data)
            if not has_attachments(attachments):
                self.stats["posts_filtered_no_attachments"] += 1
                return None

        # 9. Theme-specific filters
        if theme == "sosed":
            if "#новости" not in text.lower():
                self.stats["posts_filtered_sosed_no_hashtag"] += 1
                return None

        # 10. Дедуп по медиа (набор id вложений в этом прогоне + известные hash из work_table)
        raw_atts = post_data.get("attachments")
        if not isinstance(raw_atts, list):
            raw_atts = []
        media_ids = create_media_fingerprint(raw_atts)
        media_sig = ",".join(sorted(media_ids)) if media_ids else ""
        if media_ids:
            if any(mid in work_hash_set for mid in media_ids):
                self.stats["posts_filtered_duplicate_foto"] += 1
                self._note_duplicate(lip, None, "media-history")
                return None
            if media_sig in self._batch_media_sigs:
                self.stats["posts_filtered_duplicate_foto"] += 1
                self._note_duplicate(lip, self._batch_lip_by_key.get(f"media:{media_sig}"), "media")
                return None

        # 11. Дедуп по тексту (полный hash и «ядро» для похожих формулировок)
        if text:
            fp = create_text_fingerprint(text)
            if fp:
                if (
                    fp in self._batch_text_fps
                    or fp in recent_text_fingerprints
                    or f"txtfp:{fp}" in work_hash_set
                ):
                    self.stats["posts_filtered_duplicate_text"] += 1
                    self._note_duplicate(lip, self._batch_lip_by_key.get(f"txt:{fp}"), "text")
                    return None
                rlen = len(text_to_rafinad(text))
                if rlen >= self._min_rafinad_core:
                    cfp = create_text_core_fingerprint(text)
                    if cfp and (cfp in self._batch_core_fps or f"txtcore:{cfp}" in work_hash_set):
                        self.stats["posts_filtered_duplicate_text"] += 1
                        self._note_duplicate(
                            lip, self._batch_lip_by_key.get(f"core:{cfp}"), "text-core"
                        )
                        return None
                if rlen >= self._min_rafinad_similarity:
                    simhash = create_text_simhash(text)
                    if simhash and self._is_near_duplicate_text(simhash, rlen):
                        self.stats["near_dup_simhash"] += 1
                        self.stats["posts_filtered_duplicate_text"] += 1
                        # Первоисточник здесь не назвать: помощник отвечает «похож
                        # хоть на что-то», не говоря на что. Для журнала это не
                        # потеря — сам факт отсева важнее его виновника.
                        self._note_duplicate(lip, None, "text-simhash")
                        return None
                    # Второй сигнал: intra-batch Jaccard (переставленные/переписанные)
                    if getattr(self, "_jaccard_enabled", False):
                        tokset = text_token_set(text)
                        if len(tokset) >= self._jaccard_min_tokens and self._is_jaccard_duplicate(
                            tokset, rlen
                        ):
                            self.stats["near_dup_jaccard"] += 1
                            self.stats["posts_filtered_duplicate_text"] += 1
                            logger.info("near-dup (jaccard) drop: %s", text[:80].replace("\n", " "))
                            self._note_duplicate(lip, None, "text-jaccard")
                            return None

        # Регистрируем отобранный пост в батч-дедупе
        self._batch_lips.add(lip)
        if media_sig:
            self._batch_media_sigs.add(media_sig)
            self._batch_lip_by_key.setdefault(f"media:{media_sig}", lip)
        if text:
            fp = create_text_fingerprint(text)
            if fp:
                self._batch_text_fps.add(fp)
                self._batch_lip_by_key.setdefault(f"txt:{fp}", lip)
                if len(text_to_rafinad(text)) >= self._min_rafinad_core:
                    cfp = create_text_core_fingerprint(text)
                    if cfp:
                        self._batch_core_fps.add(cfp)
                        self._batch_lip_by_key.setdefault(f"core:{cfp}", lip)
                rlen_reg = len(text_to_rafinad(text))
                if rlen_reg >= self._min_rafinad_similarity:
                    simhash = create_text_simhash(text)
                    if simhash:
                        self._register_batch_simhash(simhash, rlen_reg)
                    if getattr(self, "_jaccard_enabled", False):
                        tokset = text_token_set(text)
                        if len(tokset) >= self._jaccard_min_tokens:
                            self._batch_token_sets.append(
                                (self._simhash_bucket_for_len(rlen_reg), tokset)
                            )

        return post_data

    def _note_duplicate(self, lip: str, original_lip: Optional[str], reason: str) -> None:
        """Запомнить отсеянный дубль на время волны (заказ владельца 2026-08-30).

        Вызывающий сохранит список в ``skipped_duplicates``, и этот lip больше не
        станет кандидатом. Без такой записи дубль не устранялся, а сдвигался: через
        пару часов следующая волна брала проигравшего как свежий пост — конкурента
        рядом уже нет, текст переписан, фото перезалито — и он выходил в ленту.

        Здесь только накопление в памяти: парсер не ходит в БД, и запись остаётся
        делом волны. Список живёт ровно один прогон и сбрасывается вместе с
        батч-множествами.
        """
        if not lip:
            return
        self._skipped_duplicates.append(
            {"lip": lip, "original_lip": original_lip, "reason": reason}
        )

    @staticmethod
    def _extract_text_simhashes(work_hash_set: Set[str]) -> List[tuple[int, str]]:
        out: List[tuple[int, str]] = []
        for item in work_hash_set:
            if not isinstance(item, str):
                continue
            if not item.startswith("txtsim:"):
                continue
            parts = item.split(":", 2)
            if len(parts) != 3:
                continue
            try:
                bucket = int(parts[1])
            except (TypeError, ValueError):
                continue
            sh = parts[2].strip().lower()
            if len(sh) != 16:
                continue
            out.append((bucket, sh))
        return out

    @staticmethod
    def _simhash_bucket_for_len(rafinad_len: int) -> int:
        return max(0, int(rafinad_len) // _SIMHASH_BUCKET_SIZE)

    @staticmethod
    def _compute_max_simhash_hamming(similarity_threshold: float) -> int:
        # SimHash is approximate; for practical 90% near-dup we use a slightly wider gate.
        raw = int((1.0 - similarity_threshold) * 128)
        return max(0, min(raw, 64))

    def _register_batch_simhash(self, simhash: str, rafinad_len: int) -> None:
        bucket = self._simhash_bucket_for_len(rafinad_len)
        self._batch_text_simhashes.add(f"{bucket}:{simhash}")

    def _iter_batch_simhashes(self) -> List[tuple[int, str]]:
        out: List[tuple[int, str]] = []
        for marker in getattr(self, "_batch_text_simhashes", set()):
            parts = marker.split(":", 1)
            if len(parts) != 2:
                continue
            try:
                bucket = int(parts[0])
            except (TypeError, ValueError):
                continue
            sh = parts[1].strip().lower()
            if len(sh) != 16:
                continue
            out.append((bucket, sh))
        return out

    def _is_near_duplicate_text(self, simhash: str, rafinad_len: int) -> bool:
        bucket = self._simhash_bucket_for_len(rafinad_len)
        gate = getattr(self, "_simhash_bucket_gate", 1)
        for b, sh in self._historical_text_simhashes:
            if abs(b - bucket) > gate:
                continue
            if simhash_hamming_distance(simhash, sh) <= self._max_simhash_hamming:
                return True
        for b, sh in self._iter_batch_simhashes():
            if abs(b - bucket) > gate:
                continue
            if simhash_hamming_distance(simhash, sh) <= self._max_simhash_hamming:
                return True
        return False

    def _is_jaccard_duplicate(self, tokset: frozenset, rafinad_len: int) -> bool:
        """Intra-batch near-dup по множеству слов (порядок-независимо).

        Ловит переставленные/переписанные пересказы одной новости в пределах
        одной сводки — класс, который char-SimHash упускает. Гейтится той же
        длины-корзиной, что и SimHash (анти-false-positive)."""
        bucket = self._simhash_bucket_for_len(rafinad_len)
        gate = getattr(self, "_simhash_bucket_gate", 1)
        for b, other in getattr(self, "_batch_token_sets", []):
            if abs(b - bucket) > gate:
                continue
            if jaccard_similarity(tokset, other) >= self._jaccard_threshold:
                return True
        return False

    def release_wave_state(self) -> None:
        """Отпустить память волны: кэш стен и все множества дедупа.

        Экземпляр парсера живёт ровно одну волну, и сборщик мусора забрал бы
        это сам — но когда дойдут руки. На боксе с 1536 МБ и без swap важно не
        «когда-нибудь», а «до фазы публикации»: районы идут в одном дочернем
        процессе подряд, и хвост предыдущей волны складывается с пиком
        следующей (P164).

        Зовётся ПОСЛЕ ``get_stats()`` и чтения ``_skipped_duplicates``: это
        части того же состояния, и порядок здесь — не стилистика. Повторный
        вызов безвреден, ``parse_posts_from_communities`` переинициализирует
        всё, чего касается.
        """
        self._wall_cache = {}
        self._batch_lips = set()
        self._batch_lip_by_key = {}
        self._batch_text_fps = set()
        self._batch_core_fps = set()
        self._batch_media_sigs = set()
        self._batch_text_simhashes = set()
        self._batch_token_sets = []
        self._historical_text_simhashes = []
        self._skipped_duplicates = []
        self._blocked_lips = set()

    def get_stats(self) -> Dict[str, Any]:
        """Get parsing statistics (for stat_mode)."""
        return self.stats.copy()

    def reset_stats(self):
        """Reset parsing statistics."""
        for key in self.stats:
            self.stats[key] = 0
