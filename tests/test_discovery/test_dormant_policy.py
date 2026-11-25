"""Tests for the tiered dormant policy (brain OK 2026-06-30).

Покрываем:

- ``classify_dormant_tier`` — тиры по возрасту last_post_at;
- auto-disable T1 в ``recheck_communities_for_region_async``: только при
  «2 подряд dormant» И возрасте >12 мес; empty_wall и T2/T3 не трогаем;
- ``dormant_disable_digest_async`` + ``_format_dormant_digest``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modules.discovery.health_check import (
    DORMANT_T1_DAYS,
    DORMANT_T2_DAYS,
    CommunityHealth,
    classify_dormant_tier,
)
from tasks import discovery_tasks as dt
from tests.test_discovery.test_recheck_tasks import _FakeSession, _make_community, _make_region

NOW = datetime(2026, 7, 5, 12, 0)


# ───────── classify_dormant_tier ─────────


def test_tier_empty_wall_when_no_posts():
    assert classify_dormant_tier(None, now=NOW) == "empty_wall"


def test_tier_t1_when_older_than_year():
    assert classify_dormant_tier(NOW - timedelta(days=DORMANT_T1_DAYS + 1), now=NOW) == "t1"


def test_tier_t2_between_six_and_twelve_months():
    assert classify_dormant_tier(NOW - timedelta(days=DORMANT_T2_DAYS + 1), now=NOW) == "t2"
    assert classify_dormant_tier(NOW - timedelta(days=DORMANT_T1_DAYS), now=NOW) == "t2"


def test_tier_t3_when_fresh_enough():
    assert classify_dormant_tier(NOW - timedelta(days=90), now=NOW) == "t3"
    assert classify_dormant_tier(NOW - timedelta(days=DORMANT_T2_DAYS), now=NOW) == "t3"


# ───────── auto-disable T1 в recheck ─────────


def _dormant_health(community_id, *, last_post_at):
    return CommunityHealth(
        community_id=community_id,
        vk_id=100,
        status="dormant",
        last_post_at=last_post_at,
        posts_sampled=5,
        suggested_category=None,
        error_code=None,
        reasoning=None,
    )


async def _run_recheck(communities, results_by_id):
    region = _make_region()
    session = _FakeSession(
        [
            {"kind": "scalar_one", "value": region},
            {"kind": "scalars_all", "value": communities},
        ]
    )

    async def fake_check(*, client, community, **_kwargs):
        return results_by_id[community.id]

    with (
        patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value="tok"),
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "VKClient", MagicMock()),
        patch.object(dt, "collect_community_health_pre_ai", side_effect=fake_check),
    ):
        return await dt.recheck_communities_for_region_async(1)


@pytest.mark.asyncio
async def test_t1_second_consecutive_dormant_disables():
    old = datetime.utcnow() - timedelta(days=DORMANT_T1_DAYS + 30)
    c = _make_community(id_=1)
    c.health_status = "dormant"  # первый strike уже был
    out = await _run_recheck([c], {1: _dormant_health(1, last_post_at=old)})
    assert out["disabled_t1"] == 1
    assert c.is_active is False
    assert c.disabled_reason == "dormant_t1_auto"
    assert c.disabled_at is not None


@pytest.mark.asyncio
async def test_t1_first_dormant_is_only_a_strike():
    old = datetime.utcnow() - timedelta(days=DORMANT_T1_DAYS + 30)
    c = _make_community(id_=1)  # health_status='active' — первый dormant
    out = await _run_recheck([c], {1: _dormant_health(1, last_post_at=old)})
    assert out["disabled_t1"] == 0
    assert c.is_active is True
    assert c.health_status == "dormant"


@pytest.mark.asyncio
async def test_t3_consecutive_dormant_is_kept():
    recent = datetime.utcnow() - timedelta(days=90)
    c = _make_community(id_=1)
    c.health_status = "dormant"
    out = await _run_recheck([c], {1: _dormant_health(1, last_post_at=recent)})
    assert out["disabled_t1"] == 0
    assert c.is_active is True


@pytest.mark.asyncio
async def test_empty_wall_consecutive_dormant_not_killed():
    c = _make_community(id_=1)
    c.health_status = "dormant"
    c.last_post_at = None
    out = await _run_recheck([c], {1: _dormant_health(1, last_post_at=None)})
    assert out["disabled_t1"] == 0
    assert c.is_active is True


# ───────── digest ─────────


def test_format_dormant_digest_lists_entries():
    items = [
        {
            "region": "mi",
            "name": "Клуб села Н",
            "vk_id": 123,
            "last_post_at": datetime(2025, 3, 1),
            "disabled_at": datetime(2026, 7, 1),
        }
    ]
    msg = dt._format_dormant_digest(items)
    assert "Dormant-политика" in msg
    assert "<b>mi</b>" in msg
    assert "vk.com/club123" in msg
    assert "2025-03-01" in msg
    assert "Обратимо" in msg


class _DigestSession(_FakeSession):
    """execute() возвращает .all() строк (Community, region_code)."""

    async def execute(self, _stmt):
        step = self._plan.pop(0)
        result = MagicMock()
        result.all.return_value = step["value"]
        return result


@pytest.mark.asyncio
async def test_digest_empty_window_is_silent():
    session = _DigestSession([{"kind": "all", "value": []}])
    with (
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "_send_telegram_html") as send_mock,
    ):
        out = await dt.dormant_disable_digest_async()
    assert out == {"success": True, "count": 0}
    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_digest_sends_telegram_when_items_present():
    c = _make_community(id_=1, vk_id=555)
    c.last_post_at = datetime(2025, 1, 1)
    c.disabled_at = datetime.utcnow()
    session = _DigestSession([{"kind": "all", "value": [(c, "mi")]}])
    with (
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "_send_telegram_html") as send_mock,
    ):
        out = await dt.dormant_disable_digest_async()
    assert out["count"] == 1
    assert out["items"][0]["vk_id"] == 555
    send_mock.assert_called_once()
