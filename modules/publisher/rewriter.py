"""Пересборка текста новости своими словами перед публикацией.

Заказ владельца 20.10: движок фильтрует поток и **формирует посты для
постинга** — то есть лента публикует не копипасту чужого текста с атрибуцией,
а наш пересказ. Форма поста остаётся «одна новость — один пост» (P168), но
текст теперь состоит из трёх частей:

- **заголовок** — одна строка, собранная моделью по контексту новости
  (завлекающая, но честная: без выдуманных фактов);
- **пересказ** 300–1400 знаков;
- **подпись источника** и хэштеги — их добавляет ``BulletinBuilder``
  (ссылки на источник владелец убрал 20.10, подробности — ``utils.post_utils``).

Заголовок и тег — необязательная надстройка: модель вправе их не дать, тогда
пост выходит как раньше. Это осознанный fail-open — украшение не должно быть
ценой испорченного пересказа.

Границы решения:

- Переписываются только посты, которые **уходят в публикацию** этой волны:
  вершина порядка публикации (срочное → свежее → рейтинг) плюс хедлайнер.
  Переписывать всю волну (десятки постов) значило бы платить за текст, который
  никто не прочитает: в ленту выходит ``max_posts_per_bulletin`` постов.
- Траурные посты **не переписываются**: некролог — не тот текст, где уместен
  пересказ, и ошибка тона там дороже выгоды.
- Любой отказ (движок молчит, ответ не по форме, пустой текст) — **fail-open**:
  публикуется исходный текст источника. Волна не останавливается никогда.

Стоимость и наблюдаемость: один вызов модели на пост, в лог — строка
``post rewrite: переписано=N отказов=M токенов=K``. Ветка P205.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config.opencode import get_publish_timeout
from config.post_rewrite import (
    get_max_chars,
    get_max_posts,
    get_min_chars,
    get_min_source_chars,
    get_model,
    rewrite_enabled,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — редактор районной новостной ленты ВКонтакте. Тебе дают текст
новости из сообщества района. Перепиши его своими словами так, чтобы пост можно
было опубликовать в нашей ленте.

ФОРМАТ ОТВЕТА — ровно три части, каждая начинается с новой строки:
ЗАГОЛОВОК: <одна строка>
ТЕГ: <одно слово>
ТЕКСТ:
<пересказ>

ПРАВИЛА ЗАГОЛОВКА:
- Одна строка до 80 знаков, без точки в конце, без кавычек и эмодзи.
- Цепляет самое интересное в новости (что произошло и где), но не врёт: нельзя
  добавлять факты, оценки и обещания, которых нет в источнике.
- Не «шок», не «сенсация», не вопрос к читателю, не кликбейт-обман.
- Не повторяй название района, если оно уже в тексте: заголовок про событие.

ПРАВИЛА ТЕГА:
- Одно слово по сути новости, маленькими буквами, без #: субботник, ремонт_дорог,
  паводок, выпускной, ярмарка.
- Не пиши общие слова: новости, культура, спорт, район, малмыж.

ЖЁСТКИЕ ПРАВИЛА ТЕКСТА:
- Сохрани ВСЕ факты, имена, названия, числа, даты, адреса и телефоны ровно как в
  источнике. Ничего не добавляй и не додумывай: чего нет в тексте, того нет и в
  пересказе.
- Пиши нейтрально, как новостная лента: без оценок, без призывов, без вопросов
  к читателю, без личных обращений.
- 600–1200 знаков, 2–4 абзаца, обычный текст.
- Не упоминай источник и сам факт пересказа: никаких «в сообществе пишут»,
  «как сообщается», «по информации».
- Не оставляй хэштеги, ссылки, упоминания через @, markdown и эмодзи.
- Если фактов мало — пиши короче, но не добивай текст общими словами.
- Не повторяй уже сказанное: каждый абзац добавляет новую информацию.
- Не добавляй обобщающий абзац в конце («таким образом», «в целом», «итак»).
- Пересказ не должен быть длиннее источника: меньше фактов — короче текст.
- Верни ТОЛЬКО три части выше: без пояснений и без кавычек вокруг ответа.
"""

