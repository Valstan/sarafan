"""Сверка классификатора (локально против боя): ключ поста и решающие поля.

**Что тут ловится.** 26.11.2026 инструмент сверки
(``infra/windows/classifier_parity.py``) прогонял вердикты, но отчёт был
пустым: «без локального вердикта: 40» при полном ответе модели. Причина —
ключ поста брался из ``to_verdict_json()``, а ``lip`` в этой сериализации
нет (там только решение: theme/action/confidence/urgent). Все вердикты
схлопывались в одну запись ``""``, и ни один пост не находился.

Тест закрывает именно этот класс: **вердикт обязан находиться по своему
``lip``**, даже если в его JSON ключа нет.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from infra.windows.classifier_parity import (  # noqa: E402
    DECISION_FIELDS,
    compare,
    field_breakdown,
    index_verdicts,
)


class _FakeVerdict:
    """Вердикт как объект проекта: ``lip`` есть, в JSON решения — нет."""

    def __init__(self, lip: str, **fields):
        self.lip = lip
        self._fields = fields

    def to_verdict_json(self) -> dict:
        return dict(self._fields)


def test_lip_берётся_из_объекта_а_не_из_json_решения():
    verdict = _FakeVerdict(
        "93211844_436208",
        theme="новости",
        action="delete",
        urgent=False,
        confidence=86,
    )
    indexed = index_verdicts([verdict])

    assert list(indexed) == ["93211844_436208"]
    assert indexed["93211844_436208"]["action"] == "delete"
    # В JSON решения ключа поста нет — и не должно быть.
    assert "lip" not in verdict.to_verdict_json()


def test_вердикты_разных_постов_не_схлопываются():
    verdicts = [
        _FakeVerdict("a_1", action="publish", theme="новости", urgent=False, confidence=70),
        _FakeVerdict("a_2", action="delete", theme="мусор", urgent=False, confidence=92),
        _FakeVerdict("b_1", action="publish", theme="культура", urgent=True, confidence=81),
    ]
    indexed = index_verdicts(verdicts)

    assert set(indexed) == {"a_1", "a_2", "b_1"}
    assert indexed["a_2"]["theme"] == "мусор"
    assert indexed["b_1"]["urgent"] is True


def test_вердикт_без_lip_пропускается_а_не_ломает_сверку():
    assert index_verdicts([_FakeVerdict("", action="publish")]) == {}
    assert index_verdicts([]) == {}
    assert index_verdicts(None) == {}


def test_сравнение_ловит_расхождение_по_решающим_полям():
    prod = {"action": "delete", "theme": "мусор", "urgent": False, "confidence": 92}
    local = {"action": "delete", "theme": "новости", "urgent": False, "confidence": 88}

    diffs = compare(prod, local)

    assert set(diffs) == {"theme", "confidence"}
    assert diffs["theme"] == {"prod": "мусор", "local": "новости"}
    assert diffs["confidence"] == {"prod": 92, "local": 88}


def test_совпадающие_вердикты_расхождений_не_дают():
    verdict = {"action": "publish", "theme": "новости", "urgent": False, "confidence": 70}

    assert compare(verdict, dict(verdict)) == {}
    assert set(DECISION_FIELDS) == {"action", "theme", "urgent", "confidence"}


def test_разбор_по_полям_отделяет_решение_от_числа():
    """Решение (действие/тема) и уверенность (confidence) — разные вещи.

    Строгая сверка «совпало/не совпало» смешивает их: пост с тем же решением
    и другой уверенностью попадает в «расходится». Разбор по полям это
    разделяет — иначе в отчёте не видно, где риск, а где шум.
    """
    rows = [
        {
            "status": "match",
            "prod": {"action": "publish", "theme": "новости", "urgent": False, "confidence": 70},
            "local": {"action": "publish", "theme": "новости", "urgent": False, "confidence": 70},
        },
        {
            "status": "differs",
            "prod": {"action": "publish", "theme": "новости", "urgent": False, "confidence": 92},
            "local": {"action": "publish", "theme": "новости", "urgent": False, "confidence": 61},
        },
        {
            "status": "differs",
            "prod": {"action": "delete", "theme": "мусор", "urgent": False, "confidence": 88},
            "local": {"action": "publish", "theme": "новости", "urgent": True, "confidence": 88},
        },
        {
            "status": "no_local_verdict",
            "prod": {"action": "publish"},
            "local": None,
        },
    ]

    stats = field_breakdown(rows)

    assert stats["action"] == {"agree": 2, "differ": 1, "compared": 3}
    assert stats["theme"] == {"agree": 2, "differ": 1, "compared": 3}
    assert stats["urgent"] == {"agree": 2, "differ": 1, "compared": 3}
    assert stats["confidence"] == {"agree": 2, "differ": 1, "compared": 3}
