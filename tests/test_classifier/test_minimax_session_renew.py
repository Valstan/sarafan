"""Сессия CLI и продление входа: «Sign in to MiniMax» — не «аккаунт мёртв».

Живая находка 22.11: CLI отвечал «Sign in to MiniMax to use Agent features»
при **живом** access-токене (панель вендора по тому же токену отвечала, сеть
была в порядке). Сломана оказалась СЕССИЯ CLI: рантайм держит её в файле
``auth-state.json`` и по полю ``status`` решает, пускать ли запрос.

Отсюда две проверки:

1. Токен, который кладёт движок, должен ** accompanied** состоянием сессии
   ``authenticated`` — иначе рантайм его не примет.
2. Отказ «Sign in» не должен означать «взять новый аккаунт»: новый аккаунт
   умирает так же, и пул выедается (замер 22.11 — 118 аккаунтов за сутки,
   0 успешных вызовов). Правильное действие — **продлить вход того же
   аккаунта** (ручка ``renew`` у Кармана).
"""

from __future__ import annotations

import json
from pathlib import Path

from modules import karman_minimax
from modules import minimax_engine as mx


def _account(tmp_path: Path, name: str = "work") -> mx.MinimaxAccount:
    root = tmp_path / name
    auth = root / "data" / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "records": {
                    "client": {
                        "accessToken": "tok-old",
                        "refreshToken": "ref-old",
                        "tokenType": "Bearer",
                        "clientId": "mcode-public",
                        "audience": "agent-backend",
                        "expiresAtMs": 4102444800000,
                        "generation": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return mx.MinimaxAccount(name=name, root=root, karman_id=42)


def _state(account: mx.MinimaxAccount) -> dict:
    path = mx.cli_session_state_path(account.data_dir)
    return json.loads(path.read_text(encoding="utf-8"))


# --- состояние сессии CLI ---------------------------------------------------


def test_session_state_is_written_as_authenticated(tmp_path):
    account = _account(tmp_path)
    assert mx.write_cli_session_state(account.data_dir, 111, 3) is True
    state = _state(account)
    assert state["status"] == "authenticated"
    assert state["expiresAtMs"] == 111
    assert state["generation"] == 3
    assert state["clientId"] == "mcode-public"


def test_session_generation_never_goes_backwards(tmp_path):
    account = _account(tmp_path)
    mx.write_cli_session_state(account.data_dir, 111, 5)
    assert mx.cli_session_generation(account.data_dir, 1) == 5


def test_leased_profile_writes_token_and_session_state(tmp_path, monkeypatch):
    """Выданный токен кладём ВМЕСТЕ с состоянием сессии — иначе CLI его не примет."""
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    offer = karman_minimax.PoolOffer(
        account={"id": 42, "email": "acc@example.test", "points_next": 400},
        access_token="tok-new",
        expires_at_ms=4102444800000,
    )
    account = mx.materialize_leased_profile(42, offer, accounts_dir=str(tmp_path))
    assert account is not None
    state = _state(account)
    assert state["status"] == "authenticated"
    assert state["expiresAtMs"] == 4102444800000
    record = json.loads(account.auth_path.read_text(encoding="utf-8"))
    written = list(record["records"].values())[0]
    assert written["accessToken"] == "tok-new"
    assert written["expiresAtMs"] == 4102444800000


# --- продление входа --------------------------------------------------------


def test_renew_rewrites_token_and_session(tmp_path, monkeypatch):
    account = _account(tmp_path)
    seen = {}

    def _renew(account_id, **kwargs):
        seen["id"] = account_id
        return {
            "account_id": account_id,
            "access_token": "tok-fresh",
            "expires_at_ms": 4202444800000,
            "renewed": True,
        }

    monkeypatch.setattr(mx.karman_minimax, "renew_account", _renew)
    assert mx.renew_account_token(account) is True
    assert seen["id"] == 42
    record = json.loads(account.auth_path.read_text(encoding="utf-8"))
    written = list(record["records"].values())[0]
    assert written["accessToken"] == "tok-fresh"
    assert written["expiresAtMs"] == 4202444800000
    state = _state(account)
    assert state["status"] == "authenticated"
    assert state["expiresAtMs"] == 4202444800000


def test_renew_failure_is_not_fatal(tmp_path, monkeypatch):
    account = _account(tmp_path)

    def _renew(account_id, **kwargs):
        raise RuntimeError("Карман не продлил аккаунт: 409")

    monkeypatch.setattr(mx.karman_minimax, "renew_account", _renew)
    assert mx.renew_account_token(account) is False


# --- ротация: продление вместо нового аккаунта ------------------------------


def _drive(monkeypatch, tmp_path, account, results, renew_ok=True):
    """Гоняет контур аренды на одном аккаунте и возвращает (результат, счётчики)."""
    calls = {"model": 0, "renew": 0, "lease": 0}

    def _run(acc, **kwargs):
        calls["model"] += 1
        return results[min(calls["model"] - 1, len(results) - 1)]

    def _renew(account_id, **kwargs):
        calls["renew"] += 1
        if not renew_ok:
            raise RuntimeError("Карман не продлил аккаунт: 409")
        return {
            "account_id": account_id,
            "access_token": "tok-fresh",
            "expires_at_ms": 4202444800000,
        }

    def _take(**kwargs):
        calls["lease"] += 1
        raise AssertionError("новый аккаунт брать нельзя — вход того же можно продлить")

    monkeypatch.setattr(mx, "run_classification", _run)
    monkeypatch.setattr(mx.karman_minimax, "renew_account", _renew)
    monkeypatch.setattr(mx.karman_minimax, "take_account", _take)
    monkeypatch.setattr(mx, "_work_area_account", lambda accounts: account)
    monkeypatch.setattr(mx, "_ensure_points", lambda acc: True)
    monkeypatch.setattr(mx, "_report_activation_once", lambda acc: None)
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_write_shared_meta", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_remember_exhausted", lambda acc: None)
    monkeypatch.setattr(mx, "_note_dead_account", lambda account_id: None)
    monkeypatch.setattr(mx, "_note_dry_failure", lambda: 0)
    monkeypatch.setattr(mx, "_dead_streak_limit", lambda: 10)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    result = mx._chat_leased([account], system="S", user="U", label="headless")
    return result, calls


def test_auth_expired_renews_same_account_and_keeps_working(monkeypatch, tmp_path):
    """Отказ «Sign in» → продлеваем тот же аккаунт, НЕ берём новый."""
    account = _account(tmp_path)
    results = [
        (False, "", {"reason": "auth_expired", "detail": "Sign in to MiniMax"}),
        (True, json.dumps({"verdicts": []}), {"model": "MiniMax-M3.1-Flash"}),
    ]
    result, calls = _drive(monkeypatch, tmp_path, account, results)
    assert result["ok"] is True
    assert calls["renew"] == 1
    assert calls["lease"] == 0


def test_renew_failure_falls_back_to_next_account(monkeypatch, tmp_path):
    """Продление не вышло — тогда уже берём следующий аккаунт (и это видно)."""
    account = _account(tmp_path)
    results = [(False, "", {"reason": "auth_expired", "detail": "Sign in to MiniMax"})]

    def _take(**kwargs):
        raise RuntimeError("Карман не ответил")

    monkeypatch.setattr(mx, "run_classification", lambda acc, **kw: results[0])
    monkeypatch.setattr(
        mx.karman_minimax,
        "renew_account",
        lambda account_id, **kwargs: (_ for _ in ()).throw(RuntimeError("409")),
    )
    monkeypatch.setattr(mx.karman_minimax, "take_account", _take)
    monkeypatch.setattr(mx, "_work_area_account", lambda accounts: account)
    monkeypatch.setattr(mx, "_ensure_points", lambda acc: True)
    monkeypatch.setattr(mx, "_report_activation_once", lambda acc: None)
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_write_shared_meta", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_remember_exhausted", lambda acc: None)
    monkeypatch.setattr(mx, "_note_dead_account", lambda account_id: None)
    monkeypatch.setattr(mx, "_note_dry_failure", lambda: 0)
    monkeypatch.setattr(mx, "_dead_streak_limit", lambda: 10)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    result = mx._chat_leased([account], system="S", user="U", label="headless")
    # Продление не вышло → ушли за следующим аккаунтом (Карман не ответил).
    assert result["ok"] is False
    assert result["reason"] == "karman_unavailable"


