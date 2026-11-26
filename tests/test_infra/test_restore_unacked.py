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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import restore_unacked  # noqa: E402
from kombu.utils.json import dumps  # noqa: E402


def _payload(task: str, args: list, routing_key: str = "digest") -> bytes:
    """Сообщение в том виде, в каком kombu кладёт его в хеш `unacked`."""
    body = base64.b64encode(json.dumps([task, args, {}]).encode("utf-8")).decode("ascii")
    message = {
        "body": body,
        "headers": {"task": task},
        "properties": {"delivery_info": {"routing_key": routing_key}},
        "content-type": "application/json",
        "content-encoding": "utf-8",
    }
    return dumps([message, "", routing_key])


def test_describe_reads_task_and_queue() -> None:
    payload = _payload(
        "tasks.digest_tasks.publish_daily_digest_region", ["vp", "2026-11-25T21:00:00"]
    )
    task, queue, args = restore_unacked.describe(payload)
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
    task, queue, _args = restore_unacked.describe(payload)
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
