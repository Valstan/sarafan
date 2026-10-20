"""Официальные названия источников вместо ручных сокращений из базы (20.10).

Проверяем контракт: подпись «Источник: …» берёт имя, как сообщество называет
себя в VK; сбой VK не мешает публикации (остаётся имя из базы).
"""

from __future__ import annotations

import pytest

from modules import source_names


class FakeVK:
    def __init__(self, mapping: dict[int, str], *, fail: bool = False):
        self.mapping = mapping
        self.fail = fail
        self.calls: list[list[int]] = []

    def get_groups_by_ids(self, ids):
        self.calls.append(list(ids))
        if self.fail:
            raise RuntimeError("vk недоступен")
        return [{"id": gid, "name": self.mapping[gid]} for gid in ids if gid in self.mapping]


@pytest.fixture(autouse=True)
def _clean_cache():
    source_names.reset_cache()
    yield
    source_names.reset_cache()


def _post(owner_id: int) -> dict:
    return {"owner_id": owner_id, "id": 1, "text": "текст"}


def test_official_name_replaces_db_abbreviation():
    vk = FakeVK({192922358: "Верхнедеревенский сельский клуб"})
    names = source_names.enrich_group_names(
        vk, {"192922358": "В.-Деревенский СК"}, [_post(-192922358)]
    )

    assert names["192922358"] == "Верхнедеревенский сельский клуб"


def test_second_wave_uses_cache():
    vk = FakeVK({1: "Первый"})
    source_names.enrich_group_names(vk, {}, [_post(-1)])
    source_names.enrich_group_names(vk, {}, [_post(-1)])

    assert len(vk.calls) == 1, "второй вызов за тем же именем — лишний запрос к VK"


def test_vk_failure_keeps_db_names():
    vk = FakeVK({}, fail=True)
    names = source_names.enrich_group_names(vk, {"5": "Как в базе"}, [_post(-5)])

    assert names == {"5": "Как в базе"}


def test_unknown_group_keeps_db_name():
    """VK не знает сообщество — не выдумываем имя, оставляем прежнее."""
    vk = FakeVK({})
    names = source_names.enrich_group_names(vk, {"7": "Старое имя"}, [_post(-7)])

    assert names["7"] == "Старое имя"


def test_personal_pages_are_not_requested():
    """groups.getById знает только сообщества; личные страницы пропускаем."""
    vk = FakeVK({296115157: "Валера"})
    names = source_names.enrich_group_names(vk, {"296115157": "Валера Шиклин"}, [_post(296115157)])

    assert vk.calls == []
    assert names["296115157"] == "Валера Шиклин"


def test_empty_name_from_vk_is_ignored():
    vk = FakeVK({9: "   "})
    names = source_names.enrich_group_names(vk, {"9": "Имя из базы"}, [_post(-9)])

    assert names["9"] == "Имя из базы"


def test_no_posts_is_a_noop():
    vk = FakeVK({})
    assert source_names.enrich_group_names(vk, {"1": "А"}, []) == {"1": "А"}
    assert vk.calls == []
