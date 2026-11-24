"""Тесты разбора доноров: общие группы и молчащие доноры (P236 / P235).

**Что проверяем.** Модуль ``modules/donor_triage.py`` решает, что можно решить
автоматически (по названию группы и нашим населённым пунктам), а что должно
остаться за человеком. Проверяются именно **правила и их границы**:

* совпадение по городу (старое правило P236);
* совпадение по локалитету из названия — с учётом падежа («Елабужского района»
  → «Елабуга»);
* группа, которая честно охватывает несколько наших районов → остаётся везде;
* история как последний довод и честный отказ, когда доводов нет;
* молчащий донор: ``dormant`` → выключить, местный → перенести в тему со
  слотом, остальное → решение человека.

Тесты чистые: VK и БД не трогаются — вся работа идёт через чистые функции.
"""

from __future__ import annotations

from typing import Any

from modules import donor_triage


def _row(
    *,
    id_: int,
    vk_id: int,
    region_id: int,
    category: str = "novost",
    name: str = "Группа",
    posts_count: int = 0,
    members: int | None = None,
    health: str = "active",
    suggested: str = "",
    created_at: str = "2026-10-02 10:00:00",
    last_post_at: str = "2026-11-20 10:00:00",
) -> Any:
    row = type("Row", (), {})()
    row.id = id_
    row.vk_id = vk_id
    row.region_id = region_id
    row.category = category
    row.name = name
    row.posts_count = posts_count
    row.members_count = members
    row.health_status = health
    row.suggested_category = suggested
    row.created_at = created_at
    row.last_post_at = last_post_at
    row.is_active = True
    row.disabled_at = None
    row.disabled_reason = None
    return row


def _region(id_: int, code: str, name: str, vk_group_id: int | None = None) -> Any:
    region = type("Region", (), {})()
    region.id = id_
    region.code = code
    region.name = name
    region.vk_group_id = vk_group_id
    region.is_active = True
    return region


def _index() -> donor_triage.LocalityIndex:
    """Индекс наших НП: три региона с характерными названиями."""
    index = donor_triage.LocalityIndex()
    index.add_region(1, "ЕЛАБУГА - ИНФО")
    index.add_locality(1, "Елабуга")
    index.add_locality(1, "Танайка")
    index.add_region(2, "АРСК - ИНФО")
    index.add_locality(2, "Арск")
    index.add_locality(2, "Шушмабаш")
    index.add_region(3, "ТУЖА, Кировская область")
    index.add_locality(3, "Тужа")
    index.add_locality(3, "Вынур")
    return index


# ---------------------------------------------------------------------------
# текст и ключи
# ---------------------------------------------------------------------------


def test_norm_text_strips_punctuation_and_folds_yo():
    assert donor_triage.norm_text("Зуевка-Фалёнки!") == "зуевка фаленки"
    assert donor_triage.norm_text(None) == ""


def test_stem_is_four_letters():
    assert donor_triage.stem("елабуга") == "елаб"
    assert donor_triage.stem("елабужского") == "елаб"
    assert donor_triage.stem("елабу") == "елаб"


def test_generic_words_do_not_pick_a_region():
    """«Малые», «новый», «город» — типовые слова: они не должны ловить регион."""
    index = donor_triage.LocalityIndex()
    index.add_locality(5, "Малые Кабаны")
    assert index.regions_mentioned("Новый город") == {}
    assert set(index.regions_mentioned("Малые Кабаны")) == {5}


def test_generic_adjective_does_not_match_other_regions():
    """«Старые Ерыклы» не должен ловить «Старое Дрожжаное» из чужого района.

    Живой случай 24.11: корень «стар» тянул полтора десятка чужих деревень
    («Старое Дрожжаное», «Старые Нары», …), и группа из одного района
    выглядела многорайонной.
    """
    index = donor_triage.LocalityIndex()
    index.add_locality(10, "Старое Дрожжаное")
    index.add_locality(20, "Старые Ерыклы")
    assert set(index.regions_mentioned("Старые Ерыклы")) == {20}
    assert 10 not in index.regions_mentioned("Старые Ерыклы")


def test_short_region_name_is_matched_exactly():
    """«Арск» — слово из четырёх букв: сравниваем точно, а не по началу."""
    index = _index()
    assert set(index.regions_mentioned("Арск")) == {2}
    # «Арского» — уже другое слово, но с тем же началом: находим.
    assert set(index.regions_mentioned("ФОРПОСТ Арского района")) == {2}


# ---------------------------------------------------------------------------
# индекс локалитетов
# ---------------------------------------------------------------------------


def test_region_name_works_as_locality():
    index = _index()
    assert set(index.regions_mentioned("АРСК - район")) == {2}


