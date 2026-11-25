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

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config.post_rewrite import (
    CAPACITY_REASONS,
    get_max_chars,
    get_max_posts,
    get_min_chars,
    get_min_source_chars,
    get_model,
    get_rewrite_engine,
    get_rewrite_paid_fallback,
    get_rewrite_timeout,
    get_rewrite_timeout_for_engine,
    paid_topup_enabled,
    rewrite_enabled,
)

logger = logging.getLogger(__name__)

#: Движок страховки: им добираем пост, блок которого не пришёл (см. ADR-0017).
#: Платный по решению владельца 17.11: «Дипсик вернуть на подстраховку, если
#: Минимакс проморгал или не смог — то отдавать пропущенное Дипсику».
PAID_TOPUP_ENGINE = "deepseek"

#: Сколько знаков сырого ответа показываем в логе, когда блок не пришёл.
#: Нужен для разбора формата: по логу видно, чем ответ отличается от ожидаемого.
_PREVIEW_CHARS = 1200

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

ПРИ НЕСКОЛЬКИХ ТЕКСТАХ СРАЗУ:
- Каждый исходный текст отмечен строкой «=== ПОСТ N ===» (N — его номер).
- Ответь столько же блоков, сколько текстов, и каждый блок начинай с той же
  метки «=== ПОСТ N ===» — с тем же N, что у исходного текста.
- Внутри блока — те же три части: ЗАГОЛОВОК, ТЕГ, ТЕКСТ.
- Не пропускай блоки и не смешивай: тексты разные, и пересказ у каждого свой.
"""

USER_TEMPLATE = """Район: {region}
Тема: {theme}

Текст источника:
{source}
"""

#: Метка поста в батче: по ней ответ разбирается на блоки, а не по порядку.
#: Номер в метке — это номер исходного текста, поэтому «перепутать» блоки
#: нельзя: блок без метки просто не применится (fail-open).
BATCH_MARKER = "=== ПОСТ {n} ==="

#: Метка в ответе модели. Разбор терпим к вариантам оформления: ``=== ПОСТ 1 ===``,
#: ``--- ПОСТ 1 ---``, ``ПОСТ 1:`` — важно лишь, чтобы читался номер.
_BATCH_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-=*#]{0,4}[ \t]*)?(?:ПОСТ|POST)[ \t]*№?[ \t]*(\d+)[ \t]*[:：]?[ \t]*"
    r"(?:[-=*#]{0,4}[ \t]*)?$",
    re.IGNORECASE | re.MULTILINE,
)
#: Имена полей в JSON-ответе агента (живой факт 18.11): номер поста, заголовок,
#: тег, текст. Порядок в списках — предпочтение: «post» важнее «id».
_JSON_NUMBER_KEYS = ("post", "пост", "номер", "number", "n", "index", "idx", "id")
_JSON_HEADLINE_KEYS = ("заголовок", "headline", "title")
_JSON_TAG_KEYS = ("тег", "tag")
_JSON_TEXT_KEYS = ("текст", "text", "body", "пересказ", "content")
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

    ⚠️ Для пересказа движок **свой** (``REWRITE_ENGINE``): пока переключатель
    был один, переписывание ушло в MiniMax не по решению, а потому что так
    стоял общий флаг, и забрало 85 % вызовов MiniMax (замер 16.11).

    **Платная страховка — по режиму ``REWRITE_PAID_FALLBACK``** (дефолт
    ``auto``): на отказ по ёмкости бокса (``insufficient_memory``,
    ``cli_slot_busy``, ``oom_killed``, ``timeout``) платный Дипсик **не
    зовётся** — переписывание косметика, и пост и так уйдёт с исходным
    текстом (fail-open). Решение владельца 25.11: «когда слота не хватает,
    первым должен уступать переписывание, а не классификация». Остальные отказы
    (движок сломан, очки кончились) платной страховкой закрываются, как
    решил владелец 17.11.
    """
    from modules.classifier.headless import chat

    kwargs.setdefault("engine", get_rewrite_engine())
    mode = get_rewrite_paid_fallback()
    if mode == "never":
        kwargs.setdefault("paid_fallback", False)
    elif mode == "auto":
        kwargs.setdefault("skip_paid_reasons", CAPACITY_REASONS)
    return chat(**kwargs)


