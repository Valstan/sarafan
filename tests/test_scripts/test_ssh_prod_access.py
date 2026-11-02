"""Unit-тесты для ``scripts/ssh_prod_access.py`` — доступ к проду из комнаты (D-035).

Скрипт — CLI-утилита вне пакета, грузим через importlib (как
``tests/test_scripts/test_neural_quota_probe.py``). Сети тут нет: ``urlopen``
подменяется заглушкой, а ``ssh-add``/``ssh`` не вызываются вовсе.

Что тут стережётся:
* CRLF ломает ключ в трубе — нормализация обязана это снимать (P150);
* «не знаю, кого осудил» и «никого не осудил» — разные вещи: ошибка чтения
  обязана отличаться от пустого результата;
* значение секрета не должно попасть в вывод: тест ловит утечку.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "ssh_prod_access", REPO_ROOT / "scripts" / "ssh_prod_access.py"
)
access = importlib.util.module_from_spec(_spec)
sys.modules["ssh_prod_access"] = access
_spec.loader.exec_module(access)

FAKE_KEY = (
    "-" * 5
    + "BEGIN OPENSSH "
    + "PRIVATE "
    + "KEY"
    + "-" * 5
    + "\nAAAA\n"
    + "-" * 5
    + "END OPENSSH PRIVATE KEY"
    + "-" * 5
    + "\n"
)


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def test_normalize_key_strips_crlf():
    """CRLF в ключе ломает ssh-add («error in libcrypto») — нормализуем."""
    raw = FAKE_KEY.replace("\n", "\r\n")
    out = access.normalize_key(raw)

    assert b"\r" not in out
    assert out.endswith(b"\n")
    assert not out.endswith(b"\n\n")
    assert out.count(b"\n") == 3


def test_normalize_key_keeps_single_trailing_newline():
    assert access.normalize_key("KEY\n\n\n") == b"KEY\n"


def test_resolve_token_from_env(monkeypatch):
    monkeypatch.setenv("SECRETS_TOKEN", " токен ")
    assert access.resolve_token(None) == "токен"


def test_resolve_token_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRETS_TOKEN", raising=False)
    token_file = tmp_path / "secrets-token"
    token_file.write_text("токен-из-файла\n", encoding="utf-8")

    assert access.resolve_token(str(token_file)) == "токен-из-файла"


def test_resolve_token_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("SECRETS_TOKEN", raising=False)
    assert access.resolve_token(str(tmp_path / "нет-такого")) is None


def test_resolve_vault_url_prefers_env(monkeypatch):
    monkeypatch.setenv("SECRETS_VAULT_URL", "https://example.invalid/api/secrets")
    assert access.resolve_vault_url() == "https://example.invalid/api/secrets"


def test_room_request_parses_secrets(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_urlopen(req, timeout=None, context=None):  # noqa: ANN001, ANN202
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse({"secrets": {"SSH_KEY__sarafan__rmz4val": FAKE_KEY}})

    monkeypatch.setattr(access.urllib.request, "urlopen", fake_urlopen)

    status, secrets = access.room_request("https://example.invalid/api/secrets", "токен")

    assert status == 200
    assert secrets["SSH_KEY__sarafan__rmz4val"] == FAKE_KEY
    assert captured["auth"] == "Bearer токен"


def test_room_request_without_token_sends_no_authorization(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_urlopen(req, timeout=None, context=None):  # noqa: ANN001, ANN202
        captured["auth"] = req.get_header("Authorization")
        return _FakeResponse({"secrets": {}})

    monkeypatch.setattr(access.urllib.request, "urlopen", fake_urlopen)

    status, secrets = access.room_request("https://example.invalid/api/secrets", None)

    assert status == 200
    assert captured["auth"] is None
    assert secrets == {}


def test_room_request_401_is_not_an_exception(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):  # noqa: ANN001, ANN202
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(access.urllib.request, "urlopen", fake_urlopen)

    status, secrets = access.room_request("https://example.invalid/api/secrets", None)

    assert status == 401
    assert secrets == {}


def test_cmd_list_never_prints_values(monkeypatch, capsys):
    """Имена — можно, значения — никогда (свойство 7 спеки vault-клиента)."""
    monkeypatch.setenv("SECRETS_TOKEN", "токен")
    monkeypatch.setenv("SECRETS_VAULT_URL", "https://example.invalid/api/secrets")

    def fake_urlopen(req, timeout=None, context=None):  # noqa: ANN001, ANN202
        return _FakeResponse({"secrets": {"SSH_KEY__sarafan__rmz4val": FAKE_KEY}})

    monkeypatch.setattr(access.urllib.request, "urlopen", fake_urlopen)

    assert access.cmd_list(access.argparse.Namespace(token_file=None)) == 0

    out = capsys.readouterr().out
    assert "SSH_KEY__sarafan__rmz4val" in out
    assert FAKE_KEY.strip() not in out
    assert "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" not in out


def test_add_key_to_agent_passes_bytes_unchanged(monkeypatch):
    """В ssh-add уходит БАЙТОВЫЙ ключ: текстовый режим Windows ломает его."""
    seen: Dict[str, Any] = {}

    class _Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, input=None, capture_output=False, timeout=None):  # noqa: ANN001, A002
        seen["cmd"] = cmd
        seen["input"] = input
        return _Result()

    monkeypatch.setattr(access.subprocess, "run", fake_run)

    ok, msg = access.add_key_to_agent(b"KEY-BYTES\n")

    assert ok, msg
    assert seen["input"] == b"KEY-BYTES\n"
    assert isinstance(seen["input"], bytes)
    assert seen["cmd"][-1] == "-"


def test_verify_requires_red_negative_control(monkeypatch, capsys):
    """Зелёная сделка без красного контроля — это не проверка (P150)."""
    calls: Dict[str, Any] = {}

    def fake_probe(alias, extra_args=None, timeout=25):  # noqa: ANN001
        calls.setdefault("args", []).append(extra_args)
        if extra_args:
            return 255, "Permission denied (publickey)."
        return 0, "3931b3fe50ab.vps.myjino.ru"

    monkeypatch.setattr(access, "ssh_probe", fake_probe)

    assert access.verify("sarafan") is True
    out = capsys.readouterr().out
    assert "отказ=да" in out
    assert calls["args"], "контроль-негатив не выполнялся"

    calls.clear()
    monkeypatch.setattr(access, "ssh_probe", lambda alias, extra_args=None, timeout=25: (0, "ok"))
    assert access.verify("sarafan") is False
