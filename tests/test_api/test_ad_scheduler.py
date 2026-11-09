"""Тесты планировщика отложенных постов рекламного кабинета (B1-b).

Сессия БД мокается (AsyncMock), VK-публикация/маршрутизация monkeypatch'атся —
без реальной БД и VK, в стиле tests/test_api/test_ad_cabinet.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import modules.publisher.vk_publisher_extended as vpe
import modules.vk_token_router as token_router
from database.models import AdClient, AdRequest, AdScheduledPost
from web.api import ad_cabinet as api
from web.api import ad_crm

# Дата заведомо в будущем (МСК) — устойчиво к запуску тестов в любой год.
_FUTURE = "2090-01-01T12:00:00"
_FUTURE2 = "2090-01-02T09:30:00"


def _scalars_all(objs):
    r = MagicMock()
    r.scalars.return_value.all.return_value = objs
    return r


def _scalar_one(obj):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    return r


def _fake_publisher(publish_result=None, **overrides):
    pub = MagicMock()
    pub.publish_bulletin = AsyncMock(
        return_value=publish_result or {"success": True, "post_id": 999, "postponed": True}
    )
    pub.set_post_comments = AsyncMock(return_value={"success": True})
    pub.delete_post = AsyncMock(return_value={"success": True})
    for k, v in overrides.items():
        setattr(pub, k, v)
    return pub


def _patch_publish(monkeypatch, pub):
    monkeypatch.setattr(token_router, "load_vk_routing", AsyncMock(return_value=("utok", {})))
    # Заливку фото делает аккаунт-владелец группы, а не публикатор.
    monkeypatch.setattr(token_router, "load_photo_upload_token", AsyncMock(return_value="phototok"))
    monkeypatch.setattr(api, "_build_wall_attachment", lambda *a, **k: [])
    monkeypatch.setattr(vpe.VKPublisher, "create_with_policy", AsyncMock(return_value=pub))


def _create_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ----------------------------------------------------------------- helpers


def test_msk_to_unix_converts_wall_clock():
    from datetime import datetime

    # 2070-01-01 03:00 МСК == 2070-01-01 00:00 UTC.
    dt = datetime(2070, 1, 1, 3, 0, 0)
    expected = int(datetime(2070, 1, 1, 0, 0, 0).replace(tzinfo=api.timezone.utc).timestamp())
    assert api._msk_to_unix(dt) == expected


def test_ad_scheduled_post_to_dict_derives_url():
    row = AdScheduledPost(
        community_vk_id=-100,
        publish_date=None,
        status="scheduled",
        vk_postponed_post_id=42,
    )
    d = row.to_dict()
    assert d["vk_post_url"] == "https://vk.com/wall-100_42"
    assert d["status"] == "scheduled"


# ----------------------------------------------------------------- create


async def test_create_scheduled_multi_date(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="реклама", dates=[_FUTURE, _FUTURE2]),
        db=db,
    )

    assert out["scheduled"] == 2
    assert out["failed"] == 0
    assert len(out["created"]) == 2
    assert all(r["status"] == "scheduled" for r in out["created"])
    # На каждую дату — отдельный wall.post с publish_date.
    assert pub.publish_bulletin.await_count == 2
    for call in pub.publish_bulletin.await_args_list:
        assert call.kwargs["publish_date"] > 0
    db.commit.assert_awaited()


async def test_create_rejects_past_date(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    with pytest.raises(HTTPException) as exc:
        await api.create_scheduled(
            api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=["2000-01-01T00:00:00"]),
            db=db,
        )
    assert exc.value.status_code == 400
    pub.publish_bulletin.assert_not_awaited()


async def test_create_rejects_empty_post(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    with pytest.raises(HTTPException) as exc:
        await api.create_scheduled(
            api.ScheduleCreateIn(community_vk_id=-100, text="  ", dates=[_FUTURE]),
            db=db,
        )
    assert exc.value.status_code == 400


async def test_create_requires_dates(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    with pytest.raises(HTTPException) as exc:
        await api.create_scheduled(
            api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[]), db=db
        )
    assert exc.value.status_code == 400


async def test_create_partial_failure(monkeypatch):
    """Одна дата падает на публикации → её строка failed, остальные scheduled."""
    pub = _fake_publisher()
    pub.publish_bulletin = AsyncMock(
        side_effect=[
            {"success": True, "post_id": 1},
            {"success": False, "error": "VK error 214 (too many postponed)"},
        ]
    )
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE, _FUTURE2]),
        db=db,
    )
    assert out["scheduled"] == 1
    assert out["failed"] == 1
    statuses = sorted(r["status"] for r in out["created"])
    assert statuses == ["failed", "scheduled"]
    failed = [r for r in out["created"] if r["status"] == "failed"][0]
    assert "postponed" in failed["error_message"]


async def test_create_closes_comments_when_disabled(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100, text="x", dates=[_FUTURE], comments_enabled=False
        ),
        db=db,
    )
    pub.set_post_comments.assert_awaited_once()
    _args, kwargs = pub.set_post_comments.await_args
    assert kwargs["enabled"] is False


async def test_create_keeps_comments_open_by_default(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE]),
        db=db,
    )
    pub.set_post_comments.assert_not_awaited()


# ------------------------------------------------- B2: запланировать заявку


async def test_remove_original_deletes_suggested_and_publishes(monkeypatch):
    """remove_original + источник + успех → wall.delete оригинала, заявка published."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    ar = AdRequest(community_vk_id=-170437443, vk_post_id=20278, status="new")
    db = _create_db()
    db.get = AsyncMock(return_value=ar)

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-170437443,
            text="реклама",
            dates=[_FUTURE],
            source_ad_request_id=7,
            remove_original=True,
        ),
        db=db,
    )

    assert out["scheduled"] == 1
    assert out["original_removed"] is True
    assert out["original_remove_error"] is None
    pub.delete_post.assert_awaited_once_with(-170437443, 20278)
    assert ar.status == "published"


