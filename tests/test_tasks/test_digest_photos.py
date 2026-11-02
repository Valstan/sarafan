"""Фото в вечерней сводке: сбор вложений и отказоустойчивость (P214).

Главное правило теста: фото — **надстройка**. Ошибка скачивания, обработки или
загрузки не должна ронять рубрику: сборник выходит без картинки.
"""

from __future__ import annotations

from typing import List

import pytest

from modules.publisher.daily_digest import DigestPhoto
from tasks import digest_tasks


class _FakeRegion:
    code = "mi"
    vk_group_id = -4242


class _FakePublisher:
    def __init__(self, attachments: List[str]):
        self._attachments = attachments

    def _client_for_group(self, group_id: int):
        return object(), True


class _FakeDraft:
    def __init__(self, photos: List[DigestPhoto]):
        self.photos = photos


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch):
    monkeypatch.setenv("DAILY_DIGEST_PHOTOS", "1")
    monkeypatch.delenv("DAILY_DIGEST_ENABLED", raising=False)


def test_photos_switch_is_read_from_env(monkeypatch):
    assert digest_tasks._photos_enabled() is True
    monkeypatch.setenv("DAILY_DIGEST_PHOTOS", "0")
    assert digest_tasks._photos_enabled() is False


@pytest.mark.asyncio
async def test_photo_attachments_builds_one_per_photo(monkeypatch):
    processed = []

    def fake_download(url, **kw):
        return b"raw-jpeg"

    def fake_process(raw, watermark):
        processed.append(watermark)
        return b"processed-jpeg"

    async def fake_upload(client, group_id, image_bytes, **kw):
        return f"photo-42_{len(processed)}"

    monkeypatch.setattr("modules.publisher.photo_processing.download_photo", fake_download)
    monkeypatch.setattr("modules.publisher.photo_processing.process_photo", fake_process)
    monkeypatch.setattr("modules.publisher.photo_processing.upload_wall_photo", fake_upload)

    draft = _FakeDraft(
        [
            DigestPhoto(url="https://cdn/1.jpg", watermark="Клуб села"),
            DigestPhoto(url="https://cdn/2.jpg", watermark="Дом культуры"),
        ]
    )

    attachments = await digest_tasks._photo_attachments(_FakePublisher([]), _FakeRegion(), draft)

    assert attachments == ["photo-42_1", "photo-42_2"]
    assert processed == ["Клуб села", "Дом культуры"]


@pytest.mark.asyncio
async def test_photo_failure_leaves_digest_intact(monkeypatch):
    """Фото не скачалось — вложений нет, но исключения тоже нет."""
    monkeypatch.setattr("modules.publisher.photo_processing.download_photo", lambda url, **kw: None)

    draft = _FakeDraft([DigestPhoto(url="https://cdn/1.jpg", watermark="Клуб села")])

    assert await digest_tasks._photo_attachments(_FakePublisher([]), _FakeRegion(), draft) == []


@pytest.mark.asyncio
async def test_photos_switch_off_means_no_attachments(monkeypatch):
    monkeypatch.setenv("DAILY_DIGEST_PHOTOS", "0")
    monkeypatch.setattr(
        "modules.publisher.photo_processing.download_photo",
        lambda url, **kw: pytest.fail("при выключенных фото ничего качать нельзя"),
    )

    draft = _FakeDraft([DigestPhoto(url="https://cdn/1.jpg", watermark="Клуб села")])

    assert await digest_tasks._photo_attachments(_FakePublisher([]), _FakeRegion(), draft) == []


@pytest.mark.asyncio
async def test_no_photos_in_draft_means_no_work(monkeypatch):
    monkeypatch.setattr(
        "modules.publisher.photo_processing.download_photo",
        lambda url, **kw: pytest.fail("без фото в черновике качать нечего"),
    )

    assert (
        await digest_tasks._photo_attachments(_FakePublisher([]), _FakeRegion(), _FakeDraft([]))
        == []
    )
