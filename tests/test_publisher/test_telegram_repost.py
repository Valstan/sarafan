"""Unit tests for Telegram repost (Flow A renderer/transport/media resolution)."""

import os

import config.runtime as runtime
import modules.publisher.telegram_repost as tr
from modules.publisher.telegram_repost import (
    ResolvedMedia,
    clean_text_for_telegram,
    mirror_bulletin_to_telegram,
    repost_to_telegram,
    resolve_media,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"ok": True, "result": {"message_id": 1}}

    def json(self):
        return self._body


class _RecordingPost:
    """Stand-in for requests.post that records calls and returns 200."""

    def __init__(self, status_code=200, body=None):
        self.calls = []
        self.status_code = status_code
        self.body = body

    def __call__(self, url, json=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        return _FakeResp(self.status_code, self.body)

    def methods(self):
        return [m for m, _ in self.calls]


class _FakeVKAsync:
    """Duck-typed VKClientAsync exposing _make_request for video.get."""

    def __init__(self, files):
        self._files = files  # dict passed back as video.files

    async def _make_request(self, method, params):
        return {"items": [{"files": self._files}]}


# --------------------------------------------------------------------------- #
# clean_text_for_telegram
# --------------------------------------------------------------------------- #
def test_clean_text_strips_hashtags_and_links():
    raw = "Заголовок новости\n\n#новости #Малмыж43\nПодробнее https://vk.com/wall-1_2"
    out = clean_text_for_telegram(raw)
    assert "#" not in out
    assert "vk.com" not in out
    assert "Заголовок новости" in out


def test_clean_text_unwraps_wiki_links():
    raw = "Текст [https://vk.com/wall-1_2|Малмыж Инфо] конец"
    out = clean_text_for_telegram(raw)
    assert "Малмыж Инфо" in out
    assert "vk.com" not in out
    assert "[" not in out


def test_clean_text_collapses_blank_lines():
    raw = "a\n\n\n\nb"
    out = clean_text_for_telegram(raw)
    assert out == "a\n\nb"


def test_clean_text_appends_extra_hashtags():
    out = clean_text_for_telegram("Текст", extra_hashtags=["Малмыж", "#Инфо"])
    assert out.endswith("#Малмыж #Инфо")


# --------------------------------------------------------------------------- #
# resolve_media
# --------------------------------------------------------------------------- #
async def test_resolve_media_keeps_mp4_drops_player():
    photo = {
        "owner_id": -1,
        "id": 10,
        "sizes": [
            {"width": 100, "url": "http://s/small.jpg"},
            {"width": 800, "url": "http://s/big.jpg"},
        ],
    }
    post = {
        "attachments": [
            {"type": "photo", "photo": photo},
            {"type": "video", "video": {"owner_id": -1, "id": 20}},
        ]
    }
    client = _FakeVKAsync({"mp4_480": "https://cdn/vid.mp4?x=1"})
    media = await resolve_media(post, client)
    assert media.photos == ["http://s/big.jpg"]
    assert media.videos == ["https://cdn/vid.mp4?x=1"]
    assert media.degraded is False


async def test_resolve_media_drops_embed_player_video():
    post = {"attachments": [{"type": "video", "video": {"owner_id": -1, "id": 20}}]}
    client = _FakeVKAsync(
        {"external": "https://youtube.com/watch?v=x", "player": "https://vk.com/video_ext.php"}
    )
    media = await resolve_media(post, client)
    assert media.videos == []
    assert media.degraded is True


# --------------------------------------------------------------------------- #
# repost_to_telegram dispatch
# --------------------------------------------------------------------------- #
async def test_repost_no_token(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "hi", ResolvedMedia())
    assert res["success"] is False
    assert poster.calls == []


async def test_repost_test_mode_no_network(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram(
        "AFONYA", "@c", "hi", ResolvedMedia(photos=["u"]), test_mode=True
    )
    assert res["success"] is True
    assert res.get("test_mode") is True
    assert poster.calls == []


async def test_repost_text_only(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "только текст", ResolvedMedia())
    assert res["success"] is True
    assert poster.methods() == ["sendMessage"]


async def test_repost_media_group_with_caption(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    media = ResolvedMedia(photos=["p1", "p2"], videos=["v1.mp4"])
    res = await repost_to_telegram("AFONYA", "@c", "короткая подпись", media)
    assert res["success"] is True
    assert poster.methods() == ["sendMediaGroup"]
    _, payload = poster.calls[0]
    assert payload["media"][0]["caption"] == "короткая подпись"
    assert len(payload["media"]) == 3


async def test_repost_media_group_long_text_followup(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    long_text = "x" * (tr.TG_CAPTION_LIMIT + 50)
    media = ResolvedMedia(photos=["p1", "p2"])
    res = await repost_to_telegram("AFONYA", "@c", long_text, media)
    assert res["success"] is True
    # No caption fits → group then a follow-up message.
    assert poster.methods() == ["sendMediaGroup", "sendMessage"]
    assert "caption" not in poster.calls[0][1]["media"][0]


async def test_repost_single_photo(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "cap", ResolvedMedia(photos=["p1"]))
    assert res["success"] is True
    assert poster.methods() == ["sendPhoto"]
    assert poster.calls[0][1]["caption"] == "cap"


async def test_repost_429_retry(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})

    seq = [_FakeResp(429, {"parameters": {"retry_after": 0}}), _FakeResp(200)]

    def fake_post(url, json=None, timeout=None, **kwargs):
        return seq.pop(0)

    monkeypatch.setattr("requests.post", fake_post)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)
    res = await repost_to_telegram("AFONYA", "@c", "t", ResolvedMedia())
    assert res["success"] is True
    assert seq == []  # both responses consumed (retry happened)


async def test_repost_docs_sent_separately(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    media = ResolvedMedia(photos=["p1"], docs=[{"url": "d1", "filename": "f"}])
    await repost_to_telegram("AFONYA", "@c", "", media)
    assert poster.methods() == ["sendPhoto", "sendDocument"]


# --------------------------------------------------------------------------- #
# mirror_bulletin_to_telegram (integration)
# --------------------------------------------------------------------------- #
async def test_mirror_bulletin_builds_clean_message(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    posts = [
        {
            "text": "Первая новость #тег https://vk.com/wall-1_2",
            "attachments": [
                {"type": "photo", "photo": {"sizes": [{"width": 600, "url": "http://p/1.jpg"}]}}
            ],
        },
        {"text": "Вторая новость", "attachments": []},
    ]
    client = _FakeVKAsync({})
    res = await mirror_bulletin_to_telegram(
        "AFONYA",
        "@malmyzh_info",
        "Новости Малмыжа:",
        posts,
        client,
    )
    assert res["success"] is True
    # One photo → sendPhoto with combined clean caption (short).
    assert poster.methods() == ["sendPhoto"]
    _, payload = poster.calls[0]
    cap = payload["caption"]
    assert "Новости Малмыжа:" in cap
    assert "Первая новость" in cap and "Вторая новость" in cap
    assert "#" not in cap and "vk.com" not in cap


# --------------------------------------------------------------------------- #
# Video >50 MB → file upload fallback (feat/tg-video-file)
# --------------------------------------------------------------------------- #
async def test_send_video_url_success_no_download(monkeypatch):
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    downloaded = {"called": False}
    monkeypatch.setattr(
        tr, "_download_to_temp", lambda *a, **k: downloaded.__setitem__("called", True)
    )
    ok = await tr._send_video("tok", "@c", "https://cdn/v.mp4", "cap")
    assert ok is True
    assert poster.methods() == ["sendVideo"]
    assert downloaded["called"] is False


async def test_send_video_falls_back_to_multipart(monkeypatch):
    # URL sendVideo fails (HTTP 400) → download + multipart upload.
    poster = _RecordingPost(status_code=400, body={"ok": False})
    monkeypatch.setattr("requests.post", poster)
    monkeypatch.setattr(tr, "_download_to_temp", lambda url, mx: "/tmp/does-not-exist.mp4")
    sent = {}

    async def fake_multipart(token, channel, path, caption):
        sent.update(path=path, caption=caption)
        return True

    monkeypatch.setattr(tr, "_send_video_multipart", fake_multipart)
    ok = await tr._send_video("tok", "@c", "https://cdn/big.mp4", "cap")
    assert ok is True
    assert sent == {"path": "/tmp/does-not-exist.mp4", "caption": "cap"}


async def test_send_video_oversize_returns_false(monkeypatch):
    poster = _RecordingPost(status_code=400, body={"ok": False})
    monkeypatch.setattr("requests.post", poster)
    monkeypatch.setattr(tr, "_download_to_temp", lambda url, mx: None)  # too big / failed
    mp = {"called": False}

    async def fake_multipart(*a, **k):
        mp["called"] = True
        return True

    monkeypatch.setattr(tr, "_send_video_multipart", fake_multipart)
    ok = await tr._send_video("tok", "@c", "https://cdn/huge.mp4", "cap")
    assert ok is False
    assert mp["called"] is False


async def test_repost_single_video_url_success(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "cap", ResolvedMedia(videos=["v.mp4"]))
    assert res["success"] is True
    assert poster.methods() == ["sendVideo"]
    assert poster.calls[0][1]["video"] == "v.mp4"
    assert poster.calls[0][1]["caption"] == "cap"


async def test_repost_single_video_oversize_degrades_to_text(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})

    async def fake_send_video(token, channel, url, caption):
        return False  # unsendable (player-only / >50MB / upload error)

    monkeypatch.setattr(tr, "_send_video", fake_send_video)
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram(
        "AFONYA", "@c", "важный текст", ResolvedMedia(videos=["big.mp4"])
    )
    assert res["degraded"] is True
    assert res["success"] is True
    assert poster.methods() == ["sendMessage"]  # text delivered as fallback


# --------------------------------------------------------------------------- #
# Partial-failure delivery accounting (fix/telegram-mirror-partial-delivery-loop)
#
# Incident 2026-08-27: sendMediaGroup failed persistently for one post while
# the follow-up text succeeded; the caller saw success=False, never marked the
# post seen and re-sent the text every beat run (@gonba_life spam). These tests
# pin the contract: media-group failure degrades to per-item sends, caption
# text is never silently lost, and `delivered` reports whether ANYTHING landed.
# --------------------------------------------------------------------------- #
class _SelectiveFailPost:
    """requests.post stand-in that fails configured API methods with HTTP 400."""

    def __init__(self, fail_methods):
        self.calls = []
        self.fail = set(fail_methods)

    def __call__(self, url, json=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        if method in self.fail:
            return _FakeResp(400, {"ok": False, "description": "Bad Request"})
        return _FakeResp(200)

    def methods(self):
        return [m for m, _ in self.calls]


async def test_repost_media_group_failure_falls_back_to_singles(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _SelectiveFailPost({"sendMediaGroup"})
    monkeypatch.setattr("requests.post", poster)
    long_text = "x" * (tr.TG_CAPTION_LIMIT + 50)
    media = ResolvedMedia(photos=["p1", "p2"])
    res = await repost_to_telegram("AFONYA", "@c", long_text, media)
    # Group failed → each photo sent individually, then the text message.
    assert poster.methods() == ["sendMediaGroup", "sendPhoto", "sendPhoto", "sendMessage"]
    assert res["delivered"] is True
    assert res["degraded"] is True
    assert res["success"] is True  # all content landed, presentation degraded


async def test_repost_media_group_and_singles_fail_text_still_delivered(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _SelectiveFailPost({"sendMediaGroup", "sendPhoto"})
    monkeypatch.setattr("requests.post", poster)
    # Short text would have been the album caption — it must not be lost when
    # the album (and every photo) fails: deliver it as a plain message.
    media = ResolvedMedia(photos=["p1", "p2"])
    res = await repost_to_telegram("AFONYA", "@c", "короткая подпись", media)
    assert poster.methods() == ["sendMediaGroup", "sendPhoto", "sendPhoto", "sendMessage"]
    assert res["delivered"] is True
    assert res["degraded"] is True
    assert res["success"] is False  # photos were lost


async def test_repost_single_photo_failure_delivers_caption_as_text(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _SelectiveFailPost({"sendPhoto"})
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "cap", ResolvedMedia(photos=["p1"]))
    assert poster.methods() == ["sendPhoto", "sendMessage"]
    assert res["delivered"] is True
    assert res["success"] is False


async def test_repost_text_only_failure_reports_undelivered(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _SelectiveFailPost({"sendMessage"})
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "текст", ResolvedMedia())
    assert res["success"] is False
    assert res["delivered"] is False


async def test_repost_clean_paths_report_delivered(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    poster = _RecordingPost()
    monkeypatch.setattr("requests.post", poster)
    res = await repost_to_telegram("AFONYA", "@c", "hi", ResolvedMedia(photos=["p1"]))
    assert res["success"] is True
    assert res["delivered"] is True


# --------------------------------------------------------------------------- #
# _download_to_temp size guard
# --------------------------------------------------------------------------- #
class _StreamResp:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, n):
        for c in self._chunks:
            yield c


def test_download_to_temp_aborts_oversize(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _StreamResp([b"x" * 100, b"x" * 100]))
    out = tr._download_to_temp("http://u/v.mp4", max_bytes=10)
    assert out is None


def test_download_to_temp_success(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _StreamResp([b"abc", b"def"]))
    out = tr._download_to_temp("http://u/v.mp4", max_bytes=1000)
    try:
        assert out is not None
        assert os.path.exists(out)
        with open(out, "rb") as fh:
            assert fh.read() == b"abcdef"
    finally:
        if out:
            os.remove(out)


def test_download_to_temp_http_error(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _StreamResp([], status_code=404))
    assert tr._download_to_temp("http://u/v.mp4", max_bytes=1000) is None


# --------------------------------------------------------------------------- #
# delivered != success — инцидент 2026-08-27/28 (спам в @gonba_life)
# --------------------------------------------------------------------------- #
_CURL_FAILED_BODY = {
    "ok": False,
    "error_code": 400,
    "description": "Bad Request: failed to send message #2 with the error message "
    '"WEBPAGE_CURL_FAILED"',
}


async def test_media_group_fails_but_text_delivered_reports_delivered(monkeypatch):
    """Альбом не ушёл, длинный текст ушёл → success False, но delivered True.

    Ровно этот расклад крутил спам в @gonba_life: зеркало видело success=False
    и слало пост заново каждые 30 минут, а канал каждый раз получал текст.
    """
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)

    def fake_post(url, json=None, timeout=None, **kwargs):
        method = url.rsplit("/", 1)[-1]
        if method in {"sendMediaGroup", "sendPhoto", "sendVideo"}:
            return _FakeResp(400, _CURL_FAILED_BODY)
        return _FakeResp(200)

    monkeypatch.setattr("requests.post", fake_post)

    media = ResolvedMedia(photos=["http://cdn/a.jpg", "http://cdn/b.jpg"])
    res = await repost_to_telegram("AFONYA", "@c", "x" * (tr.TG_CAPTION_LIMIT + 1), media)

    assert res["success"] is False  # альбом потерян — честно
    assert res["delivered"] is True  # но текст в канале, повтор дал бы дубль


async def test_total_failure_reports_not_delivered(monkeypatch):
    """Не ушло ничего → delivered False, повтор законен."""
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(400, _CURL_FAILED_BODY))

    media = ResolvedMedia(photos=["http://cdn/a.jpg", "http://cdn/b.jpg"])
    res = await repost_to_telegram("AFONYA", "@c", "x" * (tr.TG_CAPTION_LIMIT + 1), media)

    assert res["success"] is False
    assert res["delivered"] is False


async def test_text_only_success_is_delivered(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})
    monkeypatch.setattr("requests.post", _RecordingPost())
    res = await repost_to_telegram("AFONYA", "@c", "привет", ResolvedMedia())
    assert res["success"] is True and res["delivered"] is True


async def test_no_token_is_not_delivered(monkeypatch):
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {})
    res = await repost_to_telegram("NOPE", "@c", "t", ResolvedMedia())
    assert res["success"] is False and res["delivered"] is False


async def test_webpage_curl_failed_retried_once(monkeypatch):
    """WEBPAGE_CURL_FAILED транзиторен — одна повторная попытка спасает альбом."""
    monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", {"AFONYA": "tok"})

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)
    seq = [_FakeResp(400, _CURL_FAILED_BODY), _FakeResp(200)]
    monkeypatch.setattr("requests.post", lambda *a, **k: seq.pop(0))

    media = ResolvedMedia(photos=["http://cdn/a.jpg", "http://cdn/b.jpg"])
    res = await repost_to_telegram("AFONYA", "@c", "короткая подпись", media)

    assert seq == []  # обе попытки израсходованы
    assert res["success"] is True and res["delivered"] is True
