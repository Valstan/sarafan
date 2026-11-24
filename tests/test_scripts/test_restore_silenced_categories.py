"""Unit-тесты для ``scripts/restore_silenced_categories.py`` — возврат прежних тем.

**Зачем.** Применение AI-подсказок (24.11) часть доноров перевело в темы, у
которых **нет слота** в beat-расписании: их посты перестают попадать в сборку.
Возврат прежней темы опирается на журнал публикаций (у тёмного донора каждая
публикация шла через его собственную волну). Проверяем: кого считаем тёмным,
как выбираем прежнюю тему и что не трогаем.

Скрипт — CLI-утилита вне пакета, грузим через importlib; БД не трогаем.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "restore_silenced_categories", REPO_ROOT / "scripts" / "restore_silenced_categories.py"
)
restore = importlib.util.module_from_spec(_spec)
sys.modules["restore_silenced_categories"] = restore
_spec.loader.exec_module(restore)


def _row(*, id_: int, vk_id: int, category: str) -> Any:
    row = type("Row", (), {})()
    row.id = id_
    row.vk_id = vk_id
    row.category = category
    row.name = "группа"
    row.is_active = True
    return row


def _region_with_all_slots() -> list:
    """Регион, у которого есть сообщества в каждой волне со слотом."""
    return [
        _row(id_=1, vk_id=-1, category="novost"),
        _row(id_=2, vk_id=-2, category="kultura"),
        _row(id_=3, vk_id=-3, category="sport"),
        _row(id_=4, vk_id=-4, category="admin"),
        _row(id_=5, vk_id=-5, category="union"),
        _row(id_=6, vk_id=-6, category="detsad"),
    ]


def test_dark_donor_is_found_when_region_has_every_wave():
    rows = _region_with_all_slots() + [_row(id_=7, vk_id=-7, category="sosed")]
    dark = restore.is_dark(rows)

    assert [r.id for r in dark] == [7]


def test_fallback_saves_donor_when_region_lacks_a_wave():
    """Нет сообществ хотя бы в одной волне - запасной путь её подхватит."""
    rows = [
        _row(id_=1, vk_id=-1, category="novost"),
        _row(id_=2, vk_id=-2, category="kultura"),
        _row(id_=3, vk_id=-3, category="sport"),
        _row(id_=4, vk_id=-4, category="admin"),
        _row(id_=5, vk_id=-5, category="union"),
        _row(id_=7, vk_id=-7, category="sosed"),
    ]
    assert restore.is_dark(rows) == []


def test_slotted_theme_is_not_touched():
    """Донор в теме со слотом и так публикуется."""
    rows = _region_with_all_slots()
    assert restore.is_dark(rows) == []


def test_legacy_theme_is_not_touched():
    """``krugozor`` и подобные молчали и до правки - их не чиним."""
    rows = _region_with_all_slots() + [_row(id_=8, vk_id=-8, category="krugozor")]
    assert restore.is_dark(rows) == []


def test_previous_theme_is_the_most_frequent_slotted_one():
    themes = {100: Counter({"novost": 12, "union": 3})}
    assert restore.pick_previous_theme(100, themes) == "novost"


def test_previous_theme_ignores_themes_without_slots():
    """``addons``/``korotko`` - без слотов: вернуть туда = оставить молчащим."""
    themes = {100: Counter({"addons": 20, "korotko": 5})}
    assert restore.pick_previous_theme(100, themes) is None


def test_previous_theme_falls_back_to_next_slotted_theme():
    themes = {100: Counter({"addons": 20, "kultura": 4, "novost": 1})}
    assert restore.pick_previous_theme(100, themes) == "kultura"


def test_no_publications_means_no_previous_theme():
    assert restore.pick_previous_theme(100, {}) is None
