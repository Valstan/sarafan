"""Локальные хэштеги района — самый дешёвый рычаг находимости.

Замер 28.08.2026: у 23 из 36 активных районов ``regions.local_hashtags`` пуст, и
хэштеги есть ровно у той старой дюжины, которая растёт. Сборщик сводок уже умеет
клеить локальный хэштег в конец поста (``BulletinBuilder._build_hashtag_text``) —
для 23 районов ему просто нечего клеить, и каждая их сводка уходит невидимой для
поиска ВК по «#зуевка».

**Почему таблица, а не алгоритм.** Прилагательные районов нерегулярны: Луза →
лузский, Свеча → свечинский, Юрья → юрьянский, Мураши → мурашинский, Белая
Холуница → белохолуницкий, Подосиновец → подосиновский. Ни одно правило их не
выводит, а неверный хэштег хуже отсутствующего: он уводит читателя в чужую выдачу
и выглядит небрежностью в каждом посте района. Поэтому прилагательные — курируемая
таблица, а для района, которого в ней нет, генератор возвращает **только** тег
райцентра и поднимает флаг «проверить»: лучше один верный тег, чем два, из которых
второй выдуман.

**Почему это не разовый UPDATE в миграции.** Ровно так и появилась дыра, которую
чиним: 13 районов настроены захардкоженным списком в ``modules/region_config.py``,
а новые туда не попали. Заполнение делается на каждом прогоне зачисления, поэтому
следующий заведённый район получит хэштеги сам.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

# Прилагательное района по коду региона. Источник — официальные названия
# муниципальных образований Кировской области и Татарстана.
#
# Держим форму «..._район» даже там, где район официально стал округом: так
# написано у 13 уже настроенных районов, и так пишут сами жители в постах —
# а хэштег ищут люди, а не реестр.
DISTRICT_ADJECTIVES: Dict[str, str] = {
    "afanasyevo": "афанасьевский",
    "apastovsky": "апастовский",
    "arbazh": "арбажский",
    "arsk": "арский",
    "atnya": "атнинский",
    "bal": "балтасинский",
    "belholunitsa": "белохолуницкий",
    "bogorodskoe": "богородский",
    "chepetsk": "кирово_чепецкий",
    "chistopolsky": "чистопольский",
    "darovskoy": "даровской",
    "elabuga": "елабужский",
    "falenki": "фалёнский",
    "kamsko_ustinsky": "камско_устьинский",
    "kaybitsky": "кайбицкий",
    "kiknur": "кикнурский",
    "klz": "кильмезский",
    "kotelnich": "котельничский",
    "kukmor": "кукморский",
    "kumyony": "кумёнский",
    "laishevo": "лаишевский",
    "leb": "лебяжский",
    "luza": "лузский",
    "mamadysh": "мамадышский",
    "mendeleevsk": "менделеевский",
    "mi": "малмыжский",
    "murashi": "мурашинский",
    "nagorsk": "нагорский",
    "nema": "немский",
    "nizhnekamsk": "нижнекамский",
    "nolinsk": "нолинский",
    "omutninsk": "омутнинский",
    "oparino": "опаринский",
    "orichi": "оричевский",
    "orlov": "орловский",
    "pestretsy": "пестречинский",
    "pizhanka": "пижанский",
    "podosinovets": "подосиновский",
    "rybnaya_sloboda": "рыбно_слободский",
    "saby": "сабинский",
    "sanchursk": "санчурский",
    "shabalino": "шабалинский",
    "slobodskoy": "слободской",
    "sovetsk": "советский",
    "spassky": "спасский",
    "suna": "сунский",
    "svecha": "свечинский",
    "tetyushsky": "тетюшский",
    "tuzha": "тужинский",
    "tyulyachi": "тюлячинский",
    "uni": "унинский",
    "ur": "уржумский",
    "verhniy_uslon": "верхнеуслонский",
    "verhnekame": "верхнекамский",
    "verhoshizhem": "верхошижемский",
    "vp": "вятскополянский",
    "vysokaya_gora": "высокогорский",
    "yaransk": "яранский",
    "yurya": "юрьянский",
    "zelenodolsk": "зеленодольский",
    "zuevka": "зуевский",
}


@dataclass(frozen=True)
class HashtagPlan:
    """Что предлагается проставить району.

    ``needs_review`` — район не найден в таблице прилагательных: тег райцентра
    верен, тег района не выдуман и просто отсутствует. UI обязан показать такой
    район отдельно, а не считать его настроенным.
    """

    region_code: str
    hashtags: List[str]
    needs_review: bool
    note: str = ""

    def as_field(self) -> str:
        """Значение для ``regions.local_hashtags`` — CSV, как у настроенных районов."""
        return ",".join(self.hashtags)


def normalize_tag(value: str) -> str:
    """Привести название к виду хэштега: нижний регистр, пробелы и дефисы в подчёркивания.

    «Белая Холуница» → «белая_холуница», «Кирово-Чепецк» → «кирово_чепецк».
    Буква «ё» сохраняется: в «Кумёны» её пишут, и «#кумены» — другой тег.
    """
    cleaned = (value or "").strip().lower()
    cleaned = cleaned.replace("-", "_").replace("—", "_")
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"[^0-9a-zа-яё_]", "", cleaned)
    return cleaned.strip("_")


def build_hashtag_plan(
    region_code: str,
    center_name: Optional[str],
    *,
    existing: Optional[str] = None,
) -> Optional[HashtagPlan]:
    """Предложить хэштеги району. ``None`` — предлагать нечего.

    Args:
        region_code: код региона (``suna``, ``mi``, …).
        center_name: райцентр — ``regions.center_city`` либо
            ``region_configs.heshteg_local['raicentr']``.
        existing: текущее значение ``regions.local_hashtags``; непустое означает
            «уже настроено» и возвращает ``None`` — своё не перетираем никогда.
    """
    if existing and existing.strip():
        return None

    center_tag = normalize_tag(center_name or "")
    adjective = DISTRICT_ADJECTIVES.get((region_code or "").strip().lower())

    if not center_tag and not adjective:
        return None

    tags: List[str] = []
    if center_tag:
        tags.append(f"#{center_tag}")
    if adjective:
        district_tag = f"#{adjective}_район"
        if district_tag not in tags:
            tags.append(district_tag)

    if not tags:
        return None

    needs_review = adjective is None
    note = (
        "прилагательное района неизвестно — проставлен только тег райцентра" if needs_review else ""
    )
    return HashtagPlan(
        region_code=region_code,
        hashtags=tags,
        needs_review=needs_review,
        note=note,
    )


def plan_hashtags_for_regions(rows) -> List[HashtagPlan]:
    """Собрать предложения по списку районов.

    Args:
        rows: последовательность кортежей ``(code, center_name, existing)``.

    Returns:
        Планы только для тех районов, кому есть что проставить.
    """
    plans: List[HashtagPlan] = []
    for code, center_name, existing in rows:
        plan = build_hashtag_plan(code, center_name, existing=existing)
        if plan is not None:
            plans.append(plan)
    return plans
