#!/usr/bin/env python3
"""Вход в аккаунт MiniMax по device-flow с подготовкой профиля на боксе.

Схема (заказ владельца 14.11): владелец входит в аккаунты пула сам, мы работаем.
Чтобы ручной вход не превращался в ручную копировку профилей, этот скрипт
делает всё остальное:

1. создаёт профиль CLI под почту аккаунта (имя каталога и ``profile.json`` —
   из почты, поэтому сопоставление с выдачей Кармана идёт само);
2. печатает ссылку и код подтверждения — владелец открывает и подтверждает;
3. ждёт, пока CLI получит токен, и проверяет, что запись токена легла.

Запуск (на боксе, под пользователем сервисов):

    python3 scripts/minimax_account_login.py --email 7@id45d.myjino.ru

Один аккаунт — один запуск (~2 минуты: вход в браузере + подтверждение).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_ACCOUNTS_DIR = "/home/valstan/minimax-accounts"
DEFAULT_CLI = "/usr/bin/mcode"
TEMPLATE_PROFILE = "acc6"  # профиль-образец: конфиг и регион копируем с него


def profile_name(email: str) -> str:
    """Имя каталога профиля из почты (без символов, которые путают пути)."""
    safe = re.sub(r"[^A-Za-z0-9._@-]+", "_", email.strip().lower())
    return safe[:80]


def prepare(root: Path, email: str, template: str) -> Path:
    """Каталог профиля под почту: структура, права, метаданные."""
    root.mkdir(parents=True, exist_ok=True)
    profile = root / profile_name(email)
    auth_home = profile / "data" / "auth" / "prod" / "en" / "mcode-public"
    auth_home.mkdir(parents=True, exist_ok=True)
    (profile / "config").mkdir(parents=True, exist_ok=True)
    for path in (
        profile,
        profile / "data",
        profile / "data" / "auth",
        auth_home,
        profile / "config",
    ):
        os.chmod(path, 0o700)

    template_dir = root / template
    for name, target in (
        ("data/config.yaml", profile / "data" / "config.yaml"),
        (
            "data/preferences/mcode-region.json",
            profile / "data" / "preferences" / "mcode-region.json",
        ),
    ):
        src = template_dir / name
        if not src.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        os.chmod(target, 0o600)

    (profile / "profile.json").write_text(
        json.dumps({"email": email.strip().lower()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(profile / "profile.json", 0o600)
    return profile


def run_login(cli: str, profile: Path, timeout: int) -> int:
    env = dict(os.environ)
    env["MINIMAX_DATA_DIR"] = str(profile / "data")
    env["MCODE_CONFIG_DIR"] = str(profile / "config")
    env["MAVIS_DATA_DIR"] = str(profile / "data")
    proc = subprocess.run(
        [cli, "login", "--region", "global", "--no-browser"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    return proc.returncode


def has_token(profile: Path) -> bool:
    store = profile / "data" / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for record in (data.get("records") or {}).values():
        if record.get("accessToken") and record.get("refreshToken"):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Вход в аккаунт MiniMax (device-flow)")
    ap.add_argument("--email", required=True, help="почта аккаунта")
    ap.add_argument("--accounts-dir", default=DEFAULT_ACCOUNTS_DIR)
    ap.add_argument("--cli", default=DEFAULT_CLI)
    ap.add_argument("--template", default=TEMPLATE_PROFILE)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    root = Path(args.accounts_dir)
    profile = prepare(root, args.email, args.template)
    print("профиль: %s" % profile, flush=True)
    print(
        "откройте ссылку, которую напечатает CLI, войдите как %s и подтвердите" % args.email,
        flush=True,
    )
    code = run_login(args.cli, profile, args.timeout)
    if code != 0:
        print("вход не удался (код %s)" % code, file=sys.stderr)
        return code
    if not has_token(profile):
        print("токен в профиле не появился — проверьте, что подтверждение дошло", file=sys.stderr)
        return 1
    print("готово: профиль %s авторизован, почта %s" % (profile.name, args.email), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