async def test_remove_original_skipped_when_nothing_scheduled(monkeypatch):
    """Полный провал планирования → оригинал НЕ трогаем (не теряем заявку)."""
    pub = _fake_publisher()
    pub.publish_bulletin = AsyncMock(return_value={"success": False, "error": "VK 214"})
    _patch_publish(monkeypatch, pub)
    db = _create_db()
    db.get = AsyncMock()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100,
            text="x",
            dates=[_FUTURE],
            source_ad_request_id=7,
            remove_original=True,
        ),
        db=db,
    )

    assert out["scheduled"] == 0
    assert out["original_removed"] is False
    pub.delete_post.assert_not_awaited()
    db.get.assert_not_awaited()


async def test_remove_original_publishes_even_if_delete_fails(monkeypatch):
    """wall.delete оригинала упал → заявка всё равно published, ошибка в ответе."""
    pub = _fake_publisher()
    pub.delete_post = AsyncMock(return_value={"success": False, "error": "post not found"})
    _patch_publish(monkeypatch, pub)
    ar = AdRequest(community_vk_id=-100, vk_post_id=555, status="new")
    db = _create_db()
    db.get = AsyncMock(return_value=ar)

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100,
            text="x",
            dates=[_FUTURE],
            source_ad_request_id=7,
            remove_original=True,
        ),
        db=db,
    )

    assert out["original_removed"] is False
    assert out["original_remove_error"] == "post not found"
    assert ar.status == "published"


async def test_no_source_request_no_removal(monkeypatch):
    """remove_original без source_ad_request_id — игнор (обычная раскладка)."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()
    db.get = AsyncMock()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE], remove_original=True),
        db=db,
    )
    assert out["original_removed"] is False
    pub.delete_post.assert_not_awaited()
    db.get.assert_not_awaited()


# ------------------------------------------------- C: привязка client_id/price


async def test_create_binds_explicit_client_and_price(monkeypatch):
    """Явные client_id+price пишутся в отложку; клиент продвигается в scheduled."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    client = AdClient(author_vk_id=77, stage="contacted")
    db = _create_db()
    db.get = AsyncMock(return_value=client)

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100, text="реклама", dates=[_FUTURE], client_id=77, price=1500
        ),
        db=db,
    )

    assert out["scheduled"] == 1
    assert out["client_id"] == 77
    assert out["created"][0]["client_id"] == 77
    assert out["created"][0]["price"] == 1500.0
    assert client.stage == "scheduled"  # contacted → scheduled


async def test_create_resolves_client_from_source_request(monkeypatch):
    """Без явного client_id — резолвим из заявки (ar.client_id) + бэкфилл строк."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    ar = AdRequest(community_vk_id=-100, vk_post_id=5, status="new", client_id=55)
    client = AdClient(author_vk_id=55, stage="detected")
    db = _create_db()
    db.get = AsyncMock(side_effect=lambda model, pk: ar if model is AdRequest else client)

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100, text="x", dates=[_FUTURE], source_ad_request_id=9
        ),
        db=db,
    )

    assert out["client_id"] == 55
    assert out["created"][0]["client_id"] == 55  # бэкфилл на запланированную строку
    assert client.stage == "scheduled"  # detected → scheduled


async def test_create_does_not_downgrade_paid_client(monkeypatch):
    """Уже оплаченного клиента раскладка не понижает обратно в scheduled."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    client = AdClient(author_vk_id=77, stage="paid")
    db = _create_db()
    db.get = AsyncMock(return_value=client)

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE], client_id=77),
        db=db,
    )
    assert out["client_id"] == 77
    assert client.stage == "paid"  # не тронут


