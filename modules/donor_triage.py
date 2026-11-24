"""Разбор доноров: общие группы и доноры, которые молчат (P236 / P235).

**Что это чинит.** Два хвоста, которые остались за владельцем:

* **общие группы** (P236) — одна VK-группа заведена в нескольких регионах.
  Правило «город региона против города группы» (см.
  ``scripts/dedupe_shared_communities.py``) решает только случай, когда город
  группы совпал с городом **главной** группы региона. Остаются два класса:
  группа из деревни (город региона — районный центр, а группа живёт в селе) и
  группа, которая **честно охватывает несколько районов** («Объявления Свеча,
  Лебяжье, Даровской, Котельнич»). Первый класс решается локалитетом из
  названия группы, второй — не решается ничем: такая группа в нескольких
  районах и **должна** там быть.
* **молчащие доноры** (P235) — тема без слота в beat-расписании
  (``nauka``, ``sosed``, ``molodezh``, ``selhoz``, ``zdorovie``, ``priroda``,
  ``promyshlennost``): волны такой темы нет, и донор не публикуется вообще.
  Часть таких доноров вынужденно молчала и до 24.11 — прежнюю тему
  восстановить не из чего (в журнале публикаций их нет).

**Что делает модуль.** По данным решает, что можно решить **автоматически**,
и отдаёт остальное с рекомендацией и объяснением: решение остаётся за
человеком, но не с нуля — с подготовленным вариантом.

**Как решает автоматически.** По названию группы ищутся **локалитеты наших
регионов** (``RegionConfig.localities`` + название региона). Сравнение — по
основе слова, а не по подстроке: «Елабужского района» ← «Елабуга», «Молодежь
Советск43» ← «Советск».

**Чего не делает.** Ничего не пишет: модуль только считает и советует. Запись
делает либо ``scripts/donor_triage.py --apply`` (с файлом решений), либо
страница разбора (``/donor-triage``) — по одному и тому же коду, чтобы
headless- и браузерный пути не разошлись.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: Темы, у которых есть слот в beat-расписании (см.
#: ``scripts/restore_silenced_categories.py``): донор в такой теме публикуется.
SLOTTED_THEMES = ("novost", "reklama", "kultura", "sport", "admin", "union", "detsad")

#: Темы **без** слота: донор в них не публикуется (молчит).
NO_SLOT_THEMES = (
    "sosed",
    "molodezh",
    "zdorovie",
    "nauka",
    "selhoz",
    "priroda",
    "promyshlennost",
)

#: Тема, куда рекомендуем переводить локального донора, который молчит:
#: новости района — единственная тема, где любая местная группа уместна.
DEFAULT_SILENT_THEME = "novost"

#: Причина, которой помечаем выключенного донора.
REASON_SHARED = "pool_shared_group"
REASON_NOT_LOCAL = "not_local_region"
REASON_DORMANT = "dormant_t1_auto"

#: Минимальная длина слова, чтобы искать его основу (короче — слишком много
#: случайных совпадений: «св», «ун», «ок»).
_MIN_STEM_LEN = 5

#: Длина ключа сопоставления. Четыре буквы хватает, чтобы падеж не ломал
#: совпадение: «Елабужского района» и «Елабуга» дают один ключ «елабу»,
#: «Ютазы» и «Ютаза» — «ютаз».
_KEY_LEN = 4

#: Слова-мусор, которые встречаются в названиях групп и ничего не значат.
#: Часть из них — типовые прилагательные («малые», «верхние»): они и в
#: названии НП, и в любом названии группы, и совпали бы с чужими регионами.
_NOISE_WORDS = (
    "инфо",
    "группа",
    "официальная",
    "официальный",
    "сообщество",
    "новости",
    "малые",
    "малая",
    "большие",
    "большая",
    "верхние",
    "верхний",
    "нижние",
    "нижний",
    "новый",
    "новая",
    "старая",
    "старый",
    "село",
    "деревня",
    "поселок",
    "город",
    "район",
    "района",
)


def norm_text(value: Optional[str]) -> str:
    """Привести текст к сравнимому виду: нижний регистр, «ё»→«е», без пунктуации.

    Пунктуация заменяется пробелом — иначе «Зуевка-Фалёнки» не нашлось бы по
    локалитету «Фалёнки».
    """
    text = (value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def stem(word: str) -> str:
    """Ключ сопоставления слова: первые ``_KEY_LEN`` букв.

    Ключ, а не подстрока: «Елабужского района» и «Елабуга» — одно и то же
    место, но разными буквами в конце.
    """
    return word[:_KEY_LEN]


class LocalityIndex:
    """Наши населённые пункты и названия регионов → регион, которому они belong.

    Строится один раз на разбор: ``RegionConfig.localities`` (список НП) плюс
    название региона («АРБАЖ - ИНФО» → «арбаж»).

    **Два вида совпадений.** Короткие слова («Арск», «Тужа», «Уни») сравниваются
    **точно**: иначе «Университет» сошёлся бы с «Уни». Длинные — по началу
    слова: так «Елабужского района» находит «Елабуга», а «Ютазы» — «Ютаза».
    """

    def __init__(self) -> None:
        #: короткие слова (точное совпадение) → {region_id: название}
        self._exact: Dict[str, Dict[int, str]] = defaultdict(dict)
        #: начало длинного слова → {region_id: название}
        self._stems: Dict[str, Dict[int, str]] = defaultdict(dict)
        #: регион → его НП (исходные названия)
        self.by_region: Dict[int, List[str]] = defaultdict(list)

    def _add_word(self, region_id: int, word: str, label: str) -> None:
        if word in _NOISE_WORDS:
            return
        self._exact[word][int(region_id)] = label
        self._stems[word[:_KEY_LEN]][int(region_id)] = label

    def add_region(self, region_id: int, name: Optional[str]) -> None:
        """Добавить название региона (оно тоже работает как локалитет)."""
        for word in norm_text(name).split():
            self._add_word(region_id, word, word)

    def add_locality(self, region_id: int, locality: Optional[str]) -> None:
        """Добавить населённый пункт региона."""
        cleaned = norm_text(locality)
        if not cleaned:
            return
        self.by_region[int(region_id)].append(cleaned)
        for word in cleaned.split():
            self._add_word(region_id, word, cleaned)

    def regions_mentioned(self, text: Optional[str]) -> Dict[int, List[str]]:
        """Какие регионы упомянуты в тексте (по названиям НП и регионов).

        Возвращает ``{region_id: [совпавшие названия]}`` — только регионы,
        которые действительно упомянуты.
        """
        found: Dict[int, List[str]] = defaultdict(list)
        for word in norm_text(text).split():
            if word in _NOISE_WORDS:
                continue
            if len(word) >= _MIN_STEM_LEN:
                bucket = self._stems.get(word[:_KEY_LEN], {})
            else:
                bucket = self._exact.get(word, {})
            for region_id, label in bucket.items():
                if label not in found[region_id]:
                    found[region_id].append(label)
        return dict(found)


# ---------------------------------------------------------------------------
# Сбор данных и запись решений
# ---------------------------------------------------------------------------

#: Темы, у которых волна берёт «весь пул» — если у региона есть сообщества в
#: каждой из них, донор темы без слота никем не будет подобран (тёмный).
FALLBACK_THEMES = ("novost", "kultura", "sport", "admin", "union", "detsad")


async def _load_base(session: Any) -> Dict[str, Any]:
    """Регионы, конфиги, активные сообщества и журнал публикаций."""
    from sqlalchemy import select

    from database.models import Community, Region
    from database.models_extended import PublishedPost, RegionConfig

    regions = {int(r.id): r for r in (await session.execute(select(Region))).scalars().all()}
    configs = {
        str(c.region_code): c for c in (await session.execute(select(RegionConfig))).scalars().all()
    }
    rows = list(
        (await session.execute(select(Community).where(Community.is_active.is_(True))))
        .scalars()
        .all()
    )
    published = list((await session.execute(select(PublishedPost))).scalars().all())
    return {
        "regions": regions,
        "configs": configs,
        "rows": rows,
        "published": published,
    }


def build_locality_index(regions: Dict[int, Any], configs: Dict[str, Any]) -> LocalityIndex:
    """Собрать индекс наших НП: названия регионов + ``localities`` из конфига."""
    index = LocalityIndex()
    for region_id, region in regions.items():
        index.add_region(region_id, str(getattr(region, "name", "") or ""))
        code = str(getattr(region, "code", "") or "")
        config = configs.get(code)
        for locality in getattr(config, "localities", None) or []:
            index.add_locality(region_id, str(locality))
    return index


def _own_main_groups(regions: Dict[int, Any]) -> Dict[int, int]:
    """Наши собственные главные группы: их регистрацию не выключаем никогда."""
    return {
        int(rid): abs(int(r.vk_group_id))
        for rid, r in regions.items()
        if getattr(r, "vk_group_id", None)
    }


def _publications_by_owner(published: Iterable[Any]) -> Dict[int, int]:
    """Сколько раз выходил каждый донор: ``lip`` = ``owner_id_post_id``."""
    counts: Dict[int, int] = defaultdict(int)
    for post in published:
        try:
            owner = abs(int(str(post.lip or "").split("_", 1)[0]))
        except (TypeError, ValueError):
            continue
        counts[owner] += 1
    return dict(counts)


def collect_shared_groups(
    rows: Sequence[Any],
    regions: Dict[int, Any],
    own_main: Dict[int, int],
    publications: Dict[int, int],
) -> List[Dict[str, Any]]:
    """Общие группы: одна VK-группа заведена в нескольких регионах."""
    by_vk: Dict[int, List[Any]] = defaultdict(list)
    for row in rows:
        by_vk[abs(int(row.vk_id or 0))].append(row)

    groups: List[Dict[str, Any]] = []
    for vk_id, group_rows in by_vk.items():
        if not vk_id:
            continue
        region_ids = {int(r.region_id) for r in group_rows}
        if len(region_ids) < 2:
            continue
        regs: List[Dict[str, Any]] = []
        for row in sorted(group_rows, key=lambda r: int(r.region_id)):
            region_id = int(row.region_id)
            region = regions.get(region_id)
            regs.append(
                {
                    "row_id": int(row.id),
                    "region_id": region_id,
                    "region_code": str(getattr(region, "code", "") or ""),
                    "region_name": str(getattr(region, "name", "") or "")[:60],
                    "category": str(getattr(row, "category", "") or ""),
                    "posts_count": int(getattr(row, "posts_count", 0) or 0),
                    "members": getattr(row, "members_count", None),
                    "created_at": str(getattr(row, "created_at", "") or "")[:10],
                    "last_post_at": str(getattr(row, "last_post_at", "") or "")[:16],
                    "is_own_main": own_main.get(region_id) == vk_id,
                }
            )
        groups.append(
            {
                "vk_id": vk_id,
                "name": str(getattr(group_rows[0], "name", "") or "")[:120],
                "publications": int(publications.get(vk_id, 0)),
                "regs": regs,
            }
        )
    groups.sort(key=lambda g: (-len(g["regs"]), g["vk_id"]))
    return groups


def collect_silent_donors(
    rows: Sequence[Any],
    regions: Dict[int, Any],
    own_main: Dict[int, int],
    publications: Dict[int, int],
) -> List[Dict[str, Any]]:
    """Доноры в темах без слота, которые никогда не публиковались.

    «Тёмный» донор — тот, кого не возьмёт ни одна волна: у региона есть
    сообщества в каждой теме с запасным путём («весь пул»), поэтому донор
    темы без слота остаётся невидимым.
    """
    by_region: Dict[int, List[Any]] = defaultdict(list)
    for row in rows:
        by_region[int(row.region_id)].append(row)

    donors: List[Dict[str, Any]] = []
    for region_id, region_rows in by_region.items():
        categories = Counter(str(getattr(r, "category", "") or "") for r in region_rows)
        for row in region_rows:
            category = str(getattr(row, "category", "") or "")
            if category not in NO_SLOT_THEMES:
                continue
            if not all(categories[theme] > 0 for theme in FALLBACK_THEMES):
                continue
            vk_id = abs(int(row.vk_id or 0))
            if own_main.get(region_id) == vk_id:
                continue
            if publications.get(vk_id, 0):
                continue
            region = regions.get(region_id)
            donors.append(
                {
                    "row_id": int(row.id),
                    "vk_id": vk_id,
                    "region_id": region_id,
                    "region_code": str(getattr(region, "code", "") or ""),
                    "region_name": str(getattr(region, "name", "") or "")[:60],
                    "name": str(getattr(row, "name", "") or "")[:120],
                    "category": category,
                    "suggested_category": str(getattr(row, "suggested_category", "") or ""),
                    "health": str(getattr(row, "health_status", "") or ""),
                    "members": getattr(row, "members_count", None),
                    "posts_count": int(getattr(row, "posts_count", 0) or 0),
                    "last_post_at": str(getattr(row, "last_post_at", "") or "")[:16],
                    "created_at": str(getattr(row, "created_at", "") or "")[:10],
                    "publications": 0,
                }
            )
    donors.sort(key=lambda d: (d["region_code"], d["name"]))
    return donors


async def collect_candidates(
    session: Any,
    *,
    group_cities: Optional[Dict[int, str]] = None,
    region_cities: Optional[Dict[int, str]] = None,
    group_descriptions: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """Собрать оба разбора с рекомендациями.

    ``group_cities`` / ``region_cities`` / ``group_descriptions`` — данные VK
    (``groups.getById fields=city,description``). Без них часть правил не
    работает, но разбор не падает:city-правило просто не срабатывает.
    """
    base = await _load_base(session)
    regions = base["regions"]
    configs = base["configs"]
    rows = base["rows"]
    own_main = _own_main_groups(regions)
    publications = _publications_by_owner(base["published"])
    index = build_locality_index(regions, configs)

    group_cities = group_cities or {}
    region_cities = region_cities or {}
    group_descriptions = group_descriptions or {}

    shared: List[Dict[str, Any]] = []
    for group in collect_shared_groups(rows, regions, own_main, publications):
        rec = recommend_shared(
            group["regs"],
            group_name=" ".join([group["name"], group_descriptions.get(int(group["vk_id"]), "")]),
            group_city=group_cities.get(int(group["vk_id"]), ""),
            region_cities=region_cities,
            index=index,
        )
        group["group_city"] = group_cities.get(int(group["vk_id"]), "")
        group["description"] = group_descriptions.get(int(group["vk_id"]), "")[:200]
        group["recommendation"] = rec
        shared.append(group)

    donors: List[Dict[str, Any]] = []
    for donor in collect_silent_donors(rows, regions, own_main, publications):
        donor["description"] = group_descriptions.get(int(donor["vk_id"]), "")[:200]
        donor["recommendation"] = recommend_donor(donor, index=index)
        donors.append(donor)

    return {
        "shared": shared,
        "donors": donors,
        "slotted_themes": list(SLOTTED_THEMES),
        "no_slot_themes": list(NO_SLOT_THEMES),
    }


async def fetch_vk_info(vk_ids: Sequence[int], main_group_ids: Sequence[int]) -> Dict[str, Any]:
    """Города и описания групп из VK (``groups.getById fields=city,description``).

    Без VK разбор не падает: часть правил просто не срабатывает, и решение
    уходит человеку.
    """
    cities: Dict[int, str] = {}
    descriptions: Dict[int, str] = {}
    error = ""
    if not vk_ids and not main_group_ids:
        return {"cities": cities, "descriptions": descriptions, "error": "нет групп для опроса"}
    try:
        from database.connection import AsyncSessionLocal
        from modules.vk_monitor.vk_client import VKClient
        from modules.vk_token_router import pick_healthy_read_token

        async with AsyncSessionLocal() as session:
            candidate = await pick_healthy_read_token(session)
        token = getattr(candidate, "token", None) if candidate else None
        if not token:
            from config.runtime import VK_TOKENS

            token = next(iter(VK_TOKENS.values()), None)
        if not token:
            return {"cities": cities, "descriptions": descriptions, "error": "нет VK-токена"}

        client = VKClient(token=token)

        def _read(infos: List[Any]) -> None:
            for info in infos or []:
                city = (info or {}).get("city") or {}
                gid = abs(int((info or {}).get("id") or 0))
                if city.get("title"):
                    cities[gid] = str(city.get("title"))
                if (info or {}).get("description"):
                    descriptions[gid] = str((info or {}).get("description"))

        if main_group_ids:
            _read(
                await asyncio.to_thread(
                    client.get_groups_by_ids, list(main_group_ids), fields="city"
                )
            )
        if vk_ids:
            _read(
                await asyncio.to_thread(
                    client.get_groups_by_ids, list(vk_ids), fields="city,description"
                )
            )
    except Exception as exc:  # noqa: BLE001 — без VK разбор не падает
        error = "%s: %s" % (type(exc).__name__, exc)
    return {"cities": cities, "descriptions": descriptions, "error": error}


async def collect_with_vk(session: Any) -> Dict[str, Any]:
    """Полный разбор: общие группы + молчащие доноры + рекомендации.

    Один и тот же вход и для скрипта, и для страницы разбора.
    """
    from sqlalchemy import select

    from database.models import Community, Region

    regions = {int(r.id): r for r in (await session.execute(select(Region))).scalars().all()}
    rows = list(
        (await session.execute(select(Community).where(Community.is_active.is_(True))))
        .scalars()
        .all()
    )

    regions_by_vk: Dict[int, set] = {}
    for row in rows:
        regions_by_vk.setdefault(abs(int(row.vk_id or 0)), set()).add(int(row.region_id))
    shared_vk = sorted(vk for vk, rids in regions_by_vk.items() if vk and len(rids) > 1)
    main_ids = sorted({abs(int(r.vk_group_id)) for r in regions.values() if r.vk_group_id})

    vk = await fetch_vk_info(shared_vk, main_ids)
    region_cities = {
        rid: vk["cities"].get(abs(int(r.vk_group_id)), "")
        for rid, r in regions.items()
        if getattr(r, "vk_group_id", None)
    }
    report = await collect_candidates(
        session,
        group_cities=vk["cities"],
        region_cities=region_cities,
        group_descriptions=vk["descriptions"],
    )
    report["vk_error"] = vk["error"]
    return report


async def apply_decisions(session: Any, decisions: Dict[str, Any]) -> Dict[str, Any]:
    """Записать решения: что выключить, куда перевести.

    ``decisions``::

        {"shared": {"<vk_id>": {"action": "keep_region", "keep_region_id": 5}},
         "donors": {"<row_id>": {"action": "set_theme", "theme": "novost"}}}

    Действия: ``keep_region`` (оставить в одном регионе, остальные выключить),
    ``keep_all`` / ``skip`` (ничего не делать), ``disable_all`` (выключить
    везде); для доноров — ``set_theme`` (перевести в тему со слотом),
    ``disable`` (выключить), ``skip``.

    **Наши собственные главные группы не выключаются никогда** — даже при
    ``disable_all``.
    """
    from datetime import datetime

    from sqlalchemy import select

    from database.models import Community, Region

    now = datetime.utcnow()
    applied: Dict[str, Any] = {"shared": [], "donors": [], "skipped": []}

    regions = {int(r.id): r for r in (await session.execute(select(Region))).scalars().all()}
    own_main = _own_main_groups(regions)

    # --- общие группы ---
    shared_decisions = decisions.get("shared") or {}
    if shared_decisions:
        vk_ids = [abs(int(v)) for v in shared_decisions]
        rows = list(
            (
                await session.execute(
                    select(Community).where(
                        Community.is_active.is_(True),
                        Community.vk_id.in_(vk_ids + [-v for v in vk_ids]),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_vk: Dict[int, List[Any]] = defaultdict(list)
        for row in rows:
            by_vk[abs(int(row.vk_id or 0))].append(row)

        for vk_key, decision in shared_decisions.items():
            vk_id = abs(int(vk_key))
            action = str((decision or {}).get("action") or "skip")
            group_rows = by_vk.get(vk_id, [])
            if action in ("skip", "keep_all") or not group_rows:
                applied["skipped"].append({"scope": "shared", "vk_id": vk_id, "action": action})
                continue
            if action == "keep_region":
                keep_region_id = int((decision or {}).get("keep_region_id") or 0)
                targets = [r for r in group_rows if int(r.region_id) != keep_region_id]
            elif action == "disable_all":
                targets = list(group_rows)
            else:
                applied["skipped"].append(
                    {
                        "scope": "shared",
                        "vk_id": vk_id,
                        "action": action,
                        "why": "неизвестное действие",
                    }
                )
                continue
            for row in targets:
                region_id = int(row.region_id)
                if own_main.get(region_id) == vk_id:
                    applied["skipped"].append(
                        {
                            "scope": "shared",
                            "vk_id": vk_id,
                            "row_id": int(row.id),
                            "why": "наша главная группа — не выключаем",
                        }
                    )
                    continue
                row.is_active = False
                row.disabled_at = now
                row.disabled_reason = REASON_SHARED
                applied["shared"].append(
                    {
                        "vk_id": vk_id,
                        "row_id": int(row.id),
                        "region_id": region_id,
                        "action": action,
                    }
                )

    # --- молчащие доноры ---
    donor_decisions = decisions.get("donors") or {}
    if donor_decisions:
        row_ids = [int(v) for v in donor_decisions]
        rows = list(
            (
                await session.execute(
                    select(Community).where(
                        Community.id.in_(row_ids), Community.is_active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {int(r.id): r for r in rows}
        for row_key, decision in donor_decisions.items():
            row_id = int(row_key)
            action = str((decision or {}).get("action") or "skip")
            row = by_id.get(row_id)
            if row is None or action == "skip":
                applied["skipped"].append({"scope": "donors", "row_id": row_id, "action": action})
                continue
            if action == "set_theme":
                theme = str((decision or {}).get("theme") or "").strip()
                if theme not in SLOTTED_THEMES:
                    applied["skipped"].append(
                        {
                            "scope": "donors",
                            "row_id": row_id,
                            "action": action,
                            "why": "тема %s без слота — переводить некуда" % theme,
                        }
                    )
                    continue
                old_theme = str(getattr(row, "category", "") or "")
                row.category = theme
                # AI-подсказка не теряется: тема без слота остаётся видимой,
                # её можно вернуть, когда у темы появится слот.
                row.suggested_category = old_theme or getattr(row, "suggested_category", None)
                row.health_status = "changed_category"
                row.updated_at = now
                applied["donors"].append(
                    {
                        "row_id": row_id,
                        "action": action,
                        "theme": theme,
                        "was": old_theme,
                    }
                )
            elif action == "disable":
                row.is_active = False
                row.disabled_at = now
                row.disabled_reason = (
                    REASON_DORMANT
                    if str(getattr(row, "health_status", "") or "") == "dormant"
                    else REASON_NOT_LOCAL
                )
                applied["donors"].append(
                    {
                        "row_id": row_id,
                        "action": action,
                        "name": str(getattr(row, "name", "") or "")[:80],
                    }
                )
            else:
                applied["skipped"].append(
                    {
                        "scope": "donors",
                        "row_id": row_id,
                        "action": action,
                        "why": "неизвестное действие",
                    }
                )

    await session.commit()
    return applied


def recommend_shared(
    regs: Sequence[Dict[str, Any]],
    *,
    group_name: str = "",
    group_city: str = "",
    region_cities: Optional[Dict[int, str]] = None,
    index: Optional[LocalityIndex] = None,
) -> Dict[str, Any]:
    """Что делать с общей группой: где оставить, что выключить, что спросить.

    Правила (по убыванию уверенности):

    1. **Город совпал ровно с одним регионом** → оставляем там, остальное
       выключаем (это уже сделано правилом из P236).
    2. **В названии группы — локалитет ровно одного из наших регионов** →
       оставляем там. Так отсеиваются группы из деревень (город региона —
       районный центр, а группа живёт в селе).
    3. **В названии — локалитеты нескольких наших регионов** → группа честно
       охватывает несколько районов: **оставляем везде**, ничего не выключаем.
    4. **Ничего не нашли** → по истории: регион, где у группы больше постов
       (с заметным отрывом) → оставляем там; иначе — **решение за человеком**.
    """
    region_cities = region_cities or {}
    if not regs:
        return {
            "action": "skip",
            "reason": "нет регистраций",
            "keep_region_ids": [],
            "drop_row_ids": [],
            "confidence": "low",
        }

    ids = [int(r["region_id"]) for r in regs]

    # 1. город
    gcity = norm_text(group_city)
    by_city = [
        r for r in regs if gcity and gcity == norm_text(region_cities.get(int(r["region_id"]), ""))
    ]
    if len(by_city) == 1:
        keep_id = int(by_city[0]["region_id"])
        return {
            "action": "keep_region",
            "keep_region_ids": [keep_id],
            "drop_row_ids": [int(r["row_id"]) for r in regs if int(r["region_id"]) != keep_id],
            "reason": "город группы совпал с городом региона %s"
            % by_city[0].get("region_code", ""),
            "confidence": "high",
        }

    # 2 и 3. локалитеты из названия группы
    mentions = index.regions_mentioned(group_name) if index else {}
    mentioned_here = [rid for rid in mentions if rid in ids]
    if len(mentioned_here) == 1:
        keep_id = mentioned_here[0]
        return {
            "action": "keep_region",
            "keep_region_ids": [keep_id],
            "drop_row_ids": [int(r["row_id"]) for r in regs if int(r["region_id"]) != keep_id],
            "reason": "в названии группы — наш НП: %s" % ", ".join(mentions.get(keep_id, [])[:2]),
            "confidence": "high",
        }
    if len(mentioned_here) > 1:
        return {
            "action": "keep_all",
            "keep_region_ids": ids,
            "drop_row_ids": [],
            "reason": "группа охватывает несколько наших районов (%s) — в каждом она своя"
            % ", ".join(
                str(r.get("region_code", "")) for r in regs if int(r["region_id"]) in mentioned_here
            ),
            "confidence": "medium",
        }

    # 4. история
    ranked = sorted(
        regs,
        key=lambda r: (
            -int(r.get("posts_count") or 0),
            -int(r.get("publications") or 0),
            str(r.get("created_at") or ""),
        ),
    )
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    if second is not None:
        gap = int(top.get("posts_count") or 0) - int(second.get("posts_count") or 0)
        if gap >= 2 or (
            gap == 0 and int(top.get("publications") or 0) > int(second.get("publications") or 0)
        ):
            keep_id = int(top["region_id"])
            return {
                "action": "keep_region",
                "keep_region_ids": [keep_id],
                "drop_row_ids": [int(r["row_id"]) for r in regs if int(r["region_id"]) != keep_id],
                "reason": "история: в регионе %s у группы %d постов против %d"
                % (
                    top.get("region_code", ""),
                    int(top.get("posts_count") or 0),
                    int(second.get("posts_count") or 0),
                ),
                "confidence": "medium",
            }

    return {
        "action": "decide",
        "keep_region_ids": [],
        "drop_row_ids": [],
        "reason": "ни город, ни наши НП в названии не указали — нужно решение человека",
        "confidence": "low",
    }


def recommend_donor(
    donor: Dict[str, Any],
    *,
    index: Optional[LocalityIndex] = None,
) -> Dict[str, Any]:
    """Что делать с донором, который молчит (тема без слота).

    Правила:

    1. **Донор помечен ``dormant``** (замер релевантности не прошёл) → выключить:
       он и раньше молчал, а слот в ленте занимает.
    2. **В названии — наш НП** → донор местный: перевести в тему со слотом
       (``novost``), AI-подсказку оставить в ``suggested_category`` — решение
       не теряется, а откладывается на место, где тема есть.
    3. **Ничего не нашли** → решение за человеком: группа может быть и нашей
       (название не содержит НП), и чужой (областной канал).
    """
    if str(donor.get("health") or "") == "dormant":
        return {
            "action": "disable",
            "theme": None,
            "reason": "донор помечен dormant — молчал и до правки",
            "confidence": "high",
        }

    region_id = int(donor.get("region_id") or 0)
    label = "%s %s" % (
        str(donor.get("name") or ""),
        str(donor.get("description") or ""),
    )
    mentions = index.regions_mentioned(label) if index else {}
    if region_id and region_id in mentions:
        return {
            "action": "set_theme",
            "theme": DEFAULT_SILENT_THEME,
            "reason": "в названии — наш НП (%s) → переносим в тему со слотом"
            % ", ".join(mentions.get(region_id, [])[:2]),
            "confidence": "medium",
        }

    return {
        "action": "decide",
        "theme": None,
        "reason": "в названии нет наших НП — то ли не наша группа, то ли без места",
        "confidence": "low",
    }