def build_batch_prompt(sources: Sequence[str], *, region: str = "", theme: str = "") -> str:
    """Пользовательская часть промпта для нескольких постов сразу.

    Каждый текст отмечен своей меткой — по ней же разбирается ответ.
    """
    head = "Район: %s\nТема: %s\n\n" % ((region or "район").strip(), (theme or "новости").strip())
    blocks = [
        "%s\n%s" % (BATCH_MARKER.format(n=idx), str(source or "").strip())
        for idx, source in enumerate(sources, start=1)
    ]
    return head + "\n\n".join(blocks)


def split_batch_reply(raw: str, count: int) -> Dict[int, str]:
    """Ответ на батч → блоки по номерам постов.

    Разбор **по номеру в метке**, а не по порядку: иначе один лишний или
    пропущенный блок сдвинул бы все пересказы и тексты разъехались бы по
    постам. Блок без метки не применяется — пост уходит с исходным текстом.

    Ответ вовсе без меток — это ответ на **один** пост (модель проигнорировала
    формат). Одиночному посту он и достаётся; в батче из нескольких постов
    ответ без меток **не применяется ни к кому**: иначе пересказ одного поста
    мог бы уйти в другой пост, а это подмена чужого текста в ленте.
    """
    text = _strip_fence(raw or "")
    marks = list(_BATCH_MARKER_RE.finditer(text))
    if not marks:
        # Агент MiniMax на «грязном» входе вместо меток отдаёт JSON-массив
        # (живой факт 18.11: проба на постах с упоминаниями и ссылкой вернула
        # ``[{"post": 1, "ЗАГОЛОВОК": …, "ТЕГ": …, "ТЕКСТ": …}, …]``). Номер
        # поста в JSON указан явно, поэтому разбор однозначен — в отличие от
        # ответа вообще без меток, который применять нельзя.
        blocks = _split_json_reply(text, count)
        if blocks:
            return blocks
        if text.lstrip().startswith(("{", "[")):
            # Ответ — JSON, и разобрать его не вышло. Публиковать сырой JSON
            # в ленте хуже, чем не переписать пост вовсе: fail-open.
            return {}
        if count == 1:
            return {1: text}
        return {}
    blocks: Dict[int, str] = {}
    for idx, mark in enumerate(marks):
        start = mark.end()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        try:
            number = int(mark.group(1))
        except (TypeError, ValueError):
            continue
        if 1 <= number <= count:
            blocks[number] = text[start:end]
    return blocks