USER_TEMPLATE = """Район: {region}
Тема: {theme}

Текст источника:
{source}
"""
# Метки, которыми модель иногда предваряет ответ, и markdown-обёртка.
_LABEL_RE = re.compile(
    r"^\s*(?:готовый\s+текст|текст\s+поста|текст|пост|пересказ|новость)\s*:\s*",
    re.IGNORECASE,
)
_HEADLINE_LINE_RE = re.compile(r"^\s*(?:заголовок|headline)\s*[:：]\s*(.+)$", re.IGNORECASE)
_TAG_LINE_RE = re.compile(r"^\s*(?:тег|хэштег|tag|hashtag)\s*[:：]\s*(.+)$", re.IGNORECASE)
_BODY_MARK_RE = re.compile(r"^\s*(?:текст|text)\s*[:：]\s*$", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")
_MENTION_RE = re.compile(r"(?:^|\s)@[A-Za-z0-9_.]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")
_MD_EMPH_RE = re.compile(r"(\*\*|__|\*|_|`)")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
# Служебные теги, которые модель иногда предлагает вместо сути новости.
_GENERIC_TAGS = frozenset(
    {
        "новости",
        "новость",
        "культура",
        "спорт",
        "район",
        "малмыж",
        "киров",
        "область",
        "событие",
        "жизнь",
        "общество",
    }
)
# Потолок заголовка: длиннее — уже не заголовок, а первый абзац.
MAX_HEADLINE_CHARS = 90
MIN_HEADLINE_CHARS = 12


@dataclass
class RewriteStats:
    """Итог пересборки волны — для лога, тестов и журнала."""

    enabled: bool = True
    skipped_short: int = 0
    attempted: int = 0
    rewritten: int = 0
    headlines: int = 0
    failed: int = 0
    tokens: int = 0
    reasons: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "skipped_short": self.skipped_short,
            "attempted": self.attempted,
            "rewritten": self.rewritten,
            "headlines": self.headlines,
            "failed": self.failed,
            "tokens": self.tokens,
            "reasons": list(self.reasons),
            "models": list(self.models),
        }


def _chat_call(**kwargs: Any) -> Dict[str, Any]:
    """Вызов модели текущим движком.

    Рубеж переключения один на все пути — ``modules.classifier.headless.chat``
    (``CLASSIFIER_ENGINE``). Вторая копия решения разошлась бы с первой молча:
    включили бы движок для классификатора и забыли для пересказа.
    """
    from modules.classifier.headless import chat

    return chat(**kwargs)


def build_prompt(source: str, *, region: str = "", theme: str = "") -> str:
    """Пользовательская часть промпта (системная уходит отдельным аргументом)."""
    return USER_TEMPLATE.format(
        region=(region or "район").strip(),
        theme=(theme or "новости").strip(),
        source=source.strip(),
    )


def _dedupe_paragraphs(text: str) -> str:
    """Выкинуть абзацы, повторяющие уже сказанное.

    Живой прогон 20.10: на коротком источнике модель дописывала третий абзац,
    который пересказывал первый теми же словами. Читателю это видно как
    «текст кончился, а его перелили ещё раз» — поэтому повтор режем здесь, а не
    надеемся на промпт.
    """
    seen = set()
    kept: List[str] = []
    for block in text.split("\n\n"):
        norm = re.sub(r"\W+", " ", block.lower()).strip()
        if not norm:
            continue
        # Ключ — начало абзаца: пересказ того же абзаца почти всегда совпадает
        # в первых словах, а хвост может быть переставлен.
        key = norm[:80]
        if key in seen:
            continue
        seen.add(key)
        kept.append(block.strip())
    return "\n\n".join(kept)


def _strip_fence(text: str) -> str:
    """Снять ```-обёртку, которой модели иногда оборачивают весь ответ."""
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines:
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def split_reply(raw: str) -> tuple[str, str, str]:
    """Ответ модели → ``(заголовок, тег, текст)``.

    Контракт ответа — три части с метками (см. ``SYSTEM_PROMPT``). Модель
    вправе их не дать: тогда заголовок и тег пустые, а весь ответ — текст, то
    есть пост выходит как раньше. Это осознанный fail-open: заголовок — украшение,
    которого не должно быть ценой испорченного пересказа.
    """
    text = _strip_fence(raw)
    lines = text.splitlines()
    headline = ""
    tag = ""
    start = 0
    while start < len(lines):
        line = lines[start]
        if not headline:
            match = _HEADLINE_LINE_RE.match(line)
            if match:
                headline = match.group(1)
                start += 1
                continue
        if not tag:
            match = _TAG_LINE_RE.match(line)
            if match:
                tag = match.group(1)
                start += 1
                continue
        if _BODY_MARK_RE.match(line):
            start += 1
        break
    return headline, tag, "\n".join(lines[start:])


