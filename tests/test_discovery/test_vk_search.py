"""Unit tests for modules/discovery/vk_search.py — composite discovery."""

from __future__ import annotations

from unittest.mock import MagicMock

from modules.discovery.vk_search import (
    _LARGE_GROUP_MEMBERS_THRESHOLD,
    CATEGORY_KEYWORDS,
    _build_search_plan,
    _count_localities_in_text,
    _harvest_main_group_link_refs,
    _harvest_repost_owner_ids,
    _make_stem,
    _passes_relevance,
    discover_for_region,
)


def _make_client(search_results=None, enrich_results=None):
    client = MagicMock()
    client.search_groups.side_effect = (
        search_results if isinstance(search_results, list) else (lambda **_: search_results or [])
    )
    client.get_groups_by_ids.return_value = enrich_results or []
    return client


# ───────── _make_stem / _count_localities_in_text ─────────


def test_stem_short_names_kept_as_is():
    assert _make_stem("Уя") == "уя"
    assert _make_stem("Юм") == "юм"


def test_stem_strips_trailing_vowels():
    """Стем отбрасывает конечные гласные до согласной."""
    assert _make_stem("Тужа") == "туж"  # "а" → стоп на "ж"
    assert _make_stem("Шешурга") == "шешург"  # "а" → стоп на "г"
    assert _make_stem("Михайловское") == "михайловск"  # "е", "о" → стоп на "к"
    assert _make_stem("Малмыж") == "малмыж"  # уже на согласной — не трогаем


def test_count_localities_matches_declensions():
    """Стем "туж" должен матчить разные падежи "Тужа"."""
    stems = [_make_stem("Тужа")]
    assert _count_localities_in_text(stems, "Подслушано в Туже") == 1
    assert _count_localities_in_text(stems, "Тужинский район") == 1
    assert _count_localities_in_text(stems, "Тужанские новости") == 1
    assert _count_localities_in_text(stems, "Едем в Тужу за грибами") == 1


def test_count_localities_left_word_boundary_protects_from_false_positive():
    """Левый \\b защищает от "батужа" / "котужа" — не должно матчить "туж"."""
    stems = [_make_stem("Тужа")]
    assert _count_localities_in_text(stems, "Картужа Карловы Вары") == 0


def test_count_localities_multiple_localities_sum():
    stems = [_make_stem("Тужа"), _make_stem("Шешурга")]
    assert _count_localities_in_text(stems, "Новости Тужи и Шешурги") == 2
    assert _count_localities_in_text(stems, "Только Тужа") == 1
    assert _count_localities_in_text(stems, "Ничего по теме") == 0


def test_count_localities_empty_inputs():
    assert _count_localities_in_text([], "Тужа") == 0
    assert _count_localities_in_text(["туж"], "") == 0
    assert _count_localities_in_text(["туж"], None) == 0  # type: ignore[arg-type]


# ───────── _build_search_plan ─────────


def test_search_plan_starts_with_geo_query():
    plan = _build_search_plan(center_city="Малмыж", localities=[], keywords=[])
    assert plan[0] == ("Малмыж", "geo_search", True)


def test_search_plan_localities_before_keywords():
    plan = _build_search_plan(
        center_city="Малмыж",
        localities=["Шешурга", "Михайловское"],
        keywords=["новости"],
    )
    sources = [s for _, s, _ in plan]
    assert sources[0] == "geo_search"
    assert sources[1].startswith("loc:")
    assert sources[2].startswith("loc:")
    assert sources[3].startswith("kw:")


def test_search_plan_localities_have_no_city_id():
    """Локалитет-запросы должны идти БЕЗ city_id — мелкие сельские паблики
    часто помечены городом-центром района, не каждой деревней."""
    plan = _build_search_plan(
        center_city="Малмыж",
        localities=["Шешурга"],
        keywords=[],
    )
    geo = [t for t in plan if t[1] == "geo_search"][0]
    loc = [t for t in plan if t[1].startswith("loc:")][0]
    assert geo[2] is True  # use_city_id
    assert loc[2] is False


def test_search_plan_falls_back_to_category_keywords_when_keywords_empty():
    """Если кастомные keywords не заданы — берём flat-список CATEGORY_KEYWORDS."""
    plan = _build_search_plan(center_city="X", localities=[], keywords=[])
    sources = [s for _, s, _ in plan]
    # 1 geo + N keywords из всех категорий
    expected_kw = sum(len(v) for v in CATEGORY_KEYWORDS.values())
    # CATEGORY_KEYWORDS могут иметь дубликаты между категориями (сейчас нет),
    # но dedup по lower применяется — проверяем нижнюю границу.
    kw_count = sum(1 for s in sources if s.startswith("kw:"))
    assert kw_count <= expected_kw
    assert kw_count >= 1


