#!/usr/bin/env python3
"""Фактчек вечерней сводки «Короткой строкой»: пересказ против источника (P182).

    python scripts/digest_factcheck.py --days 3

Сборник — это пересказ новостей, сделанный моделью. Модель может **выдумать**
факт: имя, число, название. Промпт это запрещает
(`modules/publisher/daily_digest.py`, «Сохрани факты, имена, числа и названия
ровно как в источнике»), но запрет в промпте — не проверка. Этот скрипт
проверяет **механически**: каждый факт из пересказа должен встречаться в
тексте исходного поста.

Как устроена проверка
---------------------
1. Берём опубликованные сборники из `published_posts` (`kind='digest'`).
   У такой строки `lip` — это lip **исходного** поста, а `published_url` —
   ссылка на стену, где сборник вышел. То есть сопоставление «пункт ↔
   источник» хранится в базе, восстанавливать его не нужно.
2. Текст источника берём из `collected_post_audit.post_text` по этому lip.
3. Текст опубликованного сборника читаем из VK по `published_url`.
4. Каждый **пересказ** (часть пункта до строки «Источник:») проверяем против
   текста **его** источника. Если пересказов не удалось сопоставить с
   источниками один-в-один (формат поста менялся, пунктов больше/меньше) —
   проверяем весь сборник против объединения его источников и помечаем такую
   проверку как ослабленную.

Что считается фактом
--------------------
* **Числа** — все последовательности цифр (нормализованные: «1 200» = «1200»).
* **Имена и названия** — слова с заглавной буквы, кроме стоп-словаря
  распространённых зачинков предложения.

Проверка идёт по границам слов и без учёта регистра: «Иванов» из пересказа
должен найтись в источнике как отдельное слово. Подстрока не считается
(«Иван» ≠ «Иванов»).

Ограничение (честно записано, потому что метод смотрит в текст): проверка
ловит **факты, которых нет ни в одном источнике сборника**. Если модель
перепутала два числа между двумя разными постами сборника, этот скрипт такое
не поймает — нужна попочная сверка, для которой нужна привязка пункта к
посту по порядку (она есть не всегда). Дубли и выдуманные факты — ловятся.

Скрипт read-only: ничего не публикует, курсор дедупа не двигает, журнал
публикаций не пишет.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- ядро проверки (чистые функции, без БД и без VK) ------------------------

#: Частые зачины предложений и служебные слова: с большой буквы, но не факт.
#:
#: Список расширен 02.11 по живым данным с прода: 7 «неподтверждённых имён» из
#: 40 сборников оказались обычными словами с большой буквы («Тёплые», «Мёртвые»,
#: «Весёлый», «Днём») — модель так начинает предложение. Плюс падежные формы
#: прилагательных («Фёдоровской» из «Фёдоров»): они не имя, а форма слова.
_STOP = frozenset(
    """
