"""Тесты приёма входящих выдач по allowlist (modules.secrets_grants, D-061 двусторонний).

Держим ровно то, что делает приём согласием цели: чужое имя не принимается,
разрешённое имя от чужой комнаты не принимается, разрешённое от своей — принимается
одним POST на правильный id; отказ одной выдачи не роняет остальные.
"""

from __future__ import annotations

import pytest

from modules import secrets_grants as sg

ALLOW = {"KAZANSKAYA_INGEST_KEY": frozenset({"kazanskayamalmyzh"}), "ANY_KEY": frozenset()}


def _fake(pending, accept_status=200, accept_ok=True):
    calls = []

    def call(method, url, token, body=None):
        calls.append((method, url))
        if method == "GET":
            return 200, {"slug": "setka", "issued": [], "received": pending}
        return accept_status, (
            {"ok": accept_ok, "id": 1, "state": "active"} if accept_ok else {"error": "занято"}
        )

    return call, calls


def test_grants_url_shares_host_with_bootstrap():
    assert (
        sg.grants_url("https://vault.example/api/secrets")
        == "https://vault.example/api/secrets/grants"
    )


def test_grants_url_falls_back_to_env(monkeypatch):
    """Адрес хранилища приходит из окружения, а не из кода: репозиторий публичный."""
    monkeypatch.setenv("SECRETS_VAULT_URL", "https://vault.example/api/secrets")
    assert sg.grants_url() == "https://vault.example/api/secrets/grants"


def test_grants_url_without_address_fails_loudly(monkeypatch):
    """Нет адреса — внятная ошибка, а не URL из пустоты вида «/grants»."""
    monkeypatch.delenv("SECRETS_VAULT_URL", raising=False)
    with pytest.raises(RuntimeError, match="SECRETS_VAULT_URL"):
        sg.grants_url()


def test_vault_address_is_not_hardcoded_in_code():
    """Сторож на инфра-деталь: технический домен в коде — находка для постороннего."""
    from modules import secrets_bootstrap as sb

    assert sb.VAULT_URL == ""


def test_decide_rejects_unknown_name():
    assert (
        sg.decide({"aliasKey": "NODE_OPTIONS", "sourceSlug": "kazanskayamalmyzh"}, ALLOW)
        == "skip_name"
    )


def test_decide_rejects_allowed_name_from_wrong_room():
    """Имя из списка, но от чужой комнаты — это подмена источника, не выдача."""
    assert (
        sg.decide({"aliasKey": "KAZANSKAYA_INGEST_KEY", "sourceSlug": "evil"}, ALLOW)
        == "skip_source"
    )


def test_decide_accepts_allowed_pair_and_alias_wins():
    assert (
        sg.decide({"aliasKey": "KAZANSKAYA_INGEST_KEY", "sourceSlug": "kazanskayamalmyzh"}, ALLOW)
        == "accept"
    )
    # alias — имя у получателя; именно оно сверяется с allowlist
    g = {
        "sourceKey": "INGEST_KEY",
        "aliasKey": "KAZANSKAYA_INGEST_KEY",
        "sourceSlug": "kazanskayamalmyzh",
    }
    assert sg.decide(g, ALLOW) == "accept"


def test_decide_empty_sources_means_any_room():
    assert sg.decide({"aliasKey": "ANY_KEY", "sourceSlug": "whoever"}, ALLOW) == "accept"


def test_accept_pending_posts_only_allowed():
    pending = [
        {"id": 7, "aliasKey": "KAZANSKAYA_INGEST_KEY", "sourceSlug": "kazanskayamalmyzh"},
        {"id": 8, "aliasKey": "NODE_OPTIONS", "sourceSlug": "kazanskayamalmyzh"},
        {"id": 9, "aliasKey": "KAZANSKAYA_INGEST_KEY", "sourceSlug": "evil"},
    ]
    call, calls = _fake(pending)
    out = sg.accept_pending("skm_x", vault_url="https://v/api/secrets", allowlist=ALLOW, call=call)
    assert out["pending"] == 3
    assert out["accepted"] == ["KAZANSKAYA_INGEST_KEY"]
    assert [s["reason"] for s in out["skipped"]] == ["skip_name", "skip_source"]
    assert out["failed"] == []
    assert calls == [
        ("GET", "https://v/api/secrets/grants?pending=1"),
        ("POST", "https://v/api/secrets/grants/7/accept"),
    ]


def test_dry_run_never_posts():
    pending = [{"id": 7, "aliasKey": "KAZANSKAYA_INGEST_KEY", "sourceSlug": "kazanskayamalmyzh"}]
    call, calls = _fake(pending)
    out = sg.accept_pending(
        "t", vault_url="https://v/api/secrets", allowlist=ALLOW, dry_run=True, call=call
    )
    assert out["accepted"] == ["KAZANSKAYA_INGEST_KEY"]
    assert [m for m, _ in calls] == ["GET"]


