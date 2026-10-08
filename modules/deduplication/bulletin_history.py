"""
Utilities for bulletin history deduplication.

- Region-wide LIP/hash aggregation across all themes
- LIP extraction from published bulletin texts in target region wall
"""

from __future__ import annotations

from typing import Any, Iterable, List, Set, Tuple

from utils.post_utils import lip_of_post
from utils.vk_wall_links import extract_wall_post_refs_from_text

GLOBAL_REGION_WORK_THEME = "__region_global__"
TARGET_GROUP_POSTS_SCAN_LIMIT = 100


def build_region_dedup_sets(work_tables: Iterable[Any]) -> Tuple[Set[str], Set[str]]:
    """
    Aggregate historical dedup sets for a region from all its work tables.
    """
    lips: Set[str] = set()
    hashes: Set[str] = set()
    for wt in work_tables:
        if not wt:
            continue
        wl = getattr(wt, "lip", None) or []
        wh = getattr(wt, "hash", None) or []
        for item in wl:
            if isinstance(item, str) and item:
                lips.add(item)
        for item in wh:
            if isinstance(item, str) and item:
                hashes.add(item)
    return lips, hashes


def extract_source_lips_from_target_group_posts(posts: Iterable[dict]) -> Set[str]:
    """
    Extract source post LIPs from bulletin texts published in target region group.
    """
    out: Set[str] = set()
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        text = (post.get("text") or "").strip()
        if not text:
            continue
        for owner_id, post_id in extract_wall_post_refs_from_text(text):
            out.add(lip_of_post(owner_id, post_id))
    return out


def partition_included_lips(results: Iterable[Any]) -> Tuple[List[str], List[str]]:
    """
    Split wave publish results into published vs failed lips.

    Items are ``(kind, bulletin, publish_result)`` where ``publish_result``
    is the ``publish_bulletin`` dict (``success`` bool). Only success lips
    may enter the dedup cursor: a failed bulletin was never posted, and
    recording its lips would mute the posts for later waves.
    """
    published: List[str] = []
    failed: List[str] = []
    for _, bulletin, pub in results or []:
        lips = list(getattr(bulletin, "posts_included", None) or [])
        if isinstance(pub, dict) and pub.get("success", False):
            published.extend(lips)
        else:
            failed.extend(lips)
    return published, failed


def append_unique_limited(existing: List[str], additions: Iterable[str], limit: int) -> List[str]:
    """
    Append values preserving insertion order and keep only the tail `limit`.
    """
    base = [x for x in (existing or []) if isinstance(x, str) and x]
    for item in additions or []:
        if not isinstance(item, str) or not item:
            continue
        base.append(item)
    # Unique keeping the latest occurrence.
    seen = set()
    dedup_reversed = []
    for item in reversed(base):
        if item in seen:
            continue
        seen.add(item)
        dedup_reversed.append(item)
    out = list(reversed(dedup_reversed))
    if len(out) > limit:
        out = out[-limit:]
    return out
