"""Мост к локальному движку: чистые функции демона и журнал прогонов.

Тесты без БД и без сети: демон живёт на ПК владельца и поднимается без
зависимостей проекта, поэтому и проверяется как standalone-скрипт.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from modules.classifier import local_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_PATH = REPO_ROOT / "scripts" / "local_engine_daemon.py"
TEMPLATES_DIR = REPO_ROOT / "web" / "templates"


def _load_daemon():
    """Загрузить демона как модуль (он лежит в scripts/, а не в пакете)."""
    spec = importlib.util.spec_from_file_location("local_engine_daemon", DAEMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daemon = _load_daemon()


# ───────── разбор ответа модели ─────────


def test_extract_json_reads_plain_json():
    assert daemon._extract_json('{"verdicts": []}') == {"verdicts": []}


def test_extract_json_unwraps_code_fence():
    """Модель часто оборачивает JSON в ```json — это не повод терять батч."""
    text = '```json\n{"verdicts": [{"lip": "1_2"}]}\n```'
    assert daemon._extract_json(text) == {"verdicts": [{"lip": "1_2"}]}


def test_extract_json_returns_none_on_garbage():
    assert daemon._extract_json("не JSON вовсе") is None


# ───────── нормализация вердиктов ─────────


def _posts():
    return [
        {"lip": "1_10", "region_code": "mi"},
        {"lip": "1_11", "region_code": "mi"},
    ]


def test_normalize_drops_unknown_lip():
    """Чужой lip в запись не идёт: он заблокировал бы чужую публикацию."""
    out = daemon._normalize_verdicts(
        {"verdicts": [{"lip": "999_999", "action": "publish", "theme": "новости"}]},
        _posts(),
    )
    assert out == []


def test_normalize_keeps_known_lips_and_clamps():
    out = daemon._normalize_verdicts(
        {
            "verdicts": [
                {
                    "lip": "1_10",
                    "theme": "новости",
                    "action": "publish",
                    "confidence": 250,
                    "reasoning": "ок",
                }
            ]
        },
        _posts(),
    )
    assert len(out) == 1
    assert out[0]["lip"] == "1_10"
    assert out[0]["confidence"] == 100


def test_normalize_forces_unknown_action_to_hold():
    """Неизвестное действие — это «сомневаюсь», а не «удалить пост»."""
    out = daemon._normalize_verdicts(
        {"verdicts": [{"lip": "1_10", "theme": "новости", "action": "какая-то дичь"}]},
        _posts(),
    )
    assert out[0]["action"] == "hold"


def test_normalize_drops_foreign_merge_targets():
    out = daemon._normalize_verdicts(
        {
            "verdicts": [
                {
                    "lip": "1_10",
                    "theme": "новости",
                    "action": "publish",
                    "merge_with": ["1_11", "999_999"],
                }
            ]
        },
        _posts(),
    )
    assert out[0]["merge_with"] == ["1_11"]


# ───────── журнал прогонов (источник статистики панели) ─────────


def test_runs_log_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "runs.jsonl"
    monkeypatch.setenv("LOCAL_ENGINE_RUNS_LOG", str(path))
    local_engine.append_run({"ts": "2026-11-10T10:00:00+00:00", "posts": 20, "recorded": 18})
    local_engine.append_run({"ts": "2026-11-10T10:05:00+00:00", "posts": 0, "recorded": 0})
    runs = local_engine.read_runs()
    assert len(runs) == 2
    assert runs[0]["posts"] == 20
    assert runs[-1]["posts"] == 0


def test_runs_log_ignores_broken_lines(tmp_path, monkeypatch):
    """Битая строка журнала не должна ронять чтение статистики."""
    path = tmp_path / "runs.jsonl"
    path.write_text('{"ok": true}\nне JSON\n{"ok": false}\n', encoding="utf-8")
    monkeypatch.setenv("LOCAL_ENGINE_RUNS_LOG", str(path))
    runs = local_engine.read_runs()
    assert len(runs) == 2


def test_read_runs_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_ENGINE_RUNS_LOG", str(tmp_path / "нет-такого.jsonl"))
    assert local_engine.read_runs() == []


# ───────── приём вердиктов: чужие lip не пишутся ─────────


@pytest.mark.asyncio
async def test_submit_rejects_unknown_lip(tmp_path, monkeypatch):
    """Чужой lip не должен попасть в корпус: запись идёт по нашим постам."""
    path = tmp_path / "runs.jsonl"
    monkeypatch.setenv("LOCAL_ENGINE_RUNS_LOG", str(path))

    recorded: dict = {}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _record(session, verdicts, *, source="routine", region_codes_fallback=None):
        recorded["source"] = source
        recorded["lips"] = [v.lip for v in verdicts]
        return {"recorded": 0, "skipped_existing": 0, "skipped_missing": 1}

    monkeypatch.setattr(local_engine.service, "record_verdicts", _record)

    from modules.classifier.schema import ClassifierVerdict

    verdict = ClassifierVerdict(lip="999_999", theme="новости", action="publish")
    result = await local_engine.submit_batch(
        _Session(), {"verdicts": [verdict.model_dump()], "run": {"model": "test"}}
    )
    assert result["ok"] is True
    assert result["recorded"] == 0
    assert recorded["source"] == "local"
    # Отчёт о прогоне записан — из него панель берёт статистику.
    assert local_engine.read_runs()[-1]["source"] == "local"


@pytest.mark.asyncio
async def test_submit_requires_verdict_list():
    result = await local_engine.submit_batch(None, {"не то": "веридты"})
    assert result["ok"] is False
    assert "verdicts" in result["error"]


# ───────── SQL панели: усечение даты литералом, а не параметром ─────────


def _pg_sql(query) -> str:
    """SQL запроса под диалектом Postgres (с параметрами, как их видит сервер)."""
    from sqlalchemy.dialects import postgresql

    return str(query.compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize(
    "builder,unit,column",
    [
        (
            local_engine._daily_by_source_query,
            "day",
            "content_classifications.created_at",
        ),
        (
            local_engine._weekly_quality_query,
            "week",
            "classification_corrections.created_at",
        ),
        (
            local_engine._corrections_by_week_query,
            "week",
            "classification_corrections.created_at",
        ),
    ],
)
def test_truncation_unit_is_literal_not_bind(builder, unit, column):
    """Единица усечения — литералом, а не параметром запроса.

    С биндом Postgres не узнаёт выражение в GROUP BY («column must appear in
    the GROUP BY clause»): номера параметров в SELECT и GROUP BY разные, и
    группировка падает. Поймано на проде 10.11 — 500 на
    ``/api/local-engine/stats``.
    """
    sql = _pg_sql(builder())
    expr = "date_trunc('%s', %s)" % (unit, column)
    assert expr in sql, sql
    group_by = sql.split("GROUP BY", 1)[1]
    assert expr in group_by, sql
    assert "date_trunc(:" not in sql
    assert "date_trunc($" not in sql


def test_json_is_serialisable():
    """Отчёт о прогоне идёт в JSONL — значит, должен быть сериализуем."""
    json.dumps({"posts": 1, "recorded": 1, "regions": ["mi"]})


# ───────── шаблон панели ─────────


def test_panel_template_uses_blocks_that_exist_in_base():
    """Дочерний шаблон обязан брать блоки, которые реально есть в base.

    В ``base.html`` нет блока ``scripts`` — есть ``extra_scripts``. Шаблон с
    несуществующим блоком рендерится **молча**: страница остаётся «загрузка…»,
    потому что её JS просто не попал в HTML. Поймано на проде 10.11.
    """
    import jinja2

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES_DIR))
    base = env.get_template("base.html")
    panel = env.get_template("local_engine.html")

    unknown = sorted(b for b in panel.blocks if b not in base.blocks)
    assert unknown == [], "шаблон панели использует блоки, которых нет в base: %s" % unknown
    assert "extra_scripts" in panel.blocks
    assert "content" in panel.blocks
