"""Unit-тесты для ``scripts/apply_suggested_categories.py`` — план применения подсказок.

**Зачем.** Применение подсказки — **перевод донора между темами** (964 сообщества,
P235). Поэтому в плане важны три вещи: вёдра (было → станет), целевые темы и
страховка «наши группы не переводим никогда». Скрипт — CLI-утилита вне пакета,
грузим через importlib; БД в тестах не трогаем — проверяется чистая функция
``plan_applies``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "apply_suggested_categories", REPO_ROOT / "scripts" / "apply_suggested_categories.py"
)
apply_suggested = importlib.util.module_from_spec(_spec)
sys.modules["apply_suggested_categories"] = apply_suggested
_spec.loader.exec_module(apply_suggested)


def _row(
    *,
    id_: int,
    vk_id: int,
    category: str,
    suggested: str | None,
    name: str = "группа",
) -> Any:
    row = type("Row", (), {})()
    row.id = id_
    row.vk_id = vk_id
    row.category = category
    row.suggested_category = suggested
    row.name = name
    return row


def test_buckets_count_transitions():
    """Вёдра — это разбивка «было → станет», а не список строк."""
    rows = [
        _row(id_=1, vk_id=-100, category="union", suggested="detsad"),
        _row(id_=2, vk_id=-200, category="union", suggested="detsad"),
        _row(id_=3, vk_id=-300, category="novost", suggested="sosed"),
    ]
    plan = apply_suggested.plan_applies(rows, own_group_ids=[])

    assert len(plan["to_apply"]) == 3
    assert plan["buckets"]["union → detsad"] == 2
    assert plan["buckets"]["novost → sosed"] == 1
    assert plan["targets"]["detsad"] == 2
    assert plan["targets"]["sosed"] == 1


def test_our_own_groups_are_never_touched():
    """Наша группа — перевод темы ударил бы по маршрутизации нашей ленты."""
    rows = [
        _row(id_=1, vk_id=-777, category="union", suggested="detsad"),
        _row(id_=2, vk_id=-888, category="union", suggested="detsad"),
    ]
    plan = apply_suggested.plan_applies(rows, own_group_ids=[-777])

    assert len(plan["to_apply"]) == 1
    assert plan["to_apply"][0].vk_id == -888
    assert len(plan["skipped_own"]) == 1
    assert plan["skipped_own"][0].vk_id == -777


def test_suggestion_equal_to_current_is_not_a_transition():
    """Подсказка = текущая категория: переводить нечего, флаг просто висит."""
    rows = [_row(id_=1, vk_id=-100, category="novost", suggested="novost")]
    plan = apply_suggested.plan_applies(rows, own_group_ids=[])

    assert plan["to_apply"] == []
    assert len(plan["skipped_same"]) == 1
    assert plan["buckets"] == {}


def test_empty_suggestion_is_ignored():
    rows = [_row(id_=1, vk_id=-100, category="novost", suggested=None)]
    plan = apply_suggested.plan_applies(rows, own_group_ids=[])

    assert plan["to_apply"] == []
    assert plan["skipped_same"] == []


def test_single_slot_themes_are_flagged():
    """Тема с одним слотом в сутки — перевод туда переполняет её."""
    assert "detsad" in apply_suggested.SINGLE_SLOT_THEMES
    rows = [_row(id_=1, vk_id=-100, category="union", suggested="detsad")]
    plan = apply_suggested.plan_applies(rows, own_group_ids=[])

    assert plan["targets"]["detsad"] == 1
