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
    # Окно троттлинга — общий счётчик процесса: без сброса пауза из одного
    # теста утекает в следующий.
    monkeypatch.setattr(photo_processing, "_throttle_until", 0.0)


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


def _post_sequence(responses):
    """Ответы ``requests.post`` по очереди; последний повторяется."""
    calls = []

    def _post(url, files=None, timeout=None):
        calls.append(url)
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    return _post, calls


@pytest.mark.asyncio
async def test_upload_retries_after_empty_photo(monkeypatch):
    """ВК троттлит: файл залит, но ``photo`` пустой — это отказ, а не успех.

    Замер 09.11 (живая проба): именно этот отказ уводил фото молча — до
    починки переповторялся только 405, а за вечер потерялось 72 фото из 100.
    """
    _post, calls = _post_sequence(
        [
            _FakeResponse({"server": 1, "photo": "", "hash": "h"}),
            _FakeResponse({"server": 2, "photo": "server-photo", "hash": "h2"}),
        ]
    )
    monkeypatch.setattr("requests.post", _post)

    publisher = _FakePublisher()

    att = await upload_wall_photo(publisher, -42, b"jpeg-bytes")

    assert att == "photo20002978_777"
    assert len(calls) == 2, "пустой photo должен переповторяться, а не ронять фото"


@pytest.mark.asyncio
async def test_upload_retries_after_5xx(monkeypatch):
    """ВК отдал 500 не-json'ом — это троттлинг, пауза его лечит."""
    _post, calls = _post_sequence(
        [
            _FakeResponse(None, status_code=500),
            _FakeResponse({"server": 2, "photo": "server-photo", "hash": "h2"}),
        ]
    )
    monkeypatch.setattr("requests.post", _post)

    publisher = _FakePublisher()

    att = await upload_wall_photo(publisher, -42, b"jpeg-bytes")

    assert att == "photo20002978_777"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_upload_rejects_non_throttle_error_without_retry(monkeypatch):
    """Отказ, который паузой не лечится, ретраить бессмысленно."""
    _post, calls = _post_sequence([_FakeResponse(None, status_code=400)])
    monkeypatch.setattr("requests.post", _post)

    publisher = _FakePublisher()

    assert await upload_wall_photo(publisher, -42, b"jpeg-bytes") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_upload_gives_up_after_max_attempts(monkeypatch):
    """Троттлинг не отпустил — фото пропущено, но сборник не упал."""
    _post, calls = _post_sequence([_FakeResponse(None, status_code=405)])
    monkeypatch.setattr("requests.post", _post)

    publisher = _FakePublisher()

    assert await upload_wall_photo(publisher, -42, b"jpeg-bytes") is None
    assert len(calls) == photo_processing.UPLOAD_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_throttle_closes_window_for_next_upload(monkeypatch):
    """Открытое окно троттлинга общее: следующая загрузка ждёт, а не ломится.

    Без этого каждое фото платило бы свою паузу, а не одна пауза на процесс.
    """
    _post, _calls = _post_sequence([_FakeResponse(None, status_code=405)])
    monkeypatch.setattr("requests.post", _post)
    monkeypatch.setattr(photo_processing, "UPLOAD_405_COOLDOWN_SECONDS", 45.0)

    slept = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        slept.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)

    await upload_wall_photo(_FakePublisher(), -42, b"jpeg-bytes")

    assert 45.0 in slept, "окно троттлинга должно быть выдержано"
    assert (
        photo_processing._throttle_until > 0.0
    ), "окно должно остаться закрытым для следующих загрузок"


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


# --- Режим троттлинга (P238) ------------------------------------------------
#
# Замер 23.11 (P234): окно сводки 64 мин, из них **49 мин (77 %) — сон** на
# троттлинге загрузки фото: 42 окна × 70 с. Окно у ВК закрывалось ~46 мин из
# 64, и пауза 70 с его не открывала. Дозор 25.11 то же на другом окне: 183
# паузы на 76 регионов ≈ 2,8 мин ожидания на регион.
#
# Поэтому режим троттлинга — рычаг, а не украшение: по умолчанию поведение
# прежнее («wait»), а «skip» отдаёт слот обратно ценой фото в сборнике.
# Судьба медиа — решение владельца (заказ 24.11 «фото оставить»).


def _collect_sleeps(monkeypatch):
    """Подменить ``asyncio.sleep`` сбором задержек вместо реального сна."""
    slept = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        slept.append(delay)
        return await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    return slept


def test_throttle_mode_defaults_to_wait(monkeypatch):
    """По умолчанию поведение прежнее: ждать и повторять.

    Рычаг не должен включаться сам: судьба фото в сборнике — решение владельца.
    """
    from config.runtime import get_digest_photo_throttle_mode

    monkeypatch.delenv("DAILY_DIGEST_PHOTO_THROTTLE", raising=False)
    assert get_digest_photo_throttle_mode() == "wait"


