"""Fixtures кабинета рекламодателя: in-memory async БД с реальными таблицами.

Паттерн tests/test_classifier/conftest.py: настоящие запросы вместо
mock-сессий — изоляционные тесты обязаны уметь краснеть на настоящем SQL.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401 — конфигурация мапперов
from database.connection import Base
from database.models import (
    AdChatMessage,
    AdClient,
    AdClientPackage,
    AdInteraction,
    AdOutreachBlacklist,
    AdOutreachCampaign,
    AdOutreachRecipient,
    AdPayment,
    AdPublication,
    AdRequest,
    AdScheduledPost,
    MessageTemplate,
    Region,
    VKToken,
)
from database.models_extended import RadarUser


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [
        Region.__table__,
        RadarUser.__table__,
        AdClient.__table__,
        AdClientPackage.__table__,
        AdRequest.__table__,
        AdScheduledPost.__table__,
        AdPayment.__table__,
        AdPublication.__table__,
        AdInteraction.__table__,
        AdChatMessage.__table__,
        MessageTemplate.__table__,
        AdOutreachCampaign.__table__,
        AdOutreachRecipient.__table__,
        AdOutreachBlacklist.__table__,
        # Выбор аккаунта-владельца для заливки фото ходит в vk_tokens —
        # без таблицы тесты падают на «no such table».
        VKToken.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _owner_pings_offline(monkeypatch):
    """Пинги владельцу в тестах кабинета не ходят в Redis, Telegram и ВК.

    ``record_published``/``submit_order``/бот зовут ``vk_notify.notify_owner``;
    без заглушки каждый пинг ждал коннект к Redis (до 10 с на Windows) и при
    наличии локального ``.env`` слал бы настоящие Telegram-сообщения. Дедуп
    идёт по локальному слою ``owner_ping`` (тот же код, что при лежащем Redis).
    Тест, которому нужен свой Redis/Telegram, патчит поверх.
    """
    import config.runtime as runtime
    import modules.vk_monitor.rate_limiter as rate_limiter
    from modules.ad_cabinet.vk_bot import notify as vk_notify

    def _no_redis():
        raise RuntimeError("redis disabled in ad_cabinet tests")

    async def _community_off():
        return None

    monkeypatch.setattr(rate_limiter, "_build_redis_client", _no_redis)
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {})
    monkeypatch.setattr(runtime, "TELEGRAM_ALERT_CHAT_ID", None)
    monkeypatch.setattr(vk_notify, "community", _community_off)