def test_search_plan_dedup_localities_case_insensitive():
    plan = _build_search_plan(center_city="X", localities=["Тужа", "тужа", "ТУЖА"], keywords=[])
    loc_queries = [q for q, s, _ in plan if s.startswith("loc:")]
    assert len(loc_queries) == 1


def test_search_plan_no_center_city_still_works_with_localities():
    plan = _build_search_plan(center_city="", localities=["Тужа"], keywords=["новости"])
    sources = [s for _, s, _ in plan]
    assert "geo_search" not in sources
    assert any(s.startswith("loc:") for s in sources)


# ───────── discover_for_region ─────────


def test_discover_empty_inputs_returns_empty():
    client = MagicMock()
    assert discover_for_region(client=client, center_city="  ", localities=[]) == []
    client.search_groups.assert_not_called()


def test_discover_calls_search_for_geo_and_each_localitet():
    client = MagicMock()
    client.search_groups.return_value = []
    client.get_groups_by_ids.return_value = []

    discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Шешурга", "Михайловское"],
        keywords=[],
    )
    # 1 geo + 2 localities + N CATEGORY_KEYWORDS fallback
    kw_count = sum(len(v) for v in CATEGORY_KEYWORDS.values())
    assert client.search_groups.call_count >= 1 + 2 + kw_count - 5  # допуск на dedup


def test_discover_passes_city_id_only_for_geo_search():
    """Geo-search использует city_id; локалитеты и keyword — нет."""
    client = MagicMock()
    client.search_groups.return_value = []
    client.get_groups_by_ids.return_value = []

    discover_for_region(
        client=client,
        center_city="Тужа",
        vk_city_id=314,
        localities=["Шешурга"],
        keywords=["новости"],
    )
    calls = client.search_groups.call_args_list
    assert calls[0].kwargs["city_id"] == 314  # geo
    for c in calls[1:]:
        assert c.kwargs["city_id"] is None  # locality + keyword


def test_discover_hard_filter_drops_irrelevant_candidates():
    """Кандидат без localities в name+description отбрасывается, если
    localities были заданы."""
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "Подслушано Тужа"},
        {"id": 2, "name": "Море Парк Киров"},  # нет тужи — отбросить
        {"id": 3, "name": "ЧП Тужинский район"},  # склонение
    ]
    client.get_groups_by_ids.return_value = []

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
    )
    assert sorted(g.vk_id for g in groups) == [1, 3]


def test_discover_without_localities_requires_center_match():
    """Fail-closed: нет списка нп → фильтр НЕ выключается, а требует
    центральный стем.

    Раньше при пустом ``localities`` фильтр пропускался целиком — у 62
    регионов из 81 (список нп не засеян) в очередь модерации попадало
    всё, что вернул поиск (P235, звено 3).
    """
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "Подслушано Тужа"},
        {"id": 2, "name": "Море Парк Киров"},
    ]
    client.get_groups_by_ids.return_value = []

    groups = discover_for_region(client=client, center_city="Тужа", localities=[], keywords=[])
    assert [g.vk_id for g in groups] == [1]


def test_discover_sorts_by_matched_localities_first():
    """Паблик с тремя топонимами района обгонит крупный с одним."""
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "Тужа"},  # центр (score 1)
        {"id": 2, "name": "Тужа и Шешурга и Михайловское"},  # центр + 2 = score 3
        {"id": 3, "name": "Тужа Шешурга"},  # центр + 1 = score 2
    ]
    client.get_groups_by_ids.return_value = [
        {"id": 1, "members_count": 10000},
        {"id": 2, "members_count": 100},
        {"id": 3, "members_count": 500},
    ]

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа", "Шешурга", "Михайловское"],
        keywords=[],
    )
    assert [g.vk_id for g in groups] == [2, 3, 1]


def test_discover_sort_falls_back_to_members_count_at_equal_localities():
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "Тужа one"},
        {"id": 2, "name": "Тужа two"},
    ]
    client.get_groups_by_ids.return_value = [
        {"id": 1, "members_count": 100},
        {"id": 2, "members_count": 9000},
    ]
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
    )
    assert [g.vk_id for g in groups] == [2, 1]


