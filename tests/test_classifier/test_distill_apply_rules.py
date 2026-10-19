"""Tests прямого применения правок выученных правил (``scripts/distill_apply_rules.py``).

Скрипт — CLI вне устанавливаемого пакета, поэтому грузим его через importlib,
как ``tests/test_scripts/test_discover_scan.py``. Проверяем три вещи, ради которых
инструмент и написан: dry-run ничего не меняет, ``--apply`` пишет через
``decide_rule`` (с пересчётом ``norm_key``), а слишком длинный текст отвергается
до записи — иначе ``decide_rule`` обрезал бы правило молча.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from database.models_extended import ClassificationRule

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "distill_apply_rules", REPO_ROOT / "scripts" / "distill_apply_rules.py"
)
dar = importlib.util.module_from_spec(_spec)
sys.modules["distill_apply_rules"] = dar
_spec.loader.exec_module(dar)


async def _seed_rule(session, *, text, status="approved"):
    r = ClassificationRule(rule_text=text, status=status, source="routine", norm_key=text.lower())
    session.add(r)
    await session.commit()
    return r


def _write_edits(tmp_path, payload) -> str:
    path = tmp_path / "edits.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ───────── load_edits / validate_edits (без БД) ─────────


def test_load_edits_skips_comment_keys(tmp_path):
    path = _write_edits(tmp_path, {"_note": "комментарий", "18": "текст"})
    assert dar.load_edits(path) == {18: "текст"}


def test_load_edits_rejects_non_object(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        dar.load_edits(path)


def test_validate_edits_refuses_too_long():
    too_long = "я" * (dar._MAX_RULE_LEN + 1)
    problems = dar.validate_edits({1: too_long})
    assert problems and "лимите" in problems[0]
    assert dar.validate_edits({}) != []
    assert dar.validate_edits({1: "  "}) != []
    assert dar.validate_edits({1: "я" * dar._MAX_RULE_LEN}) == []


# ───────── apply_edits (in-memory БД) ─────────


@pytest.mark.asyncio
async def test_dry_run_changes_nothing(db_session):
    rule = await _seed_rule(db_session, text="старый текст")
    stats = await dar.apply_edits(
        db_session, {rule.id: "новый текст"}, apply=False, out=lambda _: None
    )
    assert stats == {"checked": 1, "applied": 0, "refused": 0, "mismatched": 0}
    await db_session.refresh(rule)
    assert rule.rule_text == "старый текст"


@pytest.mark.asyncio
async def test_apply_edits_rewrites_text_and_norm_key(db_session):
    rule = await _seed_rule(db_session, text="старый текст")
    stats = await dar.apply_edits(
        db_session, {rule.id: "Новый Текст"}, apply=True, out=lambda _: None
    )
    assert stats["applied"] == 1 and stats["refused"] == 0 and stats["mismatched"] == 0
    await db_session.refresh(rule)
    assert rule.rule_text == "Новый Текст"
    assert rule.norm_key == "новый текст"  # пересчитан приложением, не рукой
    assert rule.status == "approved"


@pytest.mark.asyncio
async def test_apply_edits_refuses_missing_and_not_approved(db_session):
    draft = await _seed_rule(db_session, text="черновик", status="proposed")
    out: list = []
    stats = await dar.apply_edits(
        db_session, {draft.id: "правка", 99999: "правка"}, apply=True, out=out.append
    )
    assert stats["refused"] == 2 and stats["applied"] == 0
    await db_session.refresh(draft)
    assert draft.rule_text == "черновик" and draft.status == "proposed"


@pytest.mark.asyncio
async def test_apply_edits_is_idempotent(db_session):
    rule = await _seed_rule(db_session, text="старый текст")
    edits = {rule.id: "один и тот же текст"}
    await dar.apply_edits(db_session, edits, apply=True, out=lambda _: None)
    stats = await dar.apply_edits(db_session, edits, apply=True, out=lambda _: None)
    assert stats["applied"] == 1 and stats["mismatched"] == 0
