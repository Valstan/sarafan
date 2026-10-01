#!/usr/bin/env python3
"""Завести/обновить пользователя web-слоя setka (Ф0.1 auth, миграция 037).

Операторы создаются ТОЛЬКО этим CLI (публичный /api/auth/register умеет лишь
role=radar по инвайт-коду — эскалации через web нет). Запуск на проде:

    ssh sarafan "cd /home/valstan/SETKA && set -a && source /etc/setka/setka.env && set +a \
        && ./venv/bin/python scripts/create_radar_user.py --login valstan --role operator"

Пароль спрашивается интерактивно (getpass, не light up в shell history), либо
берётся из env ``SETKA_NEW_USER_PASSWORD`` (для неинтерактивных прогонов;
unset после использования).

Повторный запуск с тем же login обновляет пароль/роль/активность (upsert).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def upsert_user(login: str, password: str, role: str, deactivate: bool) -> str:
    from sqlalchemy import select

    from database import models  # noqa: F401 - конфигурация мапперов (PR #189)
    from database.connection import AsyncSessionLocal
    from database.models_extended import RadarUser
    from modules.radar.auth import hash_password

    normalized_login = login.strip().lower()
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(RadarUser).where(RadarUser.login == normalized_login))
        ).scalar_one_or_none()
        if user is None:
            user = RadarUser(login=normalized_login)
            session.add(user)
            action = "created"
        else:
            action = "updated"
        user.password_hash = hash_password(password)
        user.role = role
        user.is_active = not deactivate
        await session.commit()
        return action


def main() -> None:
    parser = argparse.ArgumentParser(description="Create/update a setka web user")
    parser.add_argument("--login", required=True)
    parser.add_argument("--role", choices=["operator", "radar"], default="radar")
    parser.add_argument("--deactivate", action="store_true", help="отключить аккаунт")
    args = parser.parse_args()

    password = os.getenv("SETKA_NEW_USER_PASSWORD") or getpass.getpass(f"Пароль для {args.login}: ")
    if len(password) < 8:
        sys.exit("Пароль короче 8 символов — отказ.")

    action = asyncio.run(upsert_user(args.login, password, args.role, args.deactivate))
    print(f"{action}: {args.login} role={args.role} active={not args.deactivate}")


if __name__ == "__main__":
    main()
