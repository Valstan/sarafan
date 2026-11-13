"""Выгрузка сигнала дообучения: чистые функции скрипта (ADR-0016).

Проверяем то, что тихо ломает обучение: что решение оператора побеждает вердикт
модели, что по одной и той же паре (пост, аспект) берётся ПОСЛЕДНЯЯ правка
(оператор мог передумать), и что в обучающую выборку не попадает обоснование
модели — оно противоречит исправленному решению.
"""

from __future__ import annotations

import json

import pytest

from scripts import export_training_sample as exp


def _post(**kwargs):
    base = {
        "lip": "111_222",
        "region_code": "mi",
        "post_text": "В Малмыже открылась новая библиотека",
        "post_url": "https://vk.com/wall-111_222",
        "verdict": {
            "theme": "новости",
            "action": "delete",
            "merge_with": [],
            "split": False,
            "confidence": 88,
            "urgent": False,
            "importance": 40,
            "reasoning": "модель решила, что это не наше",
        },
        "overrides": {"action": "publish"},
        "corrected_at": None,
    }
    base.update(kwargs)
    return base


def test_operator_action_overrides_model_verdict():
    verdict = exp._target_verdict(_post())
    assert verdict["action"] == "publish"
    assert verdict["theme"] == "новости"
    assert verdict["lip"] == "111_222"


def test_operator_theme_overrides_model_verdict():
    verdict = exp._target_verdict(_post(overrides={"theme": "культура"}))
    assert verdict["theme"] == "культура"
    assert verdict["action"] == "delete"


def test_reasoning_is_dropped():
    """Обоснование модели противоречит исправленному решению — учить на нём нельзя."""
    verdict = exp._target_verdict(_post())
    assert verdict["reasoning"] == ""


def test_confidence_and_urgent_inherited():
    verdict = exp._target_verdict(_post())
    assert verdict["confidence"] == 88
    assert verdict["urgent"] is False
    assert verdict["importance"] == 40


def test_missing_importance_becomes_zero():
    post = _post(verdict={"theme": "новости", "action": "delete"})
    assert exp._target_verdict(post)["importance"] == 0


def test_merge_override_is_list():
    verdict = exp._target_verdict(_post(overrides={"merge": ["333_444"]}))
    assert verdict["merge_with"] == ["333_444"]


def test_merge_correction_object_marks_split():
    """Форма правки склейки в БД — объект {"split": true}, а не список lip'ов."""
    verdict = exp._target_verdict(_post(overrides={"merge": {"split": True}}))
    assert verdict["split"] is True
    assert verdict["merge_with"] == []


def test_merge_correction_string_is_not_split_into_characters():
    """Строка-флаг нельзя превращать в список посимвольно — в таргет уехал бы мусор."""
    verdict = exp._target_verdict(_post(overrides={"merge": "split"}))
    assert verdict["merge_with"] == []
    assert verdict["split"] is True


def test_merge_with_from_model_is_not_inherited():
    """Склейка от модели — её догадка; учить на ней нельзя."""
    post = _post(verdict={"theme": "новости", "action": "delete", "merge_with": ["999_888"]})
    verdict = exp._target_verdict(post)
    assert verdict["merge_with"] == []


def test_assistant_message_uses_prod_envelope():
    """Ответ должен выглядеть так же, как на проде: {"verdicts": [...]}."""
    raw = exp._assistant_message(exp._target_verdict(_post()))
    parsed = json.loads(raw)
    assert list(parsed.keys()) == ["verdicts"]
    assert len(parsed["verdicts"]) == 1
    assert parsed["verdicts"][0]["lip"] == "111_222"


def test_post_payload_shape():
    payload = exp._post_payload(
        _post(theme_hint="новости", media=[{"type": "photo"}], views=10, likes=1)
    )
    assert payload["lip"] == "111_222"
    assert payload["region_code"] == "mi"
    assert payload["media"] == [{"type": "photo"}]
    assert payload["views"] == 10
    assert payload["text"].startswith("В Малмыже")


@pytest.mark.asyncio
async def test_collect_takes_last_correction_per_aspect():
    """По одной паре (пост, аспект) побеждает последняя правка оператора."""
    rows = [
        (
            "111_222",
            "action",
            "delete",
            "publish",
            None,
            "mi",
            "текст",
            "url",
            {"theme": "новости", "action": "publish"},
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            "111_222",
            "action",
            "publish",
            "publish",
            None,
            "mi",
            "текст",
            "url",
            {"theme": "новости", "action": "publish"},
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    ]

    class _Result:
        def all(self):
            return rows

    class _Session:
        async def execute(self, _stmt):
            return _Result()

    posts = await exp._collect(_Session(), days=0, limit=0)
    assert len(posts) == 1
    assert posts[0]["overrides"]["action"] == "publish"


def test_write_jsonl_one_object_per_line(tmp_path):
    path = tmp_path / "x.jsonl"
    exp._write_jsonl(path, [{"a": 1}, {"b": "два"}])
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1]) == {"b": "два"}
