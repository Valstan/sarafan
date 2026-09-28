"""Unit-тесты для ``scripts/conveyor_dry_run.py`` — сухой прогон конвейера по сайту.

Скрипт — CLI-утилита вне устанавливаемого пакета, грузим напрямую через
importlib (как ``tests/test_scripts/test_smoke_test.py``). БД не поднимаем:
путь «нет сайта» её не касается, а живой прогон покрыт тестами runner'а.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "conveyor_dry_run", REPO_ROOT / "scripts" / "conveyor_dry_run.py"
)
dry = importlib.util.module_from_spec(_spec)
sys.modules["conveyor_dry_run"] = dry
_spec.loader.exec_module(dry)


def test_unknown_site_is_rc_1_without_db(capsys):
    assert dry.main(["--site", "нетакого"]) == 1
    assert "нет сайта" in capsys.readouterr().err


def test_render_human_shows_counts_and_preview():
    text = dry.render_human(
        {
            "site": "sabantuy",
            "selected": 2,
            "deduped": 1,
            "preview": [
                {"lip": "-1_1", "theme": "novost", "chars": 100, "media": 2},
            ],
            "preview_duplicates": [{"lip": "-1_2", "dup_of": "-1_1"}],
        }
    )
    assert "site=sabantuy selected=2 deduped=1" in text
    assert "-1_1" in text and "-1_2" in text


def test_main_json_prints_stats(monkeypatch, capsys):
    stats = {"site": "sabantuy", "selected": 0, "deduped": 0}
    monkeypatch.setattr(dry, "run_dry", lambda *a, **k: {"ok": True, "stats": stats})
    assert dry.main(["--site", "sabantuy", "--json"]) == 0
    assert '"site": "sabantuy"' in capsys.readouterr().out
