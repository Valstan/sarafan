"""Tests ретеншна аудита сбора: конфиг, регистрация beat-джобы, срез по дате."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import delete, func, select

from config.runtime import get_collection_audit_retention_days
from database.models_extended import CollectedPostAudit


def test_retention_days_default_and_bounds():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COLLECTION_AUDIT_RETENTION_DAYS", None)
        assert get_collection_audit_retention_days() == 60
    with patch.dict(os.environ, {"COLLECTION_AUDIT_RETENTION_DAYS": "90"}):
        assert get_collection_audit_retention_days() == 90
    with patch.dict(os.environ, {"COLLECTION_AUDIT_RETENTION_DAYS": "1"}):
        assert get_collection_audit_retention_days() == 7  # нижняя граница
    with patch.dict(os.environ, {"COLLECTION_AUDIT_RETENTION_DAYS": "9999"}):
        assert get_collection_audit_retention_days() == 365  # верхняя граница
    with patch.dict(os.environ, {"COLLECTION_AUDIT_RETENTION_DAYS": "junk"}):
        assert get_collection_audit_retention_days() == 60


def test_prune_task_and_beat_registered():
    from tasks.celery_app import app, prune_collected_post_audit  # noqa: F401

    assert "tasks.celery_app.prune_collected_post_audit" in app.tasks
    assert "prune-collected-post-audit-daily" in app.conf.beat_schedule
    entry = app.conf.beat_schedule["prune-collected-post-audit-daily"]
    assert entry["task"] == "tasks.celery_app.prune_collected_post_audit"


def test_prune_expires_survives_night_backlog():
    """expires обязан переживать ночной затор волн на одном ядре.

    С expires=3600 все четыре ночных prune молча протухали в очереди с
    05.09.2026 (найдено 28.09: beat слал, worker не исполнял, строк старше
    порога копилось). 6 ч покрывают затор, до следующего суточного тика
    далеко — залеживание безопасно (таски идемпотентны, длятся сантисекунды).
    """
    from tasks.celery_app import app

    for name in (
        "prune-gateway-requests-daily",
        "prune-collected-post-audit-daily",
        "prune-published-posts-daily",
        "prune-skipped-duplicates-daily",
    ):
        entry = app.conf.beat_schedule[name]
        assert entry["options"]["expires"] >= 6 * 3600, name
        assert entry["options"]["catchup"] is False, name


@pytest.mark.asyncio
async def test_prune_deletes_only_stale_rows(db_session):
    """Срез ретеншна удаляет строки старше порога, свежие оставляет."""
    now = datetime.utcnow()
    db_session.add_all(
        [
            CollectedPostAudit(
                lip="1_old",
                region_code="mi",
                decision="kept",
                collected_at=now - timedelta(days=90),
            ),
            CollectedPostAudit(
                lip="1_fresh",
                region_code="mi",
                decision="kept",
                collected_at=now - timedelta(days=10),
            ),
        ]
    )
    await db_session.commit()

    cutoff = now - timedelta(days=60)
    await db_session.execute(
        delete(CollectedPostAudit).where(CollectedPostAudit.collected_at < cutoff)
    )
    await db_session.commit()

    remaining = (await db_session.execute(select(CollectedPostAudit.lip))).scalars().all()
    assert remaining == ["1_fresh"]
    total = (await db_session.execute(select(func.count(CollectedPostAudit.id)))).scalar()
    assert total == 1