def test_locality_is_found_in_genitive_form():
    """«Елабужского района» → регион Елабуга."""
    index = _index()
    assert set(index.regions_mentioned("п. Луговой Елабужского района")) == {1}


def test_locality_and_region_name_share_key():
    """«Ютазы» и «Ютаза» — одно место, хотя буквы разные."""
    index = donor_triage.LocalityIndex()
    index.add_locality(9, "Ютаза")
    assert set(index.regions_mentioned("Ютазы районы укытучылары")) == {9}


def test_locality_of_another_region_is_not_mentioned():
    index = _index()
    assert set(index.regions_mentioned("АРСК - район")) == {2}
    assert 1 not in index.regions_mentioned("АРСК - район")


# ---------------------------------------------------------------------------
# правила для общих групп
# ---------------------------------------------------------------------------


def _regs(*rows: Any) -> list[dict[str, Any]]:
    return [
        {
            "row_id": r.id,
            "region_id": r.region_id,
            "region_code": {1: "elabuga", 2: "arsk", 3: "tuzha"}[r.region_id],
            "region_name": r.name,
            "posts_count": r.posts_count,
            "publications": 0,
            "created_at": r.created_at,
            "is_own_main": False,
        }
        for r in rows
    ]


def test_city_match_keeps_that_region():
    regs = _regs(_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2))
    rec = donor_triage.recommend_shared(
        regs,
        group_name="Объявления",
        group_city="Елабуга",
        region_cities={1: "Елабуга", 2: "Арск"},
        index=_index(),
    )
    assert rec["action"] == "keep_region"
    assert rec["keep_region_ids"] == [1]
    assert rec["drop_row_ids"] == [2]


def test_locality_in_name_keeps_that_region():
    """Группа из деревни: город региона — районный центр, а группа живёт в селе."""
    regs = _regs(_row(id_=1, vk_id=-100, region_id=2), _row(id_=2, vk_id=-100, region_id=3))
    rec = donor_triage.recommend_shared(
        regs,
        group_name="МБОУ «Шушмабашская школа»",
        group_city="Арск",
        region_cities={2: "Арск", 3: "Тужа"},
        index=_index(),
    )
    assert rec["action"] == "keep_region"
    assert rec["keep_region_ids"] == [2]
    assert rec["drop_row_ids"] == [2]


def test_group_covering_several_our_regions_stays_everywhere():
    """Объявления по четырём районам — в каждом они свои, выключать нечего."""
    regs = _regs(_row(id_=1, vk_id=-100, region_id=2), _row(id_=2, vk_id=-100, region_id=3))
    rec = donor_triage.recommend_shared(
        regs,
        group_name="Объявления Арск, Тужа, Кикнур",
        group_city="",
        region_cities={},
        index=_index(),
    )
    assert rec["action"] == "keep_all"
    assert rec["drop_row_ids"] == []


def test_history_decides_when_name_says_nothing():
    regs = _regs(
        _row(id_=1, vk_id=-100, region_id=2, posts_count=295),
        _row(id_=2, vk_id=-100, region_id=3, posts_count=0),
    )
    rec = donor_triage.recommend_shared(
        regs, group_name="Ансамбль танца", group_city="", region_cities={}, index=_index()
    )
    assert rec["action"] == "keep_region"
    assert rec["keep_region_ids"] == [2]
    assert rec["confidence"] == "medium"


def test_without_any_signal_the_decision_is_humans():
    regs = _regs(_row(id_=1, vk_id=-100, region_id=2), _row(id_=2, vk_id=-100, region_id=3))
    rec = donor_triage.recommend_shared(
        regs, group_name="Академия футбола", group_city="", region_cities={}, index=_index()
    )
    assert rec["action"] == "decide"
    assert rec["drop_row_ids"] == []


# ---------------------------------------------------------------------------
# район, названный в описании (живой случай 24.11)
# ---------------------------------------------------------------------------


def _named_reg(
    *, row_id: int, region_id: int, region_code: str, region_name: str, posts_count: int = 0
) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "region_id": region_id,
        "region_code": region_code,
        "region_name": region_name,
        "posts_count": posts_count,
        "publications": 0,
        "created_at": "2026-10-02",
        "is_own_main": False,
    }


