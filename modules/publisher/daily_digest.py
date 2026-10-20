"""Вечерняя сводка «Короткой строкой»: главное за день из невышедших постов.

Заказ владельца 20.10 (P182): вечером выходит последний пост дня — сжатые
пересказы новостей, которые сегодня **не попали в ленту**, от самых рейтинговых
к менее заметным. Ссылок нет, у каждого пункта — название источника.

Зачем это нужно, а не «ещё одна рубрика». Лента отдаёт один пост на волну, а
окно кандидата — сутки: что не вышло сегодня, не выйдет уже никогда (P168 +
PR #752). 87 % отобранного материала умирает не прочитанным; вечерний сборник —
единственное место, где он доживает до читателя.

Границы решения:

- **Отбор — фактами, а не настроением.** Кандидаты берутся из
  ``collected_post_audit`` (решение ``kept``), исключаются опубликованные
  (``published_posts`` этого района) и уже стоящие в курсоре дедупа
  (``work_tables.lip``). Сортировка — рейтинг по метрикам поста, плюс
  отдельное место самой свежей новости дня: она открывает сборник.
- **Пересказ — необязательная надстройка.** Движок недоступен или ответ не по
  форме — каждый пункт берётся первым предложением источника (обрезанным по
  границе слова). Сборник выходит всегда, когда есть что собрать.
- **Мало материала — сборника нет.** Меньше ``MIN_ITEMS`` пунктов не тянет на
  «главное за день» и читается как случайная пачка; лучше промолчать.
- **Нет текста — нет пункта.** Пост без текста (только фото) в сборник не
  попадает: пересказывать нечего, а голая ссылка на источник запрещена.

Наблюдаемость: строка в лог ``daily digest: регион=… пунктов=…`` и метрика
публикации с темой ``korotko`` (heartbeat тоже обновляется — публикация
настоящая).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

HEADER = "Короткой строкой"

# Минимальная длина исходника: короткое объявление в двух предложениях
# пересказать нельзя — пересказ выйдет длиннее источника и добавит воды.
MIN_SOURCE_CHARS = 200
MIN_ITEMS = 3
DEFAULT_ITEMS = 5
MAX_ITEMS = 7
MAX_ITEM_CHARS = 260
MAX_TOTAL_CHARS = 3400


def _rating(row: Any) -> float:
    """Рейтинг поста ТЕМ ЖЕ ``post_rating``, что у ленты и квот тем.

    Своя формула здесь была бы тихой поломкой: сборник ранжировал бы одним
    ключом, а лента — другим, и «самые популярные» означало бы разное в двух
    местах. Просмотры не измерены — пост уходит в хвост (``None``), а не
    наверх: иначе он обогнал бы районный хит на пустом месте.
    """
    from config.classifier import get_rating_views_alpha
    from utils.post_utils import post_rating

    try:
        alpha = float(get_rating_views_alpha())
    except Exception:  # pragma: no cover - конфиг всегда есть
        alpha = 0.5
    value = post_rating(
        getattr(row, "views", None),
        getattr(row, "likes", None),
        getattr(row, "comments", None),
        getattr(row, "reposts", None),
        alpha=alpha,
    )
    return float(value) if value is not None else -1.0


SYSTEM_PROMPT = """Ты — редактор районной новостной ленты ВКонтакте. Тебе дают
несколько новостей, которые сегодня не вышли в ленту. Для каждой напиши
короткий пересказ своими словами — 1–2 предложения, до 200 знаков.

ФОРМАТ ОТВЕТА: нумерованный список, ровно столько пунктов, сколько новостей:
1. <пересказ>
2. <пересказ>

ЖЁСТКИЕ ПРАВИЛА:
- Сохрани факты, имена, числа и названия ровно как в источнике. Ничего не
  добавляй и не додумывай: чего нет в тексте, того нет и в пересказе.
