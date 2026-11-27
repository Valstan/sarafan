"""Тесты приёма заявок с публичного лендинга (web/api/landing).

Заказ владельца 2026-11-27: клиент нажимает кнопку на витрине и заявка падает
в базу. Проверяем ровно то, что делает функцию безопасной для публичного
эндпоинта: недоверенный вход (цена и названия — из БД, не из тела запроса),
дедуп двойного клика, honeypot против ботов и лимиты.

Сессия БД мокается (AsyncMock) — как в tests/test_api/test_ad_crm.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from database.models import Region
from web.api import landing as api

# ----------------------------------------------------------------- helpers


def _db():
    db = AsyncMock()
    db.add = MagicMock()
    return db


def _scalars(items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = items
    return r


def _rows(items):
    r = MagicMock()
    r.all.return_value = items
    return r


def _region(code="malm", name="Малмыж", region_id=3, members=1000):
    region = Region(id=region_id, code=code, name=name, vk_group_id=-100)
    region.config = {}
    return region


def _request(ip="203.0.113.7", xff=None):
    req = MagicMock()
    req.headers = {"host": "xn--80adkmnnb2b.xn--80adkdyec4j.xn--p1ai"}
    if xff is not None:
        req.headers["X-Forwarded-For"] = xff
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.url = MagicMock()
    return req


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """Отключаем внешние каналы: Redis, Telegram, MAX."""
    monkeypatch.setattr(api, "_spam_check", lambda ip: True)
    monkeypatch.setattr(api, "_notify_owner", AsyncMock(return_value=True))


# ----------------------------------------------------------------- тесты


class TestHoneypot:
    async def test_honeypot_filled_is_not_saved(self):
        db = _db()
        payload = api.LandingOrderIn(
            name="Бот", contact="bot@example.com", website="http://spam.example"
        )
        out = await api.create_landing_order(payload, _request(), db)
        assert out["ok"] is True
        assert out["saved"] is False
        db.add.assert_not_called()


class TestOrderCreation:
    async def test_saves_order_with_server_side_price_and_regions(self, monkeypatch):
        db = _db()
        db.execute.side_effect = [_scalars([_region()]), _rows([(3, 1234, 1)])]
        db.refresh.side_effect = lambda obj: setattr(obj, "id", 42)

        # Клиент прислал свои числа (в том числе цену 0) — сервер их игнорирует.
        payload = api.LandingOrderIn(
            name="Иван Петров",
            contact="+7 900 000-00-00",
            regions=[{"code": "malm", "name": "ЧУЖОЕ ИМЯ", "members": 1, "price": 0}],
            comment="ремонт",
        )
        out = await api.create_landing_order(payload, _request(), db)

        assert out["ok"] is True
        assert out["order_id"] == 42
        assert out["ref"] == "З-42"
        assert out["saved"] is True

        # add вызывается дважды: сама заявка и метка «уведомление ушло».
        assert db.add.call_count == 2
        row = db.add.call_args_list[0][0][0]
        # Название и подписчики — из БД, не из тела запроса.
        assert row.regions_json[0]["name"] == "Малмыж"
        assert row.regions_json[0]["members"] == 1234
        assert row.reach == 1234
        # Цена — по таблице расценок, а не из запроса.
        assert row.price_estimate and row.price_estimate > 0
        assert row.comment == "ремонт"
        assert row.source_host == "xn--80adkmnnb2b.xn--80adkdyec4j.xn--p1ai"

    async def test_requested_code_absent_in_db_is_dropped(self):
        db = _db()
        db.execute.side_effect = [_scalars([]), _rows([])]
        db.refresh.side_effect = lambda obj: setattr(obj, "id", 7)

        payload = api.LandingOrderIn(
            name="Иван", contact="+7 900 000-00-00", regions=[{"code": "нет-такого"}]
        )
        out = await api.create_landing_order(payload, _request(), db)
        assert out["ok"] is True
        row = db.add.call_args[0][0]
        assert row.regions_count == 0
        assert row.reach is None


class TestDeduplication:
    async def test_same_token_returns_existing_order(self):
        db = _db()
        existing = MagicMock()
        existing.id = 5
        existing.regions_count = 2
        existing.price_estimate = 900
        db.execute.side_effect = [_scalars_first(existing)]

        payload = api.LandingOrderIn(name="Иван", contact="+7 900 000-00-00", request_token="tok-1")
        out = await api.create_landing_order(payload, _request(), db)

        assert out["order_id"] == 5
        assert out["duplicate"] is True
        db.add.assert_not_called()


def _scalars_first(item):
    r = MagicMock()
    r.scalars.return_value.first.return_value = item
    return r


class TestValidation:
    async def test_too_many_regions_rejected(self):
        db = _db()
        payload = api.LandingOrderIn(
            name="Иван",
            contact="+7 900 000-00-00",
            regions=[{"code": f"r{i}"} for i in range(api.MAX_REGIONS + 1)],
        )
        with pytest.raises(HTTPException) as exc:
            await api.create_landing_order(payload, _request(), db)
        assert exc.value.status_code == 400

    async def test_short_contact_rejected_by_schema(self):
        with pytest.raises(Exception):
            api.LandingOrderIn(name="Иван", contact="x")


class TestClientIp:
    def test_first_forwarded_for_element_wins(self):
        req = _request(xff="198.51.100.9, 81.177.6.46")
        assert api._client_ip(req) == "198.51.100.9"

    def test_falls_back_to_real_ip(self):
        req = _request()
        req.headers["X-Real-IP"] = "203.0.113.1"
        assert api._client_ip(req) == "203.0.113.1"
