#!/usr/bin/env python3
"""Доступ к прод-боксу: ключ из комнаты КАРМАНа → ssh-agent → ``ssh sarafan``.

Схема D-035 (пилот 25.08,
[P150](../docs/pending/P150-d-035-pilot-prod-ssh-klyuchey-iz-komnaty-karmana.md)):
ключ машины ``rmz4val`` лежит в комнате ``setka`` под именем
``SSH_KEY__sarafan__rmz4val`` (+ публичная половина ``SSH_PUB__sarafan__rmz4val``).
Путь «комната → ``ssh-add -`` → ``ssh sarafan``» был пройден и работает; этот
скрипт делает его повторяемым на любой новой машине.

Три грабли из пилота, здесь учтённые:

- **Только Python, никогда системный ``curl``.** На Windows curl собран со
  schannel, сервер просит TLS-ренеговорот, schannel уходит в цикл до таймаута.
  При этом комната **достижима** — это был HTTP-клиент, а не комната.
- **CRLF ломает ключ в трубе, а не в JSON.** Текстовый ``stdout`` Python на
  Windows превращает 399 байт в 406 и ``ssh-add`` падает с ``error in libcrypto``.
  Поэтому ключ нормализуется и уходит в ``ssh-add`` **бинарно**.
- **Проверка умеет краснеть.** Рядом с успешной сделкой идёт контроль-негатив:
  тот же ``ssh`` с отрезанным агентом обязан дать ``Permission denied``.
  Иначе «зелёная» проверка ничего не доказывает.

Ключ **не кладётся на диск** (режим ``--mode agent``). Запасной режим
``--mode file`` пишет ключ во временный файл с закрытым ACL — на случай, если
агент на машине не поднят; этот режим честно помечен как «ключ на диске».

Использование::

    python scripts/ssh_prod_access.py probe             # достижима ли комната (токен не нужен)
    python scripts/ssh_prod_access.py list              # имена секретов в комнате, без значений
    python scripts/ssh_prod_access.py load              # забрать ключ из комнаты в агент
    python scripts/ssh_prod_access.py load --from-file  # взять ключ из файла владельца
    python scripts/ssh_prod_access.py verify            # ssh sarafan hostname + контроль-негатив

Токен комнаты берётся из ``SECRETS_TOKEN`` (env) или из ``--token-file``
(по умолчанию ``~/.setka/secrets-token``). Адрес комнаты — из
``SECRETS_VAULT_URL`` (env) или ``~/.setka/env.local``; в коде его нет
намеренно: технический домен в отслеживаемом файле — инфра-деталь, а
репозиторий публичный (AGENTS.md, «Что не публикуется»).

Значения секретов не печатаются никогда — ни в лог, ни в исключение.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

HOST_ALIAS = "sarafan"
KEY_NAME = "SSH_KEY__sarafan__rmz4val"
PUB_NAME = "SSH_PUB__sarafan__rmz4val"

DEFAULT_LOCAL_DIR = Path.home() / ".setka"
DEFAULT_TOKEN_FILE = DEFAULT_LOCAL_DIR / "secrets-token"
DEFAULT_ENV_FILE = DEFAULT_LOCAL_DIR / "env.local"

_TIMEOUT_SEC = 15


# ───────────────────────────── источники конфигурации ─────────────────────────────


def _read_env_file(path: Path) -> Dict[str, str]:
    """Прочитать ``KEY=VALUE`` из локального файла (не из репозитория)."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def resolve_token(token_file: Optional[str]) -> Optional[str]:
    """Токен комнаты: env → локальный файл. Нигде не печатается."""
    token = (os.getenv("SECRETS_TOKEN") or "").strip()
    if token:
        return token
    path = Path(token_file) if token_file else DEFAULT_TOKEN_FILE
    if path.exists():
        token = path.read_text(encoding="utf-8", errors="replace").strip()
        return token or None
    return None


def resolve_vault_url() -> Optional[str]:
    """Адрес комнаты: env → локальный файл → пусто (клиент говорит об этом громко)."""
    url = (os.getenv("SECRETS_VAULT_URL") or "").strip()
    if url:
        return url
    env_file = _read_env_file(DEFAULT_ENV_FILE)
    url = (env_file.get("SECRETS_VAULT_URL") or "").strip()
    return url or None


# ───────────────────────────────── клиент комнаты ─────────────────────────────────


