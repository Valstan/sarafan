"""
Заголовки и хештеги сводок Postopus/SETKA (как в old_postopus Mongo).

Если в RegionConfig уже заданы zagolovki / heshteg — они имеют приоритет.
Иначе подставляются русские шаблоны по теме и названию региона.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

# Тематика → заголовок «Тема {регион}:» (fallback, когда в zagolovki нет ключа)
# sport — формулировка «Спортивные новости» как в запросе пользователя
_THEME_TITLE: dict[str, str] = {
    "novost": "Новости",
    "kultura": "Культура",
    "sport": "Спортивные новости",
    "reklama": "Объявления",
    "admin": "Администрация",
    "union": "Объединённая сводка",
    "addons": "Дополнительно",
    "sosed": "Новости соседей",
    "detsad": "Дошкольное образование",
    "setka": "Сетка регионов",
    "oblast": "Главное в области",
    "neighbors": "Новости соседей",
    "korotko": "Короткой строкой",
    # Расширенная областная повестка (community-mode oblast)
    "proisshestviya": "Происшествия",
    "molodezh": "Молодёжь",
    "nauka": "Наука и образование",
    "promyshlennost": "Экономика и промышленность",
    "selhoz": "Сельское хозяйство",
    "zdorovie": "Здоровье",
    "zhkh": "ЖКХ",
    "priroda": "Природа и туризм",
}


def theme_title(theme: str) -> str:
    """Русское имя темы — одно на заголовки сводок и на карту конвейера."""
    return _THEME_TITLE.get(theme, theme)


# Часть хештега по теме (кириллица, без #). К региональной части из
# heshteg_local пристыковывается в heshteg.
_THEME_HASHTAG_WORD: dict[str, str] = {
    "novost": "новости",
    "kultura": "культура",
    "sport": "спорт",
    "reklama": "реклама",
    "admin": "админ",
    "union": "объединение",
    "addons": "дополнительно",
    "sosed": "соседи",
    "detsad": "детсады",
    "setka": "сетка",
    "oblast": "область",
    "neighbors": "соседи",
    "korotko": "коротко",
    # Расширенная областная повестка (community-mode oblast)
    "proisshestviya": "происшествия",
    "molodezh": "молодёжь",
    "nauka": "наука",
    "promyshlennost": "экономика",
    "selhoz": "сельхоз",
    "zdorovie": "здоровье",
    "zhkh": "жкх",
    "priroda": "природа",
}


def region_display_name(region: Any, heshteg_local: Optional[dict]) -> str:
    """Человекочитаемое имя региона для заголовка."""
    name = getattr(region, "name", None) or ""
    name = name.strip()
    if name:
        return name
    raicentr = ""
    if heshteg_local and isinstance(heshteg_local, dict):
        raicentr = (heshteg_local.get("raicentr") or "").strip()
    if raicentr:
        return raicentr
    code = getattr(region, "code", None) or ""
    return str(code) or "Регион"


def resolve_bulletin_header(region_config: Any, theme: str, region: Any) -> str:
    """
    Заголовок сводки: из zagolovki[theme], иначе русский шаблон по теме и региону.

    **По умолчанию шапки нет вовсе** (решение владельца 2026-09-22): пустая
    строка — это «публиковать без заголовка», ``BulletinBuilder`` понимает её
    именно так (``None`` дал бы дефолтную шапку, поэтому здесь важно вернуть
    именно ``""``).

    Почему убрали и чего это НЕ даёт — у флага
    ``config.runtime.bulletin_header_enabled``. Возврат к шапкам — одна
    переменная окружения, включая районы с собственными ``zagolovki``: флаг
    стоит ПЕРЕД чтением конфига, иначе «убрали шапки» означало бы «убрали у
    всех, кроме тех, кто настраивал их руками», то есть ровно не то.
    """
    from config.runtime import bulletin_header_enabled

    if not bulletin_header_enabled():
        return ""

    z = getattr(region_config, "zagolovki", None) or {}
    if isinstance(z, dict) and theme in z:
        raw = z.get(theme)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    hl = getattr(region_config, "heshteg_local", None) or {}
    if not isinstance(hl, dict):
        hl = {}
    rlabel = region_display_name(region, hl)
    prefix = _THEME_TITLE.get(theme)
    if prefix:
        return f"{prefix} {rlabel}:"
    return f"📰 {theme} {rlabel}:"


def _city_hashtag(raw: str) -> str:
    """Название райцентра → аккуратный тег: «малмыж» → «#Малмыж».

    Хвостовые цифры отбрасываются: в части регионов в ``raicentr`` лежит
    «Киров43» (код региона приклеен к названию), и тег ``#Киров43`` читается
    как опечатка, а не как город.
    """
    value = " ".join((raw or "").strip().lstrip("#").split())
    value = re.sub(r"\d+$", "", value).strip()
    if not value:
        return ""
    value = value.replace("-", "_").replace("—", "_").replace(" ", "_")
    if value.islower():
        value = value[:1].upper() + value[1:]
    return f"#{value}"


def _district_hashtags(region: Any) -> List[str]:
    """Теги района из ``regions.local_hashtags`` (без #), кроме райцентра.

    Там лежит готовая пара вида ``#малмыж,#малмыжский_район``: первый тег —
    райцентр (он уходит в ``local_hashtag``), второй и далее — район/округ.
    """
    raw = getattr(region, "local_hashtags", None) if region is not None else None
    if not raw:
        return []
    parts = [p.strip().lstrip("#") for p in str(raw).replace("\n", ",").split(",")]
    out: List[str] = []
    for part in parts[1:]:
        part = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]", "", part.replace("-", "_").replace(" ", "_"))
        if len(part) >= 4 and part not in out:
            out.append(part)
    return out[:2]


def resolve_bulletin_hashtags(
    region_config: Any,
    theme: str,
    region: Any = None,
) -> Tuple[List[str], str]:
    """Тематические теги (без #) и локальный тег региона (с #).

    Схема владельца 20.10: подпись поста — это 3–4 коротких тега, а не склейка
    «культураМалмыж» (она читалась как обрубленное слово и выглядела
    недоделанной). Порядок: тема словом → тег района → тег райцентра.

    Комбинированные теги из ``region_configs.heshteg`` больше не используются
    намеренно: это ручные заготовки старого Postopus, каждое значение —
    склейка темы с названием района, ровно та форма, которую владелец забраковал.
    Данные в базе остаются, читать их незачем.
    """
    heshteg_local = getattr(region_config, "heshteg_local", None) or {}
    if not isinstance(heshteg_local, dict):
        heshteg_local = {}

    tags: List[str] = [_THEME_HASHTAG_WORD.get(theme, theme)]
    tags.extend(_district_hashtags(region))

    local_hashtag = _city_hashtag(str(heshteg_local.get("raicentr") or ""))
    return tags, local_hashtag


def resolve_mourning_bulletin_format() -> Tuple[str, List[str], str]:
    """
    Mourning-bulletin must be published without any header and hashtags.
    """
    return "", [], ""
