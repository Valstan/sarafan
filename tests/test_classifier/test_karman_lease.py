"""Пул аккаунтов MiniMax в Кармане: клиент и его интеграция в движок.

Тесты не ходят в сеть: ответы Кармана подменяются. Проверяется то, что ломается
тихо: подбор остаётся у Кармана, активация отмечается в его журнале, а его
недоступность **не** приводит к тихой работе на «своём» аккаунте (иначе один и
тот же аккаунт достался бы Сарафану и владельцу).
"""

from __future__ import annotations

import json
from pathlib import Path

from modules import karman_minimax as km
from modules import minimax_engine as mx

PANEL_CLAIMABLE_TODAY = {
    "data": {
        "days": [
            {"day_no": 1, "status": 3, "points": 400, "is_today": False},
            {"day_no": 2, "status": 3, "points": 400, "is_today": False},
            {"day_no": 3, "status": 3, "points": 400, "is_today": False},
            {"day_no": 4, "status": 3, "points": 1000, "is_today": False},
            {"day_no": 5, "status": 2, "points": 400, "is_today": True},
            {"day_no": 6, "status": 1, "points": 400, "is_today": False},
            {"day_no": 7, "status": 1, "points": 1000, "is_today": False},
        ]
    }
}

OFFER = {
    "account": {
        "id": 67,
        "title": "54",
        "email": "54@id45d.myjino.ru",
        "password": "не-печатаем",
        "activation_index": 1,
        "points_next": 400,
        "streak": 0,
        "next_bonus_in_days": 4,
        "used_on": None,
        "quota_exhausted": False,
    },
    "reason": None,
    "tier": "points_400",
    "free": 66,
    "soon": 0,
    "project": "SARAFAN",
    "today": "2026-11-13",
}


def _account(tmp_path: Path, name: str, *, email: str = "", karman_id=None):
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
    meta = {}
    if email:
        meta["email"] = email
    if karman_id is not None:
        meta["karman_account_id"] = karman_id
    if meta:
        (tmp_path / name / "profile.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    return mx.MinimaxAccount(name=name, root=tmp_path / name)


# --- адрес и токен ------------------------------------------------------------


def test_api_base_derived_from_vault_url(monkeypatch):
    monkeypatch.delenv(km.ENV_API_URL, raising=False)
    monkeypatch.setenv("SECRETS_VAULT_URL", "https://karman.invalid/api/secrets")
    assert km.api_base() == "https://karman.invalid/api/minimax"


def test_api_base_explicit_wins(monkeypatch):
    monkeypatch.setenv(km.ENV_API_URL, "https://other.invalid/api/minimax")
    monkeypatch.setenv("SECRETS_VAULT_URL", "https://karman.invalid/api/secrets")
    assert km.api_base() == "https://other.invalid/api/minimax"


def test_enabled_needs_url_and_token(monkeypatch):
    monkeypatch.delenv(km.ENV_API_URL, raising=False)
    monkeypatch.delenv("SECRETS_VAULT_URL", raising=False)
    monkeypatch.delenv(km.ENV_TOKEN, raising=False)
    monkeypatch.delenv("SECRETS_TOKEN", raising=False)
    assert km.enabled() is False
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    assert km.enabled() is False
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    assert km.enabled() is True


def test_moscow_today_is_utc_plus_three():
    from datetime import datetime, timezone

    moment = datetime(2026, 11, 13, 21, 30, tzinfo=timezone.utc)
    assert km.moscow_today(moment) == "2026-11-14"


# --- клиент: запросы ---------------------------------------------------------


def _call_recorder(responses):
    calls = []

    def _call(method, url, token, *, body=None):
        calls.append({"method": method, "url": url, "token": token, "body": body})
        return responses.pop(0)

    return calls, _call


def test_take_account_reads_offer(monkeypatch):
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(200, OFFER)])
    offer = km.take_account(call=call)
    assert offer.empty is False
    assert offer.account["id"] == 67
    assert offer.tier == "points_400"
    assert offer.free == 66
    assert offer.project == "SARAFAN"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/accounts/available")
    assert calls[0]["token"] == "room-token"


def test_take_account_empty_pool_is_not_error(monkeypatch):
    """«Сегодня аккаунтов нет» — честная пустота, а не поломка."""
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder(
        [(200, {"account": None, "reason": "no_free_accounts", "free": 0, "soon": 3})]
    )
    offer = km.take_account(call=call)
    assert offer.empty is True
    assert offer.reason == "no_free_accounts"


def test_take_account_raises_on_broken_answer(monkeypatch):
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(500, {"error": "boom"})])
    try:
        km.take_account(call=call)
    except RuntimeError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("ожидался RuntimeError")