def test_discover_dedup_by_vk_id_first_source_wins():
    """Same group via geo и locality — discovered_via = geo_search."""
    client = MagicMock()

    def search_side_effect(*, query, city_id=None, count=100, offset=0):
        if query == "Тужа":
            return [{"id": 1, "name": "Подслушано Тужа"}]
        return []

    client.search_groups.side_effect = search_side_effect
    client.get_groups_by_ids.return_value = []

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
    )
    assert len(groups) == 1
    assert groups[0].vk_id == 1
    # Первый запрос — geo_search (с city_id), он выигрывает в дедупе.
    assert groups[0].discovered_via == "geo_search"


def test_discover_filters_out_excluded_vk_ids():
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 10, "name": "Already added Тужа"},
        {"id": 20, "name": "New one Тужа"},
    ]
    client.get_groups_by_ids.return_value = []

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
        exclude_vk_ids=[10],
    )
    assert [g.vk_id for g in groups] == [20]


def test_discover_handles_negative_vk_id_in_exclude_via_abs():
    client = MagicMock()
    client.search_groups.return_value = [{"id": 10, "name": "A Тужа"}]
    client.get_groups_by_ids.return_value = []

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
        exclude_vk_ids=[-10],
    )
    assert groups == []


def test_discover_enriches_from_groups_get_by_id_once():
    """get_groups_by_ids зовётся ровно один раз для всех уникальных id."""
    client = MagicMock()
    client.search_groups.side_effect = [
        [{"id": 1, "name": "A Тужа"}, {"id": 2, "name": "B Тужа"}],
        [{"id": 3, "name": "C Тужа"}],
    ]
    client.get_groups_by_ids.return_value = [
        {"id": 1, "name": "A Тужа", "description": "desc-1", "members_count": 5000},
        {"id": 2, "name": "B Тужа", "members_count": 100},
    ]

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=["новости"],  # 1 keyword search
    )
    assert client.get_groups_by_ids.call_count == 1
    ids_arg = client.get_groups_by_ids.call_args[0][0]
    assert sorted(ids_arg) == [1, 2, 3]

    by_id = {g.vk_id: g for g in groups}
    assert by_id[1].description == "desc-1"
    assert by_id[1].members_count == 5000
    assert by_id[2].members_count == 100


def test_discover_skips_items_with_zero_id():
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 0, "name": "Bad Тужа"},
        {"id": 1, "name": "Good Тужа"},
    ]
    client.get_groups_by_ids.return_value = []
    groups = discover_for_region(
        client=client, center_city="Тужа", localities=["Тужа"], keywords=[]
    )
    assert [g.vk_id for g in groups] == [1]


def test_discover_truncates_to_top_n_after_sort():
    """При >max_candidates групп берём топ-N (после relevance + sort)."""
    client = MagicMock()
    items = [{"id": i, "name": f"G{i} Тужа"} for i in range(1, 11)]
    client.search_groups.return_value = items
    client.get_groups_by_ids.return_value = [
        {"id": i, "members_count": i * 100} for i in range(1, 11)
    ]
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
        max_candidates=3,
    )
    assert len(groups) == 3
    # Все имеют по 1 matched_localities, fallback на members_count desc.
    assert [g.vk_id for g in groups] == [10, 9, 8]


def test_discover_does_not_fetch_wall_posts_inline():
    """Sync `discover_for_region` НЕ тянет wall.get — это делает async
    `_ai_categorize_all`."""
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "A Тужа"},
        {"id": 2, "name": "B Тужа"},
    ]
    client.get_groups_by_ids.return_value = []

    discover_for_region(client=client, center_city="Тужа", localities=["Тужа"], keywords=[])
    client.get_wall_posts.assert_not_called()


# ───────── _passes_relevance: новый фильтр ─────────


def test_passes_relevance_center_stem_passes_unconditionally():
    """Центральный стем в name+description — сильный сигнал, пропускаем."""
    passes, score = _passes_relevance(
        text="Подслушано в Туже",
        locality_stems=["шешург", "коробк"],
        center_stem="туж",
        members_count=12345,
    )
    assert passes is True
    assert score == 1  # 0 child + 1 center


def test_passes_relevance_single_child_locality_without_center_fails():
    """Один матч по дочернему омонимному стему (например «лоскут»)
    без центра — мусор, отбрасываем."""
    passes, score = _passes_relevance(
        text="Мир Лоскутов — ткани для лоскутного шитья",
        locality_stems=["лоскут", "шешург", "коробк"],
        center_stem="туж",
        members_count=100,
    )
    assert passes is False
    assert score == 1  # 1 child match, no center


