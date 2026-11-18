"""Метаданные ORM: пакет ad_cabinet обязан регистрировать radar_users.

Живой факт 18.11: в воркере задача ``reconcile_scheduled_publications`` падала
каждые 15 минут с ``NoReferencedTableError`` — FK
``ad_scheduled_posts.created_by_user_id`` ссылается на ``radar_users``, а в
метаданных процесса этой таблицы не было (в веб-приложении она есть, в
воркере — нет).

Проверка идёт в **чистом подпроцессе**: в тестовом процессе ``models_extended``
уже импортирован другими тестами, и проверка прошла бы в любом случае.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ad_cabinet_package_registers_radar_users_table():
    """В процессе, импортирующем пакет ad_cabinet, radar_users есть в метаданных."""
    code = (
        "import modules.ad_cabinet\n"
        "from database.connection import Base\n"
        "assert 'radar_users' in Base.metadata.tables, "
        "'radar_users не зарегистрирован: ' + str(sorted(Base.metadata.tables))\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    env.setdefault("REDIS_URL", "redis://localhost:6379/0")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "ok" in proc.stdout


def test_ad_cabinet_reconciler_import_works():
    """Импорт пакета и модуля сверки не должен ломаться (правило — про метаданные)."""
    code = (
        "import modules.ad_cabinet\n"
        "from modules.ad_cabinet import publish_reconciler\n"
        "print('ok')\n"
    )
    env = dict(os.environ)
    env.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    env.setdefault("REDIS_URL", "redis://localhost:6379/0")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