- Пиши нейтрально, как новостная строка: без оценок, призывов и вопросов.
- Не упоминай источник: никаких «в сообществе пишут», «как сообщается».
- Без ссылок, хэштегов, упоминаний через @, markdown и эмодзи.
- Не повторяй номер внутри текста пункта. Верни только список.
"""

_LABEL_RE = re.compile(
    r"^\s*(?:готовый\s+текст|текст\s+поста|текст|пост|пересказ|новость)\s*:\s*",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")
_MD_EMPH_RE = re.compile(r"(\*\*|__|\*|_|`)")
_HASHTAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")
_MENTION_RE = re.compile(r"(?:^|\s)@[A-Za-z0-9_.]+")
_MULTI_SPACE_RE = re.compile(r"\s+")
_NUMBERED_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.+?)\s*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class DigestItem:
    """Один пункт сборника: пересказ и подпись источника."""

    text: str
    source_name: str = ""
    lip: str = ""

    def render(self) -> str:
        block = self.text.strip()
        if self.source_name:
            block = f"{block}\n\nИсточник: {self.source_name}"
        return block


@dataclass
class DigestDraft:
    """Готовый сборник: текст поста и lip'ы, которые он «израсходовал»."""

    text: str
    lips: List[str] = field(default_factory=list)
    items: List[DigestItem] = field(default_factory=list)


def day_start_utc(now: Optional[datetime] = None) -> datetime:
    """Начало сегодняшних суток по Москве, в naive UTC (формат ``collected_at``).

    Хранилище пишет ``datetime.utcnow()``; границу «за день» владелец считает
    по московским суткам, поэтому переводим здесь, а не в SQL. Naive-время
    трактуется как московское: единственный источник — ``now_moscow()``.
    """
    tz = _moscow_tz()
    if now is None:
        from utils.timezone import now_moscow

        now = now_moscow()
    if now.tzinfo is None:
        now = tz.localize(now) if hasattr(tz, "localize") else now.replace(tzinfo=tz)
    msk = now.astimezone(tz)
    start_local = msk.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def _moscow_tz():
    try:
        from utils.timezone import MOSCOW_TZ

        return MOSCOW_TZ
    except Exception:  # pragma: no cover - pytz есть в зависимостях
        return timezone(timedelta(hours=3))


