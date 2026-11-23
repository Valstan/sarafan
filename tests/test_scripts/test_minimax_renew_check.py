"""Unit-тесты для ``scripts/minimax_renew_check.py`` — проверка продления входа (P227).

Скрипт — CLI-утилита вне пакета, грузим через importlib (как
``tests/test_scripts/test_neural_quota_probe.py``). На бокс здесь ничего не
ходит: скрипт только читает файлы, поэтому проверяются разбор журналов,
чтение токен-стора и — главное — **вердикт**: когда продление ещё не могло
сработать, когда сработало, а когда Карман его отклонил.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "minimax_renew_check", REPO_ROOT / "scripts" / "minimax_renew_check.py"
)
check = importlib.util.module_from_spec(_spec)
sys.modules["minimax_renew_check"] = check
_spec.loader.exec_module(check)

#: Опорное время в epoch-секундах (2026-11-23 12:00:00 МСК).
NOW = 1795424400.0


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree(
    tmp_path: Path, *, expires_at: float, session_status: str = "authenticated"
) -> Dict[str, Path]:
    """Дерево файлов рабочей области и журналов — как на боксе."""
    accounts = tmp_path / "accounts"
    work = accounts / "work"
    auth_dir = work / "data" / "auth" / "prod" / "en" / "mcode-public"
    app = tmp_path / "app"

    _write(
        work / "profile.json",
        json.dumps(
            {
                "karman_account_id": 197,
                "email": "054@pochtaminimax.ru",
                "leased_day": "2026-11-22",
                "activation_day": "2026-11-22",
                "dead_account_ids": [8, 17, 120],
            }
        ),
    )
    _write(
        auth_dir / "auth.json",
        json.dumps(
            {
                "schemaVersion": 1,
                "records": {
                    "com.minimax.mcode.oauth.prod.en\\0abc": {
                        "accessToken": "СЕКРЕТ-НЕ-ПЕЧАТАЕМ",
                        "expiresAtMs": int(expires_at * 1000),
                        "generation": 4,
                    }
                },
            }
        ),
    )
    _write(
        auth_dir / "auth-state.json",
        json.dumps(
            {
                "status": session_status,
                "generation": 4,
                "expiresAtMs": int(expires_at * 1000),
            }
        ),
    )
    return {"accounts": accounts, "work": work, "app": app}


class TestParseLogTimestamp:
    def test_reads_celery_prefix(self) -> None:
        line = "[2026-11-23 10:12:00,006: INFO/ForkPoolWorker-2] minimax: вход аккаунта 197 продлён"
        stamp = check.parse_log_timestamp(line)
        assert stamp is not None
        assert datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S") == "2026-11-23 10:12:00"

    def test_foreign_line_is_none(self) -> None:
        assert check.parse_log_timestamp("просто строка") is None


class TestTokenState:
    def test_reads_expiry_and_session(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, expires_at=NOW + 3600)
        state = check.token_state(tree["work"])
        assert state["token_expires_at_ms"] == int((NOW + 3600) * 1000)
        assert state["session_status"] == "authenticated"
        assert state["generation"] == 4
        assert state["store_present"] is True
        assert state["session_present"] is True

    def test_missing_files_are_not_an_error(self, tmp_path: Path) -> None:
        state = check.token_state(tmp_path / "нет-такого")
        assert state["token_expires_at_ms"] == 0
        assert state["session_status"] == ""
        assert state["store_present"] is False


class TestProfileState:
    def test_reads_account(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, expires_at=NOW + 3600)
        profile = check.profile_state(tree["work"])
        assert profile["karman_id"] == 197
        assert profile["email"] == "054@pochtaminimax.ru"
        assert profile["leased_day"] == "2026-11-22"
        assert profile["dead_account_ids"] == [8, 17, 120]


class TestReadUsage:
    def test_counts_only_window(self, tmp_path: Path) -> None:
        log = tmp_path / "usage.jsonl"
        rows = [
            {"ts": _iso(NOW - 7200), "ok": True, "reason": ""},
            {"ts": _iso(NOW - 3600), "ok": True, "reason": ""},
            {"ts": _iso(NOW - 1800), "ok": False, "reason": "auth_expired"},
            {"ts": _iso(NOW - 60), "ok": False, "reason": "auth_expired"},
            {"ts": _iso(NOW - 30), "ok": True, "reason": ""},
        ]
        _write(log, "\n".join(json.dumps(row) for row in rows) + "\n")
        usage = check.read_usage(log, NOW - 3600)
        assert usage["calls"] == 4
        assert usage["ok"] == 2
        assert usage["fail_by_reason"] == {"auth_expired": 2}

    def test_missing_log(self, tmp_path: Path) -> None:
        usage = check.read_usage(tmp_path / "нет.jsonl", NOW - 3600)
        assert usage["exists"] is False
        assert usage["calls"] == 0


class TestUsageCallsAfter:
    def test_counts_calls_after_expiry(self, tmp_path: Path) -> None:
        log = tmp_path / "usage.jsonl"
        rows = [
            {"ts": _iso(NOW - 600), "ok": True},
            {"ts": _iso(NOW + 60), "ok": True},
            {"ts": _iso(NOW + 120), "ok": True},
        ]
        _write(log, "\n".join(json.dumps(row) for row in rows) + "\n")
        assert check.usage_calls_after(log, NOW) == 2
        assert check.usage_calls_after(log, NOW + 600) == 0


class TestScanWorkerLog:
    def test_counts_markers_in_window(self, tmp_path: Path) -> None:
        log = tmp_path / "celery-worker.log"
        lines = [
            "[2026-11-23 11:00:00,000: INFO] minimax: Карман выбрал аккаунт 197 — свободен",
            "[2026-11-23 11:05:00,000: INFO] minimax: вход аккаунта 197 продлён",
            "[2026-11-23 11:06:00,000: WARNING] minimax: Карман не продлил аккаунт 198 (409)",
            "[2026-11-23 11:07:00,000: WARNING] minimax: аккаунт 197 больше не работает",
            "[2026-11-22 11:00:00,000: INFO] minimax: вход аккаунта 1 продлён (старое)",
        ]
        _write(log, "\n".join(lines) + "\n")
        scan = check.scan_worker_log([log], NOW - 3600)
        assert scan["renewed"] == 1
        assert scan["renew_failed"] == 1
        assert scan["new_accounts"] == 1
        assert scan["rotated"] == 1
        assert scan["samples"]

    def test_panel_unavailable_is_counted(self, tmp_path: Path) -> None:
        """Панель-401 — след протухшего токена, даже если продление не вызывалось."""
        log = tmp_path / "celery-worker.log"
        lines = [
            "[2026-11-23 11:24:00,000: WARNING] minimax: панель аккаунта work недоступна (401)",
            "[2026-11-23 11:25:00,000: INFO] minimax: Карман выбрал аккаунт 3 — свободен",
        ]
        _write(log, "\n".join(lines) + "\n")
        scan = check.scan_worker_log([log], NOW - 3600)
        assert scan["panel_unavailable"] == 1
        assert scan["new_accounts"] == 1
        assert scan["renewed"] == 0
        assert scan["renew_failed"] == 0

    def test_samples_keep_the_newest(self, tmp_path: Path) -> None:
        """Образцы — последние в окне, а не первые: смотреть надо на свежие."""
        log = tmp_path / "celery-worker.log"
        lines = [
            "[2026-11-23 11:%02d:00,000: INFO] minimax: Карман выбрал аккаунт %d" % (minute, minute)
            for minute in range(0, 60)
        ]
        _write(log, "\n".join(lines) + "\n")
        scan = check.scan_worker_log([log], NOW - 3600)
        assert len(scan["samples"]) == 40
        assert "аккаунт 59" in scan["samples"][-1]


class TestVerdict:
    def test_not_checked_without_calls(self) -> None:
        result = check.verdict(calls_after_expiry=0, renewed=0, renew_failed=0, new_accounts=0)
        assert result["code"] == "not_checked"

    def test_renewed(self) -> None:
        result = check.verdict(calls_after_expiry=5, renewed=1, renew_failed=0, new_accounts=0)
        assert result["code"] == "renewed"

    def test_renewed_with_rotation(self) -> None:
        result = check.verdict(calls_after_expiry=5, renewed=1, renew_failed=0, new_accounts=1)
        assert result["code"] == "renewed_with_rotation"

    def test_rejected(self) -> None:
        result = check.verdict(calls_after_expiry=5, renewed=0, renew_failed=1, new_accounts=1)
        assert result["code"] == "renew_rejected"

    def test_rotated_without_trace(self) -> None:
        result = check.verdict(calls_after_expiry=5, renewed=0, renew_failed=0, new_accounts=1)
        assert result["code"] == "rotated"

    def test_no_trace_at_all(self) -> None:
        result = check.verdict(calls_after_expiry=5, renewed=0, renew_failed=0, new_accounts=0)
        assert result["code"] == "no_trace"


class TestCollect:
    def _logs(self, app: Path, worker_lines: List[str], usage_rows: List[Dict[str, Any]]) -> None:
        _write(app / "logs" / "celery-worker.log", "\n".join(worker_lines) + "\n")
        _write(
            app / "logs" / "minimax_usage.jsonl",
            "\n".join(json.dumps(row) for row in usage_rows) + "\n",
        )

    def test_renewed_end_to_end(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, expires_at=NOW - 600)
        self._logs(
            tree["app"],
            [
                "[2026-11-23 11:55:00,000: INFO] minimax: вход аккаунта 197 продлён",
            ],
            [{"ts": _iso(NOW - 300), "ok": True, "reason": ""}],
        )
        data = check.collect(
            accounts_dir=tree["accounts"],
            work_area="work",
            app_root=tree["app"],
            window_hours=24,
            now=NOW,
        )
        assert data["verdict"]["code"] == "renewed"
        assert data["calls_after_expiry"] >= 1
        assert data["account"]["karman_id"] == 197
        assert data["token"]["session_status"] == "authenticated"
        # Секрет в отчёт не попадает.
        assert "СЕКРЕТ" not in json.dumps(data, ensure_ascii=False)

    def test_not_checked_before_first_call(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, expires_at=NOW + 3600)
        self._logs(
            tree["app"],
            ["[2026-11-23 11:30:00,000: INFO] minimax: Карман выбрал аккаунт 197 — свободен"],
            [{"ts": _iso(NOW - 300), "ok": True, "reason": ""}],
        )
        data = check.collect(
            accounts_dir=tree["accounts"],
            work_area="work",
            app_root=tree["app"],
            window_hours=24,
            now=NOW,
        )
        assert data["verdict"]["code"] == "not_checked"
        assert data["token"]["expires_in_seconds"] == 3600
        assert data["log"]["new_accounts"] == 1

    def test_missing_work_area_is_visible(self, tmp_path: Path) -> None:
        """Нет рабочей области — это должно быть видно, а не молчаливый «не проверено»."""
        app = tmp_path / "app"
        self._logs(app, [], [])
        data = check.collect(
            accounts_dir=tmp_path / "нет-такого",
            work_area="work",
            app_root=app,
            window_hours=24,
            now=NOW,
        )
        assert data["work_root_exists"] is False
        assert data["verdict"]["code"] == "not_checked"

    def test_rejected_renew_end_to_end(self, tmp_path: Path) -> None:
        tree = _tree(tmp_path, expires_at=NOW - 600)
        self._logs(
            tree["app"],
            [
                "[2026-11-23 11:50:00,000: WARNING] minimax: Карман не продлил аккаунт 197 (409)",
                "[2026-11-23 11:51:00,000: INFO] minimax: Карман выбрал аккаунт 198 — свободен",
            ],
            [{"ts": _iso(NOW - 600), "ok": False, "reason": "auth_expired"}],
        )
        data = check.collect(
            accounts_dir=tree["accounts"],
            work_area="work",
            app_root=tree["app"],
            window_hours=24,
            now=NOW,
        )
        assert data["verdict"]["code"] == "renew_rejected"
        assert data["log_after_expiry"]["new_accounts"] == 1
