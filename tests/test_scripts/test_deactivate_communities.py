"""Unit tests for ``scripts/deactivate_communities.py``.

Покрываем чистую часть — разбор файла id и раскладку строк региона на
(включённые / уже выключенные / не найденные). Именно эти две вещи защищают
операцию с данными на проде: опечатка в id не должна молча «выключать никого»
и не должна выключать чужой район, а повторный запуск не должен ничего менять.

Отдельно закреплён guard на ``disabled_reason='dormant_t1_auto'``: этот код
зарезервирован авто-политикой, и ежемесячный recheck вернул бы строки в пул —
то есть ручная чистка оказалась бы мнимой.

Скрипт — CLI-утилита вне пакета, грузим через importlib (как соседние тесты).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "deactivate_communities", REPO_ROOT / "scripts" / "deactivate_communities.py"
)
deactivate = importlib.util.module_from_spec(_spec)
sys.modules["deactivate_communities"] = deactivate
_spec.loader.exec_module(deactivate)


class _Row:
    """Минимальная строка communities — только поля, которые читает скрипт."""

    def __init__(self, vk_id: int, is_active: bool = True, reason=None, name="гр"):
        self.vk_id = vk_id
        self.is_active = is_active
        self.disabled_reason = reason
        self.name = name
        self.category = "novost"


def test_parse_ids_file_abs_and_dedupe(tmp_path):
    """Знак не важен, дубликаты схлопываются, нули отбрасываются."""
    p = tmp_path / "ids.json"
    p.write_text(json.dumps([-111, 111, 222, 0]), encoding="utf-8")
    assert deactivate.parse_ids_file(str(p)) == [111, 222]


def test_parse_ids_file_tolerates_bom(tmp_path):
    """Файл, записанный PowerShell'ом (BOM в начале), читается, а не падает."""
    p = tmp_path / "ids_bom.json"
    p.write_text(json.dumps([111, 222]), encoding="utf-8-sig")
    assert deactivate.parse_ids_file(str(p)) == [111, 222]


def test_parse_ids_file_rejects_non_list(tmp_path):
    p = tmp_path / "ids.json"
    p.write_text(json.dumps({"vk_id": 111}), encoding="utf-8")
    with pytest.raises(ValueError):
        deactivate.parse_ids_file(str(p))


def test_split_targets_separates_active_already_and_missing():
    rows = [
        _Row(-222, is_active=True),
        _Row(-111, is_active=False, reason="pool_cleanup"),
        _Row(-333, is_active=True),
    ]
    active, already, missing = deactivate.split_targets(rows, [111, 222, 999])
    assert [r.vk_id for r in active] == [-222]
    assert [r.vk_id for r in already] == [-111]
    assert missing == [999]


def test_split_targets_stable_order():
    rows = [_Row(-333), _Row(-111), _Row(-222)]
    active, _, _ = deactivate.split_targets(rows, [333, 222, 111])
    assert [abs(r.vk_id) for r in active] == [111, 222, 333]


def test_split_targets_ignores_unknown_rows():
    """Строки региона, которых нет в списке, не попадают ни в одну корзину."""
    rows = [_Row(-111), _Row(-444)]
    active, already, missing = deactivate.split_targets(rows, [111])
    assert [r.vk_id for r in active] == [-111]
    assert already == [] and missing == []


def test_split_targets_empty_wanted():
    assert deactivate.split_targets([_Row(-111)], []) == ([], [], [])


def test_main_refuses_reserved_reason(tmp_path, monkeypatch):
    """'dormant_t1_auto' зарезервирован: main падает до любых обращений к БД."""
    p = tmp_path / "ids.json"
    p.write_text(json.dumps([111]), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deactivate_communities.py",
            "--region-code",
            "x",
            "--vk-ids-file",
            str(p),
            "--reason",
            "dormant_t1_auto",
        ],
    )
    assert deactivate.main() == 2
