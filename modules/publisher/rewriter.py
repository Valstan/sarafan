"""Пересборка текста новости своими словами перед публикацией.

Заказ владельца 20.10: движок фильтрует поток и **формирует посты для
постинга** — то есть лента публикует не копипасту чужого текста с атрибуцией,
а наш пересказ. Форма поста не меняется (одна новость — один пост, P168),
меняется только текст: пересказ 300–1400 знаков, атрибуция-ссылка на источник
и хэштеги остаются на месте (их добавляет ``BulletinBuilder``).

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

ЖЁСТКИЕ ПРАВИЛА:
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
- Верни ТОЛЬКО текст поста: без пояснений, без заголовка «Пост:», без кавычек
  вокруг всего текста.
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
_HASHTAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")
_MENTION_RE = re.compile(r"(?:^|\s)@[A-Za-z0-9_.]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)")
_MD_EMPH_RE = re.compile(r"(\*\*|__|\*|_|`)")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@dataclass
class RewriteStats:
    """Итог пересборки волны — для лога, тестов и журнала."""

    enabled: bool = True
    skipped_short: int = 0
    attempted: int = 0
    rewritten: int = 0
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


def clean_rewrite(raw: str) -> str:
    """Привести ответ модели к виду «просто текст поста».

    Модели регулярно добавляют markdown, метку «Пост:», хэштеги источника или
    кавычки вокруг всего текста. Всё это — шум вёрстки, а не содержание.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

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

    cleaned = clean_rewrite(str(result.get("content") or ""))
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

    post["text_original"] = source
    post["text"] = usable
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
