"""Видео-репост идёт через relay, а не в api.telegram.org напрямую (P229).

**Что было.** ``_send_video_multipart`` слал файл сырым ``requests.post`` — мимо
``telegram_http``, то есть мимо relay. С бокса ``api.telegram.org``
недоступен (G63/#041, РКН-блок), поэтому видео в Telegram-канал не уходило
вовсе, а доставка отчитывалась ``{'outputs': 2, 'delivered': 1, 'failed': 1}``
— как обычный шум, а не как поломка канала.

**Что держим здесь:**

- multipart уходит в **relay** (URL переписан, токен — заголовком, не в пути);
- **файл переоткрывается на каждой попытке.** Это не «хороший тон», а ловушка:
  ``requests`` читает файл при подготовке запроса, поэтому повтор с тем же
  handle ушёл бы с **пустым** файлом, и Telegram ответил бы «файл пуст», а не
  «повтори»;
- повтор на 429 жив.
"""

from __future__ import annotations

import pytest
import requests

from modules import telegram_http as th
from modules.publisher import telegram_repost as tr


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 1}}

    def json(self):
        return self._body


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video-bytes" * 100)
    return str(path)


class _Recorder:
    """Stand-in for requests.post: пишет URL, заголовки и ПРОЧИТАННОЕ тело."""

    def __init__(self, statuses=None):
        self.calls = []
        self.statuses = list(statuses or [])

    def __call__(self, url, **kw):
        body = b""
        files = kw.get("files")
        if files:
            for handle in files.values():
                body += handle.read()
        self.calls.append({"url": url, "headers": kw.get("headers") or {}, "body": body})
        if self.statuses:
            status, payload = self.statuses.pop(0)
            if status == "raise":
                raise requests.exceptions.ConnectionError("сеть отвалилась")
            return _FakeResp(status, payload)
        return _FakeResp()


async def test_video_multipart_goes_through_relay(monkeypatch, video_file):
    """С relay'ом — URL переписан, токен едет заголовком, в пути его нет."""
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example/bot")
    monkeypatch.setenv("TG_RELAY_SECRET", "sekret")
    rec = _Recorder()
    monkeypatch.setattr("requests.post", rec)

    ok = await tr._send_video_multipart("BOTTOKEN", "@chan", video_file, "подпись")

    assert ok is True
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["url"] == "https://relay.example/bot/sendVideo"
    assert "BOTTOKEN" not in call["url"], "токен бота в URL — он утекает в лог"
    assert call["headers"]["X-TG-Token"] == "BOTTOKEN"
    assert call["headers"]["X-Relay-Secret"] == "sekret"
    assert call["body"] == b"video-bytes" * 100


async def test_video_multipart_without_relay_uses_direct_api(monkeypatch, video_file):
    """Без relay'а дверь та же — просто прямой хост (как у всех остальных)."""
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    rec = _Recorder()
    monkeypatch.setattr("requests.post", rec)

    ok = await tr._send_video_multipart("BOTTOKEN", "@chan", video_file, None)

    assert ok is True
    assert rec.calls[0]["url"] == "https://api.telegram.org/botBOTTOKEN/sendVideo"


async def test_video_multipart_reopens_file_on_network_retry(monkeypatch, video_file):
    """Ловушка пустого файла: на второй попытке файл должен быть ПОЛНЫМ.

    Тот же handle после первой попытки стоит на EOF, и ``requests`` отправил бы
    пустое тело — Telegram ответил бы «файл пуст», а не «повтори».
    """
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example/bot")
    rec = _Recorder(statuses=[("raise", None), (200, None)])
    monkeypatch.setattr("requests.post", rec)

    ok = await tr._send_video_multipart("BOTTOKEN", "@chan", video_file, None)

    assert ok is True
    assert len(rec.calls) == 2
    assert rec.calls[0]["body"] == b"video-bytes" * 100
    assert rec.calls[1]["body"] == b"video-bytes" * 100, "повтор ушёл с пустым файлом"


async def test_video_multipart_429_retry(monkeypatch, video_file):
    """Повтор на 429 жив: тело при второй попытке тоже полное."""
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example/bot")
    rec = _Recorder(statuses=[(429, {"parameters": {"retry_after": 0}}), (200, None)])
    monkeypatch.setattr("requests.post", rec)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)

    ok = await tr._send_video_multipart("BOTTOKEN", "@chan", video_file, None)

    assert ok is True
    assert len(rec.calls) == 2
    assert rec.calls[1]["body"] == b"video-bytes" * 100


async def test_video_multipart_failure_is_reported(monkeypatch, video_file):
    """Неуспех — честный False, а не «отправилось»."""
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://relay.example/bot")
    rec = _Recorder(statuses=[(400, {"ok": False, "description": "bad file"})])
    monkeypatch.setattr("requests.post", rec)

    ok = await tr._send_video_multipart("BOTTOKEN", "@chan", video_file, None)

    assert ok is False


def test_post_multipart_closes_handles(monkeypatch, video_file):
    """Хендлы закрываются: иначе на каждый репост течёт дескриптор."""
    opened = []
    real_open = open

    def _tracking_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    import builtins

    monkeypatch.setattr(builtins, "open", _tracking_open)
    rec = _Recorder()
    monkeypatch.setattr("requests.post", rec)

    # Фабрика берёт open из globals в момент вызова — то есть ПАТЧЕННЫЙ.
    th.post_multipart(
        "https://relay.example/bot/sendVideo",
        files_factory=lambda: {"video": open(video_file, "rb")},
        data={"chat_id": "@chan"},
    )

    assert opened, "файл не открывался"
    assert all(handle.closed for handle in opened), "хендл файла остался открытым"
