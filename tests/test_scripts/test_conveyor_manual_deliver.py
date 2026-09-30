"""Unit-тесты для ``scripts/conveyor_manual_deliver.py`` — разбор файла и печать.

БД не поднимаем: путь «нет сайта / нет режима» её не касается, а весь живой
прогон вместе со сверкой журнала закрыт в ``tests/test_conveyor/test_manual_mode.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "conveyor_manual_deliver", REPO_ROOT / "scripts" / "conveyor_manual_deliver.py"
)
manual = importlib.util.module_from_spec(_spec)
sys.modules["conveyor_manual_deliver"] = manual
_spec.loader.exec_module(manual)


def test_mode_is_required(capsys):
    assert manual.main(["--site", "kultura"]) == 1
    assert "ровно один режим" in capsys.readouterr().err


def test_two_modes_at_once_is_refused(capsys):
    """``--check`` и ``--deliver`` вместе — это «и посмотреть, и отправить»."""
    assert manual.main(["--site", "kultura", "--check", "a.json", "--deliver", "b.json"]) == 1
    assert "ровно один режим" in capsys.readouterr().err


def test_unknown_site_is_rc_1_without_db(capsys):
    assert manual.main(["--site", "нетакого", "--emit", "x.json"]) == 1
    assert "нет сайта" in capsys.readouterr().err


def test_verdicts_bare_map_is_accepted(tmp_path):
    f = tmp_path / "v.json"
    f.write_text(json.dumps({"1_10": {"action": "accept"}}), encoding="utf-8")
    assert manual.load_verdicts(str(f)) == {"1_10": {"action": "accept"}}


def test_emit_file_keeps_candidates_next_to_verdicts(tmp_path):
    """Файл ``--emit`` и файл ``--deliver`` — один и тот же, дописанный на месте."""
    f = tmp_path / "plan.json"
    f.write_text(
        json.dumps({"candidates": [{"lip": "1_10"}], "verdicts": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert manual.load_verdicts(str(f)) == {}


def test_candidates_without_verdicts_field_is_an_error(tmp_path):
    """Недописанный план и пустой план выглядят одинаково — и это ровно тот
    случай, где молчание дороже всего."""
    f = tmp_path / "plan.json"
    f.write_text(json.dumps({"candidates": [{"lip": "1_10"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="verdicts"):
        manual.load_verdicts(str(f))


def test_verdict_must_be_object(tmp_path):
    f = tmp_path / "v.json"
    f.write_text(json.dumps({"1_10": "accept"}), encoding="utf-8")
    with pytest.raises(ValueError, match="должен быть объектом"):
        manual.load_verdicts(str(f))


def test_render_delivery_shows_mismatch_not_as_a_summary():
    text = manual.render_delivery(
        {
            "site": "kultura",
            "selected": 2,
            "delivered": 2,
            "rejected": 0,
            "held": 0,
            "failed": 0,
            "skipped": 0,
            "results": [{"lip": "1_10", "bucket": "delivered", "remote_id": "r1"}],
            "without_verdict": [],
        },
        [
            {
                "lip": "1_10",
                "reported": "delivered",
                "journal": "selected",
                "http_status": 201,
                "remote_id": "r1",
            }
        ],
        days=30,
    )
    assert "delivered=2" in text
    # Окно печатается: «почти пусто» и «окно слишком узкое» неразличимы,
    # пока число не названо (замер 30.09: 0 кандидатов на 3 сутках).
    assert "окно=30" in text
    assert "РАСХОЖДЕНИЕ С ЖУРНАЛОМ" in text
    assert "журнал=selected" in text


def test_manual_window_default_is_thirty_days():
    """Ручной прогон — добор, а не стриминг: дефолт конвейера (3 суток) для него
    почти бесполезен (0 кандидатов из 28 на «Культуре»)."""
    assert manual.DEFAULT_DAYS == 30


def test_default_days_argument_is_thirty():
    """Проверяем именно разбор аргументов: значение по умолчанию задаётся там."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=manual.DEFAULT_DAYS)
    assert parser.parse_args([]).days == 30
    assert parser.parse_args(["--days", "3"]).days == 3


def test_render_plan_names_the_lips_without_verdicts():
    text = manual.render_plan(
        {
            "site": "kultura",
            "rows": [
                {"lip": "1_10", "verdict": "уедет", "detail": "novosti · Заголовок"},
                {"lip": "2_20", "verdict": "нет вердикта", "detail": "останется в отборе"},
            ],
            "unknown_lips": ["9_90"],
        }
    )
    assert "1_10: уедет" in text and "2_20: нет вердикта" in text
    assert "9_90" in text


def test_emit_payload_has_place_to_write_verdicts():
    payload = manual._emit_payload(
        "kultura", {"preview": [{"lip": "1_10", "text": "текст"}], "preview_duplicates": []}
    )
    assert payload["verdicts"] == {}
    assert payload["candidates"][0]["lip"] == "1_10"
    assert "action" in payload["note"]
