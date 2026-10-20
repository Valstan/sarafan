"""
Bulletin Builder - Assembles multiple posts into a single VK bulletin post

Migrated from old_postopus bin/rw/posting_post.py
Builds formatted bulletins with headers, attribution, hashtags, and media attachments.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from utils.post_utils import extract_source_attribution, lip_of_post, post_lip
from utils.text_utils import truncate_text
from utils.vk_attachments import build_attachments_list, extract_vk_attachments


@dataclass
class BulletinPost:
    """A single post included in the bulletin"""

    post_data: Dict[str, Any]
    source_attribution: str
    text: str
    attachments: Dict[str, List[Dict]]
    lip: str
    popularity_score: float = 0.0


@dataclass
class BulletinResult:
    """Result of bulletin building"""

    text: str
    attachments_list: List[str]  # VK API attachment strings
    post_count: int
    total_length: int
    posts_included: List[str]  # lip strings
    max_length_exceeded: bool = False
    max_attachments_exceeded: bool = False


class BulletinBuilder:
    """
    Builds bulletin posts from multiple source posts.

    Migrated from old_postopus posting_post() function.

    Features:
    - Adds header from region config (zagolovki)
    - Adds source attribution: @url (group_name)
    - Adds hashtags: #hashtag #local_region
    - Respects VK limits (4096 chars, 10 media items)
    - Sorts posts by popularity
    - Tracks lip hashes to prevent re-publishing
    """

    # VK limits
    MAX_TEXT_LENGTH = 4096  # VK post text limit
    MAX_ATTACHMENTS = 10  # VK wall.post media limit
    MAX_HASHTAGS = 5  # потолок подписи: короче — читаемее (решение 20.10)
    MAX_POSTS_PER_BULLETIN = 3  # Maximum number of posts in a single bulletin

    # Default header
    DEFAULT_HEADER = "📰 Сводка новостей"

    # Маркер ✍ перед каждым элементом СНЯТ 2026-09-22 (решение владельца).
    #
    # Он достался от old_postopus и разделял новости внутри сводки. После
    # перехода на одну новость в посте (P168) разделять ему нечего: он стал
    # просто первым символом каждой публикации — а со снятием шапок тем в тот
    # же день ещё и единственным, что отличало наш пост от оригинала.
    # Владелец: «оставь чистые посты, как они есть в оригинале».
    #
    # Разделение элементов в многоэлементной сводке не пострадало: между ними
    # и раньше стояла пустая строка, маркер был украшением поверх неё.
    #
    # ⚠️ На маркере считал элементы прибор `scripts/probe_post_form_reach.py`
    # (замер «одиночный пост против сводки», P168). Он переведён на подсчёт
    # ссылок-атрибуций — иначе молча считал бы все посты после 22.09 «не нашей
    # вёрсткой» и выбрасывал бы их из замера.

    def __init__(
        self,
        header: str = "",
        hashtags: List[str] = None,
        local_hashtag: str = "",
        max_text_length: int = MAX_TEXT_LENGTH,
        repost_mode: bool = False,
        max_posts_per_bulletin: Optional[int] = None,
        footer: str = "",
        urgent_lips: Optional[Set[str]] = None,
        topical_hashtags: Optional[List[str]] = None,
    ):
        """
        Args:
            header: Bulletin header text
            hashtags: List of theme hashtags
            local_hashtag: Local region hashtag
            max_text_length: Maximum text length
            repost_mode: True = VK repost, False = copy with attribution
            max_posts_per_bulletin: Сколько новостей максимум в одном сводке (из настроек региона)
            footer: Строка-футер между постами и хэштегами (например «Ленты
                соседей: …» — этап 4 ребрендинга); пустая = без футера.
                Участвует в бюджете длины наравне с хэштегами.
            urgent_lips: lip'ы, которые нейро-вердикт признал практически
                срочными для жителей (отключения, перекрытия, аварии,
                предупреждения МЧС). Такие посты встают впереди всех остальных,
                минуя рейтинг. Пусто = порядок прежний.
            topical_hashtags: теги «по сути новости» от модели (``#субботник``,
                ``#ремонт_дорог``); уже с решёткой, список ограничен вызывающим.
        """
        # Пустая строка = без заголовка (например траурная сводка); None = дефолтный заголовок
        if header is None:
            self.header = self.DEFAULT_HEADER
        else:
            self.header = header
        self.hashtags = hashtags or []
        self.local_hashtag = local_hashtag
        self.topical_hashtags = topical_hashtags or []
        self.footer = (footer or "").strip()
        self.max_text_length = max_text_length
        self.repost_mode = repost_mode
        self.max_posts_per_bulletin = (
            int(max_posts_per_bulletin)
            if max_posts_per_bulletin is not None
            else self.MAX_POSTS_PER_BULLETIN
        )
        self.max_posts_per_bulletin = max(1, min(self.max_posts_per_bulletin, 10))
        self.urgent_lips: Set[str] = set(urgent_lips or ())

    def build_bulletin(
        self,
        posts: List[Dict[str, Any]],
        group_names: Dict[str, str] = None,
    ) -> BulletinResult:
        """
        Build bulletin from list of posts.

        Format (old_postopus style):
            {HEADER}

            {post_text_1}

            @https://vk.com/wall-... (source_name)

            {post_text_2}

            @https://vk.com/wall-... (source_name)

            #hashtag1 #hashtag2

        IMPORTANT: Posts that don't fit entirely are SKIPPED (not truncated).
        They will be included in the next iteration.

        Args:
            posts: List of VK post data dicts
            group_names: Dict mapping community_vk_id -> group display name

        Returns:
            BulletinResult with formatted text and attachments
        """
        if group_names is None:
            group_names = {}

        # Sort posts by popularity
        sorted_posts = self._sort_by_popularity(posts)

        # Build bulletin text and collect attachments
        bulletin_parts = []
        flat_attachments = []
        posts_included = []

        # Заголовок (если задан непустой)
        if self.header and str(self.header).strip():
            bulletin_parts.append(str(self.header).strip())
            bulletin_parts.append("")  # пустая строка после заголовка

        # Calculate static content that must always fit
        hashtag_text = self._build_hashtag_text()
        hashtag_overhead = len(hashtag_text) + 2 if hashtag_text else 2  # +2 for newlines
        if self.footer:
            hashtag_overhead += len(self.footer) + 2

        for post_data in sorted_posts:
            # Stop if we've reached max posts
            if len(posts_included) >= self.max_posts_per_bulletin:
                break

            # Extract post info
            owner_id = post_data.get("owner_id", post_data.get("from_id", 0))
            post_id = post_data.get("id", 0)
            post_text = post_data.get("text", "") or ""

            # Skip posts with no text (problem 3)
            if not post_text.strip():
                continue

            # Get group name (ключи в group_names — по abs(owner_id))
            community_vk_id = post_data.get("community_vk_id", owner_id)
            try:
                aid = abs(int(community_vk_id if community_vk_id is not None else owner_id))
            except (TypeError, ValueError):
                aid = abs(int(owner_id)) if owner_id else 0
            group_name = group_names.get(str(aid), "") if aid else ""

            # Build the complete post entry
            post_entry = self._format_post_entry(
                post_data, post_text, owner_id, post_id, group_name
            )

            # Extract attachments to check if they fit
            attachments = extract_vk_attachments(post_data)
            post_attachments = build_attachments_list(attachments)

            # Calculate total length if we add this post
            current_length = sum(len(part) for part in bulletin_parts)
            new_total = current_length + len(post_entry) + hashtag_overhead

            # Check if the FULL post fits (no truncation allowed)
            if new_total > self.max_text_length:
                # Skip this post — it will be included in the next iteration
                continue

            # Check if attachments fit (problem 5: if text fits but media doesn't — skip)
            current_attachment_count = len(flat_attachments)
            remaining_attachment_slots = self.MAX_ATTACHMENTS - current_attachment_count
            if len(post_attachments) > remaining_attachment_slots and post_attachments:
                # Post has media but no room — skip
                continue

            # Add the post
            bulletin_parts.append(post_entry)
            bulletin_parts.append("")  # Empty line separator

            # Track lip
            lip = lip_of_post(owner_id, post_id)
            posts_included.append(lip)

            # Collect attachments
            flat_attachments.extend(post_attachments)

        # If no post made it through the loop, return an empty result so callers
        # never publish a "bulletin" that's just a header + hashtags. All filtering
        # paths (no text, doesn't fit, attachments don't fit) end up here.
        if not posts_included:
            return BulletinResult(
                text="",
                attachments_list=[],
                post_count=0,
                total_length=0,
                posts_included=[],
                max_length_exceeded=False,
                max_attachments_exceeded=False,
            )

        # Футер (ссылки на соседей) — между постами и хэштегами.
        if self.footer:
            bulletin_parts.append(self.footer)
            bulletin_parts.append("")

        # Add hashtags at the end
        if hashtag_text:
            bulletin_parts.append(hashtag_text)

        # Join all parts — NO final truncation
        full_text = "\n".join(bulletin_parts)

        # Truncate attachments to VK limit
        if len(flat_attachments) > self.MAX_ATTACHMENTS:
            flat_attachments = flat_attachments[: self.MAX_ATTACHMENTS]

        max_attachments_exceeded = len(flat_attachments) > self.MAX_ATTACHMENTS

        return BulletinResult(
            text=full_text,
            attachments_list=flat_attachments[: self.MAX_ATTACHMENTS],
            post_count=len(posts_included),
            total_length=len(full_text),
            posts_included=posts_included,
            max_length_exceeded=False,  # No truncation — posts that don't fit are skipped
            max_attachments_exceeded=max_attachments_exceeded,
        )

    def _format_post_entry(
        self,
        post_data: Dict[str, Any],
        post_text: str,
        owner_id: int,
        post_id: int,
        group_name: str,
    ) -> str:
        """
        Format a single post entry: text and source attribution.

        Format:
            {post_text}

            @https://vk.com/wall{owner_id}_{post_id} (group_name)

        Args:
            post_data: Raw post data dict
            post_text: Post text content
            owner_id: VK owner_id
            post_id: VK post ID
            group_name: Source group display name

        Returns:
            Formatted post entry string
        """
        parts = []

        # Заголовок (модель, ``rewriter``) — отдельной строкой над пересказом.
        headline = " ".join(str(post_data.get("headline") or "").split())
        if headline:
            parts.append(headline)
            parts.append("")

        # Post marker + text
        if post_text:
            parts.append(post_text)
        else:
            parts.append("[без текста]")

        # Empty line between text and attribution only when attribution is present
        if not post_data.get("hide_attribution"):
            attribution = extract_source_attribution(post_data, group_name)
            if attribution:
                parts.append("")
                parts.append(attribution)

        return "\n".join(parts)

    def order_for_publication(self, posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Порядок, в котором посты пойдут в публикацию (публичная обёртка).

        Нужен тем, кто готовит пост ДО сборки: пересказ текста (``rewriter``)
        платит за каждый пост, поэтому переписывать надо ровно те, что попадут
        в ленту, — а это первые ``max_posts_per_bulletin`` в этом порядке.
        """
        return self._sort_by_popularity(posts)

    def _sort_by_popularity(self, posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort posts by rating score, descending (звено 5, шаг 2).

        ``post_rating`` с настраиваемой ``RATING_VIEWS_ALPHA`` вместо жёсткой
        ``post_popularity`` (та же формула при alpha=0.5 — гейт в тестах).
        Дефолт 0.25 выбран владельцем 2026-08-20 по витрине на 1078 измеренных
        кандидатах 28 районов. Откат — env, без деплоя.

        Пост без ``views`` → ``score = None`` → хвост очереди, как в витрине:
        прежний дефолт 0 схлопывал делитель в единицу, и пост без единого
        просмотра мог обогнать районный хит.
        """
        import time

        from config.classifier import get_rating_views_alpha
        from config.runtime import get_bulletin_fresh_hours
        from utils.post_utils import post_rating

        alpha = get_rating_views_alpha()
        fresh_hours = get_bulletin_fresh_hours()
        now_ts = time.time()

        def get_score(post_data):
            views = post_data.get("views")
            if isinstance(views, dict):
                views = views.get("count")
            return post_rating(
                views=views,
                likes=post_data.get("likes", {}).get("count", 0),
                comments=post_data.get("comments", {}).get("count", 0),
                reposts=post_data.get("reposts", {}).get("count", 0),
                alpha=alpha,
            )

        def is_fresh(post_data) -> bool:
            """Пост моложе порога свежести. Без даты — считаем свежим.

            «Без даты → свежий» намеренно: пост без ``date`` до сборщика доходит
            только в обход возрастного фильтра (шаг 2 отбрасывает такие), то есть
            это редкий случай, и задвигать его в хвост значило бы наказывать за
            дефект данных, а не за возраст.
            """
            if fresh_hours <= 0:
                return True
            ts = post_data.get("date")
            if not ts:
                return True
            try:
                return (now_ts - float(ts)) <= fresh_hours * 3600
            except (TypeError, ValueError):
                return True

        def is_urgent(post_data) -> bool:
            """Нейро-вердикт признал пост практически срочным для жителей.

            Срочность стоит ВПЕРЕДИ свежести, а свежесть впереди рейтинга —
            порядок ступеней и есть решение владельца 2026-09-22. У сообщения
            об аварии, вывешенного десять минут назад, нет ни просмотров, ни
            лайков; по рейтингу оно проигрывает чему угодно, а прочитать его
            людям нужно сегодня. Пустое множество (нет вердиктов, движок
            отказал) → ступень всегда ложна и порядок ровно прежний.
            """
            if not self.urgent_lips:
                return False
            return post_lip(post_data) in self.urgent_lips

        # СРОЧНОЕ, потом СВЕЖЕЕ ВПЕРЕДИ (заказ владельца 2026-08-30): «свежак
        # должен первым выходить, а если свежака нету — берутся слегка
        # устаревшие». Раньше порядок задавал только рейтинг, и вчерашний хит
        # обгонял сегодняшнюю новость. Внутри каждой группы порядок прежний —
        # по рейтингу, поэтому для волны без срочного и устаревшего поведение
        # не меняется вовсе.
        return sorted(
            posts,
            key=lambda p: (is_urgent(p), is_fresh(p), (s := get_score(p)) is not None, s or 0.0),
            reverse=True,
        )

    def _available_length(self, current_parts: List[str], post_number: int) -> int:
        """Calculate available length for current post."""
        current_length = sum(len(part) for part in current_parts)

        # Estimate remaining length needed
        # Reserve some space for hashtags and future posts
        reserved = 200  # Hashtags + spacing
        remaining = self.max_text_length - current_length - reserved

        # Don't let single post take more than 40% of space
        max_per_post = int(self.max_text_length * 0.4)

        return min(remaining, max_per_post)

    def _build_hashtag_text(self) -> str:
        """Build hashtag string for bulletin.

        Порядок подписи — от общего к частному: тема, тег по сути новости,
        локальные теги района. Потолок в пять тегов: «супер-эффективные»
        паблики держат подпись короткой, а длинная простыня тегов читается
        как спам и обрезается на превью.
        """
        raw: List[str] = []

        # Тематические теги (тема словом, районный тег района)
        raw.extend(self.hashtags)
        # Тег по сути конкретной новости — от модели.
        raw.extend(self.topical_hashtags)
        # Локальный тег района — последним, он же самый «широкий».
        if self.local_hashtag:
            raw.append(self.local_hashtag)

        if not raw:
            return ""

        formatted: List[str] = []
        seen: Set[str] = set()
        for tag in raw:
            value = str(tag or "").strip()
            if not value:
                continue
            if not value.startswith("#"):
                value = f"#{value}"
            # Внутри одного поста тег повторять незачем: «#малмыж #Малмыж» —
            # ровно то, что владелец назвал недоделанной подписью.
            key = value.lstrip("#").lower().replace("ё", "е")
            if key in seen:
                continue
            seen.add(key)
            formatted.append(value)
            if len(formatted) >= self.MAX_HASHTAGS:
                break

        return " ".join(formatted)

    def estimate_post_capacity(
        self,
        avg_post_length: int = 200,
        avg_attachments: int = 1,
    ) -> int:
        """
        Estimate how many posts can fit in bulletin.

        Args:
            avg_post_length: Average post text length
            avg_attachments: Average attachments per post

        Returns:
            Estimated number of posts
        """
        # Text capacity
        header_length = (
            (len(str(self.header).strip()) + 2) if (self.header and str(self.header).strip()) else 0
        )
        hashtag_length = len(self._build_hashtag_text()) + 2
        available = self.max_text_length - header_length - hashtag_length

        # Each post needs: text + attribution + newline
        post_overhead = 50  # Attribution + spacing
        posts_by_text = available // (avg_post_length + post_overhead)

        # Attachment capacity
        posts_by_attachments = self.MAX_ATTACHMENTS // max(avg_attachments, 1)

        # Take minimum
        return min(posts_by_text, posts_by_attachments, self.max_posts_per_bulletin)


class TextOnlyBulletinBuilder(BulletinBuilder):
    """
    Builds text-only bulletin (no media attachments).

    Migrated from old_postopus post_bezfoto() function.
    Used for advertising bulletins where images aren't needed.
    """

    def build_bulletin(
        self,
        posts: List[Dict[str, Any]],
        group_names: Dict[str, str] = None,
    ) -> BulletinResult:
        """Build text-only bulletin."""
        # Use parent build but strip attachments
        result = super().build_bulletin(posts, group_names)

        # Clear attachments
        result.attachments_list = []
        result.max_attachments_exceeded = False

        return result

    def build_bezfoto_bulletin(
        self,
        text_items: List[str],
        header: str = "",
        hashtag: str = "",
    ) -> BulletinResult:
        """
        Build bulletin from text-only items (bezfoto).

        Args:
            text_items: List of text items to include
            header: Bulletin header
            hashtag: Single hashtag for bulletin

        Returns:
            BulletinResult
        """
        parts = []

        # Add header
        if header:
            parts.append(header)
            parts.append("")

        # Add text items (limit to 15 as in old_postopus)
        for i, item in enumerate(text_items[:15]):
            parts.append(f"{i+1}. {item}")
            parts.append("")

        # Add hashtag
        if hashtag:
            tag = hashtag if hashtag.startswith("#") else f"#{hashtag}"
            parts.append(tag)

        # Join and truncate
        full_text = "\n".join(parts)
        if len(full_text) > self.max_text_length:
            full_text = truncate_text(full_text, self.max_text_length, "\n\n...")

        return BulletinResult(
            text=full_text,
            attachments_list=[],
            post_count=len(text_items[:15]),
            total_length=len(full_text),
            posts_included=[],
        )
