"""Движок MiniMax Code: панель чекина, ротация аккаунтов, разбор ответа.

Тесты не ходят в сеть: панель и запуск CLI подменяются. Проверяется то, что
ломается тихо: выбор клетки серии, порядок ротации (правило Кармана), разбор
``exec.result`` и то, что «очки кончились у всех» — это не поломка движка.
"""

from __future__ import annotations

import json
from pathlib import Path

from modules import minimax_engine as mx


def _panel(days):
    """Панель из списка клеток: (day_no, status, points, is_today)."""
    return {
        "data": {
            "days": [
                {
                    "day_no": no,
                    "status": status,
                    "points": points,
                    "is_today": is_today,
                    "bonus_points": 0,
                }
                for (no, status, points, is_today) in days
            ]
        },
        "base_resp": {"status_code": 0, "status_msg": "ok"},
    }


PANEL_DONE_TODAY = _panel(
    [
        (1, 3, 400, False),
        (2, 3, 400, False),
        (3, 3, 400, False),
        (4, 3, 1000, True),
        (5, 1, 400, False),
        (6, 1, 400, False),
        (7, 1, 1000, False),
    ]
)
PANEL_CLAIMABLE_TODAY = _panel(
    [
        (1, 3, 400, False),
        (2, 3, 400, False),
        (3, 3, 400, False),
        (4, 3, 1000, False),
        (5, 3, 400, False),
        (6, 2, 400, True),
        (7, 1, 1000, False),
    ]
)


def _account(tmp_path: Path, name: str) -> mx.MinimaxAccount:
    auth = tmp_path / name / "data" / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name / "config").mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "records": {
                    "client": {
                        "accessToken": "tok-" + name,
                        "refreshToken": "ref-" + name,
                        "clientId": "mcode-public",
                        "audience": "agent-backend",
                        "expiresAtMs": 4102444800000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return mx.MinimaxAccount(name=name, root=tmp_path / name)


# --- панель и серия -------------------------------------------------------


def test_today_cell_is_the_flagged_one():
    cell = mx._today_cell(PANEL_CLAIMABLE_TODAY)
    assert cell["day_no"] == 6
    assert cell["status"] == 2


def test_streak_counts_consecutive_claimed_days():
    # Забраны дни 1-5, сегодня 6-й — серия 6 (день 6 = пять дней подряд минус сегодня).
    assert mx._streak(PANEL_CLAIMABLE_TODAY) == 6


def test_points_of_next_activation_prefers_bonus_cell():
    # Ближайшая активация — день 7 (1000 очков).
    assert mx._points_of_next_activation(PANEL_CLAIMABLE_TODAY) == 1000


def test_distance_to_bonus_counts_days():
    assert mx._distance_to_bonus(PANEL_CLAIMABLE_TODAY) == 1


def test_rotation_order_prefers_more_points_then_closer_bonus():
    """Правило Кармана: сначала аккаунты, чья ближайшая активация даст больше очков."""
    near_bonus = mx.MinimaxAccount(name="a", root=Path("/tmp/a"))
    near_bonus.panel = PANEL_CLAIMABLE_TODAY  # следующая клетка 7 → 1000
    plain = mx.MinimaxAccount(name="b", root=Path("/tmp/b"))
    plain.panel = _panel(
        [
            (1, 3, 400, False),
            (2, 3, 400, False),
            (3, 3, 400, False),
            (4, 3, 1000, False),
            (5, 3, 400, False),
            (6, 3, 400, True),
            (7, 1, 1000, False),
        ]
    )
    assert sorted([plain, near_bonus], key=mx.account_sort_key)[0].name == "a"


# --- токен-стор ------------------------------------------------------------


def test_token_reads_live_record(tmp_path):
    account = _account(tmp_path, "acc1")
    assert account.token() == "tok-acc1"


def test_expired_token_is_refreshed(monkeypatch, tmp_path):
    """Протухший токен обновляется по refresh-токену — иначе аккаунт молча
    выпадает из пула («401» на панели выглядит как «нет очков»)."""
    account = _account(tmp_path, "acc1")
    path = account.auth_path
    data = json.loads(path.read_text(encoding="utf-8"))
    record = data["records"]["client"]
    record["accessToken"] = "старый"
    record["expiresAtMs"] = 1000
    record["refreshToken"] = "ref-1"
    path.write_text(json.dumps(data), encoding="utf-8")

    seen = {}

    class _Resp:
        def read(self):
            return json.dumps(
                {"access_token": "новый", "refresh_token": "ref-2", "expires_in": 3600}
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        seen["body"] = req.data.decode("utf-8")
        return _Resp()

    monkeypatch.setattr(mx.urllib.request, "urlopen", _fake_urlopen)
    assert account.token() == "новый"
    assert "grant_type=refresh_token" in seen["body"]
    assert "refresh_token=ref-1" in seen["body"]
    # и токен записан обратно в стор
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["records"]["client"]["accessToken"] == "новый"
    assert saved["records"]["client"]["refreshToken"] == "ref-2"


def test_refresh_without_refresh_token_gives_up(tmp_path):
    account = _account(tmp_path, "acc1")
    path = account.auth_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["records"]["client"].pop("refreshToken", None)
    data["records"]["client"]["expiresAtMs"] = 1000
    path.write_text(json.dumps(data), encoding="utf-8")
    # живой записи нет — возвращаем что есть, но не падаем
    assert account.token() is not None


def test_token_prefers_live_record_over_expired(tmp_path):
    account = _account(tmp_path, "acc2")
    path = account.auth_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["records"]["old"] = {
        "accessToken": "tok-old",
        "expiresAtMs": 1000,
        "clientId": "mcode-public",
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    assert account.token() == "tok-acc2"


def test_discover_accounts_skips_profiles_without_auth(tmp_path):
    _account(tmp_path, "acc1")
    (tmp_path / "empty").mkdir()
    names = [a.name for a in mx.discover_accounts(str(tmp_path))]
    assert names == ["acc1"]


# --- чек-ин ----------------------------------------------------------------


def test_ensure_points_accepts_already_claimed(monkeypatch, tmp_path):
    account = _account(tmp_path, "acc1")
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_DONE_TODAY)
    assert mx._ensure_points(account) is True
    assert account.claimed_today is True


def test_ensure_points_claims_when_cell_is_claimable(monkeypatch, tmp_path):
    account = _account(tmp_path, "acc1")
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx,
        "claim_points",
        lambda acc: {"claim_result": 1, "day_no": 6, "points": 400},
    )
    assert mx._ensure_points(account) is True


def test_ensure_points_refuses_unavailable_cell(monkeypatch, tmp_path):
    """Клетка не началась (status=1) — очков сегодня нет, аккаунт не берём."""
    account = _account(tmp_path, "acc1")
    panel = _panel(
        [
            (1, 3, 400, False),
            (2, 3, 400, False),
            (3, 3, 400, False),
            (4, 3, 1000, False),
            (5, 3, 400, False),
            (6, 1, 400, True),
            (7, 1, 1000, False),
        ]
    )
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: panel)
    assert mx._ensure_points(account) is False


# --- разбор ответа CLI -----------------------------------------------------


def _exec_result(status="succeeded", output=None, error=None):
    payload = {
        "schemaVersion": 1,
        "type": "exec.result",
        "status": status,
        "model": {"providerId": "minimax", "modelId": "MiniMax-M3.1-Flash"},
        "durationMs": 1705,
    }
    if output is not None:
        payload["output"] = output
    if error is not None:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=False)