# --- «панель не прочиталась» ≠ «очков нет» -----------------------------------


def _panel_with_points():
    """Панель, где сегодняшняя клетка уже забрана (status 3) — очки есть."""
    return {
        "data": {
            "days": [
                {"day_no": no, "status": status, "points": points, "is_today": is_today}
                for (no, status, points, is_today) in [
                    (1, 3, 400, True),
                    (2, 1, 400, False),
                    (3, 1, 400, False),
                    (4, 1, 1000, False),
                    (5, 1, 400, False),
                    (6, 1, 400, False),
                    (7, 1, 1000, False),
                ]
            ]
        },
        "base_resp": {"status_code": 0, "status_msg": "ok"},
    }


def test_unreadable_panel_renews_same_account(monkeypatch, tmp_path):
    """Протух токен → панель не читается (401) → это НЕ «очков нет».

    Движок обязан сначала продлить вход ТОГО ЖЕ аккаунта, а не взять новый:
    иначе пул выедается на каждом истечении access-токена.
    """
    account = _account(tmp_path)
    state = {"renew": 0, "lease": 0}

    def _panel(acc, **kw):
        if state["renew"] == 0:
            acc.panel = None  # панель не прочиталась (вендер отвечает 401)
            return None
        return _panel_with_points()

    def _renew(account_id, **kwargs):
        state["renew"] += 1
        return {
            "account_id": account_id,
            "access_token": "tok-fresh",
            "expires_at_ms": 4202444800000,
        }

    def _take(**kwargs):
        state["lease"] += 1
        raise AssertionError("новый аккаунт брать нельзя — вход того же можно продлить")

    monkeypatch.setattr(mx, "signin_panel", _panel)
    monkeypatch.setattr(mx.karman_minimax, "renew_account", _renew)
    monkeypatch.setattr(mx.karman_minimax, "take_account", _take)
    monkeypatch.setattr(mx, "_work_area_account", lambda accounts: account)
    monkeypatch.setattr(
        mx,
        "run_classification",
        lambda acc, **kw: (True, json.dumps({"verdicts": []}), {"model": "MiniMax-M3.1-Flash"}),
    )
    monkeypatch.setattr(mx, "_report_activation_once", lambda acc: None)
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_write_shared_meta", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_remember_exhausted", lambda acc: None)
    monkeypatch.setattr(mx, "_note_dead_account", lambda account_id: None)
    monkeypatch.setattr(mx, "_note_dry_failure", lambda: 0)
    monkeypatch.setattr(mx, "_dead_streak_limit", lambda: 10)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    result = mx._chat_leased([account], system="S", user="U", label="headless")
    assert result["ok"] is True
    assert state["renew"] == 1
    assert state["lease"] == 0


