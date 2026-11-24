"""Tests for VKClient discovery methods (search_groups, get_groups_by_ids,
resolve_city) — used by the auto-registration module (big idea 2026-05-22)."""

from unittest.mock import MagicMock, patch

import vk_api

from modules.vk_monitor.vk_client import VKClient


def _make_client() -> VKClient:
    with patch.object(VKClient, "_init_session"):
        client = VKClient(token="test-token")
    # _init_session is patched out, fill in attributes manually.
    client.session = MagicMock()
    client.vk = MagicMock()
    return client


# ──────────────────────────────────────────────────────────────────
# search_groups
# ──────────────────────────────────────────────────────────────────


def test_search_groups_passes_query_country_count():
    client = _make_client()
    client.vk.groups.search.return_value = {"items": [{"id": 1, "name": "A"}]}
    items = client.search_groups("Малмыж новости", count=50)
    assert items == [{"id": 1, "name": "A"}]
    _, kwargs = client.vk.groups.search.call_args
    assert kwargs["q"] == "Малмыж новости"
    assert kwargs["count"] == 50
    assert kwargs["country_id"] == 1
    # city_id should not be set when omitted
    assert "city_id" not in kwargs


def test_search_groups_includes_city_id_when_provided():
    client = _make_client()
    client.vk.groups.search.return_value = {"items": []}
    client.search_groups("новости", city_id=314, count=10)
    _, kwargs = client.vk.groups.search.call_args
    assert kwargs["city_id"] == 314


def test_search_groups_caps_count_at_1000():
    client = _make_client()
    client.vk.groups.search.return_value = {"items": []}
    client.search_groups("test", count=10_000)
    _, kwargs = client.vk.groups.search.call_args
    assert kwargs["count"] == 1000


def test_search_groups_empty_query_short_circuits():
    client = _make_client()
    items = client.search_groups("   ")
    assert items == []
    client.vk.groups.search.assert_not_called()


def test_search_groups_handles_vk_api_error_returns_empty():
    client = _make_client()
    err = vk_api.exceptions.ApiError(
        vk=None,
        method="groups.search",
        values={},
        raw=None,
        error={"error_code": 100, "error_msg": "boom"},
    )
    client.vk.groups.search.side_effect = err
    assert client.search_groups("anything") == []


def test_search_groups_enforces_rate_limit():
    client = _make_client()
    client.vk.groups.search.return_value = {"items": []}
    with patch.object(client, "_enforce_rate_limit") as rl:
        client.search_groups("foo")
    rl.assert_called_once()


# ──────────────────────────────────────────────────────────────────
# get_groups_by_ids
# ──────────────────────────────────────────────────────────────────


def test_get_groups_by_ids_batches_in_chunks_of_500():
    client = _make_client()
    client.vk.groups.getById.return_value = []
    ids = list(range(1, 1201))  # 1200 ids → 3 chunks of 500/500/200
    client.get_groups_by_ids(ids)
    assert client.vk.groups.getById.call_count == 3
    chunk_sizes = [
        len(call.kwargs["group_ids"]) for call in client.vk.groups.getById.call_args_list
    ]
    assert chunk_sizes == [500, 500, 200]


def test_get_groups_by_ids_normalizes_negative_to_positive():
    client = _make_client()
    client.vk.groups.getById.return_value = [{"id": 5}]
    client.get_groups_by_ids([-5, -7])
    _, kwargs = client.vk.groups.getById.call_args
    assert kwargs["group_ids"] == [5, 7]


def test_get_groups_by_ids_passes_fields_when_given():
    client = _make_client()
    client.vk.groups.getById.return_value = []
    client.get_groups_by_ids([10], fields="description,members_count")
    _, kwargs = client.vk.groups.getById.call_args
    assert kwargs["fields"] == "description,members_count"


def test_get_groups_by_ids_omits_fields_when_not_given():
    client = _make_client()
    client.vk.groups.getById.return_value = []
    client.get_groups_by_ids([10])
    _, kwargs = client.vk.groups.getById.call_args
    assert "fields" not in kwargs


def test_get_groups_by_ids_one_failed_chunk_does_not_abort_others():
    client = _make_client()
    err = vk_api.exceptions.ApiError(
        vk=None,
        method="groups.getById",
        values={},
        raw=None,
        error={"error_code": 100, "error_msg": "boom"},
    )
    # 1st chunk raises, 2nd returns a valid result.
    client.vk.groups.getById.side_effect = [err, [{"id": 999}]]
    result = client.get_groups_by_ids(list(range(1, 1001)))  # 2 chunks of 500
    assert result == [{"id": 999}]


def test_get_groups_by_ids_empty_input_no_call():
    client = _make_client()
    assert client.get_groups_by_ids([]) == []
    client.vk.groups.getById.assert_not_called()


# ──────────────────────────────────────────────────────────────────
# get_groups_by_refs (screen_name / club<id> — для блока «Ссылки»)
# ──────────────────────────────────────────────────────────────────