def test_accept_failure_is_reported_not_raised():
    pending = [{"id": 7, "aliasKey": "KAZANSKAYA_INGEST_KEY", "sourceSlug": "kazanskayamalmyzh"}]
    call, _ = _fake(pending, accept_status=409, accept_ok=False)
    out = sg.accept_pending("t", vault_url="https://v/api/secrets", allowlist=ALLOW, call=call)
    assert out["accepted"] == []
    assert out["failed"][0]["status"] == 409
    assert "FAILED" in sg.format_summary(out)


def test_list_pending_raises_on_non_200():
    def call(method, url, token, body=None):
        return 401, {"error": "bad token"}

    with pytest.raises(RuntimeError):
        sg.list_pending("t", vault_url="https://v/api/secrets", call=call)


def test_non_pending_rows_are_never_posted():
    """Уже принятое или отозванное не трогаем — иначе 409 на каждом прогоне."""
    pending = [
        {
            "id": 5,
            "aliasKey": "KAZANSKAYA_INGEST_KEY",
            "sourceSlug": "kazanskayamalmyzh",
            "state": "active",
        },
        {
            "id": 6,
            "aliasKey": "KAZANSKAYA_INGEST_KEY",
            "sourceSlug": "kazanskayamalmyzh",
            "state": "revoked",
        },
    ]
    call, calls = _fake(pending)
    out = sg.accept_pending("t", vault_url="https://v/api/secrets", allowlist=ALLOW, call=call)
    assert out["accepted"] == [] and out["failed"] == []
    assert [m for m, _ in calls] == ["GET"]


def test_zero_pending_is_a_noop():
    call, calls = _fake([])
    out = sg.accept_pending("t", vault_url="https://v/api/secrets", allowlist=ALLOW, call=call)
    assert out == {"pending": 0, "accepted": [], "skipped": [], "failed": []}
    assert len(calls) == 1


def test_real_allowlist_has_kazanskaya_from_its_own_room():
    assert sg.GRANT_ALLOWLIST["KAZANSKAYA_INGEST_KEY"] == frozenset({"kazanskayamalmyzh"})


def test_real_allowlist_has_portal_publish_key_from_its_own_room():
    """D-091: ключ публикации портала принимаем только из комнаты ``vmalmyzhe``.

    Имя отдельное от ``VMALMYZHE_INGEST_KEY`` намеренно — права разные, и
    автопубликация отзывается одним ``DELETE`` на grant, не разрывая доставку.
    """
    assert sg.GRANT_ALLOWLIST["VMALMYZHE_PUBLISH_KEY"] == frozenset({"vmalmyzhe"})


def test_real_allowlist_has_sabantuy_from_its_own_room():
    """D-093: ключ приёмника Сабантуя принимаем только из комнаты ``sabantuymalmyzh``.

    Ключа публикации Сабантуй не выдавал — в allowlist только ingest-имя,
    доставка идёт черновиком.
    """
    assert sg.GRANT_ALLOWLIST["SABANTUY_INGEST_KEY"] == frozenset({"sabantuymalmyzh"})


def test_real_allowlist_has_kultura_publish_key_from_its_own_room():
    """Переговорная 2026-10-03: право публикации Культуры — отдельным ключом.

    Принимаем только из комнаты ``dkmalmyzh``; от чужой комнаты то же имя —
    отказ (decide покрыт отдельно). Та же пара, что ingest/publish у портала.
    """
    assert sg.GRANT_ALLOWLIST["KULTURA_PUBLISH_KEY"] == frozenset({"dkmalmyzh"})
    assert sg.decide({"aliasKey": "KULTURA_PUBLISH_KEY", "sourceSlug": "dkmalmyzh"}) == "accept"
    assert (
        sg.decide({"aliasKey": "KULTURA_PUBLISH_KEY", "sourceSlug": "vmalmyzhe"}) == "skip_source"
    )
    assert sg.decide({"aliasKey": "SABANTUY_INGEST_KEY", "sourceSlug": "sabantuymalmyzh"}) == (
        "accept"
    )
    assert sg.decide({"aliasKey": "SABANTUY_INGEST_KEY", "sourceSlug": "evil"}) == "skip_source"


def test_real_allowlist_has_kultura_from_its_own_room():
    """Письмо brain 2026-09-30: ключ портала «Культура» принимаем только
    из комнаты ``dkmalmyzh``. Ключа публикации нет вовсе — в allowlist
    только ingest-имя, доставка идёт черновиком."""
    assert sg.GRANT_ALLOWLIST["KULTURA_INGEST_KEY"] == frozenset({"dkmalmyzh"})
    assert sg.decide({"aliasKey": "KULTURA_INGEST_KEY", "sourceSlug": "dkmalmyzh"}) == ("accept")
    assert sg.decide({"aliasKey": "KULTURA_INGEST_KEY", "sourceSlug": "evil"}) == "skip_source"
