"""Вечерняя сводка «Короткой строкой»: отбор, пересказ, сборка (P182, 20.10)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from database.models import Base
from database.models_extended import CollectedPostAudit, PublishedPost, WorkTable
from modules.publisher import daily_digest, rewriter

TEXT = (
    "В Верхней Деревне прошёл субботник. Жители убрали территорию у клуба, "
    "вывезли мусор и покрасили ограждение. Администрация поселения выделила "
    "инвентарь и транспорт, а клуб организовал чай для участников."
)
assert len(TEXT) >= daily_digest.MIN_SOURCE_CHARS


@pytest_asyncio.fixture()
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = [
        CollectedPostAudit.__table__,
        PublishedPost.__table__,
        WorkTable.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _add(
    session,
    lip: str,
    *,
    text: str = TEXT,
    region: str = "mi",
    hours_ago: float = 2,
    views: int = 0,
    likes: int = 0,
    decision: str = "kept",
    media=None,
):
    session.add(
        CollectedPostAudit(
            lip=lip,
            region_code=region,
            theme="novost",
            post_text=text,
            decision=decision,
            collected_at=datetime.utcnow() - timedelta(hours=hours_ago),
            views=views,
            likes=likes,
            comments=0,
            reposts=0,
            media=media,
        )
    )


def _ok_answer(*items: str):
    return {
        "ok": True,
        "content": "\n".join(f"{i}. {text}" for i, text in enumerate(items, 1)),
        "usage": {"total_tokens": 100},
    }


# ───────── граница «за день» ─────────


def test_day_start_is_moscow_midnight_in_utc():
    """Московские сутки начинаются в 21:00 UTC предыдущего дня."""
    from utils.timezone import MOSCOW_TZ

    now = MOSCOW_TZ.localize(datetime(2026, 10, 20, 21, 40))
    assert daily_digest.day_start_utc(now) == datetime(2026, 10, 19, 21, 0)


# ───────── отбор ─────────


@pytest.mark.asyncio
async def test_collect_skips_published_short_and_foreign_regions(db_session):
    _add(db_session, "100_1", text=TEXT, views=10)
    _add(db_session, "100_2", text=TEXT, views=500)
    _add(db_session, "100_3", text="коротко")
    _add(db_session, "200_1", text=TEXT, views=999, region="kilmez")
    _add(db_session, "100_4", text=TEXT, decision="dropped")
    db_session.add(
        PublishedPost(
            lip="100_2",
            region_code="mi",
            wave_theme="novost",
            kind="regular",
            published_at=datetime.utcnow(),
        )
    )
    await db_session.commit()

    posts = await daily_digest.collect_candidates(
        db_session, "mi", since=datetime.utcnow() - timedelta(hours=24)
    )

    lips = [p["lip"] for p in posts]
    assert "100_2" not in lips, "опубликованное в ленте в сборник не идёт"
    assert "100_3" not in lips, "короткий текст пересказывать нечего"
    assert "200_1" not in lips, "чужой район в сборник не попадает"
    assert "100_4" not in lips, "dropped в сборник не идёт"
    assert lips == ["100_1"]


@pytest.mark.asyncio
async def test_collect_respects_cursor_lips(db_session):
    _add(db_session, "100_1")
    _add(db_session, "100_2")
    _add(db_session, "100_3")
    await db_session.commit()

    posts = await daily_digest.collect_candidates(
        db_session,
        "mi",
        since=datetime.utcnow() - timedelta(hours=24),
        exclude_lips={"100_1", "100_2"},
    )

    assert [p["lip"] for p in posts] == ["100_3"]


@pytest.mark.asyncio
async def test_collect_dedupes_the_same_news_and_puts_newest_first(db_session):
    older = TEXT
    newest = "Отдельная новость про набор в секцию. " + TEXT
    _add(db_session, "100_1", text=older, hours_ago=10, views=900)
    _add(db_session, "100_2", text=older, hours_ago=9, views=800)
    _add(db_session, "100_3", text=newest, hours_ago=1, views=0)
    await db_session.commit()

    posts = await daily_digest.collect_candidates(
        db_session, "mi", since=datetime.utcnow() - timedelta(hours=24)
    )

    assert posts[0]["lip"] == "100_3", "свежая новость дня открывает сборник"
    texts = [p["text"] for p in posts]
    assert len(texts) == 2, "перепечатка той же новости второй раз не нужна"


@pytest.mark.asyncio
async def test_collect_limit_keeps_the_best_of_the_day(db_session):
    for index in range(8):
        _add(db_session, f"100_{index}", text=f"Событие номер {index}. " + TEXT, views=index)
    await db_session.commit()

    posts = await daily_digest.collect_candidates(
        db_session, "mi", since=datetime.utcnow() - timedelta(hours=24), limit=5
    )

    assert len(posts) == 5


# ───────── пересказ ─────────


def test_clean_item_strips_markup_and_links():
    raw = "**Итог** [субботника](https://vk.com/x) #новости @admin — убрали мусор."
    assert daily_digest.clean_item(raw) == "Итог субботника — убрали мусор."


def test_clean_item_cuts_at_sentence_not_mid_word():
    raw = ("Первое предложение про событие. " * 12).strip()
    cleaned = daily_digest.clean_item(raw)
    assert len(cleaned) <= daily_digest.MAX_ITEM_CHARS
    assert cleaned.endswith(".")
    assert not cleaned.endswith("собы")


def test_fallback_item_takes_first_sentences():
    item = daily_digest.fallback_item(
        "В районе открыли новую школу. На линейке выступил глава района. "
        "Далее будет ещё один факт."
    )
    assert item.startswith("В районе открыли новую школу.")
    assert len(item) <= daily_digest.MAX_ITEM_CHARS


def test_split_numbered_requires_every_item():
    raw = "1. Первое.\n2. Второе."
    assert daily_digest.split_numbered(raw, 2) == ["Первое.", "Второе."]
    assert daily_digest.split_numbered(raw, 3) == [], "неполный ответ — фолбэк"


def test_summarize_uses_engine(monkeypatch):
    posts = [{"id": 1, "text": TEXT}, {"id": 2, "text": TEXT}]
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(
            "Первый пересказ события в районе, которое случилось вчера.",
            "Второй пересказ другого события в том же районе.",
        ),
    )

    items = daily_digest.summarize(posts, region="Малмыж")

    assert items == [
        "Первый пересказ события в районе, которое случилось вчера.",
        "Второй пересказ другого события в том же районе.",
    ]


def test_summarize_empty_when_engine_fails(monkeypatch):
    monkeypatch.setattr(
        rewriter, "_chat_call", lambda **kw: {"ok": False, "reason": "network_gate"}
    )
    assert daily_digest.summarize([{"text": TEXT}]) == []


# ───────── сборка ─────────


def test_compose_digest_structure_and_tag_dedupe():
    items = [
        daily_digest.DigestItem(text="Первое событие.", source_name="Клуб села"),
        daily_digest.DigestItem(text="Второе событие.", source_name=""),
    ]
    text = daily_digest.compose_digest(
        items,
        theme_tags=["коротко", "малмыжский_район", "Коротко"],
        local_hashtag="#Малмыж",
    )

    lines = text.split("\n\n")
    assert lines[0] == "Короткой строкой"
    assert "Первое событие.\n\nИсточник: Клуб села" in text
    assert "Источник" not in text.split("Второе событие.")[1].split("#")[0]
    tags = lines[-1].split()
    assert tags[0] == "#коротко"
    assert tags.count("#коротко") == 1, "дубль тега в другом регистре снимается"
    assert tags[-1] == "#Малмыж"


@pytest.mark.asyncio
async def test_build_digest_needs_min_items(db_session):
    _add(db_session, "100_1")
    _add(db_session, "100_2")
    await db_session.commit()

    draft = await daily_digest.build_digest(
        db_session,
        "mi",
        since=datetime.utcnow() - timedelta(hours=24),
        region_name="Малмыж",
    )

    assert draft is None


@pytest.mark.asyncio
async def test_build_digest_attaches_source_names_and_lips(db_session, monkeypatch):
    for index in range(4):
        _add(
            db_session,
            f"100_{index}",
            text=f"Событие номер {index}. " + TEXT,
            hours_ago=index + 1,
        )
    await db_session.commit()
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(
            *[
                f"Пересказ новости номер {i} в двух предложениях, без лишних деталей."
                for i in range(4)
            ]
        ),
    )

    draft = await daily_digest.build_digest(
        db_session,
        "mi",
        names={"100": "Верхнедеревенский клуб"},
        theme_tags=["коротко"],
        local_hashtag="#Малмыж",
        region_name="Малмыж",
        since=datetime.utcnow() - timedelta(hours=24),
    )

    assert draft is not None
    assert "Источник: Верхнедеревенский клуб" in draft.text
    assert len(draft.lips) == 4
    assert draft.text.endswith("#коротко #Малмыж")


@pytest.mark.asyncio
async def test_build_digest_trims_items_to_fit(db_session, monkeypatch):
    for index in range(7):
        _add(
            db_session,
            f"100_{index}",
            text=f"Событие номер {index}. " + TEXT,
            hours_ago=index + 1,
        )
    await db_session.commit()
    long_item = "Очень длинный пересказ события. " * 10
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok_answer(*[long_item] * 7))

    draft = await daily_digest.build_digest(
        db_session,
        "mi",
        since=datetime.utcnow() - timedelta(hours=24),
        region_name="Малмыж",
    )

    assert draft is not None
    assert len(draft.text) <= daily_digest.MAX_TOTAL_CHARS
    assert len(draft.items) >= daily_digest.MIN_ITEMS


# ───────── задача ─────────


def test_task_is_disabled_by_env(monkeypatch):
    monkeypatch.setenv("DAILY_DIGEST_ENABLED", "0")
    from tasks.digest_tasks import publish_daily_digest

    report = publish_daily_digest(region_code="mi")

    assert report == {"success": True, "skipped": "disabled"}


@pytest.mark.asyncio
async def test_remember_published_updates_global_cursor(db_session):
    from modules.deduplication.bulletin_history import GLOBAL_REGION_WORK_THEME
    from tasks.digest_tasks import _remember_published

    await _remember_published(db_session, "mi", ["100_1", "100_2"])
    await _remember_published(db_session, "mi", ["100_2", "100_3"])

    from sqlalchemy import select

    row = (
        (
            await db_session.execute(
                select(WorkTable).where(
                    WorkTable.region_code == "mi",
                    WorkTable.theme == GLOBAL_REGION_WORK_THEME,
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.lip == ["100_1", "100_2", "100_3"], "повтор не дублируется, порядок сохранён"


# ───────── фото в сборнике (P214) ─────────


@pytest.mark.asyncio
async def test_draft_carries_photos_with_source_watermark(db_session, monkeypatch):
    """Фото пункта едет в сборник вместе с названием сообщества-источника.

    На картинку печатается имя источника (P203: «водяной знак названия
    инфо-группы»), поэтому в черновике рядом с ссылкой лежит и подпись.
    """
    for index in range(4):
        _add(
            db_session,
            f"100_{index}",
            text=f"Событие номер {index}. " + TEXT,
            hours_ago=index + 1,
            media=[{"type": "photo", "url": f"https://cdn.vk.example/{index}.jpg"}],
        )
    await db_session.commit()
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(
            *[
                f"Пересказ новости номер {i} в двух предложениях, без лишних деталей."
                for i in range(4)
            ]
        ),
    )

    draft = await daily_digest.build_digest(
        db_session,
        "mi",
        names={"100": "Верхнедеревенский клуб"},
        theme_tags=["коротко"],
        local_hashtag="#Малмыж",
        region_name="Малмыж",
        since=datetime.utcnow() - timedelta(hours=24),
    )

    assert draft is not None
    assert draft.photos, "фото источника не доехали до черновика"
    assert len(draft.photos) <= daily_digest.MAX_DIGEST_PHOTOS
    assert all(p.watermark == "Верхнедеревенский клуб" for p in draft.photos)
    assert all(p.url.startswith("https://cdn.vk.example/") for p in draft.photos)


@pytest.mark.asyncio
async def test_draft_without_media_has_no_photos(db_session, monkeypatch):
    """Пост без фото — просто пункт без картинки, а не ошибка сборки."""
    for index in range(3):
        _add(db_session, f"100_{index}", text=f"Событие номер {index}. " + TEXT)
    await db_session.commit()
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(*[f"Пересказ новости номер {i}." for i in range(3)]),
    )

    draft = await daily_digest.build_digest(
        db_session,
        "mi",
        since=datetime.utcnow() - timedelta(hours=24),
        region_name="Малмыж",
    )

    assert draft is not None
    assert draft.photos == []


# ───────── фото-only посты (шаг 2 медиа, решение владельца 03.11) ─────────


@pytest.mark.asyncio
async def test_collect_photo_candidates_takes_textless_posts_with_photo(db_session):
    """Без текста, но с фото — идут в выборку фотографий.

    Пост с текстом сюда не попадает (у него есть пересказ), а пост без фото —
    тоже (нечего показывать).
    """
    _add(
        db_session,
        "100_1",
        text=None,
        media=[{"type": "photo", "url": "https://cdn/only-photo.jpg"}],
    )
    _add(db_session, "100_2", text=TEXT)  # есть текст — это пункт, не фото
    _add(db_session, "100_3", text=None)  # нет ни текста, ни фото
    await db_session.commit()

    photos = await daily_digest.collect_photo_candidates(db_session, "mi")

    assert [p["lip"] for p in photos] == ["100_1"]
    assert photos[0]["photo_url"] == "https://cdn/only-photo.jpg"


@pytest.mark.asyncio
async def test_collect_photo_candidates_respects_cursor_and_limit(db_session):
    """Уже израсходованное (курсор дедупа) не берём, потолок — десять."""
    for index in range(12):
        _add(
            db_session,
            f"100_{index}",
            text=None,
            media=[{"type": "photo", "url": f"https://cdn/{index}.jpg"}],
        )
    await db_session.commit()

    photos = await daily_digest.collect_photo_candidates(db_session, "mi", exclude_lips=["100_0"])

    assert len(photos) == daily_digest.MAX_DIGEST_PHOTOS
    assert "100_0" not in {p["lip"] for p in photos}


@pytest.mark.asyncio
async def test_photo_only_posts_do_not_add_lines_to_the_text(db_session, monkeypatch):
    """Строк в тексте нет — значит, привязка «пункт ↔ источник» не поедет.

    Фактчек сопоставляет пункты и источники по порядку, разбирая текст по
    маркеру «Источник:». Если бы фото-only пост стал пунктом без пересказа,
    сопоставление сдвинулось бы и пошли бы ложные вердикты.
    """
    for index in range(4):
        _add(
            db_session,
            f"100_{index}",
            text=f"Событие номер {index}. " + TEXT,
            hours_ago=index + 1,
        )
    _add(
        db_session,
        "200_1",
        text=None,
        media=[{"type": "photo", "url": "https://cdn/only.jpg"}],
    )
    await db_session.commit()
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(*[f"Пересказ новости номер {i}." for i in range(4)]),
    )

    photo_candidates = await daily_digest.collect_photo_candidates(db_session, "mi")
    draft = await daily_digest.build_digest_from_posts(
        await daily_digest.collect_candidates(db_session, "mi"),
        names={"200": "Фотосообщество"},
        theme_tags=["коротко"],
        local_hashtag="#Малмыж",
        region_name="Малмыж",
        region_code="mi",
        photo_candidates=photo_candidates,
    )

    assert draft is not None
    assert "Фотосообщество" not in draft.text, "фото-only пост не должен давать строк в тексте"
    assert draft.lips == ["100_0", "100_1", "100_2", "100_3"]
    # Само фото в черновике есть — с именем сообщества для водяного знака.
    assert [p.url for p in draft.photos] == ["https://cdn/only.jpg"]
    assert draft.photos[0].watermark == "Фотосообщество"


@pytest.mark.asyncio
async def test_photo_only_photos_come_first_and_item_photos_fill_the_rest(db_session, monkeypatch):
    """Фото-only посты важнее: у них нет другого представления.

    Фото пересказанных пунктов (шаг 1) добавляются только в оставшиеся места.
    """
    for index in range(3):
        _add(
            db_session,
            f"100_{index}",
            text=f"Событие номер {index}. " + TEXT,
            hours_ago=index + 1,
            media=[{"type": "photo", "url": f"https://cdn/item-{index}.jpg"}],
        )
    # Рейтинг у фото-only постов разный — иначе порядок при равной оценке зависел
    # от микросекунд сбора и тест был нестабилен (ловил CI).
    for index, views in enumerate((500, 100)):
        _add(
            db_session,
            f"200_{index}",
            text=None,
            views=views,
            media=[{"type": "photo", "url": f"https://cdn/only-{index}.jpg"}],
        )
    await db_session.commit()
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(*[f"Пересказ новости номер {i}." for i in range(3)]),
    )

    posts = await daily_digest.collect_candidates(db_session, "mi")
    photo_candidates = await daily_digest.collect_photo_candidates(db_session, "mi")
    draft = await daily_digest.build_digest_from_posts(
        posts,
        names={"200": "Фотосообщество"},
        region_name="Малмыж",
        region_code="mi",
        photo_candidates=photo_candidates,
    )

    assert draft is not None
    urls = [p.url for p in draft.photos]
    assert urls[:2] == ["https://cdn/only-0.jpg", "https://cdn/only-1.jpg"]
    # Дальше — фото пересказанных пунктов, но не больше MAX_DIGEST_ITEM_PHOTOS.
    assert urls[2:] == [
        "https://cdn/item-0.jpg",
        "https://cdn/item-1.jpg",
        "https://cdn/item-2.jpg",
    ]
    assert len(urls) <= daily_digest.MAX_DIGEST_PHOTOS


@pytest.mark.asyncio
async def test_photos_capped_at_vk_limit(db_session, monkeypatch):
    """Десять вложений — потолок ВК; больше не прикрепляем."""
    for index in range(3):
        _add(
            db_session,
            f"100_{index}",
            text=f"Событие номер {index}. " + TEXT,
            hours_ago=index + 1,
        )
    for index in range(15):
        _add(
            db_session,
            f"300_{index}",
            text=None,
            media=[{"type": "photo", "url": f"https://cdn/p-{index}.jpg"}],
        )
    await db_session.commit()
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: _ok_answer(*[f"Пересказ новости номер {i}." for i in range(3)]),
    )

    posts = await daily_digest.collect_candidates(db_session, "mi")
    photo_candidates = await daily_digest.collect_photo_candidates(db_session, "mi")
    draft = await daily_digest.build_digest_from_posts(
        posts,
        region_name="Малмыж",
        region_code="mi",
        photo_candidates=photo_candidates,
    )

    assert draft is not None
    assert len(draft.photos) == daily_digest.MAX_DIGEST_PHOTOS == 10


@pytest.mark.asyncio
async def test_photo_only_material_alone_does_not_make_a_digest(db_session):
    """Сборник — это пересказы; фотографии их дополняют, а не заменяют.

    Правило владельца от 20.10 («меньше трёх пунктов — поста нет») остаётся в
    силе: рубрика из трёх картинок без единого слова — не рубрика.
    """
    for index in range(4):
        _add(
            db_session,
            f"300_{index}",
            text=None,
            media=[{"type": "photo", "url": f"https://cdn/p-{index}.jpg"}],
        )
    await db_session.commit()

    posts = await daily_digest.collect_candidates(db_session, "mi")
    photo_candidates = await daily_digest.collect_photo_candidates(db_session, "mi")

    assert posts == []
    assert photo_candidates, "фото-only посты должны быть найдены"
    draft = await daily_digest.build_digest_from_posts(
        posts,
        region_name="Малмыж",
        region_code="mi",
        photo_candidates=photo_candidates,
    )
    assert draft is None