def test_take_account_passes_filters(monkeypatch):
    """Фильтры Кармана: exclude по id и emails по списку наших почт."""
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(200, OFFER)])
    km.take_account(exclude=[67, 68], emails=["a@example.com"], call=call)
    url = calls[0]["url"]
    assert "exclude=67%2C68" in url
    assert "emails=a%40example.com" in url


def test_take_account_reads_why(monkeypatch):
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    payload = json.loads(json.dumps(OFFER))
    payload["why"] = "клетка 5 — сегодня 400 очков"
    calls, call = _call_recorder([(200, payload)])
    offer = km.take_account(call=call)
    assert offer.why == "клетка 5 — сегодня 400 очков"


def test_report_activation_sends_email_not_internal_id(monkeypatch):
    """Карман с 14.11 принимает email — внутренние id нам знать не нужно."""
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(200, {"account_id": 45})])
    record = km.report_activation(
        None, email="4@id45d.myjino.ru", activation_index=7, points=1000, call=call
    )
    assert record["account_id"] == 45
    assert calls[0]["body"] == {
        "email": "4@id45d.myjino.ru",
        "points": 1000,
        "activation_index": 7,
    }


def test_report_activation_falls_back_to_account_id(monkeypatch):
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(200, {"account_id": 45})])
    km.report_activation(45, call=call)
    assert calls[0]["body"] == {"account_id": 45}


def test_report_activation_sends_only_known_fields(monkeypatch):
    """Проект и дату определяет сервер по токену — клиент их не присылает."""
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(200, {"account_id": 67, "used_on": "2026-11-13"})])
    record = km.report_activation(67, points=400, activation_index=1, call=call)
    assert record["account_id"] == 67
    body = calls[0]["body"]
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/activations")
    assert body == {"account_id": 67, "points": 400, "activation_index": 1}


def test_report_activation_conflict_raises(monkeypatch):
    """409: аккаунт уже записан за другим проектом или занят окном."""
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    calls, call = _call_recorder([(409, {"error": "аккаунт занят"})])
    try:
        km.report_activation(67, call=call)
    except RuntimeError as exc:
        assert "занят" in str(exc)
    else:
        raise AssertionError("ожидался RuntimeError")


# --- метаданные профиля ------------------------------------------------------


def test_discover_accounts_reads_profile_meta(tmp_path):
    _account(tmp_path, "acc5", email="user@example.com", karman_id=42)
    accounts = mx.discover_accounts(str(tmp_path))
    assert accounts[0].email == "user@example.com"
    assert accounts[0].karman_id == 42


def test_match_lease_account_by_id_then_email(tmp_path):
    _account(tmp_path, "acc5", email="user@example.com", karman_id=42)
    accounts = mx.discover_accounts(str(tmp_path))
    assert mx._match_lease_account(accounts, {"id": 42}) is accounts[0]
    assert mx._match_lease_account(accounts, {"id": 99, "email": "user@example.com"}) is accounts[0]
    assert mx._match_lease_account(accounts, {"id": 99, "email": "other@example.com"}) is None


# --- движок: работа по выданному аккаунту ------------------------------------


def _lease_env(monkeypatch):
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    # Пауза после сбоя связи — состояние процесса, сбрасываем между тестами.
    monkeypatch.setattr(mx, "_karman_down_until", 0.0)


def _ok_call(content='{"verdicts": []}'):
    def _run(account, **kwargs):
        return True, content, {"model": "MiniMax-M3.1-Flash", "usage": {"total": 1}}

    return _run


def _offer(account_id=67, email="54@id45d.myjino.ru"):
    payload = json.loads(json.dumps(OFFER))
    payload["account"]["id"] = account_id
    payload["account"]["email"] = email
    return km.PoolOffer(
        account=payload["account"],
        reason="",
        tier=payload["tier"],
        free=payload["free"],
        soon=payload["soon"],
        project=payload["project"],
        today=payload["today"],
    )


def test_chat_works_on_account_from_karman(monkeypatch, tmp_path):
    """Аккаунт выбирает Карман; мы его активируем и отмечаем активацию."""
    _lease_env(monkeypatch)
    _account(tmp_path, "acc5", email="54@id45d.myjino.ru", karman_id=67)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx, "claim_points", lambda acc: {"claim_result": 1, "day_no": 5, "points": 400}
    )
    monkeypatch.setattr(mx, "run_classification", _ok_call())
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    reported = []
    monkeypatch.setattr(km, "take_account", lambda **kw: _offer())
    monkeypatch.setattr(
        km,
        "report_activation",
        lambda account_id, **kwargs: reported.append((account_id, kwargs)) or {},
    )

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is True
    assert result["account"] == "acc5"
    assert result["leased"] is True
    # Активация отмечена в журнале Кармана: id из выдачи + наша клетка и очки.
    assert reported and reported[0][0] == 67
    assert reported[0][1]["points"] == 400
    assert reported[0][1]["activation_index"] == 5


