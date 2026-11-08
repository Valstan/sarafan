"""Журнал публикаций — знаменатель квот тем (миграция 091).

Доля темы считается от того, что реально вышло на стену. Ни один существующий
журнал на этот вопрос не отвечал, поэтому здесь стерегутся свойства, без которых
квота считала бы по кривым данным:

* пишется по строке на каждый опубликованный lip, с темой ВЕРДИКТА (её задаёт
  владелец на странице долей), а не с темой волны;
* провалившаяся отправка в журнал не попадает — иначе расход квоты завышался бы;
* падение журнала не роняет публикацию: пост уже в ВК, откатывать нечего;
* счётчики считаются по скользящему окну и по своему региону.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401 — конфигурация мапперов
from database.connection import Base
from database.models_extended import (
    ClassifierTheme,
    ClassifierThemeAlias,
    ContentClassification,
    PublishedPost,
)
from modules import publication_journal as journal


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [
        PublishedPost.__table__,
        ContentClassification.__table__,
        ClassifierTheme.__table__,
        ClassifierThemeAlias.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _add_published(session, theme, *, region="mi", hours_ago=1, lip="100_1"):
    session.add(
        PublishedPost(
            lip=lip,
            region_code=region,
            wave_theme="novost",
            verdict_theme=theme,
            kind="regular",
            published_at=datetime.utcnow() - timedelta(hours=hours_ago),
        )
    )


# ───────── счётчики за окно ─────────


@pytest.mark.asyncio
async def test_counts_group_by_verdict_theme(db_session):
    _add_published(db_session, "новости", lip="100_1")
    _add_published(db_session, "новости", lip="100_2")
    _add_published(db_session, "спорт", lip="100_3")
    await db_session.commit()

    counts = await journal.fetch_published_counts(db_session, "mi", window_hours=24)
    assert counts == {"новости": 2, "спорт": 1}


@pytest.mark.asyncio
async def test_counts_ignore_rows_outside_the_window(db_session):
    _add_published(db_session, "новости", hours_ago=1, lip="100_1")
    _add_published(db_session, "новости", hours_ago=48, lip="100_2")
    await db_session.commit()

    assert await journal.fetch_published_counts(db_session, "mi", window_hours=24) == {"новости": 1}


@pytest.mark.asyncio
async def test_counts_are_per_region(db_session):
    # Волны 29 районов идут параллельно; общий счётчик дал бы гонку и считал бы
    # чужую ленту своей.
    _add_published(db_session, "новости", region="mi", lip="100_1")
    _add_published(db_session, "новости", region="ur", lip="100_2")
    await db_session.commit()

    assert await journal.fetch_published_counts(db_session, "mi", window_hours=24) == {"новости": 1}


@pytest.mark.asyncio
async def test_rows_without_verdict_theme_are_not_counted(db_session):
    # Доля темы считается среди тех, у кого тема есть: пост из режима
    # algorithmic-fallback вердикта не имеет и знаменатель искажать не должен.
    _add_published(db_session, None, lip="100_1")
    _add_published(db_session, "новости", lip="100_2")
    await db_session.commit()

    assert await journal.fetch_published_counts(db_session, "mi", window_hours=24) == {"новости": 1}


@pytest.mark.asyncio
async def test_broken_read_returns_empty_not_raises():
    # Счётчик не важнее волны: без него квота просто не применится.
    assert await journal.fetch_published_counts(None, "mi", window_hours=24) == {}


# ───────── резолв темы вердикта ─────────


@pytest.mark.asyncio
async def test_verdict_theme_is_canonicalised(db_session):
    db_session.add(ClassifierTheme(name="кругозор", position=1))
    db_session.add(ClassifierThemeAlias(alias="научпоп", canon="кругозор"))
    db_session.add(
        ContentClassification(
            lip="100_1",
            region_code="mi",
            model="test",
            verdict={"theme": "научпоп", "action": "publish"},
        )
    )
    await db_session.commit()

    themes = await journal._resolve_verdict_themes(db_session, ["100_1"])
    assert themes == {"100_1": "кругозор"}


@pytest.mark.asyncio
async def test_lip_without_verdict_resolves_to_nothing(db_session):
    assert await journal._resolve_verdict_themes(db_session, ["100_404"]) == {}


# ───────── запись ─────────


@pytest.mark.asyncio
async def test_failed_publish_is_not_recorded(monkeypatch):
    # work_tables.lip двигается и на неуспехе (курсор дедупа сознательно не
    # ретраит), но журнал отвечает на другой вопрос — «что стоит в ленте».
    called = []
    monkeypatch.setattr(journal, "_resolve_verdict_themes", lambda *a, **k: called.append(1))

    await journal.record_publication(
        region_code="mi",
        wave_theme="novost",
        kind="regular",
        posts_included=["100_1"],
        publish_result={"success": False, "error": "vk down"},
    )
    assert called == []


@pytest.mark.asyncio
async def test_post_not_on_wall_is_not_recorded(monkeypatch):
    # P215: post ушёл user-токеном в группу, где аккаунт не админ ⇒ пост лежит
    # в очереди «Предложенные записи» и подписчикам не виден. Журнал отвечает
    # на вопрос «что стоит в ленте» — такая строка туда не идёт.
    called = []
    monkeypatch.setattr(journal, "_resolve_verdict_themes", lambda *a, **k: called.append(1))

    await journal.record_publication(
        region_code="mi",
        wave_theme="novost",
        kind="regular",
        posts_included=["100_1"],
        publish_result={"success": True, "via": "publish-token:MAMA", "on_wall": False},
    )
    assert called == []


@pytest.mark.asyncio
async def test_post_on_wall_is_recorded(monkeypatch):
    called = []
    monkeypatch.setattr(journal, "_resolve_verdict_themes", lambda *a, **k: called.append(1))

    await journal.record_publication(
        region_code="mi",
        wave_theme="novost",
        kind="regular",
        posts_included=["100_1"],
        publish_result={"success": True, "via": "community-token", "on_wall": True},
    )
    assert called == [1]


@pytest.mark.asyncio
async def test_without_marker_behaviour_is_unchanged(monkeypatch):
    # Старый вызов без метки on_wall — ведём себя как раньше (не ломаем
    # чужих вызовов журнала).
    called = []
    monkeypatch.setattr(journal, "_resolve_verdict_themes", lambda *a, **k: called.append(1))

    await journal.record_publication(
        region_code="mi",
        wave_theme="novost",
        kind="regular",
        posts_included=["100_1"],
        publish_result={"success": True},
    )
    assert called == [1]


@pytest.mark.asyncio
async def test_empty_posts_list_is_a_noop(monkeypatch):
    called = []
    monkeypatch.setattr(journal, "_resolve_verdict_themes", lambda *a, **k: called.append(1))
    await journal.record_publication(
        region_code="mi", wave_theme="novost", kind="regular", posts_included=[]
    )
    assert called == []


@pytest.mark.asyncio
async def test_record_never_raises(monkeypatch):
    # Публикация уже ушла в ВК — откатывать нечего, и журнал не вправе ронять волну.
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("database.connection.AsyncSessionLocal", boom)
    await journal.record_publication(
        region_code="mi",
        wave_theme="novost",
        kind="regular",
        posts_included=["100_1"],
        publish_result={"success": True},
    )


# ───────── ретеншен ─────────


@pytest.mark.asyncio
async def test_prune_removes_only_old_rows(db_session):
    _add_published(db_session, "новости", hours_ago=1, lip="100_new")
    _add_published(db_session, "новости", hours_ago=24 * 500, lip="100_old")
    await db_session.commit()

    deleted = await journal.prune_published_posts(db_session, keep_days=400)
    assert deleted == 1
    left = await journal.fetch_published_counts(db_session, "mi", window_hours=24)
    assert left == {"новости": 1}


@pytest.mark.asyncio
async def test_prune_on_empty_table_is_a_noop(db_session):
    assert await journal.prune_published_posts(db_session, keep_days=400) == 0