def test_run_classification_parses_succeeded(monkeypatch, tmp_path):
    account = _account(tmp_path, "acc1")
    verdict = json.dumps({"verdicts": [{"lip": "1_2", "theme": "новости", "action": "publish"}]})

    def _fake_run(cmd, **kwargs):
        # Посты агент получает файлом — значит файл должен быть и в команде.
        assert "--file" in cmd
        posts = Path(cmd[cmd.index("--file") + 1])
        assert posts.exists()
        # Агент не видит файлов в lightweight-режиме (замерено 13.11).
        assert cmd[cmd.index("--mode") + 1] == "standard"

        class _P:
            returncode = 0
            stdout = _exec_result(output=verdict)
            stderr = ""

        return _P()

    monkeypatch.setattr(mx.subprocess, "run", _fake_run)
    monkeypatch.setattr(mx.shutil, "which", lambda name: "/usr/bin/mcode")
    ok, content, details = mx.run_classification(account, system="S", user="U")
    assert ok is True
    assert json.loads(content)["verdicts"][0]["action"] == "publish"
    assert details["model"] == "MiniMax-M3.1-Flash"


def test_run_classification_detects_quota(monkeypatch, tmp_path):
    account = _account(tmp_path, "acc1")

    class _P:
        returncode = 4
        stdout = _exec_result(status="failed", error={"message": "insufficient balance (1008)"})
        stderr = ""

    monkeypatch.setattr(mx.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(mx.shutil, "which", lambda name: "/usr/bin/mcode")
    ok, _, details = mx.run_classification(account, system="S", user="U")
    assert ok is False
    assert details["reason"] == "quota_exhausted"


def test_run_classification_without_cli(monkeypatch, tmp_path):
    account = _account(tmp_path, "acc1")
    monkeypatch.setattr(mx.shutil, "which", lambda name: None)
    ok, _, details = mx.run_classification(account, system="S", user="U")
    assert ok is False
    assert details["reason"] == "cli_missing"


# --- пул целиком ------------------------------------------------------------


def test_chat_skips_account_without_points_and_uses_next(monkeypatch, tmp_path):
    _account(tmp_path, "acc_empty")
    _account(tmp_path, "acc_good")
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))

    def _panel_for(acc, **kw):
        return PANEL_CLAIMABLE_TODAY if acc.name == "acc_good" else PANEL_DONE_TODAY

    monkeypatch.setattr(mx, "signin_panel", _panel_for)
    # У acc_empty сегодня очки уже кончились: статус клетки «не началась».
    monkeypatch.setattr(
        mx,
        "_ensure_points",
        lambda acc: acc.name == "acc_good",
    )
    monkeypatch.setattr(
        mx,
        "run_classification",
        lambda acc, **kw: (True, json.dumps({"verdicts": []}), {"model": "MiniMax-M3.1-Flash"}),
    )
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    result = mx.chat(system="S", user="U")
    assert result["ok"] is True
    assert result["account"] == "acc_good"


def test_chat_reports_accounts_exhausted(monkeypatch, tmp_path):
    """Все аккаунты без очков — это не поломка движка, а конец пула на сегодня."""
    _account(tmp_path, "acc1")
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_DONE_TODAY)
    monkeypatch.setattr(mx, "_ensure_points", lambda acc: False)
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    result = mx.chat(system="S", user="U")
    assert result["ok"] is False
    assert result["reason"] == "accounts_exhausted"


def test_chat_without_accounts(monkeypatch, tmp_path):
    monkeypatch.setattr(mx, "discover_accounts", lambda *a, **k: [])
    result = mx.chat(system="S", user="U")
    assert result["ok"] is False
    assert result["reason"] == "no_accounts"