это он она оно они этот эта эти тот та те этое там тогда после при для как
когда если чтобы где кто что не но а и в на за из по у к с о до без для про
над под между также еще ещё уже только очень сам сама само все всё вся всее
нет да или же бы был была было были будет будут может можно нужно
также несмотря однако потому поэтому кроме кромее вместо
тёплый тёплая тёплое тёплые тёпло тёплого тёплой тёплом тёплая
тёплый день тёплые дни тёплым
холодный холодная холодное холодные холодно холодного холодный день
мёртвый мёртвая мёртвое мёртвые мёртво мёртвого мёртвый
весёлый весёлая весёлое весёлые весело весёлого весёлый день
днём днём днем утром утром утро вечером вечером ночью ночью ночь
вчера сегодня завтра позавчера послезавтра сейчас тогда сразу скоро рядом
здесь тут там снова затем потом теперь также недавно давно рано поздно
вместе кроме против около после перед вместо чтобы почему зачем как
новый новая новое новые старый старая старое старые первый первая второе
другой другая другие другой весь вся все всее целый важный нужный новый
""".split()
)

_NUMBER_RE = re.compile(r"\d+")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_SOURCE_MARKER = "Источник:"

#: Ссылки и упоминания — не факты. Внутри ссылки стоят идентификаторы ВК
#: («?z=album-34480932312120662»), и они не подтверждаются текстом источника —
#: ложное срабатывание (поймано на проде 01.11: 1 из 300 пунктов).
_URL_RE = re.compile(r"(?:https?://|vk\.ru/|vk\.com/)\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?:^|\s)@[A-Za-z0-9_.]+")
_HASHTAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")

#: Число словами → цифрами. Пересказ имеет право написать «на сумму более семи
#: миллиардов» там, где источник написал «7 млрд» (и наоборот) — это не
#: выдумка. Без этой таблицы «7» против «семи» давало строгий вердикт там,
#: где его быть не должно (поймано на проде 01.11).
_WORD_NUMBERS = {
    "один": "1",
    "одна": "1",
    "одно": "1",
    "одного": "1",
    "одной": "1",
    "два": "2",
    "две": "2",
    "двух": "2",
    "двоих": "2",
    "три": "3",
    "трёх": "3",
    "трех": "3",
    "четыре": "4",
    "пять": "5",
    "пяти": "5",
    "шесть": "6",
    "семь": "7",
    "семь": "7",
    "семи": "7",
    "сём": "7",
    "восемь": "8",
    "восьми": "8",
    "девять": "9",
    "девяти": "9",
    "десять": "10",
    "десяти": "10",
    "сто": "100",
    "тысяча": "1000",
    "тысяч": "1000",
    "тысячи": "1000",
    "миллион": "1000000",
    "миллиона": "1000000",
    "миллионов": "1000000",
    "миллиард": "1000000000",
    "миллиарда": "1000000000",
    "миллиардов": "1000000000",
}

#: Пункт сборника устроен как «<пересказ>\n\nИсточник: <название>». Пересказ —
#: текст ДО маркера, название — текст ПОСЛЕ него (до пустой строки).
_ITEM_RE = re.compile(
    r"(?:^|\n\n)(?P<summary>.+?)\n\n" + re.escape(_SOURCE_MARKER) + r" (?P<source>[^\n]+)",
    re.DOTALL,
)

#: Число с разделителем тысяч: «1 200», «1 200». Первая группа — ровно три
#: цифры, дальше группы по три. Правило отличает «1 200» (одно число) от
#: «02 215» (два разных числа — так и было в пересказе про шины, где склейка
#: давала несуществующее «02215»).
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:[   ]\d{3})+")


def normalize_text(text: str) -> str:
    """Привести текст к сравнимому виду: нер��рывные пробелы, тире, ё/е."""
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = text.replace("\u0451", "\u0435")  # ё → е: «Почётного»/«Почетного»
    text = re.sub(r"[ \t]+", " ", text)
    return text.lower()


def strip_non_facts(text: str, *, keep_hashtags: bool = False) -> str:
    """Убрать ссылки, упоминания и (по умолчанию) хэштеги.

    ``keep_hashtags`` нужен для ИСТОЧНИКА: хэштег «#АксубаевскаяСОШ2» несёт
    настоящий факт (номер школы), и его нельзя выбрасывать — иначе пересказ
    «СОШ2» оказывался бы неподтверждённым (поймано на проде 01.11).
    """
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    if not keep_hashtags:
        text = _HASHTAG_RE.sub(" ", text)
    return text


def extract_numbers(text: str, *, keep_hashtags: bool = False) -> set[str]:
    """Все числа текста — цифровые группы КАК ЕСТЬ.

    Разряды склеивать нельзя: в пересказе про шины так получалось «02215»,
    которого в источнике нет ни разу («DUNLOP GRANDTREK ICE 02.» и «215/65
    R16» — это два разных числа, а не одно «02215»).
    """
    text = strip_non_facts(text, keep_hashtags=keep_hashtags)
    return {m.group(0) for m in re.finditer(r"\d+", text)}


def number_confirmation_set(text: str, *, keep_hashtags: bool = False) -> set[str]:
    """Числа, которыми источник может подтвердить число из пересказа.

    Кроме раздельных групп — ещё и слитная запись с разделителем тысяч:
    «1 200» подтверждает «1200». Склейка применяется ТОЛЬКО как форма
    подтверждения: в самом пересказе «02 215» остаётся двумя числами.
    """
    out = extract_numbers(text, keep_hashtags=keep_hashtags)
    cleaned = strip_non_facts(text, keep_hashtags=keep_hashtags)
    for match in _THOUSANDS_RE.finditer(cleaned):
        out.add(re.sub(r"[\s\u00a0\u202f]", "", match.group(0)))
    return out


def extract_names(text: str) -> set[str]:
    """Слова с заглавной буквы, кроме служебного стоп-словаря.

    Нормализация — **та же, что у источника** (``normalize_text``): ё→е и нижний
    регистр. Без этого «Тёплые» из пересказа не находилось рядом с «теплые» в
    источнике, и все 7 «неподтверждённых имён» в живом замере 02.11 (40 сборников,
    200 пунктов) оказались ложными срабатываниями на один этот асимметричный
    регистр: «Тёплые», «Мёртвые», «Весёлый», «Днём», «Фёдоровской», «Фёдорова».
    Источник нормализуется в ``check_facts``, а имя — нет: правило применялось
    только к одной стороне сравнения.
    """
    out: set[str] = set()
    for match in _WORD_RE.finditer(strip_non_facts(text)):
        word = match.group(0)
        if len(word) < 3:
            continue
        if not word[0].isupper():
            continue
        normalized = normalize_text(word)
        if normalized in _STOP:
            continue
        out.add(normalized)
    return out


def extract_facts(text: str) -> set[str]:
    """Факты текста: числа и имена/названия."""
    return {f"num:{n}" for n in extract_numbers(text)} | {f"name:{n}" for n in extract_names(text)}


#: Сколько знаков общего префикса считать совпадением. Русские падежи дают
#: «Валентину»/«Валентина», «Порубову»/«Порубова», «Кубееву»/«Кубеева» —
#: это ОДНО имя в разных падежах, а не выдумка. Общий префикс в 5+ знаков
#: такие формы ловит. А «Иван»/«Иванов» (общий префикс 4) — уже разные
#: слова, и такой порог их не путает.
_NAME_PREFIX_MIN = 5


def _name_match_kind(name: str, source_words: set[str]) -> str:
    """Как имя соотносится с источником: ``exact`` / ``form`` / ``""`` (не найдено).

    ``exact`` — слово совпало буквально; ``form`` — совпала падежная форма
    («Фёдорова» из «Фёдоров»): факт подтверждён, глаза не нужны. Разделение
    нужно, чтобы в отчёте оставались только те слова, где модель, возможно,
    выдумала имя, а не «всё подряд с большой буквы».
    """
    if name in source_words:
        return "exact"
    for word in source_words:
        common = 0
        for a, b in zip(name, word):
            if a != b:
                break
            common += 1
        if common >= _NAME_PREFIX_MIN:
            return "form"
    return ""


def _name_matches(name: str, source_words: set[str]) -> bool:
    """Есть ли в источнике это имя с учётом падежа."""
    return bool(_name_match_kind(name, source_words))


@dataclass
class Unsupported:
    """Что пересказа не подтвердилось источником.

    Разделено намеренно, потому что строгость у двух классов разная:

    * **числа** — строгий вердикт. Пересказ обязан сохранить число: если в
      источнике 12 тонн, в пересказе должно быть 12 тонн. «120» вместо «12» —
      это выдумка (или опечатка), и пересказ не имеет на это права.
    * **имена/названия** — advisory. Точное слово может не совпасть из-за
      падежа («Валентину»/«Валентина»), ё/е («Почётного»/«Почетного») или
      синонима («Запись» из «записаться»). Общий префикс в 5+ знаков
      снимает падеж, но синоним не снимает — такие случаи требуют глаз, а
      не вердикта скрипта.
    """

    numbers: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    #: Падежные формы имён, найденные в источнике («Фёдорова» из «Фёдоров»):
    #: факт подтверждён, но слово в пересказе отличается — отдельной строкой в
    #: отчёте, чтобы не мешать в глаза тем, что правда просит проверки.
    name_forms: list[str] = field(default_factory=list)

    @property
    def strict(self) -> bool:
        """Есть ли строгий вердикт (неподтверждённое число)."""
        return bool(self.numbers)

    @property
    def advisory(self) -> bool:
        """Есть ли что посмотреть глазами (неподтверждённое имя)."""
        return bool(self.names)

    def all(self) -> list[str]:
        return [f"число «{n}»" for n in self.numbers] + [f"имя/название «{n}»" for n in self.names]


def check_facts(summary: str, source: str) -> Unsupported:
    """Разделить неподтверждённые факты на числа (строго) и имена (совет)."""
    norm_source = normalize_text(source)
    source_words = set(_WORD_RE.findall(norm_source))
    # Хэштеги источника НЕ выбрасываем: «#АксубаевскаяСОШ2» — это факт.
    source_nums = number_confirmation_set(source, keep_hashtags=True)
    # Число словами в источнике («более семи миллиардов») подтверждает «7».
    for word, digits in _WORD_NUMBERS.items():
        if word in source_words:
            source_nums.add(digits)

    out = Unsupported()
    for fact in sorted(extract_facts(summary)):
        kind, _, value = fact.partition(":")
        if kind == "num":
            if value not in source_nums:
                out.numbers.append(value)
        else:
            match_kind = _name_match_kind(value, source_words)
            if match_kind == "":
                out.names.append(value)
            elif match_kind == "form":
                out.name_forms.append(value)
    return out


def unsupported_facts(summary: str, source: str) -> list[str]:
    """Факты пересказа, которых нет в источнике (список для отчёта)."""
    return check_facts(summary, source).all()


def split_digest_items(wall_text: str) -> list[str]:
    """Разобрать текст стены на пересказы.

    Пункт сборника устроен как «<пересказ>\\n\\nИсточник: <название>», поэтому
    граница однозначна: пересказ — текст ДО маркера, название — текст ПОСЛЕ
    него.

    Тонкость: перед первым пунктом стоит **заголовок** сборника
    (``HEADER = "Короткой строкой"``, ``modules/publisher/daily_digest.py``) —
    это отдельный блок, а не часть пересказа. Поэтому от региона перед
    маркером берём **последний** блок: заголовок отваливается сам, а если
    пересказ вдруг окажется многоабзацным, сохранится целиком.

    Если маркера нет вовсе (другой формат поста) — возвращаем пустой список:
    придумывать пункты по пустой строке значит проверять несуществующее.
    """
    items: list[str] = []
    for match in _ITEM_RE.finditer(wall_text):
        region = match.group("summary").strip()
        if not region:
            continue
        blocks = [b.strip() for b in region.split("\n\n") if b.strip()]
        if blocks:
            items.append(blocks[-1])
    return items


@dataclass
class ItemCheck:
    """Результат проверки одного пункта сборника."""

    region: str
    lip: str
    wall_url: str
    summary: str = ""
    source: str = ""
    unsupported: Unsupported = field(default_factory=Unsupported)
    strict_mode: bool = True
    error: str = ""

    @property
    def missing(self) -> list[str]:
        return self.unsupported.all()


def check_digest(
    *,
    region: str,
    rows: list[dict],
    wall_text: str,
    sources: dict[str, str],
) -> list[ItemCheck]:
    """Проверить один сборник: пункты против их источников.

    ``rows`` — строки ``published_posts`` для этого сборника в порядке id
    (у каждой ``lip`` источника и ``published_url`` стены). ``sources`` — lip →
    текст исходного поста.
    """
    items = split_digest_items(wall_text)
    checks: list[ItemCheck] = []

    # Строгий режим: пунктов и источников поровну, сопоставляем по порядку.
    strict = len(items) == len(rows) and len(rows) > 0

    if strict:
        for row, summary in zip(rows, items):
            lip = str(row.get("lip") or "")
            source = sources.get(lip, "")
            checks.append(
                ItemCheck(
                    region=region,
                    lip=lip,
                    wall_url=str(row.get("published_url") or ""),
                    summary=summary,
                    source=source,
                    unsupported=check_facts(summary, source) if source else Unsupported(),
                    strict_mode=True,
                    error="" if source else "нет текста источника",
                )
            )
        return checks

    # Ослабленный режим: пересказы сборника против объединения источников.
    #
    # Проверяем ТОЛЬКО пересказы, а не весь текст стены: в тексте стены ещё
    # заголовок («Короткой строкой»), названия источников и хвост с тегами —
    # и в них тоже есть числа (ссылка на канал, номер в теге), которые
    # источникам не принадлежат. Проверка всего текста давала ложные
    # «неподтверждённые числа» (поймано на проде 01.11: «1» в регионе mi).
    pool = "\n".join(sources.get(str(r.get("lip") or ""), "") for r in rows)
    pool_summary = "\n\n".join(items)
    checks.append(
        ItemCheck(
            region=region,
            lip=",".join(str(r.get("lip") or "") for r in rows),
            wall_url=str(rows[0].get("published_url") or "") if rows else "",
            summary=pool_summary,
            source=pool,
            unsupported=check_facts(pool_summary, pool),
            strict_mode=False,
            error="пункты не сопоставлены по одному — проверен весь сборник",
        )
    )
    return checks


# --- сбор данных (БД + VK) ---------------------------------------------------


def _parse_wall_ref(url: str) -> Optional[tuple[int, int]]:
    """``https://vk.com/wall-241117212_731`` → ``(241117212, 731)``."""
    match = re.search(r"wall(-?\d+)_(\d+)", url or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Фактчек сборников «Короткой строкой»")
    parser.add_argument("--days", type=int, default=3, help="сколько суток проверять")
    parser.add_argument("--region", default="", help="только один регион")
    parser.add_argument("--limit", type=int, default=40, help="максимум сборников")
    parser.add_argument("--json", action="store_true", help="отчёт целиком (JSON)")
    parser.add_argument("--show", type=int, default=3, help="сколько примеров показать")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

    from sqlalchemy import text as sql_text

    from database.connection import AsyncSessionLocal

    async def collect():
        """Собрать строки сборников, тексты источников и токены — в одном цикле.

        Один ``asyncio.run`` на всё: второй прогон создал бы новый event loop,
        а пул соединений остался бы привязан к первому (``Event loop is
        closed``).
        """
        from modules.vk_token_router import load_vk_routing

        async with AsyncSessionLocal() as session:
            params: dict = {"days": str(args.days)}
            where = (
                "kind='digest' AND published_at > now() - "
                "(CAST(:days AS text) || ' days')::interval"
            )
            if args.region:
                where += " AND region_code = :region"
                params["region"] = args.region
            rows = (
                (
                    await session.execute(
                        sql_text(
                            "SELECT id, lip, region_code, published_url, published_at "
                            f"FROM published_posts WHERE {where} ORDER BY id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            lips = [r["lip"] for r in rows]
            sources: dict[str, str] = {}
            if lips:
                src_rows = (
                    (
                        await session.execute(
                            sql_text(
                                "SELECT lip, post_text FROM collected_post_audit "
                                "WHERE lip = ANY(:lips)"
                            ),
                            {"lips": list(lips)},
                        )
                    )
                    .mappings()
                    .all()
                )
                sources = {r["lip"]: r["post_text"] or "" for r in src_rows}
            # Стены читать можно только user-токеном: community-токеном
            # ``wall.getById`` недоступен («method is unavailable with group
            # auth», ошибка 27) — проверено на проде 01.11.
            user_token, community_tokens = await load_vk_routing()
            return rows, sources, user_token, community_tokens

    import asyncio

    rows, sources, user_token, community_tokens = asyncio.run(collect())

    by_wall: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_wall[str(row["published_url"] or "")].append(dict(row))

    # Тексты стен из VK.
    wall_texts: dict[str, str] = {}
    if by_wall:
        try:
            from modules.vk_monitor.vk_client import VKClient

            # Стены читать можно только user-токеном: community-токеном
            # ``wall.getById`` недоступен («method is unavailable with group
            # auth», ошибка 27) — проверено на проде 01.11.
            if not user_token:
                print(
                    "нет user-токена — стены не прочитать (community-токеном "
                    "wall.getById недоступен)",
                    file=sys.stderr,
                )
            else:
                client = VKClient(user_token)
                for url in by_wall:
                    ref = _parse_wall_ref(url)
                    if not ref:
                        continue
                    owner_id, wall_id = ref
                    try:
                        post = client.get_post_by_id(owner_id, wall_id)
                        if post:
                            # Ключ — ИСХОДНЫЙ url: owner_id у групп отрицательный
                            # («wall--240491007_2524»), и собранный заново адрес
                            # не совпал бы с тем, что лежит в published_url.
                            wall_texts[url] = post.get("text") or ""
                    except Exception as exc:  # noqa: BLE001 — один пост не валит прогон
                        print(f"не прочитал стену {url}: {exc}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"VK недоступен: {exc}", file=sys.stderr)

    checks: list[ItemCheck] = []
    for url, wall_rows in list(by_wall.items())[: args.limit]:
        wall_text = wall_texts.get(url, "")
        if not wall_text:
            continue
        region = str(wall_rows[0].get("region_code") or "")
        checks.extend(
            check_digest(region=region, rows=wall_rows, wall_text=wall_text, sources=sources)
        )

    total_items = len(checks)
    strict_bad = [c for c in checks if c.unsupported.strict]
    advisory_bad = [c for c in checks if c.unsupported.advisory]
    form_bad = [c for c in checks if c.unsupported.name_forms]
    report = {
        "digests": len({c.wall_url for c in checks}),
        "items": total_items,
        "strict_items": sum(1 for c in checks if c.strict_mode),
        "items_with_wrong_numbers": len(strict_bad),
        "items_with_unconfirmed_names": len(advisory_bad),
        "items_with_name_forms": len(form_bad),
        "items_without_source": sum(1 for c in checks if c.error.startswith("нет")),
        "number_examples": [
            {
                "region": c.region,
                "lip": c.lip,
                "wall": c.wall_url,
                "numbers": c.unsupported.numbers,
                "summary": c.summary[:300],
            }
            for c in strict_bad[: args.show]
        ],
        "name_examples": [
            {
                "region": c.region,
                "lip": c.lip,
                "wall": c.wall_url,
                "names": c.unsupported.names,
                "summary": c.summary[:300],
            }
            for c in advisory_bad[: args.show]
        ],
        "name_form_examples": [
            {
                "region": c.region,
                "lip": c.lip,
                "wall": c.wall_url,
                "name_forms": c.unsupported.name_forms,
                "summary": c.summary[:300],
            }
            for c in form_bad[: args.show]
        ],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(
        f"сборников: {report['digests']}, пунктов: {report['items']} "
        f"(по одному к источнику: {report['strict_items']})"
    )
    print()
    print(f"СТРОГИЙ ВЕРДИКТ — не подтвердилось ЧИСЛО: {report['items_with_wrong_numbers']}")
    for ex in report["number_examples"]:
        print(f"  — {ex['region']}: {', '.join(ex['numbers'])}")
        print(f"    {ex['summary'][:180]}")
    print()
    print(
        f"СОВЕТ (нужны глаза) — не подтвердилось ИМЯ/НАЗВАНИЕ: "
        f"{report['items_with_unconfirmed_names']}"
    )
    if report["items_without_source"]:
        print(f"пунктов без текста источника: {report['items_without_source']}")
    for ex in report["name_examples"]:
        print(f"  — {ex['region']}: {', '.join(ex['names'])}")
    print()
    print(
        f"ФОРМА СЛОВА (факт подтверждён, слово отличается) — пунктов: "
        f"{report['items_with_name_forms']}"
    )
    for ex in report["name_form_examples"]:
        print(f"  — {ex['region']}: {', '.join(ex['name_forms'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
