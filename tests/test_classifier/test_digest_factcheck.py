"""Фактчек сборников «Короткой строкой»: чистое ядро проверки (P182).

Смысл модуля — поймать выдуманный моделью факт. Промпт это запрещает, но
запрет в промпте не проверка: модель может назвать число, которого в
источнике нет. Здесь проверка механическая — каждый факт из пересказа должен
встречаться в тексте источника.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.digest_factcheck import (  # noqa: E402
    check_digest,
    check_facts,
    extract_facts,
    extract_names,
    extract_numbers,
    split_digest_items,
    unsupported_facts,
)


def test_numbers_normalized_thousand_separators():
    """Разряды не склеиваются: «02 215» — это два числа, а не «02215».

    В пересказе про шины так получалось «02215», которого в источнике нет ни
    разу: там «GRANDTREK ICE 02.» и «215/65 R16» — два разных числа.
    """
    nums = extract_numbers("DUNLOP ICE 02 215/65")
    assert "02" in nums and "215" in nums
    assert "02215" not in nums
    assert extract_numbers("в 2026 году") == {"2026"}


def test_thousands_and_plain_forms_confirm_each_other():
    """«1200» в пересказе подтверждается «1 200» в источнике."""
    source = "Вложили 1 200 рублей в проект."
    summary = "В проект вложили 1200 рублей."
    assert check_facts(summary, source).numbers == []


def test_number_spelled_out_in_source_confirms_digits():
    """«семи миллиардов» в источнике подтверждает «7 млрд» в пересказе."""
    source = "В Татарстане предъявили 119 исков на сумму более семи миллиардов рублей"
    summary = "119 экологических исков на свыше 7 млрд рублей."
    out = check_facts(summary, source)
    assert out.numbers == []
    assert out.strict is False


def test_hashtag_in_source_keeps_its_number():
    """«#АксубаевскаяСОШ2» — номер школы, и он подтверждает «СОШ2»."""
    source = "Победителями стали Гатина Милана и Чукашов Клим! #АксубаевскаяСОШ2 #осень2026"
    summary = "В Аксубаевской СОШ2 назвали победителей конкурсного дня."
    out = check_facts(summary, source)
    assert out.numbers == []


def test_names_ignore_stopwords_but_keep_proper_nouns():
    names = extract_names("Это Мария Иванова открыла школу")
    assert "мария" in names
    assert "иванова" in names
    assert "это" not in names, "зачин предложения — не факт"


def test_facts_mix_numbers_and_names():
    facts = extract_facts("Вчера Мария сдала 12 тонн")
    assert "num:12" in facts
    assert "name:мария" in facts


def test_supported_fact_is_not_reported():
    source = "Вчера в школе Мария Иванова сдала 12 тонн картофеля."
    summary = "Мария Иванова сдала 12 тонн картофеля."
    assert unsupported_facts(summary, source) == []


def test_fabricated_number_is_reported():
    source = "Вчера в школе Мария Иванова сдала 12 тонн картофеля."
    summary = "Мария Иванова сдала 120 тонн картофеля."
    missing = unsupported_facts(summary, source)
    assert missing == ["число «120»"], missing


def test_fabricated_name_is_reported():
    source = "Мария Иванова открыла школу в центре города."
    summary = "Анна Петрова открыла школу в центре города."
    missing = unsupported_facts(summary, source)
    assert missing == ["имя/название «анна»", "имя/название «петрова»"], missing


def test_name_must_match_as_whole_word():
    """«Иван» в источнике не подтверждает «Иванов» в пересказе."""
    source = "Иван позвал соседа."
    summary = "Иванов позвал соседа."
    assert unsupported_facts(summary, source) == ["имя/название «иванов»"]


def test_declension_is_not_a_fabrication():
    """Падеж — не выдумка: «Валентину» подтверждается «Валентина».

    На проде 01.11 так ложно сработали 49 из 589 пунктов: имена в пересказе
    стоят в падеже, в источнике — в именительном.
    """
    source = "Валентина Михайловна Порубова, Татьяна Викторовна Брезгина."
    summary = "Внуки поздравляют Валентину Михайловну Порубову и Татьяну Викторовну Брезгину."
    out = check_facts(summary, source)
    assert out.numbers == []
    for name in (
        "валентину",
        "михайловну",
        "порубову",
        "татьяну",
        "викторовну",
        "брезгину",
    ):
        assert name not in out.names, name