def test_unreadable_panel_without_renew_takes_next_account(monkeypatch, tmp_path):
    """Продление не вышло и панель не читается — тогда уже берём следующий."""
    account = _account(tmp_path)
    state = {"lease": 0}

    def _panel(acc, **kw):
        acc.panel = None
        return None

    def _take(**kwargs):
        state["lease"] += 1
        raise RuntimeError("Карман не ответил")

    monkeypatch.setattr(mx, "signin_panel", _panel)
    monkeypatch.setattr(
        mx.karman_minimax,
        "renew_account",
        lambda account_id, **kwargs: (_ for _ in ()).throw(RuntimeError("409")),
    )
    monkeypatch.setattr(mx.karman_minimax, "take_account", _take)
    monkeypatch.setattr(mx, "_work_area_account", lambda accounts: account)
    monkeypatch.setattr(mx, "run_classification", lambda acc, **kw: (True, "{}", {}))
    monkeypatch.setattr(mx, "_report_activation_once", lambda acc: None)
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_write_shared_meta", lambda *a, **k: None)
    monkeypatch.setattr(mx, "_remember_exhausted", lambda acc: None)
    monkeypatch.setattr(mx, "_note_dead_account", lambda account_id: None)
    monkeypatch.setattr(mx, "_note_dry_failure", lambda: 0)
    monkeypatch.setattr(mx, "_dead_streak_limit", lambda: 10)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    result = mx._chat_leased([account], system="S", user="U", label="headless")
    assert result["ok"] is False
    assert state["lease"] == 1


# --- заголовок проекта ------------------------------------------------------


def test_consumer_project_matches_room_name():
    """Журнальный проект выдачи должен совпадать с именем комнаты — иначе
    ручка renew отклоняет продление с 409 («аккаунт за другой комнатой»)."""
    headers = karman_minimax.consumer_headers()
    assert headers["X-MiniMax-Project"] == "SARAFAN"
