"""Вход в аккаунт MiniMax по device-flow: подготовка профиля под почту."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "minimax_account_login",
    Path(__file__).resolve().parents[2] / "scripts" / "minimax_account_login.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["minimax_account_login"] = _mod
_spec.loader.exec_module(_mod)


def test_profile_name_is_path_safe():
    name = _mod.profile_name("7@Id45d.MyJino.ru")
    assert "/" not in name
    assert name == "7@id45d.myjino.ru"


def test_prepare_creates_profile_with_email_metadata(tmp_path):
    template = tmp_path / "acc6"
    (template / "data" / "preferences").mkdir(parents=True)
    (template / "data" / "config.yaml").write_text("logLevel: info\n", encoding="utf-8")
    (template / "data" / "preferences" / "mcode-region.json").write_text(
        '{"version": 1, "regions": {"prod": "en"}}', encoding="utf-8"
    )

    profile = _mod.prepare(tmp_path, "7@id45d.myjino.ru", "acc6")
    assert profile.name == "7@id45d.myjino.ru"
    meta = json.loads((profile / "profile.json").read_text(encoding="utf-8"))
    assert meta["email"] == "7@id45d.myjino.ru"
    # Конфиг и регион скопированы с образца, ключ записи токена будет свой.
    assert (profile / "data" / "config.yaml").exists()
    assert (profile / "data" / "preferences" / "mcode-region.json").exists()
    assert (profile / "data" / "auth" / "prod" / "en" / "mcode-public").is_dir()


def test_has_token_requires_both_tokens(tmp_path):
    profile = tmp_path / "acc"
    store = profile / "data" / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    store.parent.mkdir(parents=True)
    store.write_text(
        json.dumps({"records": {"k": {"accessToken": "a", "refreshToken": "r"}}}),
        encoding="utf-8",
    )
    assert _mod.has_token(profile) is True

    store.write_text(
        json.dumps({"records": {"k": {"accessToken": "a", "refreshToken": ""}}}),
        encoding="utf-8",
    )
    assert _mod.has_token(profile) is False

    store.write_text("{}", encoding="utf-8")
    assert _mod.has_token(profile) is False


@pytest.mark.skipif(os.name == "nt", reason="права unix — проверяем на боксе")
def test_prepare_sets_private_permissions(tmp_path):
    template = tmp_path / "acc6"
    (template / "data").mkdir(parents=True)
    profile = _mod.prepare(tmp_path, "x@example.com", "acc6")
    assert oct(profile.stat().st_mode)[-3:] == "700"
    assert oct((profile / "profile.json").stat().st_mode)[-3:] == "600"