def test_throttle_mode_follows_env(monkeypatch):
    """Env переключает режим; неизвестное значение — откат к прежнему."""
    from config.runtime import get_digest_photo_throttle_mode

    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", "skip")
    assert get_digest_photo_throttle_mode() == "skip"

    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", " SKIP ")
    assert get_digest_photo_throttle_mode() == "skip"

    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", "какая-то невнятица")
    assert get_digest_photo_throttle_mode() == "wait"


@pytest.mark.asyncio
async def test_wait_mode_keeps_three_attempts(monkeypatch):
    """Прежний режим: три попытки и две паузы — как было до рычага."""
    monkeypatch.delenv("DAILY_DIGEST_PHOTO_THROTTLE", raising=False)
    _post, calls = _post_sequence([_FakeResponse(None, status_code=405)])
    monkeypatch.setattr("requests.post", _post)
    slept = _collect_sleeps(monkeypatch)

    assert await upload_wall_photo(_FakePublisher(), -42, b"jpeg-bytes") is None
    assert len(calls) == photo_processing.UPLOAD_MAX_ATTEMPTS
    assert len(slept) == photo_processing.UPLOAD_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_once_mode_waits_once_and_gives_up(monkeypatch):
    """Режим ``once``: одна пауза вместо двух — сон короче, фото теряется реже.

    Замер 23.11: почти все фото проходили по второй-третьей попытке, то есть
    платили паузой за право добыть картинку. Здесь цена обратная: фото,
    прошедшие только с третьей попытки, теряются.
    """
    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", "once")
    _post, calls = _post_sequence([_FakeResponse(None, status_code=405)])
    monkeypatch.setattr("requests.post", _post)
    slept = _collect_sleeps(monkeypatch)

    assert await upload_wall_photo(_FakePublisher(), -42, b"jpeg-bytes") is None
    assert len(calls) == 2, "режим once — две попытки"
    assert len(slept) == 1, "режим once — одна пауза"


@pytest.mark.asyncio
async def test_skip_mode_gives_up_at_once_without_waiting(monkeypatch):
    """Режим ``skip``: отказ — фото пропущено, слот не спит.

    Цена режима — фото в сборнике; выигрыш — слот, который освобождается сразу
    (≈2,8 мин ожидания на регион по дозору 25.11).
    """
    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", "skip")
    _post, calls = _post_sequence([_FakeResponse(None, status_code=405)])
    monkeypatch.setattr("requests.post", _post)
    slept = _collect_sleeps(monkeypatch)

    assert await upload_wall_photo(_FakePublisher(), -42, b"jpeg-bytes") is None
    assert len(calls) == 1, "режим skip — одна попытка"
    assert not slept, "режим skip — без ожидания"


@pytest.mark.asyncio
async def test_skip_mode_does_not_start_upload_while_throttle_window_open(monkeypatch):
    """Пока окно троттлинга закрыто, загрузка даже не начинается.

    Троттлинг общий на процесс: ждать (отдать слот) или ломиться (получить
    тот же отказ) — оба варианта хуже, чем пропустить фото сразу.
    """
    import time

    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", "skip")
    monkeypatch.setattr(photo_processing, "_throttle_until", time.monotonic() + 60.0)
    _post, calls = _post_sequence([_FakeResponse({"photo": "p", "hash": "h"})])
    monkeypatch.setattr("requests.post", _post)
    slept = _collect_sleeps(monkeypatch)

    assert await upload_wall_photo(_FakePublisher(), -42, b"jpeg-bytes") is None
    assert not calls, "в закрытое окно ломиться нельзя — ВК отобьёт тем же"
    assert not slept, "ждать в режиме skip нельзя — это отданный слот"


@pytest.mark.asyncio
async def test_skip_mode_still_uploads_when_window_is_open(monkeypatch):
    """Закрытое окно — только после отказа; обычная ночь фото не теряет.

    Режим ``skip`` отличается от ``DAILY_DIGEST_PHOTOS=0`` именно этим: фото
    остаются в те ночи, когда ВК не троттлит.
    """
    monkeypatch.setenv("DAILY_DIGEST_PHOTO_THROTTLE", "skip")
    # Окно троттлинга открыто (обычная ночь) — явно, а не «авось сброшено».
    monkeypatch.setattr(photo_processing, "_throttle_until", 0.0)
    _post, calls = _post_sequence([_FakeResponse({"photo": "server-photo", "hash": "h"})])
    monkeypatch.setattr("requests.post", _post)

    att = await upload_wall_photo(_FakePublisher(), -42, b"jpeg-bytes")

    assert att == "photo20002978_777"
    assert len(calls) == 1
