"""Tests for community-token → publish-token fallback in VKPublisher._call_wall_post.

Mirrors the suggested/comments fallback: when wall.post or wall.repost via a
community client fails with VK code 15 or 27, the publisher should retry via
its own publish-token client. Other errors propagate.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.publisher.vk_publisher_extended import VKPublisher


def _client_with_method_only(method_return):
    """Make a client that has `method` but NOT `api_call`.

    The publisher checks `hasattr(client, 'api_call')` first; on a bare
    MagicMock both attributes exist, so we use spec to constrain available
    attributes to just `method`.
    """
    client = MagicMock(spec=["method"])
    client.method.return_value = method_return
    return client


def _make_publisher(publish_client):
    """Build a VKPublisher bypassing __init__ network."""
    publisher = VKPublisher.__new__(VKPublisher)
    publisher.vk_client = publish_client
    publisher.test_polygon_mode = False
    publisher.test_polygon_group_id = -137760500
    publisher._last_post_time = {}
    publisher._community_tokens = {}
    publisher._community_clients = {}
    return publisher


def _vk_error_response(code: int, msg: str = "Fake"):
    return {"error": {"error_code": code, "error_msg": msg}}


@pytest.mark.asyncio
async def test_repost_skips_community_token_entirely():
    """wall.repost is in _USER_TOKEN_ONLY_METHODS: it must NEVER try the
    community client even if one is provided (VK fundamentally rejects
    group-token wall.repost). Saves a guaranteed-failure round trip."""
    publish_client = _client_with_method_only({"response": {"success": 1, "post_id": 999}})
    community_client = _client_with_method_only({"response": {"success": 1, "post_id": 111}})

    publisher = _make_publisher(publish_client)
    response, via = await publisher._call_wall_post(
        params={"object": "wall-1_2"},
        method="wall.repost",
        client=community_client,
    )

    publish_client.method.assert_called_once_with("wall.repost", {"object": "wall-1_2"})
    community_client.method.assert_not_called()
    assert response == {"success": 1, "post_id": 999}
    assert via == "publish-token"


@pytest.mark.asyncio
async def test_delete_skips_community_token_entirely():
    """wall.delete — метод только для user-токена: group-токен его не умеет.

    VK отвечает community-токену на ``wall.delete`` кодом 27 «method is
    unavailable with group auth» (проектная проба 2026-08-28 + 46 отказов в
    прод-логах). 21.11 на части вызовов VK ответил кодом 5 — и policy приняла
    это за поломку токена: 9 исправных community-токенов ушли в cooldown на
    24 часа, а на витрине «Токены» висело красное «auto-disable: VK error 5».
    Group-токен в wall.delete не пробуем вообще.
    """
    publish_client = _client_with_method_only({"response": {"success": 1}})
    community_client = _client_with_method_only({"response": {"success": 1}})

    publisher = _make_publisher(publish_client)
    response, via = await publisher._call_wall_post(
        params={"owner_id": -1, "post_id": 2},
        method="wall.delete",
        client=community_client,
    )

    publish_client.method.assert_called_once_with("wall.delete", {"owner_id": -1, "post_id": 2})
    community_client.method.assert_not_called()
    assert response == {"success": 1}
    assert via == "publish-token"


@pytest.mark.asyncio
async def test_delete_never_reports_community_token_error():
    """Community-токен в wall.delete не вызывается → policy не слышит про него.

    Инвариант против повторения инцидента 21.11: отказ group-токена на
    user-only методе не должен доходить до ``policy.report_error`` — иначе
    исправный токен получает cooldown и красную надпись на витрине.
    """
    publish_client = _client_with_method_only({"response": {"success": 1}})
    community_client = _client_with_method_only(
        {"error": {"error_code": 5, "error_msg": "User authorization failed"}}
    )
    policy = MagicMock()
    policy.report_error = AsyncMock()
    policy.report_success = AsyncMock()

    publisher = _make_publisher(publish_client)
    publisher._policy = policy
    publisher._community_candidates = {1: [("COMM_1", "community-token-value")]}

    await publisher._call_wall_post(
        params={"owner_id": -1, "post_id": 2},
        method="wall.delete",
        client=community_client,
    )

    community_client.method.assert_not_called()
    policy.report_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_fallback_on_15():
    publish_client = _client_with_method_only({"response": {"post_id": 555}})
    community_client = _client_with_method_only(_vk_error_response(15, "Access denied"))

    publisher = _make_publisher(publish_client)
    response, via = await publisher._call_wall_post(
        params={"owner_id": -1, "message": "x"},
        method="wall.post",
        client=community_client,
    )

    publish_client.method.assert_called_once()
    assert response == {"post_id": 555}
    assert via == "community-fallback-publish"


@pytest.mark.asyncio
async def test_no_fallback_when_already_publish_client():
    """If the call was already routed through publish-client, no fallback retry."""
    publish_client = _client_with_method_only(_vk_error_response(27, "still failing"))
    publisher = _make_publisher(publish_client)

    with pytest.raises(Exception) as excinfo:
        await publisher._call_wall_post(
            params={"owner_id": -1, "message": "x"},
            method="wall.post",
            client=publish_client,
        )

    assert "still failing" in str(excinfo.value)
    publish_client.method.assert_called_once()


@pytest.mark.asyncio
async def test_unrelated_error_propagates():
    """VK errors outside the fallback set must bubble up without retry."""
    publish_client = _client_with_method_only({"response": {"post_id": 1}})
    community_client = _client_with_method_only(_vk_error_response(100, "validation failed"))

    publisher = _make_publisher(publish_client)

    with pytest.raises(Exception):
        await publisher._call_wall_post(
            params={"owner_id": -1, "message": "x"},
            method="wall.post",
            client=community_client,
        )

    community_client.method.assert_called_once()
    publish_client.method.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_on_code_27_when_only_in_error_msg():
    """Regression: VKClient.api_call old code path returned {'error_msg': str(ApiError)}
    without explicit error_code. _invoke must parse '[27] ...' from the message
    so retry-on-fallback still triggers — exercised here via wall.post (which
    DOES try community first; wall.repost wouldn't).
    """
    publish_client = _client_with_method_only({"response": {"post_id": 777}})
    community_client = _client_with_method_only(
        {
            "error": {
                "error_msg": (
                    "[27] Group authorization failed: " "method is unavailable with group auth."
                )
                # NOTE: no 'error_code' key — this mimics legacy VKClient behaviour.
            }
        }
    )

    publisher = _make_publisher(publish_client)
    response, via = await publisher._call_wall_post(
        params={"owner_id": -1, "message": "x"},
        method="wall.post",
        client=community_client,
    )

    publish_client.method.assert_called_once()
    assert response == {"post_id": 777}
    assert via == "community-fallback-publish"


@pytest.mark.asyncio
async def test_community_token_success_no_fallback():
    """Happy path — community-token works first try, publish-client untouched."""
    publish_client = _client_with_method_only({"response": {"post_id": 1}})
    community_client = _client_with_method_only({"response": {"post_id": 42}})

    publisher = _make_publisher(publish_client)
    response, via = await publisher._call_wall_post(
        params={"owner_id": -1, "message": "ok"},
        method="wall.post",
        client=community_client,
    )

    community_client.method.assert_called_once()
    publish_client.method.assert_not_called()
    assert response == {"post_id": 42}
    assert via == "community-token"


@pytest.mark.asyncio
async def test_global_rate_limit_throttles_publish_token():
    """Two back-to-back publish-token calls must be ≥ GLOBAL_PUBLISH_INTERVAL_SECONDS
    apart. Verifies our defence against VK captcha after fallback bursts."""
    import time

    from modules.publisher.vk_publisher_extended import VKPublisher

    publish_client = _client_with_method_only({"response": {"post_id": 1}})
    publisher = _make_publisher(publish_client)
    # Reset shared class state so we don't get penalised by other tests
    VKPublisher._last_publish_token_call = None

    # Shrink the interval for the test so it doesn't slow CI significantly
    original = VKPublisher.GLOBAL_PUBLISH_INTERVAL_SECONDS
    VKPublisher.GLOBAL_PUBLISH_INTERVAL_SECONDS = 0.3
    try:
        t0 = time.monotonic()
        await publisher._call_wall_post(
            params={"object": "wall-1_1"},
            method="wall.repost",
            client=publish_client,
        )
        await publisher._call_wall_post(
            params={"object": "wall-1_2"},
            method="wall.repost",
            client=publish_client,
        )
        elapsed = time.monotonic() - t0
    finally:
        VKPublisher.GLOBAL_PUBLISH_INTERVAL_SECONDS = original

    # First call doesn't wait, second waits the full interval.
    assert elapsed >= 0.25  # allow some slack for scheduler jitter