def room_request(url: str, token: Optional[str]) -> Tuple[int, Dict[str, str]]:
    """GET комнаты. Возвращает (статус, {имя: значение}). Значения — наверх, не в лог."""
    headers = {"User-Agent": "setka-ssh-access"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(
            req, timeout=_TIMEOUT_SEC, context=ssl.create_default_context()
        ) as resp:
            payload = json.load(resp)
            status = resp.status
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:  # noqa: BLE001 — причина в одну строку
        raise RuntimeError(f"комната недоступна: {e!r}") from None

    secrets = payload.get("secrets") or {}
    if not isinstance(secrets, dict):
        raise RuntimeError("ответ комнаты: secrets не словарь")
    return status, {str(k): str(v) for k, v in secrets.items() if str(v)}


def normalize_key(raw: str) -> bytes:
    """Ключ → байты для ``ssh-add -``: CRLF → LF, ровно один хвостовой перевод строки.

    Текстовый режим на Windows добавляет CRLF и ломает ключ в трубе (P150).
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return (text + "\n").encode("utf-8")


# ─────────────────────────────────── ssh-агент ───────────────────────────────────


def agent_available() -> bool:
    """Живой ли ssh-agent (Windows: служба OpenSSH Authentication Agent)."""
    exe = "ssh-add.exe" if os.name == "nt" else "ssh-add"
    try:
        out = subprocess.run([exe, "-l"], capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        return False
    # exit 1 — агент жив, ключей нет; exit 2 — агент недоступен.
    return out.returncode in (0, 1)


def add_key_to_agent(key_bytes: bytes) -> Tuple[bool, str]:
    """Положить ключ в агент, не на диск. Возвращает (ок, сообщение)."""
    exe = "ssh-add.exe" if os.name == "nt" else "ssh-add"
    try:
        out = subprocess.run([exe, "-"], input=key_bytes, capture_output=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, f"ssh-add не запустился: {e!r}"
    if out.returncode != 0:
        err = (out.stderr or b"").decode("utf-8", "replace").strip()
        return False, err or f"ssh-add вернул {out.returncode}"
    return True, "ключ добавлен в агент"


def write_restricted_key(key_bytes: bytes, target: Path) -> Tuple[bool, str]:
    """Запасной путь: ключ во временный файл с закрытым ACL (Windows)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(key_bytes)
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        # Наследование снимаем, доступ оставляем только владельцу: иначе
        # OpenSSH откажется работать с «незащищённым» приватным ключом.
        subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=30,
        )
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return True, f"ключ записан в {target} (режим «на диске»)"


# ─────────────────────────────────── проверки ───────────────────────────────────


