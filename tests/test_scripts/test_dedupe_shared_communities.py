"""Unit-тесты для ``scripts/dedupe_shared_communities.py`` — план чистки общих групп.

**Зачем.** P236: 71 группа заведена в нескольких районах (116 лишних регистраций).
Правило «где группа своя» — area-guard (область группы против области региона), и
оно **не должно гадать**: неизвестная область = оставить как есть. Скрипт —
CLI-утилита вне пакета, грузим через importlib; БД и VK в тестах не трогаем —
проверяются чистые функции.
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


def test_area_match_keeps_one_and_drops_the_rest():
    """Область группы — «Кировская»: регистрация в Татарстане лишняя."""
    rows = [
        _row(id_=1, vk_id=-100, region_id=1),
        _row(id_=2, vk_id=-100, region_id=2),
        _row(id_=3, vk_id=-100, region_id=3),
    ]
    plan = dedupe.decide_shared(
        {100: rows},
        region_areas={1: "Кировская область", 2: "Татарстан", 3: "Кировская область"},
        group_areas={100: "Кировская область"},
        own_region_groups={},
    )

    assert plan[100]["status"] == "ok"
    # Регион 2 — другая область → лишний. Регионы 1 и 3 — своей области не
    # различаем (область одна), оба остаются: какой из них «настоящий» —
    # area-guard не решает.
    assert plan[100]["drop"] == [2]
    assert sorted(plan[100]["keep"]) == [1, 3]


def test_area_mismatch_drops_the_foreign_registration():
    """Область региона не совпала с областью группы — регистрация лишняя."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        region_areas={1: "Кировская область", 2: "Татарстан"},
        group_areas={100: "Кировская область"},
        own_region_groups={},
    )

    assert plan[100]["drop"] == [2]
    assert plan[100]["keep"] == [1]


def test_unknown_area_is_not_guessed():
    """Область неизвестна — «не знаю» ≠ «чужая»: оставляем как есть."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        region_areas={1: None, 2: None},
        group_areas={100: None},
        own_region_groups={},
    )

    assert plan[100]["drop"] == []
    assert sorted(plan[100]["keep"]) == [1, 2]
    assert plan[100]["status"] == "unknown"


def test_group_that_is_nowhere_its_own_is_left_alone():
    """Нет ни одной регистрации в области группы — это не «лишняя», а чужая.

    Такое area-guard не разбирает (это другой класс — чужие доноры), поэтому
    скрипт такие группы не трогает, а отдаёт в отчёт.
    """
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        region_areas={1: "Кировская область", 2: "Татарстан"},
        group_areas={100: "Москва"},
        own_region_groups={},
    )

    assert plan[100]["drop"] == []
    assert plan[100]["status"] == "no_match"


def test_our_own_main_group_is_never_dropped():
    """Главная группа региона остаётся в своём регионе при любом раскладе."""
    rows = [_row(id_=1, vk_id=-100, region_id=1), _row(id_=2, vk_id=-100, region_id=2)]
    plan = dedupe.decide_shared(
        {100: rows},
        region_areas={1: "Кировская область", 2: "Татарстан"},
        group_areas={100: "Татарстан"},
        own_region_groups={1: 100},
    )

    assert 1 in plan[100]["keep"]
    assert 1 not in plan[100]["drop"]