def _json_text(item: Dict[str, Any], keys: Sequence[str]) -> str:
    """Значение из JSON-объекта по списку допустимых имён поля."""
    lowered = {str(key).strip().lower(): value for key, value in item.items()}
    for key in keys:
        if key not in lowered:
            continue
        value = lowered[key]
        if isinstance(value, (list, tuple)):
            value = "\n\n".join(str(part).strip() for part in value if str(part).strip())
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _split_json_reply(text: str, count: int) -> Dict[int, str]:
    """JSON-массив с ответами → блоки по номерам постов.

    **Зачем.** Живой факт 18.11: агент MiniMax на постах с упоминаниями и
    ссылками вместо меток ``=== ПОСТ N ===`` отдаёт JSON-массив, где номер
    поста указан полем ``post``. Разбор меток его не видел — и **оба** поста
    батча уходили с исходным текстом (отказ ``no_block``; на Дипсике такого
    не было ни разу за 129 вызовов 17.11).

    **Безопасность.** Применять ответ можно только когда номер поста указан
    явно и однозначно: номер вне диапазона, повтор номера или отсутствие
    текста — тогда блок не применяется (fail-open, исходный текст). Порядок
    элементов значения не имеет: пересказ попадает в тот пост, который назван.

    Возвращает пустой словарь, если разбирать нечего.
    """
    raw = (text or "").strip()
    if not raw:
        return {}

    payload = ""
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        payload = raw[start : end + 1]
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            payload = raw[start : end + 1]
    if not payload:
        return {}

    try:
        data = json.loads(payload)
    except ValueError:
        return {}

    items = data if isinstance(data, list) else [data]
    blocks: Dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        lowered = {str(key).strip().lower(): value for key, value in item.items()}
        number = None
        for key in _JSON_NUMBER_KEYS:
            if key not in lowered:
                continue
            try:
                number = int(lowered[key])
            except (TypeError, ValueError):
                continue
            break
        if number is None or not (1 <= number <= count):
            continue
        if number in blocks:
            # Номер повторился — какой блок чей, по JSON не понять.
            return {}
        body = _json_text(item, _JSON_TEXT_KEYS)
        if not body:
            continue
        lines = []
        headline = _json_text(item, _JSON_HEADLINE_KEYS)
        if headline:
            lines.append("ЗАГОЛОВОК: %s" % headline)
        tag = _json_text(item, _JSON_TAG_KEYS)
        if tag:
            lines.append("ТЕГ: %s" % tag)
        lines.append("ТЕКСТ:")
        lines.append(body)
        blocks[number] = "\n".join(lines)
    return blocks


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


def _apply_rewrite(
    post: Dict[str, Any],
    answer: str,
    *,
    stats: RewriteStats,
    finish: str = "",
) -> bool:
    """Применить пересказ к посту. ``False`` — пост уходит с исходным текстом.

    Общая часть для одиночного вызова и для батча: правила проверки одни и те же
    (обрыв, длина, форма) — различается только способ добычи ответа.
    """
    source = str(post.get("text") or "")
    headline_raw, tag_raw, body_raw = split_reply(answer)
    cleaned = clean_rewrite(body_raw)

    if str(finish or "").lower() == "length" or _looks_truncated(cleaned):
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


def _note_call(result: Dict[str, Any], stats: RewriteStats) -> None:
    """Учесть расход и модель одного вызова."""
    usage = result.get("usage") or {}
    if isinstance(usage, dict):
        try:
            stats.tokens += int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
    model = str(result.get("model") or "")
    if model and model not in stats.models:
        stats.models.append(model)


