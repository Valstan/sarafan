"""
Telegram repost: mirror VK bulletins / community walls to Telegram channels.

Transport uses the raw Telegram Bot API over HTTP (``requests`` in a worker
thread), consistent with ``modules.notifications.telegram_notifier`` and the
alert sender in ``tasks.celery_app``. We deliberately do NOT use
``python-telegram-bot`` here: URL-based media (photos/videos by VK CDN URL) map
cleanly onto the raw API, there is no Bot lifecycle to manage inside Celery's
reused event loop, and the whole thing is trivially mockable in tests.

Bot tokens are resolved by NAME from ``config.runtime.TELEGRAM_TOKENS`` — the
actual secrets live only in ``/etc/setka/setka.env`` (pool #008), never in the
DB or repo. The DB stores only the channel and the bot NAME (e.g. "AFONYA").

Design notes:
- Text is rebuilt clean from source-post texts (no VK source links, no VK
  hashtags) rather than regex-stripping the VK-formatted bulletin text.
- Only directly-sendable ``*.mp4`` video URLs are attached; VK embed/player
  pages are dropped (Telegram cannot upload them by URL).
- Any send failure degrades gracefully and never raises into the caller — a
  Telegram problem must never break VK publishing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.text_utils import truncate_text
from utils.vk_attachments import extract_vk_attachments, get_photo_urls, resolve_video_url

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/{method}"

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
TG_MEDIA_GROUP_MAX = 10

# Telegram не смог сам скачать медиа по URL — транзиторно, повторяется один раз.
_WEBPAGE_CURL_FAILED = "WEBPAGE_CURL_FAILED"

# Bot API hard cap for uploading a file via multipart (50 MB). Larger videos
# cannot be delivered by a bot at all and are dropped (degraded).
MAX_TG_BOT_UPLOAD_BYTES = 50 * 1024 * 1024
# Telegram downloads media-by-URL only up to ~20 MB; above that the URL-send
# fails and we fall back to downloading + multipart upload (up to 50 MB).
_DOWNLOAD_CHUNK = 256 * 1024

# [url|label] VK wiki-link -> keep label only
_WIKI_LINK_RE = re.compile(r"\[(https?://[^\]|]+)\|([^\]]+)\]")
# bare vk.com source links
_VK_URL_RE = re.compile(r"https?://(?:m\.)?vk\.com/\S+", re.IGNORECASE)
# hashtags (latin + cyrillic + digits + underscore)
_HASHTAG_RE = re.compile(r"(?<![\w])#[\wЀ-ӿ]+", re.UNICODE)


@dataclass
class ResolvedMedia:
    """Sendable media URLs resolved from a VK post."""

    photos: List[str] = field(default_factory=list)
    videos: List[str] = field(default_factory=list)
    docs: List[Dict[str, str]] = field(default_factory=list)  # {"url", "filename"}
    degraded: bool = False  # some media could not be made sendable and was dropped

    def has_media(self) -> bool:
        return bool(self.photos or self.videos or self.docs)


def clean_text_for_telegram(
    text: Optional[str], *, extra_hashtags: Optional[List[str]] = None
) -> str:
    """
    Render VK post text for Telegram: drop source links and VK hashtags,
    unwrap wiki-links to their labels, collapse blank lines. Optionally append
    a small set of Telegram-tailored hashtags.
    """
    text = text or ""
    text = _WIKI_LINK_RE.sub(lambda m: m.group(2), text)
    text = _VK_URL_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)

    # Collapse runs of blank lines and strip trailing spaces.
    out: List[str] = []
    blank = 0
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(line)
    cleaned = "\n".join(out).strip()

    if extra_hashtags:
        tags = " ".join(t if t.startswith("#") else f"#{t}" for t in extra_hashtags if t)
        if tags:
            cleaned = f"{cleaned}\n\n{tags}".strip()
    return cleaned


def _is_sendable_video_url(url: Optional[str]) -> bool:
    """Telegram can upload videos by URL only for direct files (``*.mp4``)."""
    if not url:
        return False
    path = url.split("?", 1)[0].lower()
    return path.endswith(".mp4")


async def resolve_media(
    post_data: Dict[str, Any],
    vk_async_client: Any,
    *,
    max_items: int = TG_MEDIA_GROUP_MAX,
) -> ResolvedMedia:
    """
    Resolve a single VK post's attachments into sendable Telegram media URLs.

    Photos: largest-size direct URLs (reuses ``get_photo_urls``). Videos:
    resolved via ``resolve_video_url`` and kept only if a direct ``*.mp4`` URL
    is available (embed/player pages are dropped, flagging ``degraded``). Docs:
    direct file URLs.
    """
    attachments = extract_vk_attachments(post_data)
    media = ResolvedMedia()
    media.photos = get_photo_urls(attachments, max_photos=max_items)

    for video in attachments.get("video", []) or []:
        owner_id = video.get("owner_id")
        vid = video.get("id")
        url = None
        if owner_id is not None and vid is not None and vk_async_client is not None:
            url = await resolve_video_url(vk_async_client, owner_id, vid)
        if _is_sendable_video_url(url):
            media.videos.append(url)
        else:
            media.degraded = True

    for doc in attachments.get("doc", []) or []:
        url = doc.get("url")
        if url:
            media.docs.append({"url": url, "filename": doc.get("title") or "file"})

    return media


def _media_group_items(
    media: ResolvedMedia, *, caption: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Build a Telegram ``sendMediaGroup`` array (photos + videos, max 10)."""
    items: List[Dict[str, Any]] = []
    for url in media.photos:
        items.append({"type": "photo", "media": url})
    for url in media.videos:
        items.append({"type": "video", "media": url})
    items = items[:TG_MEDIA_GROUP_MAX]
    if items and caption:
        items[0]["caption"] = caption[:TG_CAPTION_LIMIT]
    return items


