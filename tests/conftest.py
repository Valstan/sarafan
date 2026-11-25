"""
Pytest configuration and shared fixtures for SETKA tests.

This module provides fixtures that mock all external dependencies
(PostgreSQL, Redis, VK API, Telegram, LLM) so unit tests run
fast and without real infrastructure.
"""

import os

# ---------------------------------------------------------------------------
# Set dummy env vars BEFORE any SETKA module is imported.
# database.connection.py and config/runtime.py require these at import time.
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

# Ёмкость бокса под вызовы MiniMax (слоты в Redis) — в тестах выключена: иначе
# тесты ходят в боевой Redis, берут слот и держат его, и следующие тесты видят
# «слот занят». Тесты самого механизма включают его явно и подставляют Redis.
os.environ.setdefault("MINIMAX_CLI_CAPACITY_ENABLED", "0")

from datetime import datetime  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_session():
    """
    Provide an async SQLAlchemy session mock.

    Usage:
        @pytest.mark.asyncio
        async def test_something(mock_db_session):
            mock_db_session.execute.return_value = ...
    """
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def mock_scalar_result():
    """
    Provide a mock scalar result for DB queries.

    Usage:
        mock_result = mock_scalar_result(return_value=my_region)
        mock_db_session.execute.return_value = mock_result
    """

    def _make_mock(return_value=None):
        mock = MagicMock()
        mock.scalars().first.return_value = return_value
        mock.scalars().all.return_value = [return_value] if return_value else []
        mock.scalar_one_or_none.return_value = return_value
        return mock

    return _make_mock


# ---------------------------------------------------------------------------
# VK API fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_vk_api():
    """
    Provide a mocked VK API client.

    Usage:
        with patch("vk_api.VkApi", return_value=mock_vk_api):
            ...
    """
    vk = MagicMock()
    vk.method.return_value = {"items": [], "count": 0}
    vk.auth = MagicMock()
    return vk


@pytest.fixture
def sample_vk_post():
    """
    Provide a sample VK post dict for testing.
    """
    return {
        "id": 12345,
        "owner_id": -123456,
        "date": 1712500000,
        "text": "Сегодня в городе состоялось важное событие",
        "views": {"count": 1500},
        "marked_as_ads": False,
        "attachments": [],
        "comments": {"count": 5},
        "likes": {"count": 30},
        "reposts": {"count": 2},
    }


@pytest.fixture
def sample_vk_post_ad():
    """
    Provide a sample advertisement VK post.
    """
    return {
        "id": 12346,
        "owner_id": -123456,
        "date": 1712500000,
        "text": "Купите гараж недорого! Цена: 50000 руб. Звоните: +7-999-123-45-67 #реклама",
        "views": {"count": 500},
        "marked_as_ads": True,
        "attachments": [],
        "comments": {"count": 0},
        "likes": {"count": 1},
        "reposts": {"count": 0},
    }


# ---------------------------------------------------------------------------
# Redis fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """
    Provide a mocked Redis async client.
    """
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    redis.keys = AsyncMock(return_value=[])
    redis.exists = AsyncMock(return_value=False)
    return redis


# ---------------------------------------------------------------------------
# Region / context fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_region():
    """
    Provide a sample Region model instance.
    """
    from database.models import Region

    return Region(
        id=1,
        code="mi",
        name="МАЛМЫЖ - ИНФО",
        vk_group_id=-123456,
        is_active=True,
        neighbors="советск,лебяж,уржум",
        config={},
    )


@pytest.fixture
def sample_filter_context(sample_region):
    """
    Provide a standard filter context dict.
    """
    return {
        "region": sample_region,
        "region_code": "mi",
        "theme": "novost",
        "session": {
            "name_session": "novost",
            "work": {
                "novost": {
                    "lip": set(),
                    "hash": set(),
                }
            },
        },
        "filters": [],
    }


# ---------------------------------------------------------------------------
# Celery fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_celery_app():
    """
    Provide a mocked Celery application.
    """
    celery = MagicMock()
    celery.conf = MagicMock()
    celery.conf.beat_schedule = {}
    celery.tasks = {}
    return celery


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_datetime():
    """
    Provide a fixed datetime for deterministic tests.
    """
    return datetime(2026, 4, 8, 12, 0, 0)