def test_short_prefix_is_still_a_difference():
    """Порог в 5 знаков не путает «Иван» с «Иванов»."""
    source = "Иван позвал соседа."
    summary = "Иванов позвал соседа."
    assert check_facts(summary, source).names == ["иванов"]


def test_yo_and_e_are_the_same_letter():
    """«Почётного» и «Почетного» — одно слово."""
    source = "деда Анатолия Дудина - почетного гражданина района."
    summary = "Внуки поздравляют деда Анатолия Дудина — Почётного гражданина района."
    out = check_facts(summary, source)
    assert "почетного" not in out.names
    assert out.numbers == []


def test_number_mismatch_is_a_strict_verdict():
    """Число — строгий вердикт: пересказ обязан сохранить его."""
    source = "Мария открыла школу 12 мая."
    summary = "Мария открыла школу 99 мая."
    out = check_facts(summary, source)
    assert out.numbers == ["99"]
    assert out.strict is True


def test_synonym_is_advisory_not_a_verdict():
    """«Запись» из «записаться» — не выдумка, а пересказ.

    Такое слово точным совпадением не подтверждается, но и вердиктом быть не
    может: пересказ по задаче переписывает своими словами.
    """
    source = "Чтобы не стоять в очереди, лучше записаться заранее."
    summary = "Запись — на es.pfrf.ru/znp/."
    out = check_facts(summary, source)
    assert out.numbers == []
    assert out.strict is False


def test_case_does_not_matter():
    source = "МАРИЯ открыла школу."
    summary = "Мария открыла школу."
    assert unsupported_facts(summary, source) == []


def test_ids_inside_links_are_not_facts():
    """Идентификаторы ВК в ссылках — не факты (ложное срабатывание 01.11).

    В опубликованном пересказе встретилось «?z=album-34480932312120662» — это
    идентификатор альбома, а не утверждение о мире. Проверять его против
    текста источника бессмысленно: он и не должен там быть.
    """
    text = "Больше фото в альбоме https://vk.ru/narodvestnik?z=album-34480932312120662"
    assert extract_numbers(text) == set()
    assert "album" not in extract_names(text)


def test_numbers_in_plain_text_are_still_facts():
    assert extract_numbers("собрали 120 тонн") == {"120"}


def test_digest_items_split_on_source_marker():
    """Новый маркер подписи (решение владельца 08.11): «Рассказали в»."""
    wall = (
        "Вечерняя сводка\n\n"
        "Первый пункт.\n\nРассказали в Сообщество А\n\n"
        "Второй пункт.\n\nРассказали в Сообщество Б"
    )
    items = split_digest_items(wall)
    assert len(items) == 2
    assert "Первый пункт" in items[0]
    assert "Второй пункт" in items[1]


def test_digest_items_still_split_on_old_source_marker():
    """Старый маркер «Источник:» тоже разбирается: посты до 08.11 стоят в ленте.

    Фактчек проверяет уже опубликованный текст, поэтому смена подписи не
    должна оставлять старые посты неразбираемыми — иначе они молча выпадут
    из проверки.
    """
    wall = (
        "Вечерняя сводка\n\n"
        "Первый пункт.\n\nИсточник: Сообщество А\n\n"
        "Второй пункт.\n\nИсточник: Сообщество Б"
    )
    items = split_digest_items(wall)
    assert len(items) == 2
    assert "Первый пункт" in items[0]
    assert "Второй пункт" in items[1]


def test_digest_items_split_on_mixed_markers():
    """Смешанная стена (старые и новые пункты рядом) разбирается целиком."""
    wall = (
        "Вечерняя сводка\n\n"
        "Первый пункт.\n\nИсточник: Сообщество А\n\n"
        "Второй пункт.\n\nРассказали в Сообщество Б"
    )
    items = split_digest_items(wall)
    assert len(items) == 2
    assert "Первый пункт" in items[0]
    assert "Второй пункт" in items[1]


def test_digest_header_is_not_treated_as_an_item():
    """Заголовок сборника — отдельный блок, а не часть первого пересказа.

    На проде 01.11 заголовок «Короткой строкой» попадал в первый пересказ и
    давал ложное срабатывание: «имя/название “короткой”» не подтверждалось
    источником — потому что его там и не должно быть.
    """
    wall = (
        "Короткой строкой\n\n"
        "Не просто мастера, а наставники.\n\nИсточник: Управление образования\n\n"
        "Второй пункт.\n\nИсточник: Сообщество Б"
    )
    items = split_digest_items(wall)
    assert len(items) == 2
    assert items[0] == "Не просто мастера, а наставники."
    assert "короткой" not in items[0].lower()


