"""Тесты для ``scripts/reklama_source_report.py`` — отчёт об источниках реklama.

Инструмент отвечает на вопрос, который четыре месяца висел в реестре как
обещание (P023 → P188): откуда приходят посты в рекламные сводки. Проверяем
арифметику отчёта и его тон: **чужие стены не приравнены к поломке**, потому
что в теме reklama фильтр выключен по замыслу и в сводку законно попадает
репост поста с любой стены.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "reklama_source_report", REPO_ROOT / "scripts" / "reklama_source_report.py"
)
report_mod = importlib.util.module_from_spec(_spec)
sys.modules["reklama_source_report"] = report_mod
_spec.loader.exec_module(report_mod)


def _report(**over):
    base = {
        "days": 30,
        "raions_total": 58,
        "raions_with_own_board": 54,
        "raions_without_board": ["falenki", "oparino"],
        "runs": 30,
        "runs_published": 30,
        "runs_per_region": {"mi": 30},
        "own_posts": 28,
        "foreign_posts": 0,
        "unknown_posts": 0,
        "foreign": [],
    }
    base.update(over)
    return base


def test_clean_report_says_there_are_none():
    """Ноль чужих стен — это ответ, а не пустой отчёт: числа всё равно названы."""
    text = report_mod.render(_report())
    assert "свои доски=28" in text
    assert "чужие стены=0" in text
    assert "чужих стен в реklama нет" in text
    assert "по регионам: {'mi': 30}" in text


def test_foreign_wall_is_listed_but_not_called_a_failure():
    """Чужая стена показывается и помечается как «смотреть глазами».

    Формулировка важна: в теме reklama рекламный фильтр выключен по замыслу,
    и репост поста с любой стены там законен — называть это поломкой значит
    научить читателя закрывать глаза на настоящие.
    """
    text = report_mod.render(
        _report(
            foreign_posts=1,
            foreign=[
                {
                    "region": "mi",
                    "category": "proisshestviya",
                    "wall": "-194944166",
                    "name": "Поисковый отряд",
                    "own_region": "kirov_obl",
                }
            ],
        )
    )
    assert "чужие стены=1" in text
    assert "смотреть глазами" in text
    assert "Поисковый отряд" in text


def test_missing_board_is_named_with_its_count():
    """Районы без доски — главный вопрос отчёта: именно они пошли бы в фолбэк."""
    text = report_mod.render(_report())
    assert "без: 2" in text
    assert "falenki, oparino" in text


def test_unknown_wall_is_printed_as_id_not_url():
    """Стена вне реестра печатается идентификатором, а не хвостом URL.

    Проверено на живом прогоне: срез `url.rsplit('/')` давал в колонке «стена»
    мусор вида ``wall-194944166_5911761`` вместо ``-194944166``, и строка выглядела
    как ошибка инструмента там, где на самом деле просто неизвестная стена.
    """
    text = report_mod.render(
        _report(
            unknown_posts=1,
            foreign=[
                {
                    "region": "mi",
                    "category": "?",
                    "wall": "-194944166",
                    "name": "(стены нет в реестре communities)",
                }
            ],
        )
    )
    assert "стена -194944166 ·" in text
    assert "wall" not in text.split("чужие стены")[1].split("\n")[1]
