"""Unit tests for bulletin history dedup helpers."""

from types import SimpleNamespace

from modules.deduplication.bulletin_history import (
    append_unique_limited,
    build_region_dedup_sets,
    extract_source_lips_from_target_group_posts,
    partition_included_lips,
)


def test_build_region_dedup_sets_merges_all_themes():
    wt1 = SimpleNamespace(lip=["-1_1", "-1_2"], hash=["a", "b"])
    wt2 = SimpleNamespace(lip=["-2_3"], hash=["c"])
    lips, hashes = build_region_dedup_sets([wt1, wt2])
    assert lips == {"-1_1", "-1_2", "-2_3"}
    assert hashes == {"a", "b", "c"}


def test_extract_source_lips_from_target_group_posts_parses_wall_links():
    posts = [
        {"text": "✍ новость\n[https://vk.com/wall-10_11|Источник]"},
        {"text": "ещё ссылка wall-20_21 и дубликат wall-20_21"},
    ]
    lips = extract_source_lips_from_target_group_posts(posts)
    assert "10_11" in lips
    assert "20_21" in lips
    assert len(lips) == 2


def test_append_unique_limited_keeps_latest_unique_tail():
    existing = ["a", "b", "c"]
    out = append_unique_limited(existing, ["b", "d", "e"], limit=4)
    assert out == ["c", "b", "d", "e"]


def _bulletin(lips):
    return SimpleNamespace(posts_included=list(lips))


def test_partition_included_lips_all_success():
    results = [
        ("regular", _bulletin(["1_1", "1_2"]), {"success": True}),
        ("headliner", _bulletin(["2_3"]), {"success": True, "post_id": 9}),
    ]
    published, failed = partition_included_lips(results)
    assert published == ["1_1", "1_2", "2_3"]
    assert failed == []


def test_partition_included_lips_failed_go_to_failed():
    results = [
        ("regular", _bulletin(["1_1"]), {"success": True}),
        ("mourning", _bulletin(["9_9", "9_10"]), {"success": False, "error": "flood"}),
    ]
    published, failed = partition_included_lips(results)
    assert published == ["1_1"]
    assert failed == ["9_9", "9_10"]


def test_partition_included_lips_missing_result_is_failed():
    results = [
        ("regular", _bulletin(["1_1"]), None),
        ("regular", _bulletin(["2_2"]), {}),
        ("regular", SimpleNamespace(), {"success": True}),
    ]
    published, failed = partition_included_lips(results)
    assert published == []
    assert failed == ["1_1", "2_2"]