def test_passes_relevance_two_distinct_child_localities_pass_small():
    """≥2 разных дочерних стема в маленькой группе — пропускаем.
    Случайное совпадение пары омонимов в одной группе очень маловероятно."""
    passes, score = _passes_relevance(
        text="Сёла Шешурга и Михайловское",
        locality_stems=["шешург", "михайловск", "коробк"],
        center_stem="туж",
        members_count=200,
    )
    assert passes is True
    assert score == 2


def test_passes_relevance_large_group_without_center_fails_even_with_two_stems():
    """Крупный паблик (>50K members) ловит шум легче — даже 2 омонимных матча
    не считаем достаточными без явного центрального стема."""
    passes, _score = _passes_relevance(
        text="Лоскутки и Соболи в одном магазине",
        locality_stems=["лоскут", "собол"],
        center_stem="туж",
        members_count=_LARGE_GROUP_MEMBERS_THRESHOLD + 1,
    )
    assert passes is False


def test_passes_relevance_large_group_with_center_passes():
    """Крупный паблик с центральным стемом — pass."""
    passes, score = _passes_relevance(
        text="Все Свои Тужинский край — новости района",
        locality_stems=["шешург"],
        center_stem="туж",
        members_count=_LARGE_GROUP_MEMBERS_THRESHOLD + 1,
    )
    assert passes is True
    assert score == 1


def test_passes_relevance_no_center_stem_falls_back_to_two_stems():
    """Если у региона не задан center_city (центр-стем пуст) — нужно ≥2
    разных совпадений по дочерним локалитетам."""
    passes_one, _ = _passes_relevance(
        text="Только Шешурга",
        locality_stems=["шешург", "михайловск"],
        center_stem=None,
        members_count=100,
    )
    assert passes_one is False

    passes_two, _ = _passes_relevance(
        text="Шешурга и Михайловское",
        locality_stems=["шешург", "михайловск"],
        center_stem=None,
        members_count=100,
    )
    assert passes_two is True


def test_discover_drops_large_omonym_traps_without_center():
    """Регрессионный тест на инцидент tuzha 2026-05-25:
    «Чугун на разлив» (40K+) с локалитетом «Чугуны» без «туж» — мусор."""
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "Чугун на разлив 0,5л"},
        {"id": 2, "name": "Васькино.Тужинский район"},
    ]
    client.get_groups_by_ids.return_value = [
        {"id": 1, "members_count": 40961},
        {"id": 2, "members_count": 491},
    ]

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Чугуны", "Васькино"],
        keywords=[],
    )
    assert [g.vk_id for g in groups] == [2]


def test_discover_keeps_small_village_with_two_distinct_locality_matches():
    """Маленький паблик «Шешурга и Михайловское» проходит без центра."""
    client = MagicMock()
    client.search_groups.return_value = [
        {"id": 1, "name": "Сёла Шешурга и Михайловское"},
    ]
    client.get_groups_by_ids.return_value = [
        {"id": 1, "members_count": 150},
    ]

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Шешурга", "Михайловское"],
        keywords=[],
    )
    assert [g.vk_id for g in groups] == [1]


# ───────── ИНФО-страница репосты ─────────


def test_harvest_repost_owner_ids_keeps_only_community_owners_excluding_self():
    """Только сообщества (``owner_id < 0``) собираются как положительные id;
    сам main_group_id исключается; репосты ЛИЧНЫХ профилей (``owner_id >= 0``)
    отбрасываются — иначе личные профили протекают в пул (dead-ведро 050)."""
    client = MagicMock()
    client.get_wall_posts.return_value = [
        {"copy_history": [{"owner_id": -111}, {"owner_id": -222}]},  # 111, 222
        {"copy_history": [{"owner_id": -222}]},  # dup
        {"text": "no copy_history"},
        {"copy_history": [{"owner_id": -42}]},  # сам main_group → исключить
        {"copy_history": [{"owner_id": 333}]},  # ЛИЧНЫЙ профиль → отбросить
        {"copy_history": [{"owner_id": 0}]},  # owner_id 0 (не сообщество) → отбросить
        {"copy_history": [{"owner_id": None}, {"owner_id": "broken"}]},  # шум
    ]
    out = _harvest_repost_owner_ids(client, main_group_id=42)
    assert out == [111, 222]
    # wall.get вызывается с отрицательным owner_id (group convention в VK API).
    client.get_wall_posts.assert_called_once()
    kwargs = client.get_wall_posts.call_args.kwargs
    assert kwargs["owner_id"] == -42