def test_district_named_in_description_beats_ambiguous_village_in_name():
    """Описание — официальный адрес: «Верхнеуслонского района» сильнее названия.

    Живой случай: «МБДОУ Кураловский детский сад» заведён и в Спасском, и в
    Верхнеуслонском. Название называет деревню, одинаково названную в обоих
    районах, а описание — конкретный район. Оставлять надо Верхнеуслонский.
    """
    regs = [
        _named_reg(row_id=1, region_id=10, region_code="spassky", region_name="СПАССКИЙ - ИНФО"),
        _named_reg(
            row_id=2, region_id=11, region_code="verhniy_uslon", region_name="ВЕРХНИЙ УСЛОН - ИНФО"
        ),
    ]
    index = donor_triage.LocalityIndex()
    index.add_region(10, "СПАССКИЙ - ИНФО")
    index.add_locality(10, "Куралово")
    index.add_region(11, "ВЕРХНИЙ УСЛОН - ИНФО")
    index.add_locality(11, "Куралово")

    rec = donor_triage.recommend_shared(
        regs,
        group_name='МБДОУ "Кураловский детский сад"',
        group_description=(
            "Муниципальное бюджетное дошкольное образовательное учреждение "
            "«Кураловский детский сад» Верхнеуслонского муниципального района "
            "Республики Татарстан. Адрес: с. Куралово, ул. Пионерская"
        ),
        group_city="Куралово",
        region_cities={},
        index=index,
    )
    assert rec["action"] == "keep_region"
    assert rec["keep_region_ids"] == [11]
    assert rec["confidence"] == "high"
    assert rec["drop_row_ids"] == [1]


def test_school_stays_in_the_district_named_in_its_description():
    """Школа «Малоелгинская СОШ» — описание прямо называет Лаишевский район."""
    regs = [
        _named_reg(
            row_id=1,
            region_id=20,
            region_code="rybnaya_sloboda",
            region_name="РЫБНАЯ СЛОБОДА - ИНФО",
        ),
        _named_reg(row_id=2, region_id=21, region_code="laishevo", region_name="ЛАИШЕВО - ИНФО"),
    ]
    index = donor_triage.LocalityIndex()
    index.add_region(20, "РЫБНАЯ СЛОБОДА - ИНФО")
    index.add_region(21, "ЛАИШЕВО - ИНФО")

    rec = donor_triage.recommend_shared(
        regs,
        group_name="МБОУ «Малоелгинская СОШ»",
        group_description=(
            "Муниципальное бюджетное общеобразовательное учреждение "
            "«Малоелгинская средняя общеобразовательная школа» "
            "Лаишевского муниципального района Республики Татарстан"
        ),
        group_city="Малая Елга",
        region_cities={},
        index=index,
    )
    assert rec["action"] == "keep_region"
    assert rec["keep_region_ids"] == [21]


def test_description_of_a_foreign_district_does_not_decide():
    """Описание про чужой район (Казань) — не повод выбрать регион."""
    regs = _regs(_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2))
    rec = donor_triage.recommend_shared(
        regs,
        group_name="Объявления",
        group_description="Республика Татарстан, г. Казань",
        group_city="Казань",
        region_cities={},
        index=_index(),
    )
    assert rec["action"] == "decide"


def test_locality_in_description_keeps_that_region():
    """Название молчит, но в описании — наш НП."""
    regs = _regs(_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2))
    rec = donor_triage.recommend_shared(
        regs,
        group_name="Объявления",
        group_description="Объявления п. Луговой Елабужского района",
        group_city="",
        region_cities={},
        index=_index(),
    )
    assert rec["action"] == "keep_region"
    assert rec["keep_region_ids"] == [1]
    assert rec["confidence"] == "medium"


def test_district_markers_ignore_generic_prefixes():
    """«Верхний» — слово из десятков наших районов: в маркер оно не годится."""
    assert donor_triage.district_markers("ВЕРХНИЙ УСЛОН - ИНФО") == ["услон"]
    assert donor_triage.district_markers("ТУЖА, Кировская область") == []
    assert donor_triage.district_markers("ЛАИШЕВО - ИНФО") == ["лаише"]


def test_name_naming_several_places_stays_everywhere_even_if_description_names_one():
    """«Объявления Свеча, Лебяжье, Даровской, Котельнич» — группа многорайонная.

    Описание упоминает один район («Даровского района»), но название группы
    само перечисляет несколько наших мест — значит, она честно общая, и
    выключать регистрацию в одном из них нельзя.
    """
    regs = [
        _named_reg(row_id=1, region_id=30, region_code="svecha", region_name="СВЕЧА - ИНФО"),
        _named_reg(row_id=2, region_id=31, region_code="darovskoy", region_name="ДАРОВСКОЙ - ИНФО"),
    ]
    index = donor_triage.LocalityIndex()
    index.add_region(30, "СВЕЧА - ИНФО")
    index.add_region(31, "ДАРОВСКОЙ - ИНФО")

    rec = donor_triage.recommend_shared(
        regs,
        group_name="Объявления Свеча, Лебяжье, Даровской, Котельнич",
        group_description="Объявления Даровского района принимаются в Даровском районе.",
        group_city="Котельнич",
        region_cities={},
        index=index,
    )
    assert rec["action"] == "keep_all"
    assert rec["confidence"] == "medium"


