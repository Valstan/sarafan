"""Composite VK groups discovery for a region.

Strategy (2026-05-25 rework: localities-driven, см. PR feat/discovery-localities-relevance):

0. **Info-page reposts** (2026-05-26) — если у региона задан ``vk_group_id``,
   читаем ``wall.get(count=100)`` главной ИНФО-группы района и собираем
   уникальные ``copy_history.owner_id``. Это самый качественный источник —
   оператор главной страницы сам выбрал кого репостить (минимум
   false-positive). ``discovered_via='info_repost'``.
0b. **Info-page links** (2026-05-31, портировано из скила discover_scan) — блок
   «Ссылки» главной ИНФО-страницы (``groups.getById fields=links``). Оператор
   часто прямо линкует партнёрские/сельские/официальные паблики — высокоточный
   курируемый источник, который ``groups.search`` пропускает. screen_name'ы
   резолвятся через ``groups.getById``. ``discovered_via='info_links'``.
1. **Geo search** — ``groups.search(q="<center_city>", city_id=<vk_city_id>)``.
2. **Localities search** — для каждого нп из ``region.config['localities']``
   шлём ``groups.search(q="<нп>")`` без city_id. Это даёт мелкие районные
   паблики, которые VK не привязывает к городу-центру.
3. **Keyword search** — для каждого ключевика из ``region.config['discovery_keywords']``
   (или fallback ``CATEGORY_KEYWORDS``) шлём ``groups.search(q="<center_city> <keyword>")``.
4. **Dedup** — по ``id``. ``discovered_via`` фиксирует первый источник
   (info_repost имеет приоритет — он шаг 0).
5. **Enrichment** — один ``groups.getById(group_ids=…, fields=…)`` для всех
   уникальных id (в fields — ещё и ``city``: VK-город группы, по которому
   area-guard определяет «своя область или чужая»).
6. **Area guard** — группы, чей VK-город лежит в **чужой области**, выбрасываются
   (см. ``_drop_foreign_area``). Это то, чего не делает ``city_id`` в запросе:
   ``groups.search(q="Москва")`` без ``city_id`` возвращает московские паблики,
   и никакой фильтр по названию их не отличит (см. P235, звено 2).
7. **Hard relevance filter** — многокомпонентный (см. ``_passes_relevance``):
   центральный стем (``Тужа``→``туж``) пропускает безусловно; одного матча
   по дочернему локалитету недостаточно (омонимные стемы «Коробки»/«Соболи»/
   «Лоскуты»/«Чугуны»/«Фомино» дают много false-positive — инцидент tuzha
   2026-05-25); крупные группы (>50k members) требуют строго центральный
   стем. Это режет 90%+ мусора без AI. **Fail-closed:** если у региона нет
   списка нп, фильтр не выключается, а требует центральный стем (иначе у 62
   регионов из 81 в очередь попадало всё подряд — P235, звено 3).
8. **Sort** — ``(matched_localities_count desc, members_count desc)`` —
   паблик с тремя топонимами района обгонит «Море Парк Киров» с 90k подписчиками.

Recent posts (``wall.get`` per group) тянет уже async-обвязка
``tasks.discovery_tasks._ai_categorize_all``.

Возвращает список ``DiscoveredGroup`` — готовые к AI-категоризации и
upsert'у в ``community_candidates``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from modules.vk_monitor.vk_client import VKClient

logger = logging.getLogger(__name__)

# Fallback-ключевики на категорию, когда `region.config['discovery_keywords']`
# пуст. На вход VK groups.search идёт `<center_city> <kw>`.
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "novost": ["новости", "инфо", "вести"],
    "reklama": ["объявления", "доска", "барахолка", "куплю продам"],
    "sosed": ["соседи", "ДТП", "происшествия"],
    "kultura": ["культура", "афиша", "дом культуры"],
    "sport": ["спорт", "фитнес"],
    "admin": ["администрация", "район", "город"],
    "detsad": ["детский сад", "школа", "родители"],
    # Расширенная областная повестка (community-mode oblast). Ключевики
    # уникальны между категориями (dedup в _build_search_plan их бы схлопнул).
    "proisshestviya": ["ЧП", "авария", "криминал"],
    "molodezh": ["молодёжь", "студенты", "волонтёры"],
    "nauka": ["наука", "университет", "образование"],
    "promyshlennost": ["промышленность", "бизнес", "экономика"],
    "selhoz": ["сельское хозяйство", "АПК", "фермер"],
    "zdorovie": ["здоровье", "медицина", "больница"],
    "zhkh": ["ЖКХ", "благоустройство", "капремонт"],
    "priroda": ["природа", "туризм", "краеведение"],
}

# Какие fields'ы запрашиваем у groups.getById для enrichment.
# ``city`` — VK-город группы (нужен area-guard'у: чужой город → чужая область).
_ENRICH_FIELDS = "description,members_count,activity,status,screen_name,photo_200,city"

# Минимальная длина корня после стема (защита от слишком коротких корней
# вроде «У» из «Уя» — они дают много false-positive).
_STEM_MIN_LEN = 3

# Гласные русского алфавита — отбрасываем конечные гласные для упрощённого
# стема. Покрывает большинство склонений: «Тужа» → «туж», «Шешурга» →
# «шешург», «Михайловское» → «михайловск», «Тужи»/«Тужу»/«Туже» → «туж».
_RU_VOWELS = "аеёиоуыэюя"

# Порог «большой группы» — если у кандидата members_count > этого значения,
# требуем обязательного матча центрального стема (Тужа/Тужинск/…), даже если
# у группы 2+ распылённых матча по дочерним локалитетам. Большие паблики чаще
# случайно цепляются за омонимные стемы (Коробки → «коробка», Лоскуты →
# «лоскутное шитьё», Соболи → «Соболиная гора»), у них больше шанса набрать
# несколько false-positive в длинном описании. См. инцидент 2026-05-25 на
# tuzha: 1787/3784 ложно-релевантных, из них 95% — большие тематические.
_LARGE_GROUP_MEMBERS_THRESHOLD = 50_000


@dataclass
class DiscoveredGroup:
    """Snapshot VK-группы на момент discovery — то, что попадёт в
    ``community_candidates``."""

    vk_id: int
    name: str
    screen_name: Optional[str] = None
    photo_url: Optional[str] = None
    description: Optional[str] = None
    members_count: Optional[int] = None
    discovered_via: str = ""
    # Последние посты (текст) — для ai_categorizer'а; не хранится в БД.
    recent_posts: List[str] = field(default_factory=list)
    # Сколько локалитетов района нашлось в name+description. Используется
    # для сортировки и debug-логирования; в БД не пишется.
    matched_localities: int = 0
    # VK-город группы (id + название). Приходит из ``groups.getById`` fields=city.
    # Используется area-guard'ом: город → область → «наша область или нет».
    city_id: Optional[int] = None
    city_title: Optional[str] = None


def _extract_city(item: Dict) -> tuple[Optional[int], Optional[str]]:
    """Достать (city_id, city_title) из ответа VK.

    Формат менялся: у групп есть и объект ``city`` ({id, title}), и плоские
    ``city_id``/``city_title``. Берём оба — чтобы не зависеть от версии API.
    """
    city = item.get("city")
    city_id: Optional[int] = None
    city_title: Optional[str] = None
    if isinstance(city, dict):
        try:
            city_id = int(city.get("id") or 0) or None
        except (TypeError, ValueError):
            city_id = None
        city_title = (city.get("title") or "").strip() or None
    if not city_id:
        try:
            city_id = int(item.get("city_id") or 0) or None
        except (TypeError, ValueError):
            city_id = None
    if not city_title:
        city_title = (item.get("city_title") or "").strip() or None
    return city_id, city_title


def _normalize_search_item(item: Dict) -> DiscoveredGroup:
    """Convert raw groups.search/groups.getById item to DiscoveredGroup."""
    city_id, city_title = _extract_city(item)
    return DiscoveredGroup(
        vk_id=int(item.get("id") or 0),
        name=(item.get("name") or "").strip(),
        screen_name=item.get("screen_name") or None,
        photo_url=item.get("photo_200") or None,
        description=(item.get("description") or "").strip() or None,
        members_count=item.get("members_count"),
        city_id=city_id,
        city_title=city_title,
    )


def _make_stem(name: str) -> str:
    """Стем для substring-матчинга по корню русского топонима.

    Не настоящий морфологический стем (нет pymorphy2 в зависимостях) —
    наивное отбрасывание конечных гласных. Покрывает основные русские
    склонения и прилагательные:

    - «Тужа» → «туж» (матчит «Тужа», «Тужи», «Тужу», «Тужинский»);
    - «Шешурга» → «шешург»;
    - «Михайловское» → «михайловск»;
    - «Уя» → «уя» (короткое — не трогаем).

    Конечные согласные не отбрасываем (для «Малмыж» оставляем «малмыж» —
    он сам по себе матчит «Малмыжский», «Малмыже» как substring).
    """
    s = (name or "").strip().lower()
    while len(s) > _STEM_MIN_LEN and s[-1] in _RU_VOWELS:
        s = s[:-1]
    return s


def _count_localities_in_text(stems: List[str], text: str) -> int:
    """Считает, сколько локалитетов района встречается в тексте.

    Использует regex с word-boundary слева (`\\b<стем>`), чтобы не матчить
    «батужа» при стеме «туж». Справа word-boundary нет — иначе пропустим
    склонения («Тужинский», «Тужанский»).
    """
    if not stems or not text:
        return 0
    haystack = text.lower()
    count = 0
    for st in stems:
        if not st:
            continue
        # Левый word-boundary через regex (учитываем кириллицу через \w).
        pattern = r"\b" + re.escape(st)
        if re.search(pattern, haystack):
            count += 1
    return count


def _has_stem(text: str, stem: Optional[str]) -> bool:
    """True если ``stem`` встречается в ``text`` с левой word-boundary."""
    if not stem or not text:
        return False
    return bool(re.search(r"\b" + re.escape(stem), text.lower()))


def _passes_relevance(
    *,
    text: str,
    locality_stems: List[str],
    center_stem: Optional[str],
    members_count: Optional[int],
) -> tuple[bool, int]:
    """Решает, проходит ли кандидат relevance-фильтр.

    Правила (выработаны после инцидента tuzha 2026-05-25):

    * Центральный стем (``Тужа``→``туж``) — сильный, специфичный сигнал.
      Если он есть в name+description — пропускаем независимо от размера.
    * Дочерние локалитеты часто омонимны общим словам (Коробки/Соболи/
      Лоскуты/Чугуны/Фомино). Один такой матч сам по себе не достаточен.
    * Маленькие/средние группы пропускаем при ≥2 разных дочерних стемах —
      случайное совпадение пары омонимов в одной группе очень маловероятно.
    * Большие группы (>``_LARGE_GROUP_MEMBERS_THRESHOLD``) требуют **строго**
      центральный стем. Длинные описания крупных тематических пабликов часто
      случайно собирают несколько омонимов — этот класс ловит ~95% мусора.

    Возвращает ``(passes, score)``, где ``score`` идёт в ``matched_localities``
    для последующей сортировки (центр считается +1).
    """
    matched_locs = _count_localities_in_text(locality_stems, text)
    has_center = _has_stem(text, center_stem)
    score = matched_locs + (1 if has_center else 0)

    if has_center:
        return True, score

    is_large = bool(members_count and members_count > _LARGE_GROUP_MEMBERS_THRESHOLD)
    if is_large:
        # Без центрального стема большая группа считается мусором.
        return False, score

    # Маленькая/средняя: ≥2 разных дочерних — достаточно.
    return matched_locs >= 2, score


def _build_search_plan(
    *,
    center_city: str,
    localities: Sequence[str],
    keywords: Sequence[str],
) -> List[tuple[str, str, bool]]:
    """Return list of (query, source_label, use_city_id) tuples.

    Порядок (важен для discovered_via — первый источник побеждает в дедупе):
    1. geo_search — `<center_city>` с city_id (если есть).
    2. loc:<нп> — каждый локалитет района, без city_id.
    3. kw:<keyword> — `<center_city> <keyword>`, без city_id.
       Если keywords пуст — берётся flat-список из CATEGORY_KEYWORDS.
    """
    plan: List[tuple[str, str, bool]] = []
    if center_city:
        plan.append((center_city, "geo_search", True))

    seen_loc: set[str] = set()
    for loc in localities:
        q = (loc or "").strip()
        if not q or q.lower() in seen_loc:
            continue
        seen_loc.add(q.lower())
        plan.append((q, f"loc:{q}", False))

    if keywords:
        kw_source = list(keywords)
    else:
        kw_source = [kw for kws in CATEGORY_KEYWORDS.values() for kw in kws]

    seen_kw: set[str] = set()
    for kw in kw_source:
        kw_norm = (kw or "").strip()
        if not kw_norm or kw_norm.lower() in seen_kw:
            continue
        seen_kw.add(kw_norm.lower())
        q = f"{center_city} {kw_norm}".strip() if center_city else kw_norm
        plan.append((q, f"kw:{kw_norm}", False))

    return plan


DEFAULT_MAX_CANDIDATES = 150


def _resolve_own_area(client: VKClient, own_area_city_id: Optional[int]) -> Optional[str]:
    """Область нашего региона — по VK-id города области (или города-центра).

    Возвращает название области как его знает VK («Кировская область»,
    «Татарстан», «Москва») либо ``None``, если определить не удалось.
    ``None`` означает «guard выключен» — честнее, чем угадывать область и
    выбрасывать половину сети.
    """
    if not own_area_city_id:
        return None
    try:
        cities = client.get_cities_by_ids([int(own_area_city_id)])
    except Exception as e:  # extra safety — клиент не должен ронять discovery
        logger.warning("discovery: own area lookup failed (city_id=%s): %s", own_area_city_id, e)
        return None
    if not isinstance(cities, dict):
        return None
    info = cities.get(int(own_area_city_id)) or {}
    area = (info.get("area") or "").strip()
    return area or None


def _drop_foreign_area(
    groups: Sequence[DiscoveredGroup],
    *,
    client: VKClient,
    own_area: str,
) -> tuple[List[DiscoveredGroup], List[DiscoveredGroup]]:
    """Разделить кандидатов на «наша область» и «чужая область».

    Логика: у каждой группы VK отдаёт город (``city``); ``database.getCities``
    отдаёт для города его область. Если область группы известна и **не совпадает**
    с областью региона — группа чужая, в очередь модерации она не идёт.

    Чего этот guard НЕ делает:

    * не судит о группе, у которой город неизвестен (``city`` пуст или VK не знает
      такой город) — такие остаются: «не знаю» ≠ «чужая»;
    * не ограничивает область внутри области (соседний район той же области
      проходит) — это задача relevance-фильтра по названиям нп.

    ``groups.search`` без ``city_id`` отдаёт группы со всей страны, и по имени
    их не отличить: «Москва», «Казань», «Скородум» — одинаково выглядят для
    фильтра, но это разные области (P235, звено 2).
    """
    city_ids = sorted({g.city_id for g in groups if g.city_id})
    if not city_ids:
        return list(groups), []

    try:
        cities = client.get_cities_by_ids(city_ids)
    except Exception as e:  # extra safety
        logger.warning("discovery: area lookup failed for %d cities: %s", len(city_ids), e)
        return list(groups), []
    if not isinstance(cities, dict):
        return list(groups), []

    own = (own_area or "").strip().lower()
    kept: List[DiscoveredGroup] = []
    dropped: List[DiscoveredGroup] = []
    for g in groups:
        area = ""
        if g.city_id:
            area = ((cities.get(g.city_id) or {}).get("area") or "").strip()
        if area and area.lower() != own:
            dropped.append(g)
        else:
            kept.append(g)
    return kept, dropped


# Сколько последних постов главной ИНФО-страницы района читаем для сбора
# `copy_history.owner_id` (репостов). 100 покрывает 3-6 месяцев активной
# страницы, дальше уже маловероятно найти новых партнёров. Один запрос —
# дёшево по VK quota и rate-limit.
DEFAULT_INFO_REPOST_POSTS = 100


def _harvest_repost_owner_ids(
    client: VKClient,
    *,
    main_group_id: int,
    posts_count: int = DEFAULT_INFO_REPOST_POSTS,
) -> List[int]:
    """Собирает уникальные ``copy_history.owner_id`` со стены главной
    ИНФО-группы района.

    Это сильный сигнал «эта группа уже партнёр района» — оператор главной
    страницы сам выбрал её репостить. Сюда же могут попасть областные
    паблики (если главная страница их репостит) — отсев пойдёт через
    общий relevance-фильтр в ``discover_for_region``.

    Args:
        client: VKClient.
        main_group_id: positive id главной группы района (Region.vk_group_id
            хранится как положительный; знак минус для wall.get добавляем тут).
        posts_count: сколько последних постов читать (default 100).

    Returns:
        Список уникальных положительных vk_id **сообществ**. Сам
        ``main_group_id`` исключён. Репосты постов ЛИЧНЫХ профилей
        (``owner_id >= 0``) отбрасываются — см. ниже. Пустой список при
        любой ошибке (закрытая стена / VK API error).
    """
    if not main_group_id:
        return []
    try:
        posts = client.get_wall_posts(owner_id=-abs(int(main_group_id)), count=posts_count)
    except Exception as e:
        logger.warning("discovery: info_repost wall.get failed for %s: %s", main_group_id, e)
        return []

    own_id = abs(int(main_group_id))
    seen_ids: set[int] = set()
    for post in posts or []:
        for ch in post.get("copy_history") or []:
            try:
                owner = int(ch.get("owner_id"))
            except (TypeError, ValueError):
                continue
            # VK copy_history.owner_id: отрицательный = сообщество,
            # положительный = ЛИЧНЫЙ профиль пользователя. Репост поста
            # пользователя НЕ добавляем в community-пул — иначе личные профили
            # протекают в communities (dead-ведро миграции 050 состояло в
            # основном из них, brain 2026-06-30). Совпадает с
            # discover_scan.py::extract_repost_owner_ids.
            if owner >= 0:
                continue
            gid = abs(owner)
            if gid == own_id:
                continue
            seen_ids.add(gid)
    return sorted(seen_ids)


def _harvest_main_group_link_refs(
    client: VKClient,
    *,
    main_group_id: int,
) -> List[str]:
    """Собирает group-refs (screen_name) из блока «Ссылки» главной ИНФО-группы.

    Высокоточный курируемый источник: оператор главной страницы района часто
    прямо линкует партнёрские/сельские/официальные паблики в блоке «Ссылки»
    (так пул обычно и собирался вручную). Эмпирика Малмыжа (скил discover_scan,
    2026-05): блок даёт точные хиты, которые ``groups.search`` пропускает.

    Возвращает refs (screen_name'ы) для резолва через
    :meth:`VKClient.get_groups_by_refs`. ``wall``/``id`` (посты и личные профили)
    отбрасываются. Пустой список при ошибке или отсутствии блока.
    """
    if not main_group_id:
        return []
    try:
        groups = client.get_groups_by_ids([abs(int(main_group_id))], fields="links")
    except Exception as e:
        logger.warning("discovery: info_links getById failed for %s: %s", main_group_id, e)
        return []
    refs: set[str] = set()
    for g in groups or []:
        for ln in g.get("links") or []:
            m = re.search(r"vk\.com/([A-Za-z0-9_]+)", (ln.get("url") or ""))
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith("wall") or ref.startswith("id"):
                continue
            refs.add(ref)
    return sorted(refs)


def discover_for_region(
    *,
    client: VKClient,
    center_city: str,
    vk_city_id: Optional[int] = None,
    vk_group_id: Optional[int] = None,
    localities: Optional[Sequence[str]] = None,
    keywords: Optional[Sequence[str]] = None,
    per_query_count: int = 100,
    exclude_vk_ids: Optional[Sequence[int]] = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    info_repost_posts: int = DEFAULT_INFO_REPOST_POSTS,
    own_area_city_id: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> List[DiscoveredGroup]:
    """Run composite discovery.

    Args:
        client: VKClient (already initialised with a parse-token).
        center_city: human-readable name of the region centre, e.g. "Малмыж".
            Используется для geo-search и как префикс для keyword-запросов.
        vk_city_id: optional numeric VK city_id. If given, geo-search uses it.
            Локалитеты и keyword-search всегда без city_id (мелкие районные
            паблики часто помечены городом-центром района, не каждым нп).
        vk_group_id: optional positive id главной ИНФО-группы района. Если
            задан — читаем её последние посты и собираем ``copy_history.owner_id``
            как дополнительный источник кандидатов (``discovered_via='info_repost'``).
            Сильный сигнал «партнёр района» с минимумом false-positive.
        localities: список нп района (Тужа, Шешурга, Михайловское, ...).
            Используются и как поисковые запросы, и как **жёсткий фильтр**
            релевантности — кандидат без ни одного нп в name/description
            отбрасывается.
        keywords: кастомные тематические ключевики из ``region.config``.
            Если пусто — fallback на flat-список ``CATEGORY_KEYWORDS``.
        per_query_count: how many results to ask VK per call.
        exclude_vk_ids: positive ids (уже добавленные communities + rejected).
        max_candidates: top-N после relevance-фильтра и сортировки.
        info_repost_posts: сколько последних постов главной группы читать
            для harvest'а repost-кандидатов. Default 100.
        own_area_city_id: VK-id города, по которому определяется **область**
            региона (город области — у района родитель-область; иначе город-центр
            района). По ней работает area-guard: группа из чужой области в
            очередь модерации не попадает. Нет id (или VK не знает город) —
            guard выключен, и это видно в счётчиках.
        stats: непустой dict — сюда пишутся счётчики (``found_total``, ``dropped_foreign_area``,
            ``dropped_irrelevant``, ``area_guard_off`` …). Нужен, чтобы отчёт
            прогона показывал, **сколько** отсеяно и **почему**, а не только
            «сколько осталось».

    Returns:
        Список ``DiscoveredGroup``, отсортированный по
        ``(curated first, matched_localities desc, members_count desc)``.
    """
    if not (center_city or "").strip() and not (localities or []) and not vk_group_id:
        return []

    loc_list = [loc for loc in (localities or []) if (loc or "").strip()]
    kw_list = [kw for kw in (keywords or []) if (kw or "").strip()]
    plan = _build_search_plan(
        center_city=center_city or "",
        localities=loc_list,
        keywords=kw_list,
    )
    exclude_set = {abs(int(v)) for v in (exclude_vk_ids or [])}
    counters: Dict[str, int] = stats if stats is not None else {}

    seen: Dict[int, DiscoveredGroup] = {}

    # Step 0: ИНФО-страница репосты. Самый качественный источник — оператор
    # главной группы района сам выбрал кого репостить. Делаем его первым,
    # чтобы при дедупе ``discovered_via`` оставался ``info_repost``
    # (информативнее чем geo_search / kw:новости).
    if vk_group_id:
        repost_ids = _harvest_repost_owner_ids(
            client, main_group_id=int(vk_group_id), posts_count=info_repost_posts
        )
        if repost_ids:
            logger.info("discovery: info_repost — собрано %d уникальных vk_id", len(repost_ids))
        for gid in repost_ids:
            if gid in seen or gid in exclude_set:
                continue
            # Полные metadata (name, description, members_count) заберём
            # в Step 4 общим вызовом get_groups_by_ids.
            seen[gid] = DiscoveredGroup(vk_id=gid, name="", discovered_via="info_repost")

    # Step 0b: блок «Ссылки» главной ИНФО-страницы. Тоже курируемый оператором
    # источник (партнёрские паблики, собранные в блок «Ссылки» вручную), но
    # слабее репостов в дедупе — добавляем вторым. Резолвим screen_name'ы сразу
    # с metadata (Step 4 их потом перепроверит общим вызовом — идемпотентно).
    if vk_group_id:
        link_refs = _harvest_main_group_link_refs(client, main_group_id=int(vk_group_id))
        if link_refs:
            try:
                resolved = client.get_groups_by_refs(link_refs, fields=_ENRICH_FIELDS)
            except Exception as e:
                logger.warning("discovery: info_links resolve failed: %s", e)
                resolved = []
            added = 0
            for it in resolved:
                gid = int(it.get("id") or 0)
                if not gid or gid in seen or gid in exclude_set:
                    continue
                g = _normalize_search_item(it)
                g.discovered_via = "info_links"
                seen[gid] = g
                added += 1
            if added:
                logger.info("discovery: info_links — %d новых сообществ из блока «Ссылки»", added)

    # Step 1+2+3: search.
    for query, source, use_city_id in plan:
        try:
            city_id_for_call = vk_city_id if use_city_id else None
            items = client.search_groups(
                query=query,
                city_id=city_id_for_call,
                count=per_query_count,
            )
        except Exception as e:  # extra safety
            logger.warning("discovery: search_groups failed for %r: %s", query, e)
            continue
        for it in items:
            gid = int(it.get("id") or 0)
            if not gid or gid in seen or gid in exclude_set:
                continue
            g = _normalize_search_item(it)
            g.discovered_via = source
            seen[gid] = g

    if not seen:
        return []

    # Step 4: enrichment one-shot.
    ids = list(seen.keys())
    try:
        enriched = client.get_groups_by_ids(ids, fields=_ENRICH_FIELDS)
    except Exception as e:
        logger.warning("discovery: enrichment failed: %s", e)
        enriched = []

    for it in enriched:
        gid = int(it.get("id") or 0)
        g = seen.get(gid)
        if g is None:
            continue
        if it.get("name"):
            g.name = (it["name"] or "").strip() or g.name
        if it.get("screen_name"):
            g.screen_name = it["screen_name"]
        if it.get("photo_200"):
            g.photo_url = it["photo_200"]
        if it.get("description"):
            g.description = (it["description"] or "").strip() or g.description
        if it.get("members_count") is not None:
            g.members_count = it["members_count"]
        city_id, city_title = _extract_city(it)
        if city_id:
            g.city_id = city_id
        if city_title:
            g.city_title = city_title

    groups = list(seen.values())
    counters["found_total"] = len(groups)

    # Step 5.5: area guard — города из чужих областей отсекаются.
    own_area = _resolve_own_area(client, own_area_city_id)
    if own_area:
        groups, foreign = _drop_foreign_area(groups, client=client, own_area=own_area)
        counters["dropped_foreign_area"] = len(foreign)
        if foreign:
            logger.info(
                "discovery: area guard — %s кандидатов из чужой области (наша область %r): %s",
                len(foreign),
                own_area,
                ", ".join(f"{g.name!r}({g.city_title or '—'})" for g in foreign[:5]),
            )
    else:
        counters["area_guard_off"] = 1
        logger.warning(
            "discovery: area guard выключен — область региона не определена "
            "(own_area_city_id=%s); группы из чужих областей не отсекаются",
            own_area_city_id,
        )

    # Step 6: hard relevance filter + matched-count для сортировки.
    # Fail-closed: фильтр применяется ВСЕГДА. Если у региона нет списка нп —
    # требование сужается до центрального стема (иначе у 62 регионов из 81
    # фильтр был выключен целиком и в очередь попадало всё подряд — P235,
    # звено 3). Курируемые источники фильтр проходят наравне: репост
    # областного паблика главной страницей района — не наш паблик (тест
    # ``test_discover_info_repost_results_go_through_relevance_filter``).
    center_stem = _make_stem(center_city) if center_city else None
    # Из дочерних стемов исключаем сам центр, чтобы не «двоить» вес.
    loc_stems = [st for st in (_make_stem(loc) for loc in loc_list) if st and st != center_stem]
    before = len(groups)
    kept: List[DiscoveredGroup] = []
    for g in groups:
        text = " ".join(filter(None, [g.name, g.description])).strip()
        passes, score = _passes_relevance(
            text=text,
            locality_stems=loc_stems,
            center_stem=center_stem,
            members_count=g.members_count,
        )
        g.matched_localities = score
        if passes:
            kept.append(g)
    groups = kept
    counters["dropped_irrelevant"] = before - len(groups)
    logger.info(
        "discovery: relevance filter — %s/%s candidates kept "
        "(center=%r, localities=%d, large_threshold=%d)",
        len(groups),
        before,
        center_stem,
        len(loc_stems),
        _LARGE_GROUP_MEMBERS_THRESHOLD,
    )

    if not groups:
        return []

    # Step 7: sort. Сначала по matched_localities, потом по members_count
    # (информативный сигнал — мёртвые мелкие паблики модератору не интересны).
    groups.sort(
        key=lambda g: (g.matched_localities, g.members_count or 0),
        reverse=True,
    )

    if len(groups) > max_candidates:
        logger.info(
            "discovery: %s candidates → truncated to top-%s",
            len(groups),
            max_candidates,
        )
        groups = groups[:max_candidates]

    return groups