def test_check_digest_pairs_items_with_sources_in_order():
    rows = [
        {"lip": "1_1", "published_url": "https://vk.com/wall-1_2"},
        {"lip": "2_2", "published_url": "https://vk.com/wall-1_2"},
    ]
    sources = {
        "1_1": "Мария Иванова открыла школу 12 мая.",
        "2_2": "Иван Петров купил трактор.",
    }
    wall = (
        "Мария Иванова открыла школу 12 мая.\n\nИсточник: А\n\n"
        "Иван Петров купил трактор.\n\nИсточник: Б"
    )
    checks = check_digest(region="mi", rows=rows, wall_text=wall, sources=sources)
    assert len(checks) == 2
    assert all(c.strict_mode for c in checks)
    # «Мария» подтверждается своим источником, а не чужим.
    assert checks[0].missing == []
    assert checks[1].missing == []


def test_check_digest_falls_back_to_pool_when_counts_differ():
    rows = [{"lip": "1_1", "published_url": "https://vk.com/wall-1_2"}]
    sources = {"1_1": "Мария открыла школу."}
    wall = "Первый.\n\nИсточник: А\n\nВторой.\n\nИсточник: Б"
    checks = check_digest(region="mi", rows=rows, wall_text=wall, sources=sources)
    assert len(checks) == 1
    assert not checks[0].strict_mode
    assert checks[0].error


def test_check_digest_flags_fabrication_against_its_own_source():
    rows = [
        {"lip": "1_1", "published_url": "https://vk.com/wall-1_2"},
        {"lip": "2_2", "published_url": "https://vk.com/wall-1_2"},
    ]
    sources = {
        "1_1": "Мария открыла школу 12 мая.",
        "2_2": "Иван купил трактор.",
    }
    wall = "Мария открыла школу 99 мая.\n\nИсточник: А\n\nИван купил трактор.\n\nИсточник: Б"
    checks = check_digest(region="mi", rows=rows, wall_text=wall, sources=sources)
    assert checks[0].missing == ["число «99»"]
    assert checks[1].missing == []


@pytest.mark.parametrize(
    "summary,source,expect_empty",
    [
        ("В Башкортостане запустили программу.", "В Башкортостане запустили программу.", True),
        ("В Башкортостане запустили программу.", "В Татарстане запустили программу.", False),
    ],
)
def test_geography_is_not_silently_swapped(summary, source, expect_empty):
    missing = unsupported_facts(summary, source)
    assert (missing == []) is expect_empty, missing


@pytest.mark.parametrize(
    "summary,source",
    [
        ("Тёплые дорогие к зиме", "Теплые дорогие к зиме, готовьтесь"),
        ("Мёртвые дубы у пацана", "Мертвые дубы у пацана спилили"),
        ("Весёлый праздник прошёл", "Веселый праздник прошёл в селе"),
        ("Днём в городе жарко", "Днем в городе жарко, все гуляют"),
    ],
)
def test_yo_e_is_normalized_on_both_sides(summary, source):
    """«Тёплые» из пересказа == «теплые» из источника.

    Живой замер на проде 02.11 (40 сборников, 200 пунктов) дал 7 «неподтверждённых
    имён» — и все семь оказались ложными: правило «ё→е» применялось только к
    источнику, а имя из пересказа сравнивалось как есть. Сравнение должно быть
    симметричным.
    """
    out = check_facts(summary, source)
    assert out.names == [], out.names
    assert out.numbers == []


def test_case_form_of_a_name_is_reported_separately():
    """Падежная форма («Фёдорова» из «Федоров») — факт подтверждён, глаза не нужны."""
    out = check_facts("Фёдорова отчитался", "Федоров отчитался о работе")

    assert out.names == []
    assert out.name_forms == ["федорова"]


def test_invented_name_is_still_caught():
    """Правка регистра не превращает инструмент в формальность."""
    out = check_facts("По словам Фёдора Кузьмина", "По словам жителей села")

    # Имена нормализованы (ё→е), как и слова источника.
    assert "федора" in out.names
    assert "кузьмина" in out.names