def ssh_probe(alias: str, extra_args=None, timeout: int = 25) -> Tuple[int, str]:
    """``ssh <alias> hostname`` → (код возврата, вывод)."""
    cmd = ["ssh"] + list(extra_args or []) + [alias, "hostname"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    text = (out.stdout or "") + (out.stderr or "")
    return out.returncode, text.strip()


def verify(alias: str) -> bool:
    """Успех — это не «exit 0», а ЗЕЛЁНАЯ сделка ПЛЮС красный контроль-негатив."""
    print(f"=== проверка доступа: ssh {alias} ===")
    code, text = ssh_probe(alias)
    print(f"  сделка: exit={code}")
    for line in text.splitlines()[:6]:
        print(f"    {line}")

    # Контроль-негатив: тот же alias без агентских ключей обязан отказать.
    neg_code, neg_text = ssh_probe(
        alias, ["-o", "IdentityFile=/dev/null", "-o", "IdentitiesOnly=yes"]
    )
    denied = "Permission denied" in neg_text or "permission denied" in neg_text
    print(f"  контроль-негатив: exit={neg_code} отказ={'да' if denied else 'НЕТ'}")

    if code == 0 and denied:
        print("  ИТОГ: доступ есть, проверка умеет краснеть — можно работать")
        return True
    print("  ИТОГ: доступа нет (или проверка не краснеет — это тоже поломка)")
    return False


# ─────────────────────────────────── команды ───────────────────────────────────


def cmd_probe(_args) -> int:
    url = resolve_vault_url()
    if not url:
        print(
            "адрес комнаты не задан: SECRETS_VAULT_URL в env или "
            f"{DEFAULT_ENV_FILE} — строка вида SECRETS_VAULT_URL=https://<хост>/api/secrets",
            file=sys.stderr,
        )
        return 1
    status, secrets = room_request(url, None)
    print(f"GET {url}")
    print(f"  статус: {status}")
    if status == 401:
        print("  401 — комната ДОСТИЖИМА, нужен токен (SECRETS_TOKEN)")
        return 0
    if status == 200:
        print(f"  имена в комнате: {len(secrets)}")
        return 0
    print(f"  неожиданный статус: {status}", file=sys.stderr)
    return 1


def cmd_list(args) -> int:
    url = resolve_vault_url()
    token = resolve_token(args.token_file)
    if not url or not token:
        print("нужны и адрес комнаты, и токен (SECRETS_TOKEN / --token-file)", file=sys.stderr)
        return 1
    status, secrets = room_request(url, token)
    if status != 200:
        print(f"комната ответила {status}", file=sys.stderr)
        return 1
    print(f"имена в комнате ({len(secrets)}):")
    for name in sorted(secrets):
        print(f"  {name}  ({len(secrets[name])} байт)")
    return 0


def cmd_load(args) -> int:
    key_bytes: bytes
    if args.from_file:
        # Ключ берём с диска — комната и токен в этом режиме не нужны.
        path = Path(args.from_file)
        if not path.exists():
            print(f"файл ключа не найден: {path}", file=sys.stderr)
            return 1
        key_bytes = normalize_key(path.read_text(encoding="utf-8", errors="replace"))
        print(f"ключ взят из файла {path} ({len(key_bytes)} байт)")
    else:
        url = resolve_vault_url()
        token = resolve_token(args.token_file)
        if not url or not token:
            print("нужны и адрес комнаты, и токен (SECRETS_TOKEN / --token-file)", file=sys.stderr)
            return 1
        status, secrets = room_request(url, token)
        if status != 200:
            print(f"комната ответила {status}", file=sys.stderr)
            return 1
        raw = secrets.get(KEY_NAME)
        if not raw:
            print(f"в комнате нет {KEY_NAME}", file=sys.stderr)
            return 1
        key_bytes = normalize_key(raw)
        print(f"ключ {KEY_NAME} забран из комнаты ({len(key_bytes)} байт)")
        pub = secrets.get(PUB_NAME)
        if pub:
            print(f"публичная половина {PUB_NAME} тоже на месте ({len(pub.strip())} байт)")

    if args.mode == "agent":
        if not agent_available():
            print(
                "ssh-agent недоступен. Поднять службу: Start-Service ssh-agent (от админа). "
                "Либо взять запасной путь: --mode file",
                file=sys.stderr,
            )
            return 1
        ok, msg = add_key_to_agent(key_bytes)
        print(f"  {msg}")
        if not ok:
            return 1
    else:
        target = (
            Path(args.key_file)
            if args.key_file
            else (
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "setka-ssh" / "id_sarafan"
            )
        )
        ok, msg = write_restricted_key(key_bytes, target)
        print(f"  {msg}")
        if not ok:
            return 1
        if not args.no_verify:
            return 0 if verify(HOST_ALIAS) else 1
        return 0

    if not args.no_verify:
        return 0 if verify(HOST_ALIAS) else 1
    return 0


def cmd_verify(_args) -> int:
    return 0 if verify(HOST_ALIAS) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="достижима ли комната (токен не нужен)")

    p_list = sub.add_parser("list", help="имена секретов в комнате, без значений")
    p_list.add_argument("--token-file", help="файл с токеном комнаты")

    p_load = sub.add_parser("load", help="забрать ключ и загрузить в агент")
    p_load.add_argument("--token-file", help="файл с токеном комнаты")
    p_load.add_argument("--from-file", help="взять ключ из файла вместо комнаты")
    p_load.add_argument(
        "--mode",
        choices=("agent", "file"),
        default="agent",
        help="agent — ключ не покидает память; file — во временный файл с закрытым ACL",
    )
    p_load.add_argument("--key-file", help="куда писать ключ в режиме file")
    p_load.add_argument("--no-verify", action="store_true", help="не проверять ssh после загрузки")

    sub.add_parser("verify", help="проверить доступ: сделка + контроль-негатив")

    args = parser.parse_args(argv)
    handlers = {
        "probe": cmd_probe,
        "list": cmd_list,
        "load": cmd_load,
        "verify": cmd_verify,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
