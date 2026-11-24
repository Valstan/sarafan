"""Unit-тесты для ``scripts/dedupe_shared_communities.py`` — план чистки общих групп.

**Зачем.** P236: группа, заведенная в нескольких районах, кормит ленты нескольких
районов. Правило «где группа своя» — географическое: город региона (город его
главной группы) против города группы. Оно **не должно гадать**: неизвестный город
= оставить как есть. Скрипт — CLI-утилита вне пакета, грузим через importlib; БД и
VK в тестах не трогаем — проверяются чистые функции.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "dedupe_shared_communities", REPO_ROOT / "scripts" / "dedupe_shared_communities.py"
)
dedupe = importlib.util.module_from_spec(_spec)
sys.modules["dedupe_shared_communities"] = dedupe
_spec.loader.exec_module(dedupe)


def _row(*, id_: int, vk_id: int, region_id: int, posts_count: int = 0) -> Any:
    row = type("Row", (), {})()
    row.id = id_
    row.vk_id = vk_id
    row.region_id = region_id
    row.posts_count = posts_count
    row.name = "группа"
    row.is_active = True
    return row


def test_group_in_two_regions_is_shared():
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    shared, duplicates = dedupe.find_shared_and_duplicates(rows)

    assert list(shared.keys()) == [100]
    assert len(shared[100]) == 2
    assert duplicates == {}


def test_two_rows_in_one_region_are_duplicates():
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=1)]
    shared, duplicates = dedupe.find_shared_and_duplicates(rows)

    assert shared == {}
    assert list(duplicates.keys()) == [(100, 1)]


def test_single_registration_is_neither():
    rows = [_row(id_=1, vk_id=-100, region_id=1)]
    shared, duplicates = dedupe.find_shared_and_duplicates(rows)

    assert shared == {}
    assert duplicates == {}


def test_duplicate_survivor_is_the_row_with_history():
    """Оставляем строку с большей историей; при равенстве — старшую."""
    older = _row(id_=1, vk_id=-100, region_id=1, posts_count=10)
    newer = _row(id_=2, vk_id=-100, region_id=1, posts_count=0)

    assert dedupe.pick_duplicate_survivor([newer, older]) is older
    tie_a = _row(id_=5, vk_id=-100, region_id=1, posts_count=3)
    tie_b = _row(id_=4, vk_id=-100, region_id=1, posts_count=3)
    assert dedupe.pick_duplicate_survivor([tie_b, tie_a]) is tie_b


def test_city_match_keeps_its_region_and_drops_the_other():
    """Город группы совпал с городом одного региона — он и остаётся."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        group_cities={100: "Котельнич"},
        region_cities={1: "Котельнич", 2: "Даровской"},
        own_region_groups={},
    )

    assert plan[100]["status"] == "ok"
    assert plan[100]["keep"] == [1]
    assert plan[100]["drop"] == [2]


def test_city_mismatch_drops_the_foreign_registration():
    """Города известны и не совпали — регистрация лишняя."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        group_cities={100: "Котельнич"},
        region_cities={1: "Котельнич", 2: "Свеча"},
        own_region_groups={},
    )

    assert plan[100]["drop"] == [2]
    assert plan[100]["keep"] == [1]


def test_unknown_city_is_not_guessed():
    """Город неизвестен — «не знаю» ≠ «чужая»: оставляем как есть."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        group_cities={100: ""},
        region_cities={1: "", 2: ""},
        own_region_groups={},
    )

    assert plan[100]["drop"] == []
    assert sorted(plan[100]["keep"]) == [1, 2]
    assert plan[100]["status"] == "unknown"


def test_group_that_is_nowhere_its_own_is_left_alone():
    """Своего региона не нашлось — это не «лишняя», а «нигде не своя».

    Деревня или областной центр: чистить нельзя, группа идёт на разбор.
    """
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        group_cities={100: "Казань"},
        region_cities={1: "Малмыж", 2: "Тужа"},
        own_region_groups={},
    )

    assert plan[100]["drop"] == []
    assert plan[100]["status"] == "no_match"


def test_our_own_main_group_is_never_dropped():
    """Главная группа региона остаётся в своём регионе при любом раскладе."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        group_cities={100: "Малмыж"},
        region_cities={1: "Малмыж", 2: "Тужа"},
        own_region_groups={1: 100},
    )

    assert 1 in plan[100]["keep"]
    assert 1 not in plan[100]["drop"]


def test_city_names_are_normalized():
    """«г. Котельнич», «Котельнич», «город Котельнич» — это один город."""
    assert dedupe.norm_city("г. Котельнич") == "котельнич"
    assert dedupe.norm_city("Котельнич") == "котельнич"
    assert dedupe.norm_city("город Котельнич") == "котельнич"
    assert dedupe.norm_city("Слободской") == "слободской"
    assert dedupe.norm_city(None) == ""