async def _call(token: str, method: str, payload: Dict[str, Any]) -> bool:
    """
    POST to the Telegram Bot API once (with a single ``RetryAfter`` retry on 429).
    Never raises — returns success bool and logs failures.
    """
    from modules import telegram_http as tg_http

    url = _TG_API.format(token=token, method=method)
    for attempt in range(2):
        try:
            resp = await asyncio.to_thread(tg_http.post, url, json=payload, timeout=30)
        except Exception as e:  # network error
            logger.warning("Telegram %s request error: %s", method, e)
            return False

        if resp.status_code == 200:
            return True

        try:
            body = resp.json()
        except Exception:
            body = {}

        if resp.status_code == 429 and attempt == 0:
            retry_after = int((body.get("parameters") or {}).get("retry_after", 2))
            logger.warning("Telegram %s flood control, retry after %ss", method, retry_after)
            await asyncio.sleep(retry_after + 1)
            continue

        # WEBPAGE_CURL_FAILED — Telegram сам не смог скачать медиа по URL из
        # VK CDN. Отказ транзиторный (в логах падает то элемент #1, то #6 у
        # одного и того же поста), и на 400 ничего не доставлено — повтор
        # безопасен и часто спасает альбом.
        if resp.status_code == 400 and _WEBPAGE_CURL_FAILED in str(body) and attempt == 0:
            logger.warning("Telegram %s: %s, одна повторная попытка", method, _WEBPAGE_CURL_FAILED)
            await asyncio.sleep(1)
            continue

        logger.warning("Telegram %s failed: %s %s", method, resp.status_code, str(body)[:300])
        return False
    return False


async def _send_text(token: str, channel: str, text: str) -> bool:
    """Send a plain text message (no link preview)."""
    return await _call(
        token,
        "sendMessage",
        {"chat_id": channel, "text": text, "disable_web_page_preview": True},
    )


