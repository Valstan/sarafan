"""Мост к локальному движку: чистые функции демона и журнал прогонов.

Тесты без БД и без сети: демон живёт на ПК владельца и поднимается без
зависимостей проекта, поэтому и проверяется как standalone-скрипт.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
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


@pytest.mark.asyncio
async def test_submit_shadow_does_not_write_to_corpus(tmp_path, monkeypatch):
    """Режим тени: вердикт не занимает пост в корпусе, но отчёт пишется.

    Так демон может работать, пока качество локальной модели не измерено:
    корпус остаётся облачным, а панель видит прогоны.
    """
    path = tmp_path / "runs.jsonl"
    monkeypatch.setenv("LOCAL_ENGINE_RUNS_LOG", str(path))

    called = {"n": 0}

    async def _record(session, verdicts, *, source="routine", region_codes_fallback=None):
        called["n"] += 1
        return {"recorded": 1, "skipped_existing": 0, "skipped_missing": 0}

    monkeypatch.setattr(local_engine.service, "record_verdicts", _record)

    from modules.classifier.schema import ClassifierVerdict

    verdict = ClassifierVerdict(lip="1_10", theme="новости", action="publish")
    result = await local_engine.submit_batch(
        None,
        {"verdicts": [verdict.model_dump()], "run": {"model": "test"}},
        record=False,
    )
    assert result["ok"] is True
    assert result["shadow"] is True
    assert result["recorded"] == 0
    assert called["n"] == 0, "в режиме тени корпус трогать нельзя"
    run = local_engine.read_runs()[-1]
    assert run["shadow"] is True
    assert run["verdicts_parsed"] == 1


# ───────── клиент opencode (локальный движок) ─────────


def _load_opencode_client():
    """Клиент opencode лежит в scripts/ рядом с демоном — грузим как модуль."""
    spec = importlib.util.spec_from_file_location(
        "local_engine_opencode", REPO_ROOT / "scripts" / "local_engine_opencode.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


opencode = _load_opencode_client()


def test_opencode_parse_messages_reads_text():
    """Текст ответа лежит в content[type=text] — по частям, не в одном поле."""
    messages = [
        {
            "info": {"tokens": {"input": 870, "output": 32}},
            "parts": [{"type": "text", "text": '{"verdicts": []}'}],
        }
    ]
    text, usage, error = opencode.parse_messages(messages)
    assert text == '{"verdicts": []}'
    assert error is None
    assert usage["input"] == 870


def test_opencode_parse_messages_surfaces_error():
    """Отказ сессии (idle.outcome != succeeded) не должен выглядеть как пустой ответ."""
    text, usage, error = opencode.parse_messages(
        [{"info": {"error": {"name": "provider.no-route"}}}]
    )
    assert text == ""
    assert error is not None
    assert "provider.no-route" in error


def test_opencode_read_password_missing_file():
    """Нет файла с паролем — понятный отказ, а не исключение на ровном месте."""
    with pytest.raises(opencode.OpencodeError):
        opencode.read_password(Path("нет-такого-файла.json"))


def test_opencode_chat_rejects_empty_prompt():
    result = opencode.chat(base_url="http://127.0.0.1:1", user="   ")
    assert result["ok"] is False
    assert result["reason"] == "empty_prompt"


def test_daemon_local_mode_runs_command_without_ssh():
    """Режим «демон на боксе»: мост зовётся локально, SSH не нужен.

    Так движок живёт и тикает на сервере, а ПК владельца не должен быть
    включён, чтобы классификация шла.
    """
    import argparse as _argparse

    daemon_module = _load_daemon()

    args = _argparse.Namespace(local=True, ssh_host="sarafan")
    out = daemon_module._exec(args, "echo локально")
    assert "локально" in out

    args = _argparse.Namespace(local=False, ssh_host="sarafan")
    assert hasattr(daemon_module, "_ssh"), "SSH-путь должен остаться"


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


def test_panel_template_links_static_js_instead_of_inline():
    """JS панели лежит отдельным файлом, а не inline в шаблоне.

    Inline-скрипт поймал SyntaxError на проде 10.11: страница открывалась, но
    все блоки оставались «загрузка…» — запроса к API не было вовсе, и ошибка
    выглядела как «панель просто пустая».
    """
    html = (TEMPLATES_DIR / "local_engine.html").read_text(encoding="utf-8")
    assert "/static/js/local_engine.js" in html
    assert "<script>" not in html


def test_source_labels_are_human_readable():
    """Источники вердикта в панели читаются по-русски, а не служебными ключами.

    В базе ``source`` — служебный ключ (``routine``/``api``/``headless``/
    ``local``): в панели он должен читаться. Названия отдаёт API — тогда они
    живут в одном месте.
    """
    labels = local_engine.SOURCE_LABELS
    assert labels["headless"] == "Волна на сервере"
    assert labels["local"] == "Демон Опенкода"
    assert labels["routine"] == "Старая рутина (удалена)"
    assert labels["api"] == "Приём готовых вердиктов"
    for key, label in labels.items():
        assert label and label != key, "ключ %s должен называться по-русски" % key

    js = (REPO_ROOT / "web" / "static" / "js" / "local_engine.js").read_text(encoding="utf-8")
    for key in labels:
        assert "sourceLabel(s, labels)" in js
    assert "source_labels" in js


def test_dead_source_is_marked_as_not_working():
    """Мёртвый путь нельзя показывать живым.

    Старая облачная рутина удалена владельцем 11.11: она не тратит денег и
    больше не разбирает посты. Пока она в панели светилась как «ещё один
    источник», человек читал «рутина обрабатывает посты» — а она не
    обрабатывает ничего с 06.11.
    """
    assert local_engine.SOURCE_ALIVE["routine"] is False
    assert local_engine.SOURCE_ALIVE["headless"] is True
    assert local_engine.SOURCE_ALIVE["local"] is True
    for key in local_engine.SOURCE_ALIVE:
        assert key in local_engine.SOURCE_LABELS
        assert local_engine.SOURCE_NOTES.get(key), "у пути %s должно быть пояснение" % key


@pytest.mark.parametrize(
    "model,expected",
    [
        ("deepseek-flash", local_engine.ENGINE_DEEPSEEK),
        ("deepseek-chat", local_engine.ENGINE_DEEPSEEK),
        ("deepseek-v4-pro", local_engine.ENGINE_DEEPSEEK),
        ("opencode/deepseek-v4.1-flash-free", local_engine.ENGINE_OPENCODE),
        ("opencode/glm-5.3-flash-free", local_engine.ENGINE_OPENCODE),
        ("deepseek-v4.1-flash-free", local_engine.ENGINE_OPENCODE),
        ("flint-40b-free", local_engine.ENGINE_OPENCODE),
        ("glm-5.3-flash-free", local_engine.ENGINE_OPENCODE),
        ("minimax/MiniMax-M3.1-Flash", local_engine.ENGINE_MINIMAX),
        ("", local_engine.ENGINE_UNKNOWN),
        (None, local_engine.ENGINE_UNKNOWN),
    ],
)
def test_engine_of_model_tells_paid_from_free(model, expected):
    """Движок достаётся из имени модели: платный Дипсик ≠ бесплатный Опенкод.

    Путь (``source``) и движок (``model``) — разные вещи: одна волна считает и
    тем, и другим. Пока панель звала «облако» = «Дипсик», работа бесплатного
    Опенкода внутри той же волны (23 813 вердиктов за месяц) была не видна.
    """
    assert local_engine.engine_of_model(model) == expected


def test_engine_buckets_split_paid_and_free():
    """Вердикты раскладываются по движкам, а не по путям доставки."""
    rows = [
        ("deepseek-flash", 4430),
        ("opencode/deepseek-v4.1-flash-free", 23813),
        ("flint-40b-free", 19),
        ("minimax/MiniMax-M3.1-Flash", 120),
        ("", 7),
    ]
    bucket = local_engine.engine_buckets(rows)
    assert bucket[local_engine.ENGINE_DEEPSEEK] == 4430
    assert bucket[local_engine.ENGINE_OPENCODE] == 23832
    assert bucket[local_engine.ENGINE_MINIMAX] == 120
    assert bucket[local_engine.ENGINE_UNKNOWN] == 7
    assert sum(bucket.values()) == sum(n for _m, n in rows)


def test_sum_by_source_adds_days_instead_of_keeping_last():
    """«За 30 дней» по пути доставки — это сумма дней, а не цифры одного дня.

    На проде словарь собирался так, что предыдущие дни перетирались
    последним: «за 30 дней» показывало 4 430 (это был один день) вместо
    суммы. Панель врала молча — цифра выглядела правдоподобно.
    """
    rows = [
        ("headless", "2026-11-10", 5008),
        ("headless", "2026-11-11", 4430),
        ("local", "2026-11-11", 55),
    ]
    total = local_engine.sum_by_source(rows)
    assert total["headless"] == 9438
    assert total["local"] == 55
    assert sum(total.values()) == 9493


def test_stats_payload_carries_engine_split(tmp_path, monkeypatch):
    """Сводка отдаёт разрез «кем ответили», режим движка и состояние демона.

    Контракт страницы: без этих полей панель снова станет «кашей» — путь,
    движок и живость должны быть видны в одном ответе.
    """
    monkeypatch.setenv("LOCAL_ENGINE_RUNS_LOG", str(tmp_path / "runs.jsonl"))
    monkeypatch.setenv("CLASSIFIER_ENGINE", "minimax")

    class _Result:
        def all(self):
            return []

        def scalar(self):
            return datetime(2026, 11, 11, 18, 13, 6, 840047)

    class _Session:
        async def execute(self, *args, **kwargs):
            return _Result()

    data = __import__("asyncio").run(local_engine.stats(_Session()))

    for key in (
        "engine_windows",
        "engine_by_day",
        "models_30d",
        "engine_now",
        "daemon",
        "source_meta",
        "last_verdict_at",
        "verdicts_by_source_24h",
    ):
        assert key in data, "сводка без %s" % key

    assert data["engine_now"]["mode"] == "minimax"
    assert "Минимакс" in data["engine_now"]["label"]
    assert set(data["engine_windows"]["24h"]["by_engine"]) == {
        local_engine.ENGINE_DEEPSEEK,
        local_engine.ENGINE_OPENCODE,
        local_engine.ENGINE_MINIMAX,
        local_engine.ENGINE_UNKNOWN,
    }
    assert data["source_meta"]["routine"]["alive"] is False
    assert data["source_meta"]["headless"]["alive"] is True
    # Контур opencode снят с прода 14.11 — панель говорит об этом прямо.
    assert data["daemon"]["decommissioned"] is True
    assert "MiniMax" in data["daemon"]["note"]

    # Время из БД — UTC без зоны. Без пометки UTC браузер прочитает его как
    # московское и покажет «4 ч назад» вместо «1 ч назад»: минус три часа врёт
    # ровно в ту сторону, где человек не догадается, что ошибка в часах.
    assert data["last_verdict_at"].endswith("+00:00")
    assert "18:13:06" in data["last_verdict_at"]


def test_panel_shows_who_answered_and_what_is_alive():
    """Панель обязана показывать разрез «кем ответили» и состояние путей.

    Без этого страница читалась как «каша»: в одном блоке «облако», в другом
    «рутина обрабатывает посты», и понять, что работает и кто платит, было
    нельзя.
    """
    html = (TEMPLATES_DIR / "local_engine.html").read_text(encoding="utf-8")
    js = (REPO_ROOT / "web" / "static" / "js" / "local_engine.js").read_text(encoding="utf-8")

    for token in ("engine_windows", "engine_by_day", "models_30d", "source_meta"):
        assert token in js, "JS панели должен рисовать %s" % token
    for block_id in (
        "c-engine-now",
        "c-v-24h",
        "c-daemon",
        "c-last",
        "models-box",
        "engine-days-box",
        "sources-box",
    ):
        assert block_id in html, "в шаблоне панели нет блока %s" % block_id
        assert block_id in js, "JS панели не рисует блок %s" % block_id

    # Служебные английские подписи в шаблоне не остаются.
    assert ">action<" not in html
    assert ">theme<" not in html
    assert "merge" not in html


def test_panel_js_parses():
    """Синтаксис JS панели проверяется node'ом (если он есть)."""
    import shutil
    import subprocess

    js = REPO_ROOT / "web" / "static" / "js" / "local_engine.js"
    assert js.exists(), "JS панели должен лежать отдельным файлом"

    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — синтаксис JS не проверить")

    proc = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
