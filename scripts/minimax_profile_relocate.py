#!/usr/bin/env python3
"""Перенести профиль MiniMax Code на другую машину (починить ключ записи токена).

**Зачем.** Рантайм MiniMax Code ищет запись токена в сторе по ключу
``com.minimax.mcode.oauth.<buildEnv>.<region>\\0<base64url(sha256(<dataDir>/auth
+ "\\0" + clientId))>``
— то есть ключ **зависит от абсолютного пути каталога данных профиля**. Профиль,
скопированный на другую машину, сохраняет старый ключ, а рантайм ищет новый —
и говорит «Sign in to MiniMax», хотя токен живой (панель чекина и посты отвечают).

**Что делает.** Переименовывает запись в ``auth.json`` так, чтобы ключ
соответствовал текущему пути профиля. Токены те же, профиль тот же — меняется
только имя записи. Никакого входа и браузера не нужно.

Проверено 13.11.2026: после переименования CLI на боксе признаёт профиль и
работает с моделью.

**Метаданные профиля.** Рядом с профилем живёт ``profile.json`` — почта
аккаунта и его id в Кармане. По ним Сарафан узнаёт профиль, когда Карман
выдаёт аккаунт из пула (см. ``modules/karman_minimax.py``). Файл опционален:
без него движок работает, но аренда не сможет сопоставить аккаунт с профилем.

Запуск (на той машине, где живёт профиль):

    python3 scripts/minimax_profile_relocate.py --accounts-dir /home/valstan/minimax-accounts
    python3 scripts/minimax_profile_relocate.py --accounts-dir /home/valstan/minimax-accounts \
        --profile acc5 --email user@example.com --karman-id 42
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
from typing import Optional

AUTH_REL = Path("auth/prod/en/mcode-public")
CLIENT_ID = "mcode-public"
BUILD_ENV = "prod"
REGION = "en"
META_NAME = "profile.json"


def expected_key(auth_home: Path) -> str:
    """Ключ, под которым рантайм ищет запись для этого пути."""
    digest = hashlib.sha256(("%s\0%s" % (str(auth_home), CLIENT_ID)).encode("utf-8")).digest()
    account = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return "com.minimax.mcode.oauth.%s.%s\0%s" % (BUILD_ENV, REGION, account)


def relocate(profile_root: Path) -> str:
    auth_home = (profile_root / "data" / "auth").resolve()
    path = auth_home / BUILD_ENV / REGION / CLIENT_ID / "auth.json"
    if not path.exists():
        return "нет auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records") or {}
    if not records:
        return "в сторе нет записей"
    wanted = expected_key(auth_home)
    if wanted in records:
        return "ключ уже верный"
    old_key = next(iter(records))
    records[wanted] = records.pop(old_key)
    data["records"] = records
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "ключ переименован: %s → %s" % (
        old_key.split("\0")[-1][:8],
        wanted.split("\0")[-1][:8],
    )


def write_meta(
    profile_root: Path,
    *,
    email: str = "",
    karman_id: Optional[int] = None,
) -> str:
    """Записать метаданные профиля (почта и id в Кармане) в ``profile.json``.

    Существующие поля не затираются: пустое значение означает «не задано».
    """
    path = profile_root / META_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    changed = []
    if email:
        if data.get("email") != email:
            changed.append("email")
        data["email"] = email
    if karman_id is not None:
        if data.get("karman_account_id") != karman_id:
            changed.append("karman_account_id")
        data["karman_account_id"] = int(karman_id)
    if not changed:
        return "метаданные уже на месте" if data else "метаданных нет и задавать нечего"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "записано: " + ", ".join(changed)


def main() -> int:
    ap = argparse.ArgumentParser(description="Починить ключ записи токена под путь профиля")
    ap.add_argument("--accounts-dir", default="", help="каталог пула аккаунтов")
    ap.add_argument("--profile", default="", help="профиль (по умолчанию — все)")
    ap.add_argument("--email", default="", help="почта аккаунта для метаданных")
    ap.add_argument("--karman-id", type=int, default=None, help="id аккаунта в Кармане")
    args = ap.parse_args()
    root = Path(args.accounts_dir or "/home/valstan/minimax-accounts")
    if not root.is_dir():
        print("нет каталога пула: %s" % root)
        return 1
    if (args.email or args.karman_id is not None) and not args.profile:
        # Почта и id — свойства одного аккаунта; размазывать их по всему пулу
        # нельзя (иначе все профили «станут» одним и тем же аккаунтом).
        print("нужно указать --profile: почта и id относятся к одному аккаунту")
        return 1
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if args.profile and entry.name != args.profile:
            continue
        print("%-10s %s" % (entry.name, relocate(entry)))
        meta = write_meta(entry, email=args.email, karman_id=args.karman_id)
        if meta:
            print("%-10s %s" % ("", meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
