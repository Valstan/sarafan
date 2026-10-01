"""Локальные хэштеги района.

Главное свойство, которое здесь проверяется: генератор **не выдумывает**
прилагательное района. Неверный хэштег уводит читателя в чужую выдачу и виден в
каждом посте — он хуже отсутствующего.
"""

from modules.promotion.hashtags import (
    DISTRICT_ADJECTIVES,
    build_hashtag_plan,
    normalize_tag,
    plan_hashtags_for_regions,
)


class TestNormalizeTag:
    def test_lowercases_and_joins_words(self):
        assert normalize_tag("Белая Холуница") == "белая_холуница"

    def test_hyphen_becomes_underscore(self):
        assert normalize_tag("Кирово-Чепецк") == "кирово_чепецк"

    def test_keeps_yo(self):
        # «#кумены» и «#кумёны» — разные теги, и жители пишут второй.
        assert normalize_tag("Кумёны") == "кумёны"

    def test_drops_punctuation(self):
        assert normalize_tag("Тужа,") == "тужа"

    def test_empty_is_empty(self):
        assert normalize_tag("") == ""
        assert normalize_tag(None) == ""


class TestBuildHashtagPlan:
    def test_known_district_gets_two_tags(self):
        plan = build_hashtag_plan("suna", "Суна")
        assert plan is not None
        assert plan.hashtags == ["#суна", "#сунский_район"]
        assert plan.needs_review is False
        assert plan.as_field() == "#суна,#сунский_район"

    def test_irregular_adjective_comes_from_table(self):
        # Ни одно правило не выводит «свечинский» из «Свеча» — только таблица.
        assert build_hashtag_plan("svecha", "Свеча").hashtags[1] == "#свечинский_район"
        assert build_hashtag_plan("luza", "Луза").hashtags[1] == "#лузский_район"
        assert build_hashtag_plan("yurya", "Юрья").hashtags[1] == "#юрьянский_район"
        assert build_hashtag_plan("murashi", "Мураши").hashtags[1] == "#мурашинский_район"

    def test_unknown_district_gets_only_center_and_review_flag(self):
        plan = build_hashtag_plan("newdistrict", "Новосёлово")
        assert plan is not None
        assert plan.hashtags == ["#новосёлово"]
        assert plan.needs_review is True
        assert plan.note

    def test_existing_value_is_never_overwritten(self):
        assert build_hashtag_plan("suna", "Суна", existing="#моё") is None

    def test_blank_existing_is_treated_as_missing(self):
        assert build_hashtag_plan("suna", "Суна", existing="   ") is not None

    def test_no_center_but_known_district_still_gives_district_tag(self):
        plan = build_hashtag_plan("suna", None)
        assert plan is not None
        assert plan.hashtags == ["#сунский_район"]

    def test_nothing_to_offer_returns_none(self):
        assert build_hashtag_plan("newdistrict", None) is None

    def test_center_equal_to_district_is_not_duplicated(self):
        plan = build_hashtag_plan("suna", "Сунский")
        assert len(plan.hashtags) == len(set(plan.hashtags))


class TestPlanForRegions:
    def test_skips_configured_regions(self):
        rows = [
            ("suna", "Суна", None),
            ("mi", "Малмыж", "#малмыж,#малмыжский_район"),
            ("zuevka", "Зуевка", ""),
        ]
        plans = plan_hashtags_for_regions(rows)
        assert [p.region_code for p in plans] == ["suna", "zuevka"]

    def test_all_three_new_districts_are_known(self):
        # Суна, Кумёны и Зуевка запущены 27.08 — именно ради них модуль и пишется.
        for code in ("suna", "kumyony", "zuevka"):
            assert code in DISTRICT_ADJECTIVES

    def test_every_live_tatarstan_district_has_an_adjective(self):
        """Каждый район Татарстана в сети обязан иметь прилагательное.

        Список берётся из миграций-скелетов, а не из кода: предыдущая проверка
        хардкодила тройку 27.08, и поэтому молча пропустила батч №6
        (Камское Устье / Кайбицы / Чистополь, 29.09) — эти три района остались
        без районного хэштега в продвижении. Гейт на тройку проверяет только ту
        тройку; гейт на список проверяет любую следующую порцию.
        """
        for code in (
            # батчи №1–№3 (сентябрь 2026)
            "saby",
            "arsk",
            "tyulyachi",
            "mamadysh",
            "atnya",
            "vysokaya_gora",
            "pestretsy",
            "rybnaya_sloboda",
            "zelenodolsk",
            "laishevo",
            "elabuga",
            "mendeleevsk",
            "nizhnekamsk",
            "verhniy_uslon",
            # батч №6 (29.09) — именно эти три выпали из проверки
            "kamsko_ustinsky",
            "kaybitsky",
            "chistopolsky",
            # батч №7 (01.10)
            "apastovsky",
            "tetyushsky",
            "spassky",
        ):
            assert code in DISTRICT_ADJECTIVES, f"{code}: нет прилагательного района"