def _download_to_temp(url: str, max_bytes: int) -> Optional[str]:
    """Stream ``url`` to a temp ``.mp4`` file, aborting if it exceeds ``max_bytes``.

    Returns the temp file path, or ``None`` on HTTP error, oversize, or any
    exception. Caller is responsible for removing the file. Sync — call via
    ``asyncio.to_thread``.
    """
    import tempfile

    import requests

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    path = tmp.name
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            if r.status_code != 200:
                logger.warning("Video download HTTP %s for %s", r.status_code, url)
                tmp.close()
                os.remove(path)
                return None
            total = 0
            for chunk in r.iter_content(_DOWNLOAD_CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    logger.info("Video exceeds %d bytes, aborting download: %s", max_bytes, url)
                    tmp.close()
                    os.remove(path)
                    return None
                tmp.write(chunk)
        tmp.close()
        return path
    except Exception as e:
        logger.warning("Video download failed for %s: %s", url, e)
        try:
            tmp.close()
            os.remove(path)
        except OSError:
            pass
        return None


async def _send_video_multipart(
    token: str, channel: str, file_path: str, caption: Optional[str]
) -> bool:
    """Upload a local video file via multipart ``sendVideo`` (single 429 retry)."""
    import requests

    url = _TG_API.format(token=token, method="sendVideo")
    data: Dict[str, Any] = {"chat_id": channel}
    if caption:
        data["caption"] = caption

    def _do():
        with open(file_path, "rb") as fh:
            return requests.post(url, data=data, files={"video": fh}, timeout=180)

    for attempt in range(2):
        try:
            resp = await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning("Telegram sendVideo (file) request error: %s", e)
            return False
        if resp.status_code == 200:
            return True
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code == 429 and attempt == 0:
            retry_after = int((body.get("parameters") or {}).get("retry_after", 2))
            logger.warning("Telegram sendVideo (file) flood control, retry after %ss", retry_after)
            await asyncio.sleep(retry_after + 1)
            continue
        logger.warning("Telegram sendVideo (file) failed: %s %s", resp.status_code, str(body)[:300])
        return False
    return False


async def _send_video(token: str, channel: str, video_url: str, caption: Optional[str]) -> bool:
    """Send a video: try by URL first, then fall back to download + multipart.

    Telegram only fetches media-by-URL up to ~20 MB. When the URL-send fails
    (typically oversize), download the direct ``.mp4`` (capped at 50 MB — the
    Bot API upload limit) and send it as a multipart file. Returns ``False`` if
    the video can't be delivered at all (player-only, >50 MB, or upload error),
    so the caller can degrade gracefully.
    """
    payload: Dict[str, Any] = {"chat_id": channel, "video": video_url}
    if caption:
        payload["caption"] = caption
    if await _call(token, "sendVideo", payload):
        return True

    logger.info("sendVideo by URL failed for %s; trying file upload", channel)
    path = await asyncio.to_thread(_download_to_temp, video_url, MAX_TG_BOT_UPLOAD_BYTES)
    if not path:
        return False
    try:
        return await _send_video_multipart(token, channel, path, caption)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def repost_to_telegram(
    bot_name: str,
    channel: str,
    text: str,
    media: ResolvedMedia,
    *,
    test_mode: bool = False,
) -> Dict[str, Any]:
    """
    Send a single repost (text + media) to a Telegram channel using bot
    ``bot_name`` (token resolved from env). Honors ``test_mode`` (no network).
    Degrades media-group → per-item singles → text gracefully: an album refused
    by Telegram (typically one URL it cannot fetch) is re-sent item by item, so
    one bad URL does not sink the whole post; the caption text is re-sent as a
    plain message when its media carrier fails and is never silently lost.

    Возвращает ДВА разных флага, и путать их нельзя:

    - ``success`` — прошло всё, что собирались отправить;
    - ``delivered`` — в канал ушло **хоть что-то**.

    Один пост рассылается несколькими независимыми вызовами (альбом + текст,
    когда он длиннее подписи), и они падают независимо. ``success=False`` при
    ``delivered=True`` означает «часть уже в канале» — повтор такого поста даёт
    не доставку, а дубль. Вызывающий, который ретраит, обязан смотреть на
    ``delivered`` (см. ``modules.telegram_gonba_mirror``).
    """
    from config.runtime import TELEGRAM_TOKENS

    token = TELEGRAM_TOKENS.get((bot_name or "").upper())
    if not token:
        logger.warning("No Telegram token for bot '%s'; skipping repost to %s", bot_name, channel)
        return {"success": False, "delivered": False, "error": f"no token for bot {bot_name}"}

    text = text or ""
    if len(text) > TG_TEXT_LIMIT:
        text = truncate_text(text, TG_TEXT_LIMIT)

    if not text and not media.has_media():
        return {"success": True, "delivered": False, "skipped": "empty"}

    if test_mode:
        logger.info(
            "[TEST] Telegram repost to %s via %s: text=%dch ph=%d vid=%d doc=%d degraded=%s",
            channel,
            bot_name,
            len(text),
            len(media.photos),
            len(media.videos),
            len(media.docs),
            media.degraded,
        )
        return {"success": True, "delivered": False, "test_mode": True}

    success = True
    delivered = False
    text_as_caption = False
    group = _media_group_items(media)
    caption = text if (text and len(text) <= TG_CAPTION_LIMIT) else None

    if len(group) >= 2:
        items = _media_group_items(media, caption=caption)
        ok = await _call(token, "sendMediaGroup", {"chat_id": channel, "media": items})
        if ok:
            delivered = True
            text_as_caption = caption is not None
        else:
            # Album refused (typically one URL Telegram can't fetch) — fall back
            # to per-item sends so one bad URL doesn't sink the whole post. The
            # text goes as a separate message below, never as a caption here.
            media.degraded = True
            item_failed = False
            for url in media.photos[:TG_MEDIA_GROUP_MAX]:
                ok_item = await _call(token, "sendPhoto", {"chat_id": channel, "photo": url})
                delivered = delivered or ok_item
                item_failed = item_failed or not ok_item
            for url in media.videos[: max(0, TG_MEDIA_GROUP_MAX - len(media.photos))]:
                ok_item = await _send_video(token, channel, url, None)
                delivered = delivered or ok_item
                item_failed = item_failed or not ok_item
            success = success and not item_failed
    elif len(group) == 1:
        item = group[0]
        if item["type"] == "photo":
            payload: Dict[str, Any] = {"chat_id": channel, "photo": item["media"]}
            if caption:
                payload["caption"] = caption
            ok = await _call(token, "sendPhoto", payload)
            success = success and ok
            if ok:
                delivered = True
                text_as_caption = caption is not None
            else:
                media.degraded = True
        else:  # video — try URL, then file upload (≤50MB); degrade to text if both fail
            ok = await _send_video(token, channel, item["media"], caption)
            if ok:
                delivered = True
                text_as_caption = caption is not None
            else:
                # Video unsendable (player-only / >50MB / upload error). Keep the
                # post: its text still goes out below; flag degraded. Deliberately
                # NOT a failure — matches the historical graceful-degradation
                # contract (an undeliverable video must not fail the repost).
                media.degraded = True

    # Text goes out as its own message whenever it wasn't delivered as a
    # caption — including when its intended media carrier failed above.
    if text and not text_as_caption:
        ok = await _send_text(token, channel, text)
        success = success and ok
        delivered = delivered or ok

    # Documents cannot share a photo/video media group — send separately.
    for doc in media.docs[:TG_MEDIA_GROUP_MAX]:
        ok_doc = await _call(token, "sendDocument", {"chat_id": channel, "document": doc["url"]})
        delivered = delivered or ok_doc
        if not ok_doc:
            media.degraded = True

    if not delivered:
        success = False

    return {"success": success, "delivered": delivered, "degraded": media.degraded}


async def mirror_bulletin_to_telegram(
    bot_name: str,
    channel: str,
    header: Optional[str],
    posts: List[Dict[str, Any]],
    vk_async_client: Any,
    *,
    extra_hashtags: Optional[List[str]] = None,
    test_mode: bool = False,
) -> Dict[str, Any]:
    """
    Flow A helper: render a bulletin (header + the source posts that made it in)
    into one clean Telegram message with media (capped at 10 items total),
    then send it.
    """
    text_parts: List[str] = []
    if header and header.strip():
        text_parts.append(header.strip())

    media = ResolvedMedia()
    for post in posts:
        cleaned = clean_text_for_telegram(post.get("text") or "")
        if cleaned:
            text_parts.append(cleaned)
        # Keep collecting media until the group is full (photos+videos ≤ 10).
        if len(media.photos) + len(media.videos) < TG_MEDIA_GROUP_MAX:
            rm = await resolve_media(post, vk_async_client)
            media.photos.extend(rm.photos)
            media.videos.extend(rm.videos)
            media.docs.extend(rm.docs)
            media.degraded = media.degraded or rm.degraded

    # Cap combined photo+video to the group limit (photos prioritized).
    media.photos = media.photos[:TG_MEDIA_GROUP_MAX]
    remaining = max(0, TG_MEDIA_GROUP_MAX - len(media.photos))
    media.videos = media.videos[:remaining]

    text = "\n\n".join(text_parts)
    if extra_hashtags:
        text = clean_text_for_telegram(text, extra_hashtags=extra_hashtags)

    return await repost_to_telegram(bot_name, channel, text, media, test_mode=test_mode)