def test_harvest_repost_owner_ids_drops_personal_profile_reposts():
    """Стена, где главная репостит только посты пользователей (положительные
    owner_id) → пустой пул кандидатов (не протекают личные профили)."""
    client = MagicMock()
    client.get_wall_posts.return_value = [
        {"copy_history": [{"owner_id": 555}, {"owner_id": 666}]},  # оба — люди
        {"copy_history": [{"owner_id": 777}]},
    ]
    assert _harvest_repost_owner_ids(client, main_group_id=42) == []


def test_harvest_repost_owner_ids_returns_empty_on_vk_error():
    """Закрытая стена / VK error → пустой результат, без raise.

    Discovery не должен падать целиком из-за info-репостов."""
    client = MagicMock()
    client.get_wall_posts.side_effect = RuntimeError("VK error 15: access denied")
    assert _harvest_repost_owner_ids(client, main_group_id=42) == []


def test_harvest_repost_owner_ids_returns_empty_without_main_group():
    """Регион без vk_group_id — нет источника, без VK-вызова."""
    client = MagicMock()
    assert _harvest_repost_owner_ids(client, main_group_id=0) == []
    client.get_wall_posts.assert_not_called()


def test_discover_picks_info_repost_candidates_first():
    """Если у региона есть vk_group_id — кандидаты из репостов попадают
    в seen первыми, ``discovered_via='info_repost'`` побеждает в дедупе
    над geo_search / loc / kw."""
    client = MagicMock()
    client.get_wall_posts.return_value = [
        {"copy_history": [{"owner_id": -555}]},
    ]
    client.search_groups.return_value = [
        # Та же группа id=555 находится и через VK search — но должна
        # сохранить discovered_via='info_repost' (info-page имеет priority).
        {"id": 555, "name": "Паблик Тужи"},
    ]
    client.get_groups_by_ids.return_value = [
        {"id": 555, "name": "Паблик Тужи", "members_count": 500},
    ]
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        vk_group_id=42,
        localities=["Тужа"],
        keywords=[],
    )
    assert [g.vk_id for g in groups] == [555]
    assert groups[0].discovered_via == "info_repost"


def test_discover_info_repost_results_go_through_relevance_filter():
    """Репосты главной страницы — сильный сигнал, но если группа явно
    нерелевантна (например главная репостит областной паблик «Вести
    Кировской области» с 200K подписчиков и без 'туж' в имени) — она
    отвалится в relevance-фильтре (большая группа без центрального стема)."""
    client = MagicMock()
    client.get_wall_posts.return_value = [
        {"copy_history": [{"owner_id": -111}, {"owner_id": -222}]},
    ]
    client.search_groups.return_value = []
    client.get_groups_by_ids.return_value = [
        {"id": 111, "name": "Васькино.Тужинский район", "members_count": 491},
        {"id": 222, "name": "Вести. Кировская область", "members_count": 200000},
    ]
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        vk_group_id=42,
        localities=["Тужа"],
        keywords=[],
    )
    # 111 проходит (has center stem «туж»), 222 нет (большая без центра)
    assert [g.vk_id for g in groups] == [111]


def test_discover_skips_info_repost_when_no_vk_group_id():
    """Регион без vk_group_id — wall.get не вызывается."""
    client = MagicMock()
    client.search_groups.return_value = []
    client.get_groups_by_ids.return_value = []
    discover_for_region(
        client=client,
        center_city="Тужа",
        vk_group_id=None,
        localities=["Тужа"],
        keywords=[],
    )
    client.get_wall_posts.assert_not_called()


def test_discover_skips_info_repost_for_excluded_vk_ids():
    """Если id из репостов уже в exclude_vk_ids (уже добавлен в communities)
    — не дублируем, просто пропускаем."""
    client = MagicMock()
    client.get_wall_posts.return_value = [
        {"copy_history": [{"owner_id": -111}, {"owner_id": -222}]},
    ]
    client.search_groups.return_value = []
    client.get_groups_by_ids.return_value = [
        {"id": 222, "name": "Тужа район", "members_count": 100},
    ]
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        vk_group_id=42,
        localities=["Тужа"],
        keywords=[],
        exclude_vk_ids=[111],  # 111 уже добавлен
    )
    assert [g.vk_id for g in groups] == [222]


# ───────── ИНФО-страница: блок «Ссылки» (info_links, портировано 2026-05-31) ─────────


