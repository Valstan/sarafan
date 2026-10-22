"""Обновление метрик постов в окне 72 часов (звено 5, шаг 1).

Границы отбора проверяются на чистых функциях: «старше 72 часов не трогаем»
и «уже опубликованное нами не трогаем» — это правила владельца, и они должны
падать тестом, а не выясняться на счёте вызовов ВК.

Ниже — те же границы, но уже на ``select_refresh_candidates``/``apply_metrics``
через in-memory БД (фикстура ``db_session`` из ``conftest.py``): чистые функции
проверяют логику отсева, а эти тесты — что она реально применена к запросу и
записи, а не потерялась на стыке с SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from database.models_extended import CollectedPostAudit
from modules.classifier.metrics_refresh import (
    apply_metrics,
    drop_already_published,
    ref_from_post_url,
    select_refresh_candidates,
)


def test_ref_from_post_url_keeps_owner_sign():
    # lip теряет знак owner_id (abs), а wall.getById его требует. Знак
    # восстанавливаем из post_url, где он сохранён.
    assert ref_from_post_url("https://vk.com/wall-196153274_8272") == (-196153274, 8272)


def test_ref_from_broken_url_is_none():
    for bad in ("", None, "https://vk.com/id1", "https://vk.com/wallабв_1"):
        assert ref_from_post_url(bad) is None, f"url={bad!r}"


def test_drop_already_published_removes_ours_only():
    cands = [((-1, 10), "1_10"), ((-2, 20), "2_20"), ((-3, 30), "3_30")]
    out = drop_already_published(cands, {"2_20"})
    assert [lip for _, lip in out] == ["1_10", "3_30"]


def test_drop_already_published_with_empty_set_keeps_everything():
    cands = [((-1, 10), "1_10")]
    assert drop_already_published(cands, set()) == cands


def _row(lip, owner, post_id, *, decision="kept", published_at=None, collected_at=None, **extra):
    """Строка аудита для тестов БД — без метрик, если не передали ``extra``."""
    return CollectedPostAudit(
        lip=lip,
        region_code="mi",
        post_url=f"https://vk.com/wall{owner}_{post_id}",
        decision=decision,
        published_at=published_at,
        collected_at=collected_at or datetime.utcnow(),
        **extra,
    )


@pytest.mark.asyncio
async def test_select_refresh_candidates_uses_collected_at_when_published_at_is_null(db_session):
    """Запасная ветка: без published_at (наследие до миграции 080) решает collected_at.

    ``published_at > cutoff`` на NULL даёт NULL (не True) — без второй ветки
    в OR такие строки потерялись бы из выборки молча, а это ровно 7774
    существующих строки на проде.
    """
    now = datetime.utcnow()
    row = _row("1_10", -1, 10, published_at=None, collected_at=now - timedelta(hours=1))
    db_session.add(row)
    await db_session.commit()

    out, unparsable = await select_refresh_candidates(db_session, hours=72)
    assert [lip for _, lip in out] == ["1_10"]
    assert unparsable == 0


@pytest.mark.asyncio
async def test_select_refresh_candidates_skips_posts_older_than_window(db_session):
    """Пост старше 72 часов не попадает в выборку — граница владельца, не оптимизация."""
    now = datetime.utcnow()
    old = _row(
        "1_20",
        -1,
        20,
        published_at=now - timedelta(hours=100),
        collected_at=now - timedelta(hours=100),
    )
    db_session.add(old)
    await db_session.commit()

    out, _ = await select_refresh_candidates(db_session, hours=72)
    assert out == []


@pytest.mark.asyncio
async def test_select_refresh_candidates_includes_both_kept_and_dropped(db_session):
    """Обе стороны аудита обязаны попасть в выборку — иначе D-024 нечем проверить."""
    now = datetime.utcnow()
    kept = _row("1_30", -1, 30, decision="kept", published_at=now - timedelta(hours=1))
    dropped = _row("1_31", -1, 31, decision="dropped", published_at=now - timedelta(hours=1))
    db_session.add_all([kept, dropped])
    await db_session.commit()

    out, _ = await select_refresh_candidates(db_session, hours=72)
    assert {lip for _, lip in out} == {"1_30", "1_31"}


@pytest.mark.asyncio
async def test_apply_metrics_fills_published_at_only_when_null(db_session):
    """published_at перезаписывается только пока его нет — дата поста не меняется."""
    now = datetime.utcnow()
    existing_date = now - timedelta(days=5)
    has_date = _row("2_1", -1, 1, published_at=existing_date)
    no_date = _row("2_2", -1, 2, published_at=None)
    db_session.add_all([has_date, no_date])
    await db_session.commit()

    new_date = now - timedelta(hours=1)
    metrics = {
        (-1, 1): {"views": 10, "likes": 1, "comments": 0, "reposts": 0, "published_at": new_date},
        (-1, 2): {"views": 20, "likes": 2, "comments": 0, "reposts": 0, "published_at": new_date},
    }
    lip_by_ref = {(-1, 1): "2_1", (-1, 2): "2_2"}

    await apply_metrics(db_session, metrics, lip_by_ref)

    rows = {r.lip: r for r in (await db_session.execute(select(CollectedPostAudit))).scalars()}
    assert rows["2_1"].published_at == existing_date  # уже была — не тронута
    assert rows["2_2"].published_at == new_date  # была NULL — заполнена


@pytest.mark.asyncio
async def test_apply_metrics_does_not_wipe_measured_values_with_missing_ones(db_session):
    """Поле, которого ВК не прислал, НЕ затирает уже измеренное значение.

    Так бывает штатно: карусель сменила токен, и community-token просмотров не
    видит. Затирание превращало строку в «свежую и не меренную» одновременно —
    ``metrics_updated_at`` стоит, а просмотров нет, — и витрина показывала бы
    прочерк на посте, который вчера был измерен.
    """
    row = _row(
        "3_1", -1, 1, published_at=datetime.utcnow(), views=99, likes=1, comments=2, reposts=3
    )
    db_session.add(row)
    await db_session.commit()

    # ВК прислал только likes; views/comments/reposts в ответе отсутствуют.
    metrics = {
        (-1, 1): {
            "views": None,
            "likes": 5,
            "comments": None,
            "reposts": None,
            "published_at": None,
        }
    }
    lip_by_ref = {(-1, 1): "3_1"}

    await apply_metrics(db_session, metrics, lip_by_ref)

    updated = (
        await db_session.execute(select(CollectedPostAudit).where(CollectedPostAudit.lip == "3_1"))
    ).scalar_one()
    assert updated.likes == 5  # пришло — записано
    assert updated.views == 99  # не пришло — прежнее измеренное сохранено
    assert updated.comments == 2
    assert updated.reposts == 3
    assert updated.metrics_updated_at is not None


@pytest.mark.asyncio
async def test_apply_metrics_does_not_stamp_freshness_without_any_metric(db_session):
    """Ни одной метрики в ответе — штампа свежести не будет.

    Иначе ``metrics_updated_at`` означал бы «таска до строки доходила», а не
    «строка измерена», и приёмочная проверка доли измеренных считала бы не то.
    """
    row = _row("3_2", -1, 2, published_at=datetime.utcnow())
    db_session.add(row)
    await db_session.commit()

    metrics = {
        (-1, 2): {
            "views": None,
            "likes": None,
            "comments": None,
            "reposts": None,
            "published_at": None,
        }
    }

    updated = await apply_metrics(db_session, metrics, {(-1, 2): "3_2"})

    row_db = (
        await db_session.execute(select(CollectedPostAudit).where(CollectedPostAudit.lip == "3_2"))
    ).scalar_one()
    assert row_db.metrics_updated_at is None
    assert updated == 0


@pytest.mark.asyncio
async def test_apply_metrics_counts_changed_rows_not_answers(db_session):
    """Счётчик — ``rowcount``, а не число ответов ВК.

    Гейт ``no_metrics_fetched`` должен значить «в БД ничего не поменялось».
    Если считать ответы, метрика на несуществующий lip (пост уехал из аудита
    по ретенции между выборкой и записью) отчиталась бы как обновление.
    """
    db_session.add(_row("3_3", -1, 3, published_at=datetime.utcnow()))
    await db_session.commit()

    metrics = {
        (-1, 3): {"views": 10, "likes": 1, "comments": 0, "reposts": 0, "published_at": None},
        (-1, 4): {"views": 20, "likes": 2, "comments": 0, "reposts": 0, "published_at": None},
    }
    lip_by_ref = {(-1, 3): "3_3", (-1, 4): "3_4_которого_нет"}

    assert await apply_metrics(db_session, metrics, lip_by_ref) == 1


@pytest.mark.asyncio
async def test_refresh_metrics_reports_failure_when_nothing_updated(db_session, monkeypatch):
    """checked>0, updated==0 (ВК отказал на всех батчах) — явный неуспех, не тихий ok.

    ``fetch_metrics_for_token`` глотает отказы по-батчево и может вернуть
    пустой словарь даже с живым токеном (бан токена посреди прохода, сетевой
    сбой на всех батчах разом). «Проверено много, обновлено ноль» — тот же
    класс отказа, что инцидент 2026-08-19, где таска рапортовала успех,
    ничего не сделав; без этой проверки он повторился бы молча.
    """
    from unittest.mock import AsyncMock, MagicMock

    import modules.classifier.metrics_refresh as metrics_refresh_mod
    import modules.vk_monitor.post_metrics as post_metrics_mod
    import modules.vk_token_router as token_router_mod
    from modules.classifier.metrics_refresh import refresh_metrics

    row = _row("4_1", -1, 1, published_at=datetime.utcnow() - timedelta(hours=1))
    db_session.add(row)
    await db_session.commit()

    # load_published_lips бьёт в work_tables, которой нет в фикстуре db_session
    # (conftest создаёт только таблицы HITL-классификатора) — не часть того,
    # что проверяет этот тест, поэтому просто отдаём «наших публикаций нет».
    monkeypatch.setattr(metrics_refresh_mod, "load_published_lips", AsyncMock(return_value=set()))
    monkeypatch.setattr(token_router_mod, "get_healthy_read_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(post_metrics_mod, "fetch_metrics_for_token", lambda api, refs, **kw: {})
    monkeypatch.setattr("vk_api.VkApi", MagicMock())

    result = await refresh_metrics(db_session, hours=72)
    assert result == {
        "ok": False,
        "error": "no_metrics_fetched",
        "checked": 1,
        "updated": 0,
        "skipped_published": 0,
        "unparsable_urls": 0,
    }


# --- work_tables.lip: чужая JSON-колонка, четыре пишущих модуля ----------------


async def _make_work_tables(session):
    """Создать work_tables в in-memory БД: фикстура db_session её не заводит."""
    from database.models_extended import WorkTable

    conn = await session.connection()
    await conn.run_sync(lambda c: WorkTable.__table__.create(c, checkfirst=True))
    return WorkTable


def _work(WorkTable, lip, theme):
    """Строка work_tables: интересен только lip.

    ``theme`` разная у каждой строки не для красоты — на (region_code, theme)
    стоит UNIQUE.
    """
    return WorkTable(region_code="mi", theme=theme, lip=lip)


@pytest.mark.asyncio
async def test_load_published_lips_reads_lists(db_session):
    WorkTable = await _make_work_tables(db_session)
    db_session.add_all(
        [_work(WorkTable, ["1_10", "2_20"], "novost"), _work(WorkTable, ["3_30"], "sport")]
    )
    await db_session.commit()

    from modules.classifier.metrics_refresh import load_published_lips

    assert await load_published_lips(db_session) == {"1_10", "2_20", "3_30"}


@pytest.mark.asyncio
async def test_load_published_lips_survives_non_list_values(db_session, caplog):
    """Не-список в work_tables.lip не уносит круг таски.

    В колонку пишут четыре разных модуля (cascaded_bulletin,
    copy_setka_network, krugozor_broadcast, telegram_gonba_mirror), схема JSON
    ничего не гарантирует. Раньше строка-словарь давала TypeError, внешний
    try/except таски превращал его в ok:False, и так — каждые 3 часа
    бесконечно, видно только в логе. Битую строку пропускаем с
    предупреждением, остальные lip'ы обязаны доехать.

    Строка ('1_10') тоже непригодна и тоже пропускается: она итерируется по
    символам и набила бы множество мусором вида {'1','_','0'} — молча, без
    единого исключения.
    """
    WorkTable = await _make_work_tables(db_session)
    db_session.add_all(
        [
            _work(WorkTable, {"lip": "1_10"}, "novost"),  # словарь вместо списка
            _work(WorkTable, "1_10", "sport"),  # строка вместо списка
            _work(WorkTable, None, "kultura"),  # пусто — норма, не ошибка
            _work(WorkTable, ["9_99"], "afisha"),  # нормальная строка
        ]
    )
    await db_session.commit()

    from modules.classifier.metrics_refresh import load_published_lips

    with caplog.at_level("WARNING"):
        out = await load_published_lips(db_session)

    assert out == {"9_99"}
    assert any("не список" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_select_refresh_candidates_counts_unparsable_urls(db_session):
    """Строки с неразбираемым post_url считаются, а не пропадают молча.

    Приёмочная проверка «доля views IS NOT NULL около 90%» без этого числа
    показывала бы необъяснимое расхождение.
    """
    now = datetime.utcnow()
    good = _row("5_1", -1, 1, published_at=now - timedelta(hours=1))
    bad = CollectedPostAudit(
        lip="5_2",
        region_code="mi",
        post_url="https://vk.com/club123",  # не wall — ref не собрать
        decision="kept",
        published_at=now - timedelta(hours=1),
        collected_at=now,
    )
    db_session.add_all([good, bad])
    await db_session.commit()

    out, unparsable = await select_refresh_candidates(db_session, hours=72)

    assert [lip for _, lip in out] == ["5_1"]
    assert unparsable == 1


@pytest.mark.asyncio
async def test_refresh_metrics_releases_db_before_going_to_vk(db_session, monkeypatch):
    """Транзакция БД закрывается ДО похода в ВК и токен уезжает в фетчер.

    Круг из 78 батчей идёт через per-token тормоз — это полминуты-минута.
    Держать всё это время открытую транзакцию значит держать Postgres в
    idle-in-transaction восемь раз в сутки. Токен в вызове фетчера — вторая
    половина той же находки: без него батчи ушли бы мимо общего тормоза.
    """
    from unittest.mock import AsyncMock, MagicMock

    import modules.classifier.metrics_refresh as metrics_refresh_mod
    import modules.vk_monitor.post_metrics as post_metrics_mod
    import modules.vk_token_router as token_router_mod
    from modules.classifier.metrics_refresh import refresh_metrics

    db_session.add(_row("6_1", -1, 1, published_at=datetime.utcnow() - timedelta(hours=1)))
    await db_session.commit()

    events = []
    original_commit = db_session.commit

    async def spy_commit():
        events.append("commit")
        await original_commit()

    monkeypatch.setattr(db_session, "commit", spy_commit)
    monkeypatch.setattr(metrics_refresh_mod, "load_published_lips", AsyncMock(return_value=set()))
    monkeypatch.setattr(token_router_mod, "get_healthy_read_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr("vk_api.VkApi", MagicMock())

    seen_kwargs = {}

    def fake_fetch(api, refs, **kw):
        events.append("vk")
        seen_kwargs.update(kw)
        return {(-1, 1): {"views": 5, "likes": 1, "comments": 0, "reposts": 0}}

    monkeypatch.setattr(post_metrics_mod, "fetch_metrics_for_token", fake_fetch)

    result = await refresh_metrics(db_session, hours=72)

    assert result["ok"] is True and result["updated"] == 1
    assert events.index("commit") < events.index("vk"), events
    assert seen_kwargs["token"] == "tok"


@pytest.mark.asyncio
async def test_apply_metrics_counts_a_freshly_dated_row_once(db_session):
    """Строка с пустой published_at считается ОДИН раз, а не два.

    lip уникален, поэтому обе ветки записи (по IS NULL и по IS NOT NULL) бьют
    в одну и ту же строку. Без условия второй UPDATE переписывал бы её же
    значения в той же транзакции и инкремент шёл бы дважды. На первом прод-круге
    published_at пуст у всех 7774 строк — таска отрапортовала бы updated вдвое
    больше checked, и число, по которому читают исход круга, врало бы вдвое.
    """
    db_session.add(_row("7_1", -1, 1, published_at=None))
    await db_session.commit()

    new_date = datetime.utcnow() - timedelta(hours=2)
    metrics = {
        (-1, 1): {"views": 10, "likes": 1, "comments": 0, "reposts": 0, "published_at": new_date}
    }

    assert await apply_metrics(db_session, metrics, {(-1, 1): "7_1"}) == 1

    row = (
        await db_session.execute(select(CollectedPostAudit).where(CollectedPostAudit.lip == "7_1"))
    ).scalar_one()
    assert row.published_at == new_date
    assert row.views == 10


@pytest.mark.asyncio
async def test_apply_metrics_counts_an_already_dated_row_once(db_session):
    """Вторая ветка (дата уже есть) тоже даёт ровно один инкремент и не трогает дату."""
    existing_date = datetime.utcnow() - timedelta(days=2)
    db_session.add(_row("7_2", -1, 2, published_at=existing_date))
    await db_session.commit()

    metrics = {
        (-1, 2): {
            "views": 20,
            "likes": 2,
            "comments": 0,
            "reposts": 0,
            "published_at": datetime.utcnow() - timedelta(hours=1),
        }
    }

    assert await apply_metrics(db_session, metrics, {(-1, 2): "7_2"}) == 1

    row = (
        await db_session.execute(select(CollectedPostAudit).where(CollectedPostAudit.lip == "7_2"))
    ).scalar_one()
    assert row.published_at == existing_date  # дата поста не меняется
    assert row.views == 20


@pytest.mark.asyncio
async def test_apply_metrics_writes_rows_in_batches(db_session, monkeypatch):
    """Круг пишет пачками, а не по запросу на строку.

    Замер 22.10: ~14 500 отдельных UPDATE за круг, 290–368 с на таске, 48 мин
    слота в сутки. Пачка — это одна поездка в Postgres на 500 строк; здесь
    потолок искусственно сжат до 2, чтобы границы были видны.
    """
    from modules.classifier import metrics_refresh

    for i in range(1, 4):
        db_session.add(_row(f"8_{i}", -1, i, published_at=datetime.utcnow()))
    await db_session.commit()

    metrics = {
        (-1, i): {"views": 10 * i, "likes": i, "comments": 0, "reposts": 0, "published_at": None}
        for i in range(1, 4)
    }
    lip_by_ref = {(-1, i): f"8_{i}" for i in range(1, 4)}

    chunks = []
    real = metrics_refresh._apply_batch

    async def spy(session, rows, now):
        chunks.append(len(rows))
        return await real(session, rows, now)

    monkeypatch.setattr(metrics_refresh, "_BATCH_ROWS", 2)
    monkeypatch.setattr(metrics_refresh, "_apply_batch", spy)

    assert await metrics_refresh.apply_metrics(db_session, metrics, lip_by_ref) == 3
    assert chunks == [2, 1]


@pytest.mark.asyncio
async def test_apply_metrics_skips_rows_that_would_not_change(db_session):
    """Дата уже есть, метрик нет — писать нечего, и это НЕ «обновлённая строка».

    Иначе гейт ``no_metrics_fetched`` («проверено много, обновлено ноль — это
    отказ») не сработал бы никогда: пачка отметила бы такие строки изменёнными.
    """
    existing_date = datetime.utcnow() - timedelta(days=2)
    db_session.add(_row("8_9", -1, 9, published_at=existing_date, views=7))
    await db_session.commit()

    metrics = {
        (-1, 9): {
            "views": None,
            "likes": None,
            "comments": None,
            "reposts": None,
            "published_at": datetime.utcnow() - timedelta(hours=1),
        }
    }

    assert await apply_metrics(db_session, metrics, {(-1, 9): "8_9"}) == 0

    row = (
        await db_session.execute(select(CollectedPostAudit).where(CollectedPostAudit.lip == "8_9"))
    ).scalar_one()
    assert row.published_at == existing_date
    assert row.views == 7
    assert row.metrics_updated_at is None


@pytest.mark.asyncio
async def test_load_published_lips_narrows_to_candidates(db_session):
    """Вопрос задаётся только про кандидатов окна — ответ от этого не меняется.

    Без сужения функция поднимала в память ВСЮ колонку work_tables.lip, то
    есть всю историю публикаций сети, хотя вызывающему нужно пересечение с
    7,8 тыс. кандидатов окна. На боксе с 1536 МБ и без swap это разница,
    которая достаётся даром: drop_already_published всё равно смотрит только
    на кандидатов.
    """
    WorkTable = await _make_work_tables(db_session)
    db_session.add_all(
        [_work(WorkTable, ["1_10", "2_20"], "novost"), _work(WorkTable, ["3_30"], "sport")]
    )
    await db_session.commit()

    from modules.classifier.metrics_refresh import load_published_lips

    out = await load_published_lips(db_session, {"2_20", "9_99"})

    assert out == {"2_20"}


@pytest.mark.asyncio
async def test_load_published_lips_without_candidates_keeps_old_behaviour(db_session):
    """Без аргумента — прежний полный ответ: сужение не должно быть обязательным."""
    WorkTable = await _make_work_tables(db_session)
    db_session.add_all([_work(WorkTable, ["1_10", "2_20"], "novost")])
    await db_session.commit()

    from modules.classifier.metrics_refresh import load_published_lips

    assert await load_published_lips(db_session) == {"1_10", "2_20"}


@pytest.mark.asyncio
async def test_load_published_lips_with_empty_candidates_asks_nothing(db_session):
    """Пустое множество кандидатов — не повод ходить в БД."""
    WorkTable = await _make_work_tables(db_session)
    db_session.add_all([_work(WorkTable, ["1_10"], "novost")])
    await db_session.commit()

    from modules.classifier.metrics_refresh import load_published_lips

    assert await load_published_lips(db_session, set()) == set()


@pytest.mark.asyncio
async def test_refresh_metrics_asks_only_about_window_candidates(db_session, monkeypatch):
    """refresh_metrics обязан передавать кандидатов, иначе сужение мертво.

    Легко сделать функцию с необязательным аргументом и забыть его передать:
    тесты самой функции останутся зелёными, а на проде ничего не изменится.
    """
    from unittest.mock import AsyncMock

    from modules import vk_token_router as token_router_mod
    from modules.classifier import metrics_refresh as mod

    seen = {}

    async def _fake_candidates(session, *, hours):
        return ([((-100, 7), "100_7")], 0)

    async def _fake_published(session, candidate_lips=None):
        seen["candidates"] = candidate_lips
        return set()

    monkeypatch.setattr(mod, "select_refresh_candidates", _fake_candidates)
    monkeypatch.setattr(mod, "load_published_lips", _fake_published)
    monkeypatch.setattr(token_router_mod, "get_healthy_read_token", AsyncMock(return_value=None))

    await mod.refresh_metrics(db_session)

    assert seen["candidates"] == {"100_7"}