def rewrite_batch(
    posts: Sequence[Dict[str, Any]],
    *,
    region: str = "",
    theme: str = "",
    stats: Optional[RewriteStats] = None,
) -> int:
    """Переписать несколько постов **одним** вызовом. Возвращает число успешных.

    Зачем батч: вызов — недешёвая операция (у MiniMax это запуск агента-CLI:
    ~16 000 токенов и десятки секунд), а переписываемых постов в волну — до
    двух. Один вызов на два поста вместо двух вызовов — вдвое меньше расхода
    пула и вдвое меньше времени в единственном слоте воркера.

    Разбор ответа — **по номеру поста в метке** (``=== ПОСТ N ===``), а не по
    порядку: один пропущенный блок не должен сдвинуть остальные пересказы.
    Пост без блока уходит с исходным текстом (fail-open).
    """
    stats = stats if stats is not None else RewriteStats()
    items = [p for p in posts if isinstance(p, dict)]
    if not items:
        return 0
    stats.attempted += len(items)

    sources = [str(p.get("text") or "") for p in items]
    result = _chat_call(
        system=SYSTEM_PROMPT,
        user=build_batch_prompt(sources, region=region, theme=theme),
        temperature=0.3,
        model=get_model() or None,
        timeout=get_rewrite_timeout(),
        label="post_rewrite",
        posts=len(items),
    )
    _note_call(result, stats)
    if not result.get("ok"):
        reason = str(result.get("reason") or "engine_failed")
        stats.failed += len(items)
        stats.reasons.append(reason)
        return 0

    blocks = split_batch_reply(str(result.get("content") or ""), len(items))
    finish = str(result.get("finish") or "")
    done = 0
    missing: List[int] = []
    for idx, post in enumerate(items, start=1):
        block = blocks.get(idx)
        if not block:
            # Блок не пришёл (модель пропустила или перепутала номер) — пост
            # уходит с исходным текстом, остальные пересказы не страдают.
            missing.append(idx)
            continue
        if _apply_rewrite(post, block, stats=stats, finish=finish):
            done += 1

    if missing:
        logger.warning(
            "post rewrite: блоков не хватило (%d из %d) — добираем платным Дипсиком; "
            "сырой ответ: %s",
            len(items) - len(missing),
            len(items),
            _preview(str(result.get("content") or "")),
        )
        done += _recover_missing(
            items,
            missing,
            region=region,
            theme=theme,
            stats=stats,
        )
    return done


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    """Кусок сырого ответа движка для разбора формата (обрезан по длине)."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _recover_missing(
    items: Sequence[Dict[str, Any]],
    missing: Sequence[int],
    *,
    region: str = "",
    theme: str = "",
    stats: RewriteStats,
) -> int:
    """Добить посты, чьи блоки не пришли, платным Дипсиком.

    **Зачем.** Живой факт 18.11: после перевода переписывания на MiniMax в
    журнале появился отказ ``no_block`` — модель ответила, но метки
    ``=== ПОСТ N ===`` в ответе не оказалось, и оба поста батча ушли с
    исходным текстом. На Дипсике (замер 17.11: 129 вызовов) такого не было ни
    разу: метки он соблюдает. Это тот же случай, что и «движок проморгал» —
    пересказа нет, — а решение владельца 17.11 прямо называет страховкой
    Дипсика: «если Минимакс проморгал или не смог — то отдавать пропущенное
    Дипсику».

    **Как.** Два шага, оба платные и оба дешёвые (Дипсик отвечает за секунды):

    1. повторный вызов **того же батча** платным движком — метки он держит,
       поэтому обычно хватает одного вызова;
    2. оставшиеся посты — по одному: ответ на один пост и есть его блок.

    Шаг 2 не трогает посты, чей блок пришёл, но не прошёл проверку формы
    (короткий/длинный/оборванный текст): там дело в содержании, а второй вызов
    того же текста ничего не изменит.

    Не повторяем бесконечно: шаг 1 — один вызов, шаг 2 — по одному вызову на
    пост. Не помогло — fail-open, пост уходит с исходным текстом.
    """
    if not missing:
        return 0

    timeout = get_rewrite_timeout_for_engine(PAID_TOPUP_ENGINE)
    model = get_model() or None
    recovered = 0
    recovered_idx = set()
    form_failed_idx = set()
    pending = list(missing)

    if not paid_topup_enabled():
        logger.warning(
            "post rewrite: платная страховка выключена — %d постов уйдут с исходным текстом",
            len(missing),
        )
        _count_missing_failures(missing, recovered_idx, form_failed_idx, stats)
        return 0

    # Шаг 1: батч заново платным движком.
    if len(pending) > 1:
        sources = [str(items[idx - 1].get("text") or "") for idx in pending]
        result = _chat_call(
            system=SYSTEM_PROMPT,
            user=build_batch_prompt(sources, region=region, theme=theme),
            temperature=0.3,
            model=model,
            timeout=timeout,
            engine=PAID_TOPUP_ENGINE,
            label="post_rewrite",
            posts=len(pending),
        )
        _note_call(result, stats)
        if result.get("ok"):
            blocks = split_batch_reply(str(result.get("content") or ""), len(pending))
            finish = str(result.get("finish") or "")
            still: List[int] = []
            for pos, idx in enumerate(pending, start=1):
                block = blocks.get(pos)
                if not block:
                    still.append(idx)
                    continue
                if _apply_rewrite(items[idx - 1], block, stats=stats, finish=finish):
                    recovered += 1
                    recovered_idx.add(idx)
                else:
                    # Блок пришёл, но не прошёл проверку формы: отказ уже учтён
                    # в _apply_rewrite, второй вызов того же текста не поможет.
                    form_failed_idx.add(idx)
            pending = still

    # Шаг 2: остаток — по одному посту (ответ на один пост и есть его блок).
    for idx in pending:
        source = str(items[idx - 1].get("text") or "")
        result = _chat_call(
            system=SYSTEM_PROMPT,
            user=build_prompt(source, region=region, theme=theme),
            temperature=0.3,
            model=model,
            timeout=timeout,
            engine=PAID_TOPUP_ENGINE,
            label="post_rewrite",
            posts=1,
        )
        _note_call(result, stats)
        if not result.get("ok"):
            continue
        if _apply_rewrite(
            items[idx - 1],
            str(result.get("content") or ""),
            stats=stats,
            finish=str(result.get("finish") or ""),
        ):
            recovered += 1
            recovered_idx.add(idx)

    _count_missing_failures(missing, recovered_idx, form_failed_idx, stats)
    logger.warning(
        "post rewrite: страховка вернула %d из %d постов",
        recovered,
        len(missing),
    )
    return recovered


def _count_missing_failures(
    missing: Sequence[int],
    recovered_idx: set,
    form_failed_idx: set,
    stats: RewriteStats,
) -> None:
    """Учесть отказ ``no_block`` для постов, которых страховка не спасла.

    Пост, чей блок пришёл, но не прошёл проверку формы, отказ уже учтён в
    ``_apply_rewrite`` (причина ``shape``/``truncated``) — второй раз его не
    считаем, иначе в логе будет один отказ на два.
    """
    for idx in missing:
        if idx in recovered_idx or idx in form_failed_idx:
            continue
        stats.failed += 1
        stats.reasons.append("no_block")


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
        timeout=get_rewrite_timeout(),
        label="post_rewrite",
        posts=1,
    )
    _note_call(result, stats)

    if not result.get("ok"):
        stats.failed += 1
        stats.reasons.append(str(result.get("reason") or "engine_failed"))
        return False

    return _apply_rewrite(post, str(result.get("content") or ""), stats=stats)


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

    eligible: List[Dict[str, Any]] = []
    for post in list(posts)[:max_posts]:
        if not isinstance(post, dict):
            continue
        source = str(post.get("text") or "")
        if len(source.strip()) < min_source:
            # Короткое объявление пересказывать нечего: пересказ длиннее
            # источника только добавит воды.
            stats.skipped_short += 1
            continue
        eligible.append(post)

    # Один вызов на всю порцию: вызов — самая дорогая операция на этом пути,
    # а постов в волну немного (потолок POST_REWRITE_MAX_POSTS=2). По одному
    # вызову на пост мы сжигали 85 % вызовов MiniMax (замер 16.11).
    try:
        rewrite_batch(eligible, region=region, theme=theme, stats=stats)
    except Exception as e:  # noqa: BLE001 — fail-open, волна не стоит
        stats.failed += len(eligible)
        stats.reasons.append(f"error:{e.__class__.__name__}")
        logger.warning("post rewrite: сбой на батче — оставляю исходники", exc_info=True)

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


#: Публичный API модуля. ``rewrite_post`` — сознательный одно-постовый вход:
#: батч-рефакторинг ``a2a3228`` перевёл волну на ``rewrite_posts``, но
#: одно-постовый сценарий (постовый режим переписывания, потолок
#: ``POST_REWRITE_MAX_POSTS=2``, ручные прогоны) остаётся рабочим и
#: объявленным, а не «забытым хвостом» (P019, прогон 19.11).
__all__ = [
    "RewriteStats",
    "build_batch_prompt",
    "build_prompt",
    "clean_headline",
    "clean_rewrite",
    "clean_topic_tag",
    "rewrite_batch",
    "rewrite_post",
    "rewrite_posts",
    "split_batch_reply",
    "split_reply",
]
