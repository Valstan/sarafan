"""Разбор сообщений, зависших в redis `unacked`.

Инструмент возврата (`scripts/restore_unacked.py`) трогает боевую очередь,
поэтому его разбор проверяется отдельно и без брокера: чистая логика должна
однозначно сказать, какая задача и в какой очереди лежит, — иначе отчёт
врёт, а вернуть можно не то.

Проверяем на настоящей сериализации kombu (его же формат лежит в `unacked`).
"""

from __future__ import annotations

import base64
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import restore_unacked  # noqa: E402
from kombu.utils.json import dumps  # noqa: E402


def _payload(
    task: str,
    args: list,
    routing_key: str = "digest",
    expires_in: int = 3600,
) -> bytes:
    """Сообщение в том виде, в каком kombu кладёт его в хеш `unacked`."""
    body = base64.b64encode(json.dumps([task, args, {}]).encode("utf-8")).decode("ascii")
    eta = datetime.now(timezone.utc) + timedelta(seconds=60)
    headers = {"task": task, "eta": eta.isoformat()}
    if expires_in is not None:
        headers["expires"] = (eta + timedelta(seconds=expires_in)).isoformat()
    message = {
        "body": body,
        "headers": headers,
        "properties": {"delivery_info": {"routing_key": routing_key}},
        "content-type": "application/json",
        "content-encoding": "utf-8",
    }
    return dumps([message, "", routing_key])


def test_describe_reads_task_and_queue() -> None:
    payload = _payload(
        "tasks.digest_tasks.publish_daily_digest_region", ["vp", "2026-11-25T21:00:00"]
    )
    task, queue, args, _headers = restore_unacked.describe(payload)
    assert task == "tasks.digest_tasks.publish_daily_digest_region"
    assert queue == "digest"
    assert "vp" in args


def test_describe_survives_broken_body() -> None:
    """Битое тело не должно ронять разбор: задача и очередь важнее аргументов."""
    message = {
        "body": base64.b64encode(b"not json").decode("ascii"),
        "headers": {"task": "tasks.broken"},
        "properties": {},
    }
    payload = dumps([message, "", "celery"])
    task, queue, _args, _headers = restore_unacked.describe(payload)
    assert task == "tasks.broken"
    assert queue == "celery"


def test_summarize_counts_by_task() -> None:
    raw = {
        b"tag1": _payload("tasks.digest_tasks.publish_daily_digest_region", ["vp"]),
        b"tag2": _payload("tasks.digest_tasks.publish_daily_digest_region", ["ur"]),
        b"tag3": _payload("tasks.copy", ["copy", "setka"], routing_key="wave"),
    }
    counter = restore_unacked.summarize(raw)
    assert counter["tasks.digest_tasks.publish_daily_digest_region"] == 2
    assert counter["tasks.copy"] == 1
    assert sum(counter.values()) == 3


def test_summarize_marks_unparsable_messages() -> None:
    """Нераспознанное сообщение считается, а не теряется молча."""
    raw = {b"tag1": b"{not json at all"}
    counter = restore_unacked.summarize(raw)
    assert counter["<не разобрано>"] == 1


def test_expiry_state_sees_dead_and_alive() -> None:
    """Протухшая задача должна быть видна в отчёте — иначе возврат бессмыслен."""
    now = time.time()
    alive_headers = {"expires": datetime.fromtimestamp(now + 600).isoformat()}
    dead_headers = {"expires": datetime.fromtimestamp(now - 600).isoformat()}
    assert restore_unacked.expiry_state(alive_headers, now) == "жива"
    assert restore_unacked.expiry_state(dead_headers, now) == "протухла"
    assert restore_unacked.expiry_state({}, now) == "срока нет"


def test_refresh_deadline_extends_only_dead_tasks() -> None:
    """Живую задачу не трогаем, протухшую — продлеваем."""
    now = time.time()
    dead = {"expires": datetime.fromtimestamp(now - 600).isoformat()}
    alive = {"expires": datetime.fromtimestamp(now + 600).isoformat()}

    assert restore_unacked.refresh_deadline(dead, now=now, ttl_sec=3600, eta_in=0) is True
    assert restore_unacked.parse_moment(dead["expires"]) > now + 3000

    assert restore_unacked.refresh_deadline(alive, now=now, ttl_sec=3600, eta_in=0) is False


def test_refresh_deadline_moves_eta_forward() -> None:
    """Сдвиг eta — чтобы возвращённые задачи не начали бить все разом."""
    now = time.time()
    headers = {
        "expires": datetime.fromtimestamp(now - 600).isoformat(),
        "eta": datetime.fromtimestamp(now - 60).isoformat(),
    }
    restore_unacked.refresh_deadline(headers, now=now, ttl_sec=3600, eta_in=900)
    assert restore_unacked.parse_moment(headers["eta"]) >= now + 800


class _FakeRedis:
    """Минимальный клиент: помнит, что записали в хеш."""

    def __init__(self) -> None:
        self.written: dict = {}

    def hset(self, key, field, value):  # noqa: ANN001, ANN201
        self.written[field] = value
        return 1


def test_select_messages_applies_queue_filter() -> None:
    """Сухой прогон должен показывать ровно то, что вернётся.

    Живой стенд 27.11 07:45: в `unacked` лежали 10 сообщений (6 волновых и 4
    сводки), а `--queue wave` отбирал 6 — но отчёт показывал все десять, и
    выглядело это как «инструмент возвращает не то, что обещает».
    """
    raw = {
        b"tag1": _payload(
            "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
            ["copy", "setka"],
            routing_key="wave",
        ),
        b"tag2": _payload(
            "tasks.digest_tasks.publish_daily_digest_region",
            ["vp"],
            routing_key="digest",
        ),
    }
    selected = restore_unacked.select_messages(raw, only_queue="wave")
    assert list(selected) == [b"tag1"], f"отбор по очереди неверный: {list(selected)}"

    everything = restore_unacked.select_messages(raw)
    assert set(everything) == {b"tag1", b"tag2"}


def test_select_messages_applies_expired_filter() -> None:
    """`--only-expired` отбирает протухшие; живые остаются нетронутыми."""
    now = time.time()
    alive = _payload(
        "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        ["copy", "setka"],
        routing_key="wave",
        expires_in=3600,
    )
    dead = _payload(
        "tasks.parsing_scheduler_tasks.parse_and_publish_theme",
        ["laishevo", "novost"],
        routing_key="wave",
        expires_in=-3600,
    )
    selected = restore_unacked.select_messages(
        {b"alive": alive, b"dead": dead}, only_expired=True, now=now
    )
    assert list(selected) == [b"dead"], f"отбор по сроку неверный: {list(selected)}"


def test_write_back_puts_refreshed_headers_into_the_hash() -> None:
    """Правка срока обязана лежать в хеше: брокер читает тело оттуда.

    Иначе `restore_by_tag` вернёт протухшую задачу — и воркер отбросит её
    как просроченную (так потерялся хвост веера 27.11).
    """
    from kombu.utils.json import loads

    payload = _payload("tasks.digest_tasks.publish_daily_digest_region", ["vp"])
    message, exchange, routing_key = loads(payload)
    headers = message["headers"]
    headers["expires"] = datetime.fromtimestamp(time.time() + 7200).isoformat()

    client = _FakeRedis()
    restore_unacked.write_back(client, "unacked", b"tag1", message, exchange, routing_key)

    assert b"tag1" in client.written
    stored_message, stored_exchange, stored_rk = loads(client.written[b"tag1"])
    assert stored_message["headers"]["expires"] == headers["expires"]
    assert stored_exchange == exchange
    assert stored_rk == routing_key
