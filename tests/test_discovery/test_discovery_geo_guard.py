"""Гео-guard в discovery: чужие области не попадают в очередь модерации.

Контекст — P235 (аудит discovery от 23.11.2026): 329 кандидатов в очереди
модерации оказались группами из чужих областей, потому что

1. поиск по нп идёт **без** ``city_id`` — ``groups.search(q="Москва")``
   возвращает московские паблики;
2. фильтр релевантности их пропускал — стем топонима найден в имени;
3. у 62 регионов из 81 список нп пуст, и там фильтр был выключен целиком;
4. ИИ в тот период молчал, и кандидаты вставлялись без категории.

Здесь закрыты звенья 1–3 (код) и 4 (очередь модерации не принимает кандидатов
без вердикта ИИ). Звено «почистить списки нп» — это данные (P124/P200).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from modules.discovery.vk_search import discover_for_region
from tasks.discovery_tasks import _upsert_candidates

# ───────── helpers ─────────


def _make_client(search_results, enrich_results=None, areas=None):
    """Клиент с подменённым ``database.getCities``.

    ``areas`` — словарь ``{city_id: {"title": ..., "area": ...}}``; именно его
    отдаёт ``VKClient.get_cities_by_ids``.
    """
    client = MagicMock()
    client.search_groups.return_value = list(search_results)
    client.get_groups_by_ids.return_value = list(enrich_results or [])
    lookup = areas or {}

    def _cities_by_ids(city_ids):
        return {int(cid): lookup[int(cid)] for cid in (city_ids or []) if int(cid) in lookup}

    client.get_cities_by_ids.side_effect = _cities_by_ids
    return client


_OUR_CITY = 314  # Малмыж
_OUR_AREA = "Кировская область"


def _client_with_area_guard(search_results, enrich_results, areas):
    client = _make_client(search_results, enrich_results, areas)
    # Область региона резолвится по городу области — он же в areas.
    return client


# ───────── area guard ─────────


def test_area_guard_drops_group_from_foreign_area():
    """Группа из чужой области в очередь не попадает (Москва вместо
    Кировской области) — даже если её название прошло фильтр по нп."""
    client = _client_with_area_guard(
        [
            {"id": 1, "name": "Москва — районные новости"},
            {"id": 2, "name": "Верхнешижемье — новости"},
        ],
        [
            {"id": 1, "name": "Москва — районные новости", "city": {"id": 1, "title": "Москва"}},
            {"id": 2, "name": "Верхнешижемье — новости", "city": {"id": 314, "title": "Малмыж"}},
        ],
        {
            _OUR_CITY: {"title": "Малмыж", "area": _OUR_AREA},
            1: {"title": "Москва", "area": "Москва"},
        },
    )

    stats: dict = {}
    groups = discover_for_region(
        client=client,
        center_city="Верхнешижемье",
        localities=["Верхнешижемье"],
        keywords=[],
        own_area_city_id=_OUR_CITY,
        stats=stats,
    )

    assert [g.vk_id for g in groups] == [2]
    assert stats["dropped_foreign_area"] == 1


def test_area_guard_keeps_group_without_city():
    """Город неизвестен — «не знаю» ≠ «чужая»: группа остаётся."""
    client = _client_with_area_guard(
        [{"id": 1, "name": "Тужа — новости"}],
        [{"id": 1, "name": "Тужа — новости"}],  # без city
        {_OUR_CITY: {"title": "Малмыж", "area": _OUR_AREA}},
    )

    stats: dict = {}
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
        own_area_city_id=_OUR_CITY,
        stats=stats,
    )

    assert [g.vk_id for g in groups] == [1]
    assert stats.get("dropped_foreign_area", 0) == 0


def test_area_guard_off_when_own_area_unknown():
    """Область региона не определилась — guard выключен, и это **видно**
    в счётчиках (а не молча)."""
    client = _client_with_area_guard(
        [{"id": 1, "name": "Тужа — новости"}],
        [{"id": 1, "name": "Тужа — новости", "city": {"id": 1, "title": "Москва"}}],
        {1: {"title": "Москва", "area": "Москва"}},
    )

    stats: dict = {}
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=["Тужа"],
        keywords=[],
        own_area_city_id=None,
        stats=stats,
    )

    assert [g.vk_id for g in groups] == [1]
    assert stats["area_guard_off"] == 1
    assert "dropped_foreign_area" not in stats


def test_area_guard_reads_flat_city_id_shape():
    """VK отдаёт и плоский ``city_id``/``city_title`` — guard понимает обе формы."""
    client = _client_with_area_guard(
        [{"id": 1, "name": "Москва — районные новости"}],
        [{"id": 1, "name": "Москва — районные новости", "city_id": 1, "city_title": "Москва"}],
        {
            _OUR_CITY: {"title": "Малмыж", "area": _OUR_AREA},
            1: {"title": "Москва", "area": "Москва"},
        },
    )

    stats: dict = {}
    groups = discover_for_region(
        client=client,
        center_city="Верхнешижемье",
        localities=["Верхнешижемье"],
        keywords=[],
        own_area_city_id=_OUR_CITY,
        stats=stats,
    )

    assert groups == []
    assert stats["dropped_foreign_area"] == 1


# ───────── fail-closed relevance filter ─────────


def test_relevance_filter_is_fail_closed_without_localities():
    """Пустой список нп → фильтр требует центральный стем (а не выключен)."""
    client = _make_client(
        [
            {"id": 1, "name": "Подслушано Тужа"},
            {"id": 2, "name": "Море Парк Киров"},
        ],
        [],
        {},
    )

    stats: dict = {}
    groups = discover_for_region(
        client=client,
        center_city="Тужа",
        localities=[],
        keywords=[],
        stats=stats,
    )

    assert [g.vk_id for g in groups] == [1]
    assert stats["dropped_irrelevant"] == 1
    assert stats["found_total"] == 2


# ───────── ИИ молчит → в очередь не идёт ─────────


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    """Минимальная сессия для ``_upsert_candidates``."""

    def __init__(self, existing=None):
        self._existing = list(existing or [])
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        return _FakeResult(self._existing)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True


def _group(vk_id: int, name: str):
    from modules.discovery.vk_search import DiscoveredGroup

    return DiscoveredGroup(vk_id=vk_id, name=name, discovered_via="loc:Тужа")


async def test_candidate_without_ai_verdict_does_not_enter_moderation_queue():
    """ИИ не ответил → кандидат не вставляется: в очереди модерации не должно
    быть строк, которые модель не видела (P235, звено 4)."""
    session = _FakeSession()
    groups = [_group(10, "Тужа — новости")]

    counts = await _upsert_candidates(
        session,
        region_id=1,
        groups=groups,
        ai_by_vk_id={10: {"success": False, "error": "engine timeout"}},
    )

    assert session.added == []
    assert counts["skipped_ai_failed"] == 1
    assert counts["inserted"] == 0


async def test_candidate_with_ai_verdict_enters_moderation_queue():
    session = _FakeSession()
    groups = [_group(10, "Тужа — новости")]

    counts = await _upsert_candidates(
        session,
        region_id=1,
        groups=groups,
        ai_by_vk_id={10: {"success": True, "category": "novost", "confidence": 80}},
    )

    assert len(session.added) == 1
    assert session.added[0].ai_category == "novost"
    assert session.added[0].status == "pending"
    assert counts["skipped_ai_failed"] == 0