def test_get_groups_by_refs_joins_string_refs():
    client = _make_client()
    client.vk.groups.getById.return_value = [{"id": 5, "screen_name": "a"}]
    out = client.get_groups_by_refs(["tuzha_sport", "club123"], fields="description")
    assert out == [{"id": 5, "screen_name": "a"}]
    _, kwargs = client.vk.groups.getById.call_args
    # refs передаются строкой через запятую (не abs(int) как в get_groups_by_ids)
    assert kwargs["group_ids"] == "tuzha_sport,club123"
    assert kwargs["fields"] == "description"


def test_get_groups_by_refs_skips_blank_and_empty_input():
    client = _make_client()
    assert client.get_groups_by_refs([]) == []
    assert client.get_groups_by_refs(["", "  ", None]) == []  # type: ignore[list-item]
    client.vk.groups.getById.assert_not_called()


def test_get_groups_by_refs_one_failed_chunk_does_not_abort_others():
    client = _make_client()
    err = vk_api.exceptions.ApiError(
        vk=None,
        method="groups.getById",
        values={},
        raw=None,
        error={"error_code": 100, "error_msg": "boom"},
    )
    client.vk.groups.getById.side_effect = [err, [{"id": 999}]]
    result = client.get_groups_by_refs([f"club{i}" for i in range(1000)])  # 2 chunks of 500
    assert result == [{"id": 999}]


# ──────────────────────────────────────────────────────────────────
# get_cities_by_ids (area-guard discovery: город → область)
# ──────────────────────────────────────────────────────────────────


def test_get_cities_by_ids_returns_area_per_city():
    client = _make_client()
    client.vk.database.getCities.return_value = {
        "items": [
            {"id": 314, "title": "Малмыж", "area": "Кировская область"},
            {"id": 1, "title": "Москва", "area": "Москва"},
        ]
    }
    out = client.get_cities_by_ids([314, 1])
    assert out[314]["area"] == "Кировская область"
    assert out[1]["area"] == "Москва"
    _, kwargs = client.vk.database.getCities.call_args
    assert kwargs["city_ids"] == "314,1"
    assert kwargs["country_id"] == 1


def test_get_cities_by_ids_skips_non_positive_and_dedups():
    client = _make_client()
    client.vk.database.getCities.return_value = {"items": []}
    client.get_cities_by_ids([0, -5, 314, 314])
    _, kwargs = client.vk.database.getCities.call_args
    assert kwargs["city_ids"] == "314"


def test_get_cities_by_ids_empty_input_no_call():
    client = _make_client()
    assert client.get_cities_by_ids([]) == {}
    client.vk.database.getCities.assert_not_called()


def test_get_cities_by_ids_handles_vk_api_error():
    client = _make_client()
    err = vk_api.exceptions.ApiError(
        vk=None,
        method="database.getCities",
        values={},
        raw=None,
        error={"error_code": 100, "error_msg": "boom"},
    )
    client.vk.database.getCities.side_effect = err
    assert client.get_cities_by_ids([314]) == {}


def test_get_cities_by_ids_enforces_rate_limit():
    client = _make_client()
    client.vk.database.getCities.return_value = {"items": []}
    with patch.object(client, "_enforce_rate_limit") as rl:
        client.get_cities_by_ids([314])
    rl.assert_called_once()


# ──────────────────────────────────────────────────────────────────
# resolve_city
# ──────────────────────────────────────────────────────────────────


def test_resolve_city_returns_items():
    client = _make_client()
    client.vk.database.getCities.return_value = {
        "items": [{"id": 314, "title": "Малмыж", "area": "Малмыжский р-н"}]
    }
    cities = client.resolve_city("Малмыж")
    assert cities == [{"id": 314, "title": "Малмыж", "area": "Малмыжский р-н"}]
    _, kwargs = client.vk.database.getCities.call_args
    assert kwargs["q"] == "Малмыж"
    assert kwargs["country_id"] == 1


def test_resolve_city_caps_count_at_100():
    client = _make_client()
    client.vk.database.getCities.return_value = {"items": []}
    client.resolve_city("X", count=500)
    _, kwargs = client.vk.database.getCities.call_args
    assert kwargs["count"] == 100


def test_resolve_city_empty_query_short_circuits():
    client = _make_client()
    assert client.resolve_city("") == []
    client.vk.database.getCities.assert_not_called()


def test_resolve_city_handles_vk_api_error():
    client = _make_client()
    err = vk_api.exceptions.ApiError(
        vk=None,
        method="database.getCities",
        values={},
        raw=None,
        error={"error_code": 100, "error_msg": "boom"},
    )
    client.vk.database.getCities.side_effect = err
    assert client.resolve_city("anything") == []


def test_resolve_city_enforces_rate_limit():
    client = _make_client()
    client.vk.database.getCities.return_value = {"items": []}
    with patch.object(client, "_enforce_rate_limit") as rl:
        client.resolve_city("Киров")
    rl.assert_called_once()
