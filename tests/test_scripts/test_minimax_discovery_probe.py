"""Unit-тесты для ``scripts/minimax_discovery_probe.py`` — проба пути discovery.

**Зачем.** 24.11 ночной срез discovery отработал целиком на платном Дипсике:
MiniMax не ответил ни разу (``--system-prompt-file ... is empty``), и это было
видно только в панели расхода — по логу узнали через сутки. Проба существует
именно для этого класса отказа: **отказ MiniMax должен быть виден сразу**, а не
замаскирован платной страховкой.

Скрипт — CLI-утилита вне пакета, грузим через importlib (как
``tests/test_scripts/test_minimax_renew_check.py``). Никаких вызовов движка:
``categorize_candidate`` подменяется, поэтому тесты не ходят ни в MiniMax, ни в
сеть.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "minimax_discovery_probe", REPO_ROOT / "scripts" / "minimax_discovery_probe.py"
)
probe = importlib.util.module_from_spec(_spec)
sys.modules["minimax_discovery_probe"] = probe
_spec.loader.exec_module(probe)

from modules.discovery.ai_categorizer import ENV_ENGINE, ENV_PAID_FALLBACK  # noqa: E402


def _ok_result() -> dict:
    return {
        "success": True,
        "engine": "minimax",
        "model": "MiniMax-M3.1-Flash",
        "category": "admin",
        "confidence": 88,
        "is_info_page": True,
        "reasoning": "официальная группа администрации",
        "tokens_prompt": 14780,
        "tokens_completion": 159,
    }


def _run(monkeypatch, capsys, *, result: dict, argv: list | None = None):
    """Прогнать ``main()`` с подменённым движком. Возвращает (код, вывод, вызовы).

    Env-ключи движка выставляются через ``monkeypatch`` **до** запуска: так
    pytest вернёт их исходные значения после теста, даже если скрипт переписал
    их напрямую (``os.environ[...] = ...``).
    """
    seen: list[dict] = []

    async def fake_categorize(**kwargs):
        seen.append(kwargs)
        return result

    monkeypatch.setenv(ENV_ENGINE, "deepseek")
    monkeypatch.setenv(ENV_PAID_FALLBACK, "1")
    monkeypatch.setattr(probe, "categorize_candidate", fake_categorize)
    monkeypatch.setattr(sys, "argv", ["minimax_discovery_probe.py"] + list(argv or []))
    code = probe.main()
    captured = capsys.readouterr()
    return code, captured, seen


def test_success_reports_minimax_and_exits_zero(monkeypatch, capsys):
    """MiniMax ответил — код возврата 0, в JSON видно движок и вердикт."""
    code, captured, seen = _run(monkeypatch, capsys, result=_ok_result())

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["engine"] == "minimax"
    assert payload["category"] == "admin"
    assert payload["confidence"] == 88
    assert payload["tokens_prompt"] == 14780
    assert len(seen) == 1


def test_failure_exits_one_and_says_so(monkeypatch, capsys):
    """Отказ движка — код возврата 1 и внятная причина, а не тишина."""
    code, captured, _ = _run(
        monkeypatch,
        capsys,
        result={"success": False, "engine": "minimax", "error": "no_accounts"},
    )

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert payload["error"] == "no_accounts"
    assert "ПРОБА НЕ ПРОШЛА" in captured.err


def test_paid_fallback_is_off_by_default(monkeypatch, capsys):
    """Платная страховка Дипсика выключена — иначе отказ MiniMax не виден.

    Именно так 24.11 откат ночного среза на Дипсик остался незамеченным: срез
    «отработал», а ответивший движок был не тот.
    """
    _run(monkeypatch, capsys, result=_ok_result())

    assert os.environ[ENV_PAID_FALLBACK] == "0"
    assert os.environ[ENV_ENGINE] == "minimax"


def test_allow_paid_fallback_keeps_fallback_enabled(monkeypatch, capsys):
    """Явный флаг возвращает страховку — когда платный вызов нужен намеренно."""
    _run(monkeypatch, capsys, result=_ok_result(), argv=["--allow-paid-fallback"])

    assert os.environ[ENV_PAID_FALLBACK] == "1"


def test_card_reaches_the_categorizer(monkeypatch, capsys):
    """Карточка сообщества доходит до категоризатора как есть."""
    _, _, seen = _run(monkeypatch, capsys, result=_ok_result())

    card = seen[0]
    assert card["name"]
    assert card["region_name"]
    assert card["recent_posts"]
