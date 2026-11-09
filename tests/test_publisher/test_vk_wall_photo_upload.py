"""Тесты загрузки картинок на стену сообщества (планировщик кабинета)."""

from __future__ import annotations

from unittest.mock import MagicMock

import modules.publisher.vk_wall_photo_upload as wpu


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


def test_upload_wall_photo_returns_attachment(monkeypatch):
    api = MagicMock()
    api.photos.getWallUploadServer.return_value = {"upload_url": "http://up"}
    api.photos.saveWallPhoto.return_value = [{"owner_id": -5, "id": 99}]
    monkeypatch.setattr(
        wpu.requests,
        "post",
        lambda *a, **k: _resp({"server": 1, "photo": "p", "hash": "h"}),
    )

    att = wpu.upload_wall_photo(api, b"bytes", group_id=5)

    assert att == "photo-5_99"
    # VK ждёт положительный group_id для wall-upload.
    api.photos.getWallUploadServer.assert_called_once_with(group_id=5)


def test_upload_wall_photo_normalizes_negative_group_id(monkeypatch):
    api = MagicMock()
    api.photos.getWallUploadServer.return_value = {"upload_url": "http://up"}
    api.photos.saveWallPhoto.return_value = [{"owner_id": -5, "id": 1, "access_key": "ak"}]
    monkeypatch.setattr(
        wpu.requests,
        "post",
        lambda *a, **k: _resp({"server": 1, "photo": "p", "hash": "h"}),
    )

    att = wpu.upload_wall_photo(api, b"x", group_id=-5)

    # access_key в строку вложения НЕ идёт: с ним фото осталось бы личным
    # (owner_id = пользователь) и копилось бы в альбоме владельца. Без него фото
    # переходит в собственность группы — docs/ops/photo-upload-path.md.
    assert att == "photo-5_1"
    api.photos.getWallUploadServer.assert_called_once_with(group_id=5)


def test_upload_wall_photo_empty_returns_none(monkeypatch):
    api = MagicMock()
    api.photos.getWallUploadServer.return_value = {"upload_url": "http://up"}
    monkeypatch.setattr(
        wpu.requests,
        "post",
        lambda *a, **k: _resp({"server": 1, "photo": "[]", "hash": "h"}),
    )

    assert wpu.upload_wall_photo(api, b"x", group_id=5) is None


def test_upload_wall_images_caps_at_10(monkeypatch):
    api = MagicMock()
    api.photos.getWallUploadServer.return_value = {"upload_url": "http://up"}
    counter = {"n": 0}

    def _save(*a, **k):
        counter["n"] += 1
        return [{"owner_id": -5, "id": counter["n"]}]

    api.photos.saveWallPhoto.side_effect = _save
    monkeypatch.setattr(
        wpu.requests,
        "post",
        lambda *a, **k: _resp({"server": 1, "photo": "p", "hash": "h"}),
    )

    out = wpu.upload_wall_images(api, [b"x"] * 14, group_id=5)

    assert len(out) == 10
    assert all(a.startswith("photo-5_") for a in out)