def clean_headline(raw: str) -> str:
    """Привести строку заголовка к виду «одна строка без разметки».

    Пустая строка — штатный ответ: заголовка не будет. Обрезка идёт по границе
    слова, а не по символу: обрубок вида «Глава района открыл шко» владелец
    назвал главной придиркой к прежним постам (20.10).
    """
    text = (raw or "").strip()
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _HASHTAG_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    text = _MD_EMPH_RE.sub("", text)
    text = re.sub(r"^[\s«\"“”'‘’]+|[\s«\"“”'‘’]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    # Эмодзи и прочие символы вне букв/цифр/базовой пунктуации — не заголовок.
    text = re.sub(r"[^\w\s\-–—:,.!?()/%«»\"'№]", "", text).strip()
    if len(text) < MIN_HEADLINE_CHARS:
        return ""
    if len(text) > MAX_HEADLINE_CHARS:
        cut = text[:MAX_HEADLINE_CHARS]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(" ,;:—-")
    return text


def clean_topic_tag(raw: str) -> str:
    """Тег по сути новости → ``#слово`` либо пустая строка.

    Модель отвечает «одним словом», но регулярно приносит фразу, решётку или
    общее слово («новости»). Такой тег не нужен: он дублирует тематический и
    раздувает подпись поста.
    """
    value = (raw or "").strip().strip("«»\"'").lstrip("#").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", "_", value).lower()
    value = re.sub(r"[^0-9a-zа-яё_]", "", value).strip("_")
    if not (3 <= len(value) <= 25):
        return ""
    if value in _GENERIC_TAGS:
        return ""
    return f"#{value}"


def clean_rewrite(raw: str) -> str:
    """Привести ответ модели к виду «просто текст поста».

    Модели регулярно добавляют markdown, метку «Пост:», хэштеги источника или
    кавычки вокруг всего текста. Всё это — шум вёрстки, а не содержание.
    """
    text = _strip_fence(raw)

    text = _LABEL_RE.sub("", text, count=1).strip()

    # Кавычки вокруг всего текста («…», "…", '…') — обёртка, не часть новости.
    for left, right in (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'")):
        if len(text) > 2 and text.startswith(left) and text.endswith(right):
            text = text[1:-1].strip()
            break

    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_EMPH_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return _dedupe_paragraphs(text)


def _usable(source: str, result: str, *, min_chars: int, max_chars: int) -> Optional[str]:
    """Годен ли пересказ. ``None`` — нет, публикуем исходник (fail-open).

    Потолок считается и от источника: пересказ обязан быть **не длиннее**
    источника (с запасом 35 %). Иначе модель добивает короткий источник
    общими словами — тот же живой прогон 20.10 показал 471 знак исходника
    и 638 знаков пересказа без единого нового факта.
    """
    if not result:
        return None
    if len(result) < min_chars:
        return None
    if len(result) > max_chars:
        return None
    source_cap = max(int(len(source.strip()) * 1.35), min_chars)
    if len(result) > source_cap:
        return None
    if result.strip() == source.strip():
        return None
    return result


def _looks_truncated(text: str) -> bool:
    """Текст кончается на полуслове? Признак — последний знак без завершения.

    Проверяем только длинные ответы: у короткой реплики «без точки» это норма
    (заголовок, подпись), а пересказ новости — проза, и наша форма ответа
    требует законченных предложений. Цена ложного срабатывания мала —
    публикуем исходный текст; цена пропуска велика — оборванная фраза в ленте.
    """
    stripped = (text or "").strip()
    if len(stripped) < 200:
        return False
    last = stripped[-1]
    return last.isalpha() or last.isdigit()


def rewrite_post(
    post: Dict[str, Any],
    *,
    region: str = "",
    theme: str = "",
    stats: Optional[RewriteStats] = None,
) -> bool:
    """Переписать текст одного поста на месте. Возвращает True при успехе."""
    stats = stats if stats is not None else RewriteStats()
    source = str(post.get("text") or "")
    stats.attempted += 1

    result = _chat_call(
        system=SYSTEM_PROMPT,
        user=build_prompt(source, region=region, theme=theme),
        temperature=0.3,
        model=get_model() or None,
        timeout=get_publish_timeout(),
        label="post_rewrite",
    )
    usage = result.get("usage") or {}
    if isinstance(usage, dict):
        try:
            stats.tokens += int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
    model = str(result.get("model") or "")
    if model and model not in stats.models:
        stats.models.append(model)

    if not result.get("ok"):
        stats.failed += 1
        stats.reasons.append(str(result.get("reason") or "engine_failed"))
        return False

    headline_raw, tag_raw, body_raw = split_reply(str(result.get("content") or ""))
    cleaned = clean_rewrite(body_raw)

    if str(result.get("finish") or "").lower() == "length" or _looks_truncated(cleaned):
        # Ответ оборван — лимитом токенов модели или её собственным обрывом.
        # Публиковать половину предложения хуже, чем исходный текст: живой
        # прогон 21.10 дал «…место завалено грудо» (775 знаков, ровно обрыв),
        # и по тексту это неотличимо от нормального ответа. Признак ``finish
        # == length`` приходит от модели, эвристика — на случай, когда
        # провайдер причину не сообщил.
        stats.failed += 1
        stats.reasons.append("truncated")
        return False

    usable = _usable(
        source,
        cleaned,
        min_chars=get_min_chars(),
        max_chars=get_max_chars(),
    )
    if usable is None:
        stats.failed += 1
        stats.reasons.append(f"shape:{len(cleaned)}")
        return False

    headline = clean_headline(headline_raw)
    topic_tag = clean_topic_tag(tag_raw)

    post["text_original"] = source
    post["text"] = usable
    # Заголовок и тег — необязательная надстройка: пустая строка значит «как
    # раньше». Они не участвуют в проверке длины пересказа: заголовок это
    # отдельная строка поста, а не часть пересказа.
    if headline:
        post["headline"] = headline
        stats.headlines += 1
    if topic_tag:
        post["topic_tag"] = topic_tag
    stats.rewritten += 1
    return True


def rewrite_posts(
    posts: Sequence[Dict[str, Any]],
    *,
    region: str = "",
    theme: str = "",
    limit: Optional[int] = None,
) -> RewriteStats:
    """Переписать верхушку порядка публикации (вызывающий передаёт её готовой).

    ``posts`` — уже упорядоченный список кандидатов на публикацию: срочное
    вперёд, дальше свежее и рейтинг (тот же порядок, что у ``BulletinBuilder``),
    при необходимости с хедлайнером первым. Берём не больше ``limit`` постов —
    остальные в эту волну не публикуются, платить за их текст незачем.

    Никогда не бросает: любой сбой — исходный текст и запись в лог.
    """
    stats = RewriteStats(enabled=rewrite_enabled())
    if not posts:
        return stats
    if not stats.enabled:
        return stats

    min_source = get_min_source_chars()
    max_posts = limit if limit is not None else get_max_posts()
    max_posts = max(0, int(max_posts))

    for post in list(posts)[:max_posts]:
        if not isinstance(post, dict):
            continue
        source = str(post.get("text") or "")
        if len(source.strip()) < min_source:
            # Короткое объявление пересказывать нечего: пересказ длиннее
            # источника только добавит воды.
            stats.skipped_short += 1
            continue
        try:
            rewrite_post(post, region=region, theme=theme, stats=stats)
        except Exception as e:  # noqa: BLE001 — fail-open, волна не стоит
            stats.failed += 1
            stats.reasons.append(f"error:{e.__class__.__name__}")
            logger.warning("post rewrite: сбой на посте — оставляю исходник", exc_info=True)

    if stats.attempted or stats.skipped_short:
        logger.info(
            "post rewrite: регион=%s тема=%s переписано=%d отказов=%d" " коротких=%d токенов=%d%s",
            region or "?",
            theme or "?",
            stats.rewritten,
            stats.failed,
            stats.skipped_short,
            stats.tokens,
            f" (причины: {', '.join(stats.reasons[:3])})" if stats.reasons else "",
        )
    return stats