def test_chat_reports_account_not_authorized(monkeypatch, tmp_path):
    """Аккаунт от Кармана без нашего профиля — работать нечем, и это не поломка."""
    _lease_env(monkeypatch)
    _account(tmp_path, "acc5", email="other@example.com", karman_id=1)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    monkeypatch.setattr(km, "take_account", lambda **kw: _offer())

    def _never(*a, **k):
        raise AssertionError("модель звать нельзя — нет профиля под аккаунт")

    monkeypatch.setattr(mx, "run_classification", _never)

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is False
    assert result["reason"] == "account_not_authorized"


def test_chat_reports_empty_pool(monkeypatch, tmp_path):
    """Пустой пул у Кармана — это не поломка движка (волна уходит на DeepSeek)."""
    _lease_env(monkeypatch)
    _account(tmp_path, "acc5", email="54@id45d.myjino.ru", karman_id=67)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(
        km,
        "take_account",
        lambda **kw: km.PoolOffer(account=None, reason="no_free_accounts"),
    )

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is False
    assert result["reason"] == "accounts_exhausted"


def test_chat_takes_next_account_after_conflict(monkeypatch, tmp_path):
    """409 по первому аккаунту — берём следующий."""
    _lease_env(monkeypatch)
    _account(tmp_path, "acc1", email="one@example.com", karman_id=1)
    _account(tmp_path, "acc2", email="two@example.com", karman_id=2)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx, "claim_points", lambda acc: {"claim_result": 1, "day_no": 5, "points": 400}
    )
    monkeypatch.setattr(mx, "run_classification", _ok_call())
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    offers = [_offer(1, "one@example.com"), _offer(2, "two@example.com")]
    monkeypatch.setattr(km, "take_account", lambda **kw: offers.pop(0))

    calls = []

    def _report(account_id, **kwargs):
        calls.append(account_id)
        if account_id == 1:
            raise RuntimeError("аккаунт занят")
        return {}

    monkeypatch.setattr(km, "report_activation", _report)

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is True
    assert result["account"] == "acc2"
    assert calls == [1, 2]


def test_chat_skips_account_without_profile_and_takes_next(monkeypatch, tmp_path):
    """Нет профиля — просим Кармана пропустить аккаунт (exclude) и берём следующего."""
    _lease_env(monkeypatch)
    _account(tmp_path, "acc5", email="good@example.com", karman_id=42)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx, "claim_points", lambda acc: {"claim_result": 1, "day_no": 5, "points": 400}
    )
    monkeypatch.setattr(mx, "run_classification", _ok_call())
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    offers = [_offer(7, "other@example.com"), _offer(42, "good@example.com")]
    asked = []

    def _next(exclude=None, emails=None, **kw):
        asked.append({"exclude": list(exclude or []), "emails": list(emails or [])})
        return offers.pop(0)

    monkeypatch.setattr(km, "take_account", _next)
    monkeypatch.setattr(km, "report_activation", lambda *a, **k: {})

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is True
    assert result["account"] == "acc5"
    # Первый запрос — без исключений; второй — с id неподходящего аккаунта.
    assert asked[0]["exclude"] == []
    assert asked[1]["exclude"] == [7]
    # Наши почты по умолчанию НЕ фильтруются: выбор аккаунта — у Кармана.
    assert asked[0]["emails"] == []
    assert asked[1]["emails"] == []


def test_chat_sends_emails_filter_only_when_enabled(monkeypatch, tmp_path):
    """Режим «работаем на своих профилях»: фильтр включается флагом."""
    _lease_env(monkeypatch)
    monkeypatch.setenv(mx.ENV_EMAILS_FILTER, "1")
    _account(tmp_path, "acc5", email="good@example.com", karman_id=42)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx, "claim_points", lambda acc: {"claim_result": 1, "day_no": 5, "points": 400}
    )
    monkeypatch.setattr(mx, "run_classification", _ok_call())
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    asked = []

    def _next(exclude=None, emails=None, **kw):
        asked.append({"exclude": list(exclude or []), "emails": list(emails or [])})
        return _offer(42, "good@example.com")

    monkeypatch.setattr(km, "take_account", _next)
    monkeypatch.setattr(km, "report_activation", lambda *a, **k: {})

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is True
    assert asked[0]["emails"] == ["good@example.com"]


def test_enabled_can_be_switched_off(monkeypatch):
    """Аренда выключается флагом, не вынимая адрес и токен из окружения."""
    monkeypatch.setenv(km.ENV_API_URL, "https://karman.invalid/api/minimax")
    monkeypatch.setenv(km.ENV_TOKEN, "room-token")
    assert km.enabled() is True
    monkeypatch.setenv(km.ENV_LEASE_SWITCH, "0")
    assert km.enabled() is False