async def test_create_no_client_keeps_null(monkeypatch):
    """Без клиента и заявки — client_id в ответе None, лишних db.get нет."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()
    db.get = AsyncMock()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE]),
        db=db,
    )
    assert out["client_id"] is None
    db.get.assert_not_awaited()


# ------------------------------------------------- С2: срок размещения (expires_at)


async def test_create_expire_days_sets_expiry(monkeypatch):
    """expire_days → expires_at = publish_date + N дней (на каждый пост)."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE], expire_days=14),
        db=db,
    )
    # publish 2090-01-01T12:00 + 14 дней = 2090-01-15T12:00
    assert out["created"][0]["expires_at"] == "2090-01-15T12:00:00"


async def test_create_expire_at_sets_explicit_expiry(monkeypatch):
    """expire_at → одна явная дата снятия для всех постов раскладки."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100,
            text="x",
            dates=[_FUTURE, _FUTURE2],
            expire_at="2090-03-01T00:00:00",
        ),
        db=db,
    )
    assert out["created"][0]["expires_at"] == "2090-03-01T00:00:00"
    assert out["created"][1]["expires_at"] == "2090-03-01T00:00:00"


async def test_create_expire_at_takes_priority_over_days(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(
            community_vk_id=-100,
            text="x",
            dates=[_FUTURE],
            expire_days=5,
            expire_at="2090-03-01T00:00:00",
        ),
        db=db,
    )
    assert out["created"][0]["expires_at"] == "2090-03-01T00:00:00"


async def test_create_no_expiry_by_default(monkeypatch):
    """Без срока — expires_at NULL (пост висит вечно)."""
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE]),
        db=db,
    )
    assert out["created"][0]["expires_at"] is None


async def test_create_rejects_nonpositive_expire_days(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    with pytest.raises(HTTPException) as exc:
        await api.create_scheduled(
            api.ScheduleCreateIn(community_vk_id=-100, text="x", dates=[_FUTURE], expire_days=0),
            db=db,
        )
    assert exc.value.status_code == 400


async def test_create_rejects_bad_expire_at(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    with pytest.raises(HTTPException) as exc:
        await api.create_scheduled(
            api.ScheduleCreateIn(
                community_vk_id=-100, text="x", dates=[_FUTURE], expire_at="не-дата"
            ),
            db=db,
        )
    assert exc.value.status_code == 400


# ------------------------------------------------- С5: сквозное оформление (accept)


async def test_accept_request_orchestrates(monkeypatch):
    """accept = upsert клиента + ответ + create_scheduled; свод результатов."""
    ar = AdRequest(id=7, community_vk_id=-100, text_snapshot="реклама", region_id=3, status="new")
    db = _create_db()
    db.get = AsyncMock(return_value=ar)
    monkeypatch.setattr(
        ad_crm,
        "upsert_from_request",
        AsyncMock(return_value={"client": {"id": 5}, "created": True}),
    )
    sched_mock = AsyncMock(
        return_value={"scheduled": 1, "failed": 0, "original_removed": True, "client_id": 5}
    )
    monkeypatch.setattr(api, "create_scheduled", sched_mock)
    send_mock = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(api, "send_reply", send_mock)

    out = await api.accept_request(
        7,
        api.AcceptRequestIn(dates=[_FUTURE], price=1500, expire_days=14, reply_message="спасибо"),
        db=db,
    )

    assert out["scheduled"] == 1
    assert out["original_removed"] is True
    assert out["client_id"] == 5
    send_mock.assert_awaited_once()  # reply_message задан → отправляем
    # create_scheduled получил правильные поля заявки.
    sched_arg = sched_mock.await_args.args[0]
    assert sched_arg.community_vk_id == -100
    assert sched_arg.source_ad_request_id == 7
    assert sched_arg.price == 1500
    assert sched_arg.expire_days == 14
    assert sched_arg.remove_original is True


async def test_accept_request_no_reply_when_empty(monkeypatch):
    ar = AdRequest(id=7, community_vk_id=-100, text_snapshot="x", status="new")
    db = _create_db()
    db.get = AsyncMock(return_value=ar)
    monkeypatch.setattr(
        ad_crm,
        "upsert_from_request",
        AsyncMock(return_value={"client": {"id": 5}, "created": False}),
    )
    monkeypatch.setattr(
        api, "create_scheduled", AsyncMock(return_value={"scheduled": 1, "failed": 0})
    )
    send_mock = AsyncMock()
    monkeypatch.setattr(api, "send_reply", send_mock)

    out = await api.accept_request(7, api.AcceptRequestIn(dates=[_FUTURE]), db=db)
    assert out["scheduled"] == 1
    send_mock.assert_not_awaited()  # без reply_message ответ не шлём


async def test_accept_request_404(monkeypatch):
    db = _create_db()
    db.get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await api.accept_request(999, api.AcceptRequestIn(dates=[_FUTURE]), db=db)
    assert exc.value.status_code == 404


async def test_accept_request_requires_dates():
    ar = AdRequest(id=7, community_vk_id=-100, status="new")
    db = _create_db()
    db.get = AsyncMock(return_value=ar)
    with pytest.raises(HTTPException) as exc:
        await api.accept_request(7, api.AcceptRequestIn(dates=[]), db=db)
    assert exc.value.status_code == 400


# ----------------------------------------------------------------- list


async def test_list_scheduled_serializes():
    row = AdScheduledPost(
        community_vk_id=-100, publish_date=None, status="scheduled", vk_postponed_post_id=7
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalars_all([row]))
    out = await api.list_scheduled(db=db)
    assert len(out["scheduled"]) == 1
    assert out["scheduled"][0]["vk_post_url"] == "https://vk.com/wall-100_7"


# ----------------------------------------------------------------- cancel


async def test_cancel_deletes_and_marks(monkeypatch):
    pub = _fake_publisher()
    _patch_publish(monkeypatch, pub)
    row = AdScheduledPost(
        community_vk_id=-100, publish_date=None, status="scheduled", vk_postponed_post_id=42
    )
    db = AsyncMock()
    db.add = MagicMock()  # реальная AsyncSession.add синхронна (лог взаимодействий)
    db.execute = AsyncMock(return_value=_scalar_one(row))  # SELECT … FOR UPDATE

    out = await api.cancel_scheduled(1, db=db)
    assert out["status"] == "cancelled"
    pub.delete_post.assert_awaited_once_with(-100, 42)


async def test_cancel_vk_fail_keeps_status(monkeypatch):
    pub = _fake_publisher()
    pub.delete_post = AsyncMock(return_value={"success": False, "error": "already published"})
    _patch_publish(monkeypatch, pub)
    row = AdScheduledPost(
        community_vk_id=-100, publish_date=None, status="scheduled", vk_postponed_post_id=42
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_one(row))

    out = await api.cancel_scheduled(1, db=db)
    assert out["cancel_error"] == "already published"
    assert row.status == "scheduled"  # статус НЕ изменён


async def test_cancel_404():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_one(None))
    with pytest.raises(HTTPException) as exc:
        await api.cancel_scheduled(999, db=db)
    assert exc.value.status_code == 404


# ------------------------------------------- отказ шлюза ВК против отказа ВК


async def test_gateway_outage_is_reported_as_retryable(monkeypatch):
    """«ВК полежал» и «ВК отказал» — разные события, и оператору нужно разное.

    07.09 ВК отвечал 5xx полторы минуты. Строки раскладки вставали в ``failed``
    с текстом «VK API error: Response code 502», и по нему нельзя было понять,
    повторять раскладку или идти разбираться с правами.

    Автоповтора здесь нет намеренно: отложенные посты живут в собственной
    отложке ВК, celery-задачи, которая бы их дожимала, в проекте не
    существует, — а заводить её ради полутора минут аварии значит завести
    новый механизм с собственным риском дублей. Повторяет человек; ему и
    говорим словами.
    """
    pub = _fake_publisher(
        publish_result={
            "success": False,
            "error": "VK API error: Response code 502",
            "transient": True,
        }
    )
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="реклама", dates=[_FUTURE]),
        db=db,
    )

    assert out["vk_unavailable"] is True
    assert out["scheduled"] == 0
    row = out["created"][0]
    # Статус остаётся failed СОЗНАТЕЛЬНО: только он выводит строку из-под
    # уникума uq_ad_sched_client_day_slot (pending/scheduled/published), то
    # есть повторная раскладка той же даты пройдёт, а не упрётся в 409.
    assert row["status"] == "failed"
    assert "повторите раскладку" in row["error_message"]
    assert "502" not in row["error_message"], "человеку нужен не код шлюза, а действие"


async def test_ordinary_refusal_is_not_dressed_up_as_an_outage(monkeypatch):
    """Гвоздь: отказ ВК с кодом не должен предлагать «просто повторите».

    Повтор раскладки на 214 («нет права постить») означал бы бесконечный круг
    по кнопке вместо разбирательства с правами.
    """
    pub = _fake_publisher(
        publish_result={
            "success": False,
            "error": "VK API error: [214] Access to adding post denied",
            "vk_error_code": 214,
        }
    )
    _patch_publish(monkeypatch, pub)
    db = _create_db()

    out = await api.create_scheduled(
        api.ScheduleCreateIn(community_vk_id=-100, text="реклама", dates=[_FUTURE]),
        db=db,
    )

    assert out["vk_unavailable"] is False
    row = out["created"][0]
    assert row["status"] == "failed"
    assert "повторите" not in row["error_message"]
    assert "214" in row["error_message"], "сырую строку с кодом в БД сохраняем как прежде"
