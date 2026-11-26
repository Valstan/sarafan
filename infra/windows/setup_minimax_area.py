#!/usr/bin/env python3
"""Поднять рабочую область движка MiniMax Code на локальной машине (Windows).

**Зачем.** На бою движок работает через пул аккаунтов MiniMax Code: у каждого
аккаунта — свой профиль CLI, токен-стор и состояние сессии, и движок выбирает
аккаунт сам. На локальной машине этот пул нужно завести один раз: без него
`modules.minimax_engine` не видит ни одного аккаунта и отвечает
``no_accounts`` — а это значит, что локальный стенд не может классифицировать
посты вовсе (и не может сверить свои вердикты с боевыми).

**Что делает скрипт.** Берёт профиль MiniMax Code, уже заведённый на машине
владельца (тот, в котором работает сам MiniMax Code — по умолчанию профиль
проекта `SARAFAN` в каталоге областей), и заводит из него рабочую область
движка в каталоге пула:

    <MINIMAX_ACCOUNTS_DIR>/<area>/config/config.yaml
    <MINIMAX_ACCOUNTS_DIR>/<area>/data/auth/prod/en/mcode-public/auth.json

Токен-стор и состояние сессии CLI пишутся **штатной функцией проекта**
(:func:`modules.minimax_engine.write_access_token`) — тем же кодом, которым
движок кладёт токен арендованного аккаунта. Поэтому профиль получается
такого же вида, как на бою, и движок принимает его без всяких обходов.

**Refresh-токен намеренно НЕ переносится.** Обмен refresh-токена на сервере
**ротирует** его, а живой профиль принадлежит работающей сессии владельца:
перенеся, обмен обесценил бы refresh-токен в живом профиле и сессия
владельца лишилась бы возможности продлить вход. Поэтому в рабочую область
кладётся штатная заглушка проекта (``REFRESH_PLACEHOLDER``): access-токен
живёт ~9 часов, а когда истечёт — область перезаводится заново (или приходит
новая аренда у Кармана).

**Секреты в репозиторий не попадают:** рабочая область живёт вне репозитория
(по умолчанию — каталог `minimax-accounts` в профиле пользователя).

Запуск:

    .\\venv\\Scripts\\python.exe infra\\windows\\setup_minimax_area.py
    .\\venv\\Scripts\\python.exe infra\\windows\\setup_minimax_area.py --source-profile <путь>
    .\\venv\\Scripts\\python.exe infra\\windows\\setup_minimax_area.py --check
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Имя области движка по умолчанию (как на бою — одна рабочая область).
DEFAULT_AREA = "work"

#: Каталог профилей MiniMax Code на машине владельца.
AREAS_DIRNAME = ".minimax-areas"

#: Проект, чей профиль берём по умолчанию.
DEFAULT_PROJECT = "SARAFAN"


def accounts_dir(explicit: str = "") -> Path:
    """Каталог пула аккаунтов (тот же, что читает движок)."""
    if explicit:
        return Path(explicit)
    env = os.getenv("MINIMAX_ACCOUNTS_DIR")
    if env:
        return Path(env)
    return Path.home() / "minimax-accounts"


def find_source_profile(explicit: str = "") -> Path:
    """Найти профиль MiniMax Code, из которого заводим рабочую область.

    Порядок: явный путь → профиль проекта в каталоге областей → единственный
    профиль в каталоге. Если выбор неоднозначен — выбираем самый свежий и
    печатаем, какой именно (молча выбрать «не тот» — значит завести область
    из чужого аккаунта).
    """
    if explicit:
        return Path(explicit).expanduser()

    areas_root = Path.home() / AREAS_DIRNAME
    if not areas_root.is_dir():
        raise SystemExit(
            f"Каталог профилей MiniMax Code не найден: {areas_root}. "
            "Укажите профиль явно: --source-profile <путь>"
        )

    candidates: List[Path] = []
    for entry in sorted(areas_root.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "profile.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(meta.get("project") or "").strip().upper() == DEFAULT_PROJECT:
            candidates.append(entry)

    if not candidates:
        candidates = [e for e in sorted(areas_root.iterdir()) if (e / "profile.json").exists()]
    if not candidates:
        raise SystemExit(f"Профили MiniMax Code не найдены в {areas_root}")

    if len(candidates) > 1:
        candidates.sort(key=lambda p: (p / "profile.json").stat().st_mtime, reverse=True)
        print(
            "Профилей несколько — берём самый свежий: %s "
            "(остальные: %s)" % (candidates[0].name, ", ".join(c.name for c in candidates[1:]))
        )
    return candidates[0]


def read_token(source_profile: Path) -> Dict[str, Any]:
    """Достать access-токен и срок его жизни из токен-стора профиля."""
    auth_path = source_profile / "auth" / "prod" / "en" / "mcode-public" / "auth.json"
    if not auth_path.exists():
        raise SystemExit(f"Токен-стор профиля не найден: {auth_path}")
    store = json.loads(auth_path.read_text(encoding="utf-8"))
    records = store.get("records") or {}
    if not records:
        raise SystemExit(f"В токен-сторе профиля нет записей: {auth_path}")
    record = list(records.values())[0]
    access = str(record.get("accessToken") or "")
    if not access:
        raise SystemExit(f"В записи токен-стора нет accessToken: {auth_path}")
    expires_at_ms = int(record.get("expiresAtMs") or 0)
    if expires_at_ms <= int(time.time() * 1000):
        raise SystemExit(
            "Access-токен профиля истёк — нужен свежий вход. "
            "Обновите вход в MiniMax Code на этой машине и повторите."
        )
    return {
        "access": access,
        "expires_at_ms": expires_at_ms,
        "email": str(record.get("email") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Рабочая область движка MiniMax Code (Windows)")
    parser.add_argument(
        "--source-profile", default="", help="Профиль MiniMax Code, из которого берём токен."
    )
    parser.add_argument("--accounts-dir", default="", help="Каталог пула аккаунтов.")
    parser.add_argument(
        "--area", default=DEFAULT_AREA, help="Имя рабочей области (по умолчанию work)."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Только проверить: показать, что движок видит, и выйти.",
    )
    args = parser.parse_args()

    from modules import minimax_engine as mx

    pool = accounts_dir(args.accounts_dir)
    source = find_source_profile(args.source_profile)
    print(f"Источник (профиль MiniMax Code): {source}")
    print(f"Каталог пула: {pool}")

    if args.check:
        found = mx.discover_accounts(str(pool))
        if not found:
            print("Пул пуст: движок не увидит ни одного аккаунта.")
            return 1
        for account in found:
            print(
                f"Аккаунт: {account.name} | почта: {account.email} | "
                f"токен живой: {bool(account.token())}"
            )
        return 0

    token = read_token(source)
    root = pool / args.area
    (root / "config").mkdir(parents=True, exist_ok=True)

    account = mx.MinimaxAccount(name=root.name, root=root, email=token["email"])
    # Refresh-токен намеренно не переносим — см. docstring модуля.
    if not mx.write_access_token(
        account, token["access"], token["expires_at_ms"], mx.REFRESH_PLACEHOLDER
    ):
        print("Токен в рабочую область не записан.")
        return 1
    print(f"Токен-стор и состояние сессии CLI записаны: {account.auth_path}")

    src_config = source / "config.yaml"
    dst_config = root / "config" / "config.yaml"
    if src_config.exists() and not dst_config.exists():
        shutil.copy2(src_config, dst_config)
        print(f"Конфиг CLI скопирован: {dst_config}")

    meta_src = source / "profile.json"
    meta: Dict[str, Any] = {}
    if meta_src.exists():
        try:
            meta = json.loads(meta_src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    (root / "profile.json").write_text(
        json.dumps(
            {
                "email": meta.get("email") or token["email"],
                "karman_account_id": meta.get("karman_account_id"),
                "project": meta.get("project") or DEFAULT_PROJECT,
                "updated_at_ms": int(time.time() * 1000),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    found = mx.discover_accounts(str(pool))
    print("Движок видит аккаунты:", [(a.name, a.email) for a in found])
    print(
        "Готово. Дальше: "
        "set MINIMAX_ACCOUNTS_DIR=%s && "
        "python infra\\windows\\classifier_parity.py --limit 40" % pool
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
