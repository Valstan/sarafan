"""Обработка фото для вечерней сводки: водяной знак, шум, ресайз (P214).

Спецификация — решения владельца от 18.10 (P203): ресайз **без обрезки**,
водяной знак полупрозрачный с тенью, шум незаметный для глаза.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

import modules.publisher.photo_processing as photo_processing
from modules.publisher.photo_processing import (
    MAX_H,
    MAX_W,
    add_noise,
    add_watermark,
    process_photo,
    resize_for_phone,
    upload_wall_photo,
)


def _photo(width: int = 2000, height: int = 1000, color=(120, 130, 140)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    # Яркие квадраты у краёв: по ним видно, что кропа не было — метка осталась
    # у того же края. Квадрат, а не точка: JPEG и ресайз съедают одиночный пиксель.
    for x0, y0 in ((0, 0), (width - 40, 0), (0, height - 40), (width - 40, height - 40)):
        for x in range(x0, x0 + 40):
            for y in range(y0, y0 + 40):
                img.putpixel((x, y), (255, 0, 0))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()


def test_resize_keeps_proportions_and_does_not_crop():
    """Обрезка запрещена решением владельца: пропорции сохраняются, углы на месте."""
    img = Image.open(io.BytesIO(_photo()))
    resized = resize_for_phone(img.convert("RGB"))

    assert resized.width <= MAX_W and resized.height <= MAX_H
    # Исходник 2000×1000 (2:1) — после ресайза пропорция та же.
    assert abs(resized.width / resized.height - 2.0) < 0.02
    # Красные угловые квадраты остались по краям: значит, ничего не отрезано.
    for corner in ((0, 0), (resized.width - 1, 0)):
        r, g, b = resized.getpixel(corner)
        assert r > g + 40, f"угол срезан: {corner}"


def test_process_photo_returns_jpeg_without_exif():
    out = process_photo(_photo(), "Малмыжский район")

    assert out[:2] == b"\xff\xd8", "ожидался JPEG"
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert not img.info.get("exif"), "EXIF должен быть вычищен"
    assert img.width <= MAX_W and img.height <= MAX_H


def test_process_photo_makes_a_copy_not_a_duplicate():
    """Смысл цифрового шума: картинка перестаёт быть копией исходника."""
    raw = _photo()
    out = process_photo(raw, "Малмыжский район")

    assert out != raw
    original = Image.open(io.BytesIO(raw)).convert("RGB").resize((40, 20))
    processed = Image.open(io.BytesIO(out)).convert("RGB").resize((40, 20))
    diff = sum(
        abs(pa[i] - pb[i])
        for pa, pb in zip(original.getdata(), processed.getdata())
        for i in range(3)
    )
    assert diff > 0


def test_watermark_is_faint_but_present():
    """Владелец: «еле видно, но видно» — угол меняется, картинка почти не темнеет."""
    base = Image.new("RGB", (1080, 1350), (200, 200, 200))
    marked = add_watermark(base, "Малмыжский район")

    corner = [
        (x, y)
        for y in range(0, 160)
        for x in range(0, 400)
        if marked.getpixel((x, y)) != base.getpixel((x, y))
    ]
    assert corner, "знак не нарисован"

    changed = sum(
        1
        for y in range(0, 1350, 10)
        for x in range(0, 1080, 10)
        if abs(marked.getpixel((x, y))[0] - base.getpixel((x, y))[0]) > 3
    )
    # Знак занимает угол и не размазывает картинку целиком.
    assert 0 < changed < (1350 // 10) * (1080 // 10) * 0.25


def test_watermark_without_text_changes_nothing():
    base = Image.new("RGB", (200, 200), (10, 20, 30))
    assert add_watermark(base, "   ").tobytes() == base.tobytes()


def test_noise_is_subtle():
    """Шум «незаметный для глаза» — средний сдвиг яркости близок к нулю."""
    base = Image.new("RGB", (200, 200), (128, 128, 128))
    noisy = add_noise(base)

    pixels = list(noisy.getdata())
    mean = sum(sum(p) for p in pixels) / (len(pixels) * 3)
    assert 120 < mean < 136


class _FakePublisher:
    """Публикатор с одним фото: ``_invoke`` отвечает по-разному на методы."""

    def __init__(self, *, fail: bool = False, owner_client=None):
        self.fail = fail
        self.calls = []
        self.clients = []
        self.owner_client = owner_client if owner_client is not None else object()

    async def _photo_upload_client(self):
        """Клиент аккаунта-владельца: заливает фото он, а не community-токен."""
        return self.owner_client

    async def _invoke(self, client, method, params):
        self.calls.append((method, params))
        self.clients.append(client)
        if self.fail:
            raise RuntimeError("ВК недоступен")
        if method == "photos.getWallUploadServer":
            return {"upload_url": "https://upload.vk.example/srv"}
        if method == "photos.saveWallPhoto":
            return {"items": [{"owner_id": 20002978, "id": 777}]}
        raise AssertionError(f"неожиданный метод: {method}")


class _NoOwnerPublisher(_FakePublisher):
    """Аккаунта-владельца нет — заливать нечем."""

    async def _photo_upload_client(self):
        return None


@pytest.fixture(autouse=True)
def _fast_upload(monkeypatch):
    """Троттлинг загрузок и пауза после 405 в тестах — мгновенно."""
    monkeypatch.setattr(photo_processing, "UPLOAD_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(photo_processing, "UPLOAD_405_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(photo_processing, "_last_upload_at", 0.0)


@pytest.mark.asyncio
async def test_upload_wall_photo_returns_attachment(monkeypatch):
    publisher = _FakePublisher()
    monkeypatch.setattr("requests.post", _fake_post)

    att = await upload_wall_photo(publisher, -42, b"jpeg-bytes")

    assert att == "photo20002978_777"
    methods = [m for m, _ in publisher.calls]
    assert methods == ["photos.getWallUploadServer", "photos.saveWallPhoto"]
    # Загрузка идёт в ту же группу, в которую публикуем.
    assert publisher.calls[0][1]["group_id"] == 42


@pytest.mark.asyncio
async def test_upload_uses_owner_account_not_community_token(monkeypatch):
    """Заливает АККАУНТ-ВЛАДЕЛЕЦ: community-токен залить в стену не может ([27])."""
    owner_client = object()
    publisher = _FakePublisher(owner_client=owner_client)
    monkeypatch.setattr("requests.post", _fake_post)

    await upload_wall_photo(publisher, -42, b"jpeg-bytes")

    assert publisher.clients and all(c is owner_client for c in publisher.clients)


@pytest.mark.asyncio
async def test_upload_without_owner_account_is_skipped(monkeypatch):
    """Нет аккаунта-владельца — фото пропускаем, в ВК не ходим вовсе."""
    publisher = _NoOwnerPublisher()
    monkeypatch.setattr("requests.post", _fake_post)

    assert await upload_wall_photo(publisher, -42, b"jpeg-bytes") is None
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_upload_retries_after_405(monkeypatch):
    """Upload-сервер ВК отдаёт 405 при частых заливках — это не запрет.

    Отказ проходит после паузы, поэтому фото уходит со второй попытки, а не
    выпадает из сборника.
    """
    attempts = []

    def _post_405_then_ok(url, files=None, timeout=None):
        attempts.append(url)
        if len(attempts) == 1:
            return _FakeResponse(None, status_code=405)
        return _FakeResponse({"photo": "server-photo", "server": 1, "hash": "h"})

    publisher = _FakePublisher()
    monkeypatch.setattr("requests.post", _post_405_then_ok)

    slept = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        slept.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    att = await upload_wall_photo(publisher, -42, b"jpeg-bytes")

    assert att == "photo20002978_777"
    assert len(attempts) == 2
    # Пауза после 405 выдержана — иначе ретрай бьётся в тот же лимит.
    assert photo_processing.UPLOAD_405_COOLDOWN_SECONDS in slept


@pytest.mark.asyncio
async def test_upload_wall_photo_survives_failure(monkeypatch):
    """Фото не обязато быть: ошибка ВК не должна ронять сборник."""
    publisher = _FakePublisher(fail=True)
    monkeypatch.setattr("requests.post", _fake_post)

    assert await upload_wall_photo(publisher, -42, b"jpeg-bytes") is None


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if self._payload is None:
            raise ValueError("ответ не json")
        return self._payload


def _fake_post(url, files=None, timeout=None):
    return _FakeResponse({"photo": "server-photo", "server": 1, "hash": "h"})