def _normalize_for_dedupe(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()[:80]


def _owner_id_from_lip(lip: str) -> int:
    try:
        return int(str(lip).split("_", 1)[0])
    except (TypeError, ValueError):
        return 0


async def collect_candidates(
    session: Any,
    region_code: str,
    *,
    since: Optional[datetime] = None,
    limit: int = DEFAULT_ITEMS,
    exclude_lips: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """Посты дня, прошедшие фильтр и не опубликованные — от рейтинговых.

    ``exclude_lips`` — курсор дедупа (``work_tables.lip``): пост мог выйти до
    появления журнала публикаций или быть помеченным вручную, и повторять его
    в сборнике нельзя.
    """
    from sqlalchemy import exists, func, select

    from database.models_extended import CollectedPostAudit, PublishedPost

    since = since or day_start_utc()
    limit = max(MIN_ITEMS, min(int(limit or DEFAULT_ITEMS), MAX_ITEMS))
    excluded = {str(lip) for lip in exclude_lips or () if str(lip or "").strip()}

    already_published = exists().where(
        PublishedPost.lip == CollectedPostAudit.lip,
        PublishedPost.region_code == region_code,
    )
    # Рейтинг считаем в Python (`post_rating`), а не в SQL: одна и та же
    # формула с лентой важнее, чем экономия строк. Окно берём с запасом —
    # после дедупа перепечаток и отсева курсора рейтинговать нечего, если
    # выбрать ровно `limit` строк.
    stmt = (
        select(CollectedPostAudit)
        .where(
            CollectedPostAudit.region_code == region_code,
            CollectedPostAudit.decision == "kept",
            CollectedPostAudit.collected_at >= since,
            CollectedPostAudit.post_text.isnot(None),
            func.length(CollectedPostAudit.post_text) >= MIN_SOURCE_CHARS,
            ~already_published,
        )
        .order_by(CollectedPostAudit.collected_at.desc())
        .limit(limit * 20)
    )
    rows = (await session.execute(stmt)).scalars().all()

    candidates: List[Dict[str, Any]] = []
    seen_texts = set()
    for row in rows:
        lip = str(getattr(row, "lip", "") or "")
        text = str(getattr(row, "post_text", "") or "").strip()
        if not lip or not text or lip in excluded:
            continue
        key = _normalize_for_dedupe(text)
        if key and key in seen_texts:
            # Одна новость разными lip (перепечатка) — в сборнике она одна.
            continue
        seen_texts.add(key)
        candidates.append(
            {
                "lip": lip,
                "text": text,
                "owner_id": _owner_id_from_lip(lip),
                "collected_at": getattr(row, "collected_at", None),
                "score": _rating(row),
                "theme": getattr(row, "theme", None),
            }
        )

    if not candidates:
        return []

    # Свежая новость дня открывает сборник: владелец просил начать с самой
    # последней новости дня. Дальше — по рейтингу, как в ленте.
    newest = max(
        candidates,
        key=lambda p: (p.get("collected_at") or datetime.min, p.get("score") or 0),
    )
    ranked = sorted(
        candidates,
        key=lambda p: (p.get("score") or 0, p.get("collected_at") or datetime.min),
        reverse=True,
    )[:limit]
    if newest["lip"] not in {p["lip"] for p in ranked}:
        ranked = ranked[: max(0, limit - 1)] + [newest]
    ordered = [newest] + [p for p in ranked if p["lip"] != newest["lip"]]
    return ordered[:limit]


def clean_item(raw: str) -> str:
    """Пересказ пункта → одна строка без разметки, не длиннее потолка."""
    text = (raw or "").strip()
    text = _LABEL_RE.sub("", text, count=1)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_EMPH_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    if len(text) <= MAX_ITEM_CHARS:
        return text
    cut = text[:MAX_ITEM_CHARS]
    if "." in cut:
        cut = cut[: cut.rfind(".") + 1]
    elif " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip().rstrip(",;:—-") + ("." if cut and cut[-1] not in ".!?" else "")


def fallback_item(source: str) -> str:
    """Первое-второе предложение источника — когда модель молчит."""
    text = _MULTI_SPACE_RE.sub(" ", (source or "")).strip()
    if not text:
        return ""
    pieces: List[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if pieces and len(" ".join(pieces + [sentence])) > MAX_ITEM_CHARS:
            break
        pieces.append(sentence)
        if len(" ".join(pieces)) >= 120:
            break
    return clean_item(" ".join(pieces))


def build_items_prompt(posts: Sequence[Dict[str, Any]], *, region: str = "") -> str:
    lines = [f"Район: {(region or 'район').strip()}", ""]
    for index, post in enumerate(posts, start=1):
        text = _MULTI_SPACE_RE.sub(" ", str(post.get("text") or "")).strip()
        lines.append(f"[{index}]")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def split_numbered(raw: str, expected: int) -> List[str]:
    """Разобрать нумерованный ответ. Не по форме — пустой список (фолбэк)."""
    items: Dict[int, str] = {}
    for line in (raw or "").splitlines():
        match = _NUMBERED_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if 1 <= number <= expected and number not in items:
            items[number] = match.group(2)
    if len(items) != expected:
        return []
    return [items[i] for i in range(1, expected + 1)]


def summarize(posts: Sequence[Dict[str, Any]], *, region: str = "") -> List[str]:
    """Пересказы пунктов от текущего движка. Пустой список — фолбэк."""
    if not posts:
        return []
    from modules.publisher import rewriter

    try:
        result = rewriter._chat_call(
            system=SYSTEM_PROMPT,
            user=build_items_prompt(posts, region=region),
            temperature=0.3,
            model=rewriter.get_model() or None,
            label="daily_digest",
        )
    except Exception:  # noqa: BLE001 — сборник не стоит из-за движка
        logger.warning("daily digest: вызов движка упал", exc_info=True)
        return []

    if not result.get("ok"):
        logger.info(
            "daily digest: движок отказал (%s) — беру первые предложения источников",
            result.get("reason"),
        )
        return []

    items = split_numbered(str(result.get("content") or ""), len(posts))
    cleaned = [clean_item(item) for item in items]
    if len(cleaned) != len(posts) or any(len(item) < 40 for item in cleaned):
        logger.info("daily digest: ответ не по форме — беру первые предложения источников")
        return []
    return cleaned


def _tags_line(theme_tags: Iterable[str], local_hashtag: str) -> str:
    tags: List[str] = []
    seen = set()
    for raw in list(theme_tags or []) + ([local_hashtag] if local_hashtag else []):
        value = str(raw or "").strip()
        if not value:
            continue
        tag = value if value.startswith("#") else f"#{value}"
        key = tag.lstrip("#").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return " ".join(tags[:5])


def compose_digest(
    items: Sequence[DigestItem],
    *,
    theme_tags: Iterable[str] = (),
    local_hashtag: str = "",
    header: str = HEADER,
) -> str:
    """Собрать текст поста: заголовок, пункты с источниками, теги."""
    blocks: List[str] = []
    head = (header or "").strip()
    if head:
        blocks.append(head)
    for item in items:
        rendered = item.render().strip()
        if rendered:
            blocks.append(rendered)
    tags = _tags_line(theme_tags, local_hashtag)
    if tags:
        blocks.append(tags)
    return "\n\n".join(blocks).strip()


async def build_digest_from_posts(
    posts: Sequence[Dict[str, Any]],
    *,
    names: Optional[Dict[str, str]] = None,
    theme_tags: Iterable[str] = (),
    local_hashtag: str = "",
    region_name: str = "",
    region_code: str = "",
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> Optional[DigestDraft]:
    """Сборник из уже отобранных постов. ``None`` — материала мало или не влез."""
    if len(posts) < MIN_ITEMS:
        logger.info(
            "daily digest: регион=%s кандидатов=%d (<%d) — сборник не выходит",
            region_code,
            len(posts),
            MIN_ITEMS,
        )
        return None

    summaries = summarize(posts, region=region_name)
    if len(summaries) != len(posts):
        summaries = [fallback_item(p["text"]) for p in posts]

    names = names or {}
    items: List[DigestItem] = []
    for post, summary in zip(posts, summaries):
        if not summary:
            continue
        items.append(
            DigestItem(
                text=summary,
                source_name=str(names.get(str(post["owner_id"])) or ""),
                lip=post["lip"],
            )
        )

    if len(items) < MIN_ITEMS:
        logger.info(
            "daily digest: регион=%s пригодных пунктов=%d — сборник не выходит",
            region_code,
            len(items),
        )
        return None

    text = ""
    while items:
        text = compose_digest(items, theme_tags=theme_tags, local_hashtag=local_hashtag)
        if len(text) <= max_total_chars or len(items) <= MIN_ITEMS:
            break
        items.pop()

    if len(text) > max_total_chars:
        logger.info(
            "daily digest: регион=%s текст %d знаков — не влез (потолок %d), сборник не выходит",
            region_code,
            len(text),
            max_total_chars,
        )
        return None

    return DigestDraft(text=text, lips=[item.lip for item in items], items=items)


async def build_digest(
    session: Any,
    region_code: str,
    *,
    names: Optional[Dict[str, str]] = None,
    theme_tags: Iterable[str] = (),
    local_hashtag: str = "",
    region_name: str = "",
    since: Optional[datetime] = None,
    limit: int = DEFAULT_ITEMS,
    exclude_lips: Iterable[str] = (),
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> Optional[DigestDraft]:
    """Собрать сборник для района: отбор кандидатов и сборка текста."""
    posts = await collect_candidates(
        session, region_code, since=since, limit=limit, exclude_lips=exclude_lips
    )
    return await build_digest_from_posts(
        posts,
        names=names,
        theme_tags=theme_tags,
        local_hashtag=local_hashtag,
        region_name=region_name,
        region_code=region_code,
        max_total_chars=max_total_chars,
    )