def test_same_place_name_in_two_regions_waits_for_a_person():
    """«с. Куралово» есть и в Спасском, и в Верхнеуслонском — какой, решает владелец."""
    regs = [
        _named_reg(row_id=1, region_id=10, region_code="spassky", region_name="СПАССКИЙ - ИНФО"),
        _named_reg(
            row_id=2, region_id=11, region_code="verhniy_uslon", region_name="ВЕРХНИЙ УСЛОН - ИНФО"
        ),
    ]
    index = donor_triage.LocalityIndex()
    index.add_region(10, "СПАССКИЙ - ИНФО")
    index.add_locality(10, "Куралово")
    index.add_region(11, "ВЕРХНИЙ УСЛОН - ИНФО")
    index.add_locality(11, "Куралово")

    rec = donor_triage.recommend_shared(
        regs,
        group_name="Храм св нмчч. и исп. Российских. с. Куралово",
        group_description="",
        group_city="Ульяновка",
        region_cities={},
        index=index,
    )
    assert rec["action"] == "keep_all"
    assert rec["confidence"] == "low"


# ---------------------------------------------------------------------------
# правила для молчащих доноров
# ---------------------------------------------------------------------------


def _donor(**kwargs: Any) -> dict[str, Any]:
    base = {
        "row_id": 1,
        "vk_id": 100,
        "region_id": 2,
        "name": "Молодёжь Арского района",
        "description": "",
        "health": "active",
    }
    base.update(kwargs)
    return base


def test_dormant_donor_is_disabled():
    rec = donor_triage.recommend_donor(_donor(health="dormant"), index=_index())
    assert rec["action"] == "disable"


def test_local_donor_moves_to_slotted_theme():
    rec = donor_triage.recommend_donor(
        _donor(name="Подростковый клуб Арского района"), index=_index()
    )
    assert rec["action"] == "set_theme"
    assert rec["theme"] == "novost"


def test_unknown_donor_waits_for_a_person():
    rec = donor_triage.recommend_donor(_donor(name="SciTopus"), index=_index())
    assert rec["action"] == "decide"
    assert rec["theme"] is None


# ---------------------------------------------------------------------------
# сбор данных
# ---------------------------------------------------------------------------


def test_shared_groups_are_collected():
    rows = [
        _row(id_=1, vk_id=-100, region_id=1),
        _row(id_=2, vk_id=-100, region_id=2),
        _row(id_=3, vk_id=-200, region_id=1),
    ]
    regions = {1: _region(1, "elabuga", "ЕЛАБУГА - ИНФО"), 2: _region(2, "arsk", "АРСК - ИНФО")}
    groups = donor_triage.collect_shared_groups(rows, regions, {}, {})
    assert len(groups) == 1
    assert groups[0]["vk_id"] == 100
    assert len(groups[0]["regs"]) == 2


def test_silent_donor_needs_all_fallback_themes_in_region():
    """Пока у региона есть сообщества во всех темах с запасным путём — донор тёмный."""
    themes = list(donor_triage.FALLBACK_THEMES)
    rows = [
        _row(id_=i, vk_id=-1000 - i, region_id=1, category=theme) for i, theme in enumerate(themes)
    ]
    rows.append(_row(id_=99, vk_id=-777, region_id=1, category="nauka"))
    regions = {1: _region(1, "arbazh", "АРБАЖ - ИНФО")}
    donors = donor_triage.collect_silent_donors(rows, regions, {}, {})
    assert [d["row_id"] for d in donors] == [99]


def test_donor_with_publications_is_not_silent():
    themes = list(donor_triage.FALLBACK_THEMES)
    rows = [
        _row(id_=i, vk_id=-1000 - i, region_id=1, category=theme) for i, theme in enumerate(themes)
    ]
    rows.append(_row(id_=99, vk_id=-777, region_id=1, category="nauka"))
    regions = {1: _region(1, "arbazh", "АРБАЖ - ИНФО")}
    donors = donor_triage.collect_silent_donors(rows, regions, {}, {777: 3})
    assert donors == []


def test_our_own_main_group_is_never_a_candidate():
    themes = list(donor_triage.FALLBACK_THEMES)
    rows = [
        _row(id_=i, vk_id=-1000 - i, region_id=1, category=theme) for i, theme in enumerate(themes)
    ]
    rows.append(_row(id_=99, vk_id=-777, region_id=1, category="nauka"))
    regions = {1: _region(1, "arbazh", "АРБАЖ - ИНФО", vk_group_id=-777)}
    donors = donor_triage.collect_silent_donors(rows, regions, {1: 777}, {})
    assert donors == []