def test_token_store_key_matches_runtime_formula(tmp_path):
    """Ключ записи токена выводится из пути — иначе CLI не найдёт токен."""
    import base64
    import hashlib

    auth_home = tmp_path / "data" / "auth"
    digest = hashlib.sha256(("%s\0mcode-public" % str(auth_home)).encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert mx.token_store_key(auth_home).endswith(expected)
    assert mx.token_store_key(auth_home).startswith("com.minimax.mcode.oauth.prod.en")


def test_materialize_leased_profile_writes_token_store(tmp_path, monkeypatch):
    """Токен от Кармана → готовый профиль, который CLI признаёт."""
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    offer = km.PoolOffer(
        account={"id": 67, "email": "a@example.com"},
        access_token="tok-abc",
        refresh_token="ref-abc",
        expires_at_ms=1794696314153,
    )
    account = mx.materialize_leased_profile(67, offer)
    assert account is not None
    store = json.loads(account.auth_path.read_text(encoding="utf-8"))
    key = next(iter(store["records"]))
    assert key == mx.token_store_key((account.root / "data" / "auth").resolve())
    assert store["records"][key]["accessToken"] == "tok-abc"
    assert store["records"][key]["refreshToken"] == "ref-abc"


def test_chat_works_on_token_from_karman(monkeypatch, tmp_path):
    """Карман отдал токен — работаем на нём, без локального профиля."""
    _lease_env(monkeypatch)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx, "claim_points", lambda acc: {"claim_result": 1, "day_no": 5, "points": 400}
    )
    monkeypatch.setattr(mx, "run_classification", _ok_call())
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    offer = _offer(67, "a@example.com")
    offer.access_token = "tok-abc"
    offer.refresh_token = "ref-abc"
    monkeypatch.setattr(km, "take_account", lambda **kw: offer)
    monkeypatch.setattr(km, "report_activation", lambda *a, **k: {})

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is True
    assert result["account"] == "leased-67"


def test_chat_does_not_fall_back_to_local_pool_when_karman_is_down(monkeypatch, tmp_path):
    """Карман недоступен — это не повод работать на аккаунте, который он держит."""
    _lease_env(monkeypatch)
    _account(tmp_path, "acc5", email="54@id45d.myjino.ru", karman_id=67)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    def _boom(**kw):
        raise RuntimeError("Карман не ответил")

    monkeypatch.setattr(km, "take_account", _boom)
    monkeypatch.setattr(
        mx,
        "run_classification",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("движок не должен звать модель без выдачи от Кармана")
        ),
    )

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is False
    assert result["reason"] == "karman_unavailable"


def test_chat_pauses_after_karman_failure(monkeypatch, tmp_path):
    """Карман лежит — мы к нему не ходим, пока не истекла пауза.

    Иначе каждый чанк волны ждал бы сетевого таймаута.
    """
    _lease_env(monkeypatch)
    _account(tmp_path, "acc5", email="54@id45d.myjino.ru", karman_id=67)
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    calls = []

    def _boom(**kw):
        calls.append(1)
        raise RuntimeError("Карман не ответил")

    monkeypatch.setattr(km, "take_account", _boom)

    first = mx.chat(system="S", user="U", posts=1)
    assert first["reason"] == "karman_unavailable"

    second = mx.chat(system="S", user="U", posts=1)
    assert second["reason"] == "karman_unavailable"
    # Второй вызов к Карману не ходил — сработала пауза.
    assert len(calls) == 1


def test_chat_without_lease_uses_local_pool(monkeypatch, tmp_path):
    """Ручек Кармана нет — работаем своим пулом (как до аренды)."""
    monkeypatch.delenv(km.ENV_API_URL, raising=False)
    monkeypatch.delenv(km.ENV_TOKEN, raising=False)
    monkeypatch.delenv("SECRETS_VAULT_URL", raising=False)
    monkeypatch.delenv("SECRETS_TOKEN", raising=False)
    _account(tmp_path, "acc5")
    monkeypatch.setenv(mx.ENV_ACCOUNTS_DIR, str(tmp_path))
    monkeypatch.setattr(mx, "signin_panel", lambda acc, **kw: PANEL_CLAIMABLE_TODAY)
    monkeypatch.setattr(
        mx, "claim_points", lambda acc: {"claim_result": 1, "day_no": 5, "points": 400}
    )
    monkeypatch.setattr(mx, "run_classification", _ok_call())
    monkeypatch.setattr(mx, "record_usage", lambda *a, **k: None)

    def _never(*a, **k):
        raise AssertionError("Карман не должен зваться без ручек пула")

    monkeypatch.setattr(km, "take_account", _never)

    result = mx.chat(system="S", user="U", posts=1)
    assert result["ok"] is True
    assert result["account"] == "acc5"
