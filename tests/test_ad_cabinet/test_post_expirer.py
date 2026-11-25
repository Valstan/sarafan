"""Тесты авто-снятия рекламных постов по сроку (С2, run_expiry).

VK-удаление инжектируется (delete_post), сессия БД — фейковая (async CM),
чтобы покрыть чистую логику снятия без сети и реальной БД.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from database.models import AdPublication
from modules.ad_cabinet import post_expirer as pe


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.add = MagicMock()
        self.commit = AsyncMock()

    async def execute(self, stmt):
        r = MagicMock()
        r.scalars.return_value.all.return_value = self._rows
        return r


class _FakeSessionCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _pub(**kw):
    defaults = dict(
        id=1,
        client_id=3,
        community_vk_id=-100,
        vk_post_id=55,
        scheduled_post_id=9,
        status="published",
        expires_at=datetime(2026, 6, 1, 10, 0),
    )
    defaults.update(kw)
    return AdPublication(**defaults)


def _run(rows, delete_post, now=datetime(2026, 6, 2, 12, 0)):
    session = _FakeSession(rows)
    out = asyncio.run(
        pe.run_expiry(
            session_factory=lambda: _FakeSessionCM(session),
            delete_post=delete_post,
            now=now,
        )
    )
    return out, session


def test_expired_post_is_removed():
    pub = _pub()
    out, session = _run([pub], delete_post=lambda owner, pid: True)
    assert out["removed"] == 1
    assert pub.status == "removed"
    assert pub.removed_at is not None
    # событие 'removed' добавлено в таймлайн
    assert session.add.call_count >= 1
    session.commit.assert_awaited()


def test_delete_failure_keeps_published():
    """wall.delete не удался → пост остаётся published (повторим завтра)."""
    pub = _pub()
    out, session = _run([pub], delete_post=lambda owner, pid: False)
    assert out["removed"] == 0
    assert pub.status == "published"
    assert pub.removed_at is None
    session.add.assert_not_called()


def test_no_vk_post_id_skipped():
    pub = _pub(vk_post_id=None)
    out, session = _run([pub], delete_post=lambda owner, pid: True)
    assert out["removed"] == 0
    assert pub.status == "published"
    session.add.assert_not_called()


def test_deleter_exception_is_safe():
    def boom(owner, pid):
        raise RuntimeError("vk down")

    pub = _pub()
    out, _ = _run([pub], delete_post=boom)
    assert out["removed"] == 0
    assert pub.status == "published"


def test_empty_selection_noop():
    """Нет вышедших постов с истёкшим сроком → ничего не делаем."""
    out, session = _run([], delete_post=lambda owner, pid: True)
    assert out == {"removed": 0, "checked": 0}
    session.add.assert_not_called()


def test_multiple_posts_mixed_results():
    p1 = _pub(id=1, vk_post_id=11)
    p2 = _pub(id=2, vk_post_id=22)
    # первый удаляется, второй — нет.
    out, _ = _run([p1, p2], delete_post=lambda owner, pid: pid == 11)
    assert out == {"removed": 1, "checked": 2}
    assert p1.status == "removed"
    assert p2.status == "published"


def test_default_deleter_uses_user_token_only(monkeypatch):
    """``wall.delete`` — только user-токен: community-токен метод не умеет.

    VK отвечает group-токену на ``wall.delete`` кодом 27 «method is unavailable
    with group auth», а 21.11 на части вызовов — кодом 5, и policy приняла это
    за поломку токена (cooldown 24 ч + красная надпись на витрине «Токены»).
    Community-токен в удалении не пробуем вообще.
    """
    import sys
    from types import SimpleNamespace

    used_tokens = []

    class _Wall:
        def delete(self, owner_id, post_id):
            return {"response": {"success": 1}}

    class _Api:
        def __init__(self, token):
            used_tokens.append(token)
            self.wall = _Wall()

    monkeypatch.setitem(
        sys.modules,
        "vk_api",
        SimpleNamespace(
            VkApi=lambda token: SimpleNamespace(get_api=lambda: _Api(token)),
        ),
    )

    deleter = pe._build_default_deleter("USER_TOKEN")
    assert deleter(-100, 55) is True
    assert used_tokens == ["USER_TOKEN"], "community-токен в wall.delete не пробуем"


def test_default_deleter_without_user_token_does_nothing(monkeypatch):
    """Нет user-токена — удаления не происходит (молча, без вызова VK)."""
    import sys
    from types import SimpleNamespace

    def _boom(token):  # pragma: no cover — вызываться не должен
        raise AssertionError("vk_api.VkApi не должен вызываться без user-токена")

    monkeypatch.setitem(sys.modules, "vk_api", SimpleNamespace(VkApi=_boom))

    deleter = pe._build_default_deleter("")
    assert deleter(-100, 55) is False
