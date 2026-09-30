"""Unit tests for tasks/discovery_tasks.py recheck-functions (вторая итерация
big idea — модуль авто-регистрации регионов и сообществ).

Покрываем:

- ``recheck_communities_for_region_async`` — обход одного региона: пишет
  health поля, пропускает is_active=False, корректно отчитывается.
- ``recheck_all_active_regions_async`` — обход всех регионов, агрегирует
  report, дёргает Telegram-alert.
- Pure helpers: ``_format_recheck_message``, ``_has_interesting_findings``,
  ``_dormant_days_for_region``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.models import Community, Region
from modules.discovery.health_check import CommunityHealth
from modules.discovery.vk_search import DiscoveredGroup
from tasks import discovery_tasks as dt

# ───────── helpers ─────────


class _FakeSession:
    """Async ctx-manager that hands out canned execute() results in order."""

    def __init__(self, plan):
        self._plan = list(plan)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        step = self._plan.pop(0)
        result = MagicMock()
        if step["kind"] == "scalar_one":
            result.scalar_one_or_none.return_value = step["value"]
        elif step["kind"] == "scalars_all":
            scalars = MagicMock()
            scalars.all.return_value = step["value"]
            result.scalars.return_value = scalars
        return result

    async def commit(self):
        self.committed = True


def _make_region(*, id_=1, code="mi", name="МАЛМЫЖ — ИНФО", config=None, is_active=True):
    r = Region(id=id_, code=code, name=name, is_active=is_active, config=config or {})
    return r


def _make_community(*, id_, vk_id=100, category="novost", is_active=True):
    c = Community(
        id=id_, region_id=1, vk_id=vk_id, name=f"C{id_}", category=category, is_active=is_active
    )
    c.health_status = "active"
    c.last_post_at = None
    c.suggested_category = None
    c.checked_at = None
    c.last_error_code = None
    return c


def _health(
    community_id,
    status,
    *,
    suggested=None,
    error_code=None,
    last_post_at=None,
    tokens_prompt=0,
    tokens_completion=0,
    tokens_known=True,
):
    return CommunityHealth(
        community_id=community_id,
        vk_id=100,
        status=status,
        last_post_at=last_post_at,
        posts_sampled=5,
        suggested_category=suggested,
        error_code=error_code,
        reasoning=None,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        tokens_known=tokens_known,
    )


# ───────── цена прогона в отчёте (P001) ─────────
#
# Recheck — самая крупная статья расхода в модуле discovery (замер 01.10.2026:
# 1786 вызовов за прогон), и вопрос «включать ли авто-подбор» упирается именно в
# цену. Если она живёт только в логе, вопрос висит до следующего grep'а по
# архиву логов — четыре месяца так и висел.


def test_tokens_of_health_sums_measured_calls():
    got = dt._tokens_of_health(
        [
            _health(1, "active", tokens_prompt=800, tokens_completion=50),
            _health(2, "active", tokens_prompt=900, tokens_completion=60),
        ]
    )
    assert got["tokens_prompt"] == 1700
    assert got["tokens_completion"] == 110
    assert got["tokens_measured"] is True
    assert got["tokens_unmeasured_calls"] == 0


def test_tokens_of_health_flags_unmeasured_call():
    """Стена с текстом, но без счётчиков от провайдера — ноль обманчив.

    Эвристика «ноль бывает только у мёртвых стен» здесь не годится: у этой
    стены вызов был, и оплачен, но посчитать его нечем.
    """
    got = dt._tokens_of_health(
        [
            _health(1, "active", tokens_prompt=800, tokens_completion=50),
            _health(2, "active", tokens_known=False),
        ]
    )
    assert got["tokens_prompt"] == 800
    assert got["tokens_measured"] is False
    assert got["tokens_unmeasured_calls"] == 1


def test_tokens_of_health_empty_is_measured_zero():
    got = dt._tokens_of_health([])
    assert got["tokens_prompt"] == 0
    assert got["tokens_completion"] == 0
    assert got["tokens_measured"] is True


# ───────── _dormant_days_for_region ─────────


def test_dormant_days_default_when_config_missing():
    r = _make_region(config={})
    assert dt._dormant_days_for_region(r) == dt.DEFAULT_DORMANT_DAYS


def test_dormant_days_default_when_config_invalid():
    r = _make_region(config={"dormant_days": "lots"})
    assert dt._dormant_days_for_region(r) == dt.DEFAULT_DORMANT_DAYS


def test_dormant_days_uses_region_override():
    r = _make_region(config={"dormant_days": 14})
    assert dt._dormant_days_for_region(r) == 14


def test_dormant_days_ignores_non_positive_override():
    r = _make_region(config={"dormant_days": 0})
    assert dt._dormant_days_for_region(r) == dt.DEFAULT_DORMANT_DAYS


# ───────── recheck_communities_for_region_async ─────────


@pytest.mark.asyncio
async def test_recheck_returns_failure_when_no_token():
    with patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value=None):
        out = await dt.recheck_communities_for_region_async(1)
    assert out["success"] is False
    assert "VK parse-token" in out["error"]


@pytest.mark.asyncio
async def test_recheck_returns_failure_when_region_missing():
    session = _FakeSession([{"kind": "scalar_one", "value": None}])
    with (
        patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value="tok"),
        patch.object(dt, "AsyncSessionLocal", return_value=session),
    ):
        out = await dt.recheck_communities_for_region_async(42)
    assert out["success"] is False
    assert "region 42 not found" in out["error"]


@pytest.mark.asyncio
async def test_recheck_zero_communities_returns_empty_report():
    region = _make_region()
    session = _FakeSession(
        [
            {"kind": "scalar_one", "value": region},
            {"kind": "scalars_all", "value": []},
        ]
    )
    with (
        patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value="tok"),
        patch.object(dt, "AsyncSessionLocal", return_value=session),
    ):
        out = await dt.recheck_communities_for_region_async(1)
    assert out == {
        "success": True,
        "region": "mi",
        "total": 0,
        "active": 0,
        "dormant": 0,
        "dead": 0,
        "changed_category": 0,
        "errors": 0,
        "disabled_t1": 0,
    }


@pytest.mark.asyncio
async def test_recheck_writes_health_fields_and_aggregates_counts():
    region = _make_region()
    c_active = _make_community(id_=1, vk_id=10)
    c_dormant = _make_community(id_=2, vk_id=20)
    c_dead = _make_community(id_=3, vk_id=30)
    c_changed = _make_community(id_=4, vk_id=40)
    communities = [c_active, c_dormant, c_dead, c_changed]
    session = _FakeSession(
        [
            {"kind": "scalar_one", "value": region},
            {"kind": "scalars_all", "value": communities},
        ]
    )
    fixed_last_post = datetime(2026, 5, 20, 12, 0)
    results = {
        1: _health(1, "active", last_post_at=fixed_last_post),
        2: _health(2, "dormant"),
        3: _health(3, "dead", error_code=15),
        4: _health(4, "changed_category", suggested="reklama"),
    }

    async def fake_check(*, client, community, **_kwargs):
        return results[community.id]

    with (
        patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value="tok"),
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "VKClient", MagicMock()),
        patch.object(dt, "check_community_health", side_effect=fake_check),
    ):
        out = await dt.recheck_communities_for_region_async(1)

    assert out["success"] is True
    assert out["total"] == 4
    assert out["active"] == 1
    assert out["dormant"] == 1
    assert out["dead"] == 1
    assert out["changed_category"] == 1
    # dead не считается transient error'ом
    assert out["errors"] == 0
    # in-place поля проставлены
    assert c_active.health_status == "active"
    assert c_active.last_post_at == fixed_last_post
    assert c_active.checked_at is not None
    assert c_dead.health_status == "dead"
    assert c_changed.health_status == "changed_category"
    assert c_changed.suggested_category == "reklama"
    # active не должен иметь suggested_category
    assert c_active.suggested_category is None
    # error_code recheck'а персистится (миграция 058): dead хранит код, чистые — NULL
    assert c_dead.last_error_code == 15
    assert c_active.last_error_code is None
    assert session.committed is True


@pytest.mark.asyncio
async def test_recheck_counts_transient_errors():
    region = _make_region()
    c = _make_community(id_=1)
    session = _FakeSession(
        [
            {"kind": "scalar_one", "value": region},
            {"kind": "scalars_all", "value": [c]},
        ]
    )
    # Transient: status остался прежним 'active', но error_code заполнен (e.g. 6).
    transient = CommunityHealth(
        community_id=1,
        vk_id=100,
        status="active",
        last_post_at=None,
        posts_sampled=0,
        suggested_category=None,
        error_code=6,
        reasoning="rate limit",
    )

    async def fake_check(**_kwargs):
        return transient

    with (
        patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value="tok"),
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "VKClient", MagicMock()),
        patch.object(dt, "check_community_health", side_effect=fake_check),
    ):
        out = await dt.recheck_communities_for_region_async(1)
    assert out["errors"] == 1
    assert out["active"] == 1
    # transient-код тоже персистится — виден до следующего чистого recheck'а
    assert c.last_error_code == 6


@pytest.mark.asyncio
async def test_recheck_clean_run_clears_stale_error_code():
    region = _make_region()
    c = _make_community(id_=1)
    c.last_error_code = 15  # прошлый recheck видел ошибку
    session = _FakeSession(
        [
            {"kind": "scalar_one", "value": region},
            {"kind": "scalars_all", "value": [c]},
        ]
    )

    async def fake_check(**_kwargs):
        return _health(1, "active")  # error_code=None — чистый прогон

    with (
        patch.object(dt, "_pick_parse_token", new_callable=AsyncMock, return_value="tok"),
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "VKClient", MagicMock()),
        patch.object(dt, "check_community_health", side_effect=fake_check),
    ):
        await dt.recheck_communities_for_region_async(1)
    assert c.last_error_code is None


# ───────── recheck_all_active_regions_async ─────────


@pytest.mark.asyncio
async def test_recheck_all_aggregates_per_region_reports():
    r1 = _make_region(id_=1, code="mi")
    r2 = _make_region(id_=2, code="vp")
    outer_session = _FakeSession([{"kind": "scalars_all", "value": [r1, r2]}])
    per_region_reports = [
        {
            "success": True,
            "region": "mi",
            "total": 3,
            "active": 2,
            "dormant": 1,
            "dead": 0,
            "changed_category": 0,
            "errors": 0,
        },
        {
            "success": True,
            "region": "vp",
            "total": 5,
            "active": 4,
            "dormant": 0,
            "dead": 1,
            "changed_category": 0,
            "errors": 0,
        },
    ]
    plan = list(per_region_reports)

    async def fake_recheck_region(region_id, **_kwargs):
        return plan.pop(0)

    with (
        patch.object(dt, "AsyncSessionLocal", return_value=outer_session),
        patch.object(
            dt, "recheck_communities_for_region_async", AsyncMock(side_effect=fake_recheck_region)
        ),
        patch.object(dt, "_maybe_send_recheck_telegram_alert") as alert_mock,
    ):
        out = await dt.recheck_all_active_regions_async()

    assert out["success"] is True
    assert out["total_regions"] == 2
    assert [r["region"] for r in out["regions"]] == ["mi", "vp"]
    alert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_recheck_all_empty_regions_short_circuits():
    outer_session = _FakeSession([{"kind": "scalars_all", "value": []}])
    with patch.object(dt, "AsyncSessionLocal", return_value=outer_session):
        out = await dt.recheck_all_active_regions_async()
    assert out == {"success": True, "regions": [], "total_regions": 0}


# ───────── _has_interesting_findings ─────────


def test_has_interesting_findings_true_on_dead():
    reports = [{"success": True, "region": "mi", "dead": 1, "dormant": 0, "changed_category": 0}]
    assert dt._has_interesting_findings(reports) is True


def test_has_interesting_findings_false_on_all_active():
    reports = [{"success": True, "region": "mi", "dead": 0, "dormant": 0, "changed_category": 0}]
    assert dt._has_interesting_findings(reports) is False


def test_has_interesting_findings_ignores_failed_reports():
    reports = [{"success": False, "error": "boom"}]
    assert dt._has_interesting_findings(reports) is False


# ───────── _format_recheck_message ─────────


def test_format_message_includes_per_region_breakdown():
    reports = [
        {
            "success": True,
            "region": "mi",
            "total": 5,
            "active": 3,
            "dormant": 1,
            "dead": 1,
            "changed_category": 0,
            "errors": 0,
        },
        {
            "success": True,
            "region": "vp",
            "total": 4,
            "active": 2,
            "dormant": 0,
            "dead": 0,
            "changed_category": 2,
            "errors": 0,
        },
    ]
    msg = dt._format_recheck_message(reports)
    assert "Discovery recheck" in msg
    assert "<b>mi</b>" in msg
    assert "<b>vp</b>" in msg
    assert "dead: 1" in msg
    assert "changed_category: 2" in msg


def test_format_message_skips_region_with_no_findings():
    reports = [
        {
            "success": True,
            "region": "mi",
            "total": 5,
            "active": 5,
            "dormant": 0,
            "dead": 0,
            "changed_category": 0,
            "errors": 0,
        },
        {
            "success": True,
            "region": "vp",
            "total": 1,
            "active": 0,
            "dormant": 1,
            "dead": 0,
            "changed_category": 0,
            "errors": 0,
        },
    ]
    msg = dt._format_recheck_message(reports)
    # mi с нулевыми non-active в детальной разбивке не должен попасть как bullet
    assert msg.count("<b>mi</b>") == 0
    assert "<b>vp</b>" in msg


def test_format_message_shows_failed_regions():
    reports = [{"success": False, "region": "mi", "error": "no token"}]
    msg = dt._format_recheck_message(reports)
    assert "ошибка" in msg
    assert "no token" in msg


# ───────── _ai_categorize_all (wall.get параллельный fetch) ─────────


@pytest.mark.asyncio
async def test_ai_categorize_fetches_wall_posts_when_client_passed():
    """С client → _ai_categorize_all сам тянет wall.get и кладёт в recent_posts.

    Это переехало из `discover_for_region` (PR #32-fix), потому что sync
    sequential цикл там висел на 100+ группах из-за rate-limit Lock.
    """
    groups = [
        DiscoveredGroup(vk_id=10, name="A"),
        DiscoveredGroup(vk_id=20, name="B"),
    ]

    def wall_side_effect(*, owner_id, count, offset=0):
        return [{"text": f"post from {abs(owner_id)}"}, {"text": ""}]

    client = MagicMock()
    client.get_wall_posts.side_effect = wall_side_effect

    ai_mock = AsyncMock(
        return_value={"success": True, "category": "novost", "confidence": 80, "reasoning": "ok"}
    )
    with patch.object(dt, "categorize_candidate", ai_mock):
        result = await dt._ai_categorize_all(
            groups, "Test", client=client, posts_per_group=5, max_concurrent=2
        )

    assert set(result.keys()) == {10, 20}
    assert groups[0].recent_posts == ["post from 10"]
    assert groups[1].recent_posts == ["post from 20"]
    # owner_id отрицательный для VK групп
    assert all(c.kwargs["owner_id"] < 0 for c in client.get_wall_posts.call_args_list)
    assert all(c.kwargs["count"] == 5 for c in client.get_wall_posts.call_args_list)


@pytest.mark.asyncio
async def test_ai_categorize_skips_wall_get_when_no_client():
    """Без client (старый путь / unit-тесты) wall.get не зовётся."""
    groups = [DiscoveredGroup(vk_id=10, name="A")]
    ai_mock = AsyncMock(
        return_value={"success": True, "category": "novost", "confidence": 80, "reasoning": "ok"}
    )
    with patch.object(dt, "categorize_candidate", ai_mock):
        result = await dt._ai_categorize_all(groups, "Test")

    assert 10 in result
    assert ai_mock.await_count == 1


@pytest.mark.asyncio
async def test_ai_categorize_wall_get_failure_does_not_block():
    """Если wall.get падает на одну группу — другие всё равно отрабатывают."""
    groups = [
        DiscoveredGroup(vk_id=1, name="A"),
        DiscoveredGroup(vk_id=2, name="B"),
    ]

    def wall_side_effect(*, owner_id, count, offset=0):
        if owner_id == -1:
            raise RuntimeError("VK down")
        return [{"text": "ok"}]

    client = MagicMock()
    client.get_wall_posts.side_effect = wall_side_effect
    ai_mock = AsyncMock(
        return_value={"success": True, "category": "novost", "confidence": 80, "reasoning": "ok"}
    )
    with patch.object(dt, "categorize_candidate", ai_mock):
        result = await dt._ai_categorize_all(groups, "Test", client=client)

    assert groups[0].recent_posts == []
    assert groups[1].recent_posts == ["ok"]
    assert result[1]["success"] is True
    assert result[2]["success"] is True
