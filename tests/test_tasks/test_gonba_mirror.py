"""Tests for Flow B — Гоньба VK wall → Telegram mirror."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from modules.telegram_gonba_mirror import execute_gonba_telegram_mirror
from utils.post_utils import lip_of_post

VK_ID = -218688001


class _Result:
    def __init__(self, first=None):
        self._first = first

    def scalars(self):
        m = MagicMock()
        m.first.return_value = self._first
        return m


class _FakeSession:
    """Returns queued results per execute() call; records commits."""

    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, stmt):
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None

    def add(self, obj):
        return None


class _FakeVK:
    def __init__(self, posts):
        self._posts = posts

    def get_wall_posts(self, owner_id, count, offset):
        return self._posts


class _FakeVKAsyncCM:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _make_request(self, method, params):
        return {"items": []}


def _community(**over):
    base = dict(id=847, vk_id=VK_ID, telegram_channel="@gonba_life", telegram_bot="VALSTANBOT")
    base.update(over)
    return SimpleNamespace(**base)


def _patch_common(monkeypatch, posts, wt, sent_calls):
    async def _tokens(session):
        return {"T": "tok"}

    monkeypatch.setattr("modules.vk_token_router.get_active_parse_tokens", _tokens)
    monkeypatch.setattr("modules.vk_monitor.vk_client.VKClient", lambda tok: _FakeVK(posts))
    monkeypatch.setattr("modules.vk_monitor.vk_client_async.VKClientAsync", _FakeVKAsyncCM)
    monkeypatch.setattr(
        "modules.publisher.telegram_repost_config.telegram_repost_disabled", lambda: False
    )

    async def _fake_repost(bot, channel, text, media, *, test_mode=False):
        sent_calls.append({"bot": bot, "channel": channel, "text": text})
        return {"success": True}

    monkeypatch.setattr("modules.publisher.telegram_repost.repost_to_telegram", _fake_repost)


async def test_gonba_mirrors_only_fresh_non_ad(monkeypatch):
    now = int(time.time())
    seen_lip = lip_of_post(VK_ID, 1)
    posts = [
        {"id": 1, "owner_id": VK_ID, "date": now - 100, "text": "уже виденный"},
        {"id": 2, "owner_id": VK_ID, "date": now - 10 * 24 * 3600, "text": "старый"},
        {
            "id": 3,
            "owner_id": VK_ID,
            "date": now - 200,
            "text": "купить скидка заказать" " цена: 100 звоните: 999 whatsapp",
            "marked_as_ads": True,
        },
        {"id": 4, "owner_id": VK_ID, "date": now - 300, "text": "Свежая новость одна"},
        {"id": 5, "owner_id": VK_ID, "date": now - 50, "text": "Свежая новость два"},
    ]
    wt = SimpleNamespace(lip=[seen_lip], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session, test_mode=False)

    assert res["success"] is True
    # posts 1 (seen), 2 (old), 3 (ad) filtered; 4 & 5 sent, oldest-first.
    assert [c["text"] for c in sent] == ["Свежая новость одна", "Свежая новость два"]
    assert all(c["bot"] == "VALSTANBOT" and c["channel"] == "@gonba_life" for c in sent)
    assert res["stats"]["sent"] == 2
    assert res["stats"]["skipped_seen"] == 1
    assert res["stats"]["skipped_old"] == 1
    assert res["stats"]["skipped_ads"] == 1
    # lip history advanced with sent posts (and ad lip marked seen).
    assert lip_of_post(VK_ID, 4) in wt.lip
    assert lip_of_post(VK_ID, 5) in wt.lip
    assert lip_of_post(VK_ID, 3) in wt.lip  # ad marked seen
    assert session.commits >= 1


async def test_gonba_test_mode_does_not_mutate_cursor(monkeypatch):
    now = int(time.time())
    posts = [
        {"id": 4, "owner_id": VK_ID, "date": now - 300, "text": "Свежая новость одна"},
        {"id": 5, "owner_id": VK_ID, "date": now - 50, "text": "Свежая новость два"},
    ]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session, test_mode=True)

    # Dry-run reports what it WOULD send but leaves the persistent cursor intact.
    assert res["stats"]["sent"] == 2
    assert wt.lip == []  # cursor untouched
    assert session.commits == 0


async def test_gonba_respects_cap(monkeypatch):
    now = int(time.time())
    posts = [
        {"id": i, "owner_id": VK_ID, "date": now - i, "text": f"новость {i}"} for i in range(1, 6)
    ]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    monkeypatch.setattr(
        "modules.publisher.telegram_repost_config.get_gonba_max_posts_per_run", lambda: 2
    )
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)
    assert res["stats"]["sent"] == 2  # capped


async def test_gonba_partial_delivery_marks_seen(monkeypatch):
    """Incident 2026-08-27: album failed, text landed — success=False kept the
    post unseen and the beat re-sent the same text every 30 min. Anything that
    reached the channel must advance the dedup cursor."""
    now = int(time.time())
    posts = [{"id": 7, "owner_id": VK_ID, "date": now - 60, "text": "Поездка в Йошкар-Олу"}]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)

    async def _partial_repost(bot, channel, text, media, *, test_mode=False):
        sent.append({"text": text})
        return {"success": False, "delivered": True, "degraded": True}

    monkeypatch.setattr("modules.publisher.telegram_repost.repost_to_telegram", _partial_repost)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert lip_of_post(VK_ID, 7) in wt.lip  # seen → no re-send next run
    assert session.commits >= 1
    assert res["stats"]["sent_partial"] == 1  # частичная доставка — свой счётчик
    assert res["success"] is False  # partial delivery is still surfaced
    assert any("partial" in e or "частичн" in e for e in res["errors"])


async def test_gonba_total_failure_keeps_post_unseen(monkeypatch):
    """Nothing reached the channel → do NOT mark seen; retry next run (bounded
    by GONBA_MAX_POST_AGE_HOURS)."""
    now = int(time.time())
    posts = [{"id": 8, "owner_id": VK_ID, "date": now - 60, "text": "Новость"}]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)

    async def _failed_repost(bot, channel, text, media, *, test_mode=False):
        return {"success": False, "delivered": False, "error": "bot token revoked"}

    monkeypatch.setattr("modules.publisher.telegram_repost.repost_to_telegram", _failed_repost)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert lip_of_post(VK_ID, 8) not in (wt.lip or [])
    assert res["stats"]["sent"] == 0
    assert res["success"] is False


async def test_gonba_no_channel_configured(monkeypatch):
    session = _FakeSession([_Result(_community(telegram_channel=None, telegram_bot=None))])
    res = await execute_gonba_telegram_mirror(session)
    assert res["success"] is False
    assert "telegram_channel" in res["error"]


async def test_gonba_disabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REPOST_DISABLED", "1")
    session = _FakeSession([])  # short-circuits before any query
    res = await execute_gonba_telegram_mirror(session)
    assert res.get("skipped") == "disabled"


# --------------------------------------------------------------------------- #
# Частичная доставка — инцидент 2026-08-27/28: одно и то же сообщение уходило
# в @gonba_life каждые 30 минут, потому что «отправлено» считалось только по
# полному успеху, а текст при этом каждый раз доезжал.
# --------------------------------------------------------------------------- #
def _patch_repost(monkeypatch, result, sent):
    async def _fake(bot, channel, text, media, *, test_mode=False):
        sent.append({"bot": bot, "channel": channel, "text": text})
        return dict(result)

    monkeypatch.setattr("modules.publisher.telegram_repost.repost_to_telegram", _fake)


async def test_gonba_partial_delivery_marks_post_seen(monkeypatch):
    now = int(time.time())
    posts = [{"id": 7570, "owner_id": VK_ID, "date": now - 300, "text": "длинный текст поста"}]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    _patch_repost(monkeypatch, {"success": False, "delivered": True}, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert res["stats"]["sent_partial"] == 1
    assert res["stats"]["sent"] == 0
    # Главное: пост записан как отправленный — следующая волна его не продублирует.
    assert lip_of_post(VK_ID, 7570) in wt.lip
    # И потеря не молчит: прогон красный, причина в errors.
    assert res["success"] is False
    assert any("частично" in e for e in res["errors"])


async def test_gonba_total_failure_leaves_post_for_retry(monkeypatch):
    """Ничего не доставлено — повтор законен, курсор не двигаем."""
    now = int(time.time())
    posts = [{"id": 7570, "owner_id": VK_ID, "date": now - 300, "text": "длинный текст поста"}]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts=None)
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    _patch_repost(monkeypatch, {"success": False, "delivered": False}, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert res["stats"]["sent"] == 0
    assert res["stats"]["sent_partial"] == 0
    assert lip_of_post(VK_ID, 7570) not in (wt.lip or [])


# --------------------------------------------------------------------------- #
# Потолок попыток (миграция 089). Полный провал повторять правильно — но не
# бесконечно: раньше границей был только возраст поста, до ~96 попыток за 48 ч.
# --------------------------------------------------------------------------- #
async def test_gonba_failed_attempts_counter_grows(monkeypatch):
    now = int(time.time())
    posts = [{"id": 7572, "owner_id": VK_ID, "date": now - 300, "text": "текст"}]
    lip = lip_of_post(VK_ID, 7572)
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts={lip: 1})
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    _patch_repost(monkeypatch, {"success": False, "delivered": False}, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert wt.failed_attempts == {lip: 2}  # счётчик вырос
    assert lip not in (wt.lip or [])  # но пост ещё в работе
    assert res["stats"]["given_up"] == 0
    assert any("попытка 2 из 3" in e for e in res["errors"])


async def test_gonba_gives_up_after_max_attempts(monkeypatch):
    now = int(time.time())
    posts = [{"id": 7572, "owner_id": VK_ID, "date": now - 300, "text": "текст"}]
    lip = lip_of_post(VK_ID, 7572)
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts={lip: 2})  # третья будет последней
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    _patch_repost(monkeypatch, {"success": False, "delivered": False}, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert res["stats"]["given_up"] == 1
    assert lip in wt.lip  # помечен отправленным — больше не пробуем
    assert not wt.failed_attempts  # счётчик по нему больше не нужен
    assert any("сдались после 3 попыток" in e for e in res["errors"])


async def test_gonba_attempts_reset_on_delivery(monkeypatch):
    """Доставка обнуляет счётчик — иначе прошлые провалы копились бы вечно."""
    now = int(time.time())
    posts = [{"id": 7572, "owner_id": VK_ID, "date": now - 300, "text": "текст"}]
    lip = lip_of_post(VK_ID, 7572)
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts={lip: 2})
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)  # _patch_common шлёт success=True

    assert res["stats"]["sent"] == 1
    assert lip in wt.lip
    assert not wt.failed_attempts


async def test_gonba_attempts_self_prune_when_post_leaves_wall(monkeypatch):
    """Пост выпал из окна стены — счётчик по нему выбрасывается, словарь не растёт."""
    now = int(time.time())
    gone = lip_of_post(VK_ID, 1000)  # такого поста на стене уже нет
    posts = [{"id": 7572, "owner_id": VK_ID, "date": now - 300, "text": "текст"}]
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts={gone: 2})
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    session = _FakeSession([_Result(_community()), _Result(wt)])

    await execute_gonba_telegram_mirror(session)

    assert gone not in (wt.failed_attempts or {})


async def test_gonba_max_attempts_zero_disables_cap(monkeypatch):
    """GONBA_MAX_SEND_ATTEMPTS=0 — потолка нет, поведение как до миграции 089."""
    now = int(time.time())
    posts = [{"id": 7572, "owner_id": VK_ID, "date": now - 300, "text": "текст"}]
    lip = lip_of_post(VK_ID, 7572)
    wt = SimpleNamespace(lip=[], hash=[], failed_attempts={lip: 99})
    sent = []
    _patch_common(monkeypatch, posts, wt, sent)
    _patch_repost(monkeypatch, {"success": False, "delivered": False}, sent)
    monkeypatch.setattr(
        "modules.publisher.telegram_repost_config.get_gonba_max_send_attempts", lambda: 0
    )
    session = _FakeSession([_Result(_community()), _Result(wt)])

    res = await execute_gonba_telegram_mirror(session)

    assert res["stats"]["given_up"] == 0
    assert lip not in (wt.lip or [])
    assert wt.failed_attempts == {lip: 100}