def test_harvest_main_group_link_refs_parses_and_filters():
    """Из блока «Ссылки» забираем screen_name'ы сообществ; ``wall``/``id``
    (посты и личные профили) и внешние домены отбрасываем; dedup."""
    client = MagicMock()
    client.get_groups_by_ids.return_value = [
        {
            "links": [
                {"url": "https://vk.com/tuzha_sport"},
                {"url": "https://vk.com/club12345"},
                {"url": "https://vk.com/tuzha_sport"},  # dup
                {"url": "https://vk.com/wall-1_2"},  # пост → исключить
                {"url": "https://vk.com/id777"},  # личный профиль → исключить
                {"url": "https://example.com/external"},  # не vk → no match
                {"url": ""},  # пусто
            ]
        }
    ]
    refs = _harvest_main_group_link_refs(client, main_group_id=42)
    assert refs == ["club12345", "tuzha_sport"]
    # links-блок тянется через groups.getById с fields=links
    assert client.get_groups_by_ids.call_args.kwargs["fields"] == "links"


def test_harvest_main_group_link_refs_empty_on_error_or_no_group():
    client = MagicMock()
    client.get_groups_by_ids.side_effect = RuntimeError("VK error")
    assert _harvest_main_group_link_refs(client, main_group_id=42) == []

    client2 = MagicMock()
    assert _harvest_main_group_link_refs(client2, main_group_id=0) == []
    client2.get_groups_by_ids.assert_not_called()


def test_discover_info_links_adds_candidates_from_links_block():
    """Блок «Ссылки» главной → кандидаты с ``discovered_via='info_links'``."""
    client = MagicMock()
    client.get_wall_posts.return_value = []  # нет репостов
    client.get_groups_by_ids.side_effect = [
        # Step 0b: links-блок главной
        [
            {
                "links": [
                    {"url": "https://vk.com/tuzha_sport"},
                    {"url": "https://vk.com/wall-1_2"},  # исключить
                ]
            }
        ],
        # Step 4: enrichment (кандидат уже обогащён через refs)
        [],
    ]
    client.get_groups_by_refs.return_value = [
        {"id": 777, "name": "Спорт Тужа", "members_count": 300, "screen_name": "tuzha_sport"},
    ]
    client.search_groups.return_value = []

    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        vk_group_id=42,
        localities=["Тужа"],
        keywords=[],
    )
    assert [g.vk_id for g in groups] == [777]
    assert groups[0].discovered_via == "info_links"
    refs_arg = client.get_groups_by_refs.call_args[0][0]
    assert "tuzha_sport" in refs_arg
    assert not any(r.startswith("wall") or r.startswith("id") for r in refs_arg)


def test_discover_source_priority_repost_then_links_then_search():
    """Дедуп-приоритет: info_repost > info_links > search."""
    client = MagicMock()
    client.get_wall_posts.return_value = [{"copy_history": [{"owner_id": -100}]}]  # repost 100
    client.get_groups_by_ids.side_effect = [
        # Step 0b links: 100 (дубль репоста) + 200
        [{"links": [{"url": "https://vk.com/club100"}, {"url": "https://vk.com/club200"}]}],
        # Step 4 enrichment для всех id
        [
            {"id": 100, "name": "Тужа A", "members_count": 100},
            {"id": 200, "name": "Тужа B", "members_count": 100},
            {"id": 300, "name": "Тужа C", "members_count": 100},
        ],
    ]
    client.get_groups_by_refs.return_value = [
        {"id": 100, "name": "Тужа A", "members_count": 100},  # дубль репоста
        {"id": 200, "name": "Тужа B", "members_count": 100},
    ]
    # search находит 200 (дубль links) и 300 (новый)
    client.search_groups.return_value = [
        {"id": 200, "name": "Тужа B"},
        {"id": 300, "name": "Тужа C"},
    ]
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        vk_group_id=42,
        localities=["Тужа"],
        keywords=[],
    )
    via = {g.vk_id: g.discovered_via for g in groups}
    assert via[100] == "info_repost"  # репост побеждает блок «Ссылки»
    assert via[200] == "info_links"  # блок «Ссылки» побеждает search
    assert via[300] == "geo_search"  # только из search


def test_discover_skips_info_links_when_no_vk_group_id():
    client = MagicMock()
    client.search_groups.return_value = []
    client.get_groups_by_ids.return_value = []
    discover_for_region(
        client=client, center_city="Тужа", vk_group_id=None, localities=["Тужа"], keywords=[]
    )
    client.get_groups_by_refs.assert_not_called()
