#!/usr/bin/env python3
"""Возврат задач, зависших в redis `unacked` после жёсткой остановки воркера.

**Что это чинит.** Воркер забирает задачу из очереди и подтверждает её (ack)
после выполнения. Если воркер снят жёстко (`Stop-Process`, как это делает
`infra/windows/stop-unit.ps1`), подтверждение не успевает уйти: сообщение
остаётся в хеше `unacked` и в очереди его больше нет. Компоновщик (kombu)
возвращает такие сообщения сам — но по таймауту видимости, и только для
доставок **своего** потребителя. Доставки погибшего воркера не принадлежат
никому, и на стенде они лежали больше часа: 58 задач веера вечерней сводки
(26.11) так и не вернулись в очередь — регионы не получили сборник.

**Как чинит.** Тем же механизмом, которым пользуется сам компоновщик:
`QoS.restore_by_tag(tag)` атомарно достаёт сообщение из `unacked`, чистит
индексы и кладёт его обратно в **его** очередь — с теми же заголовками
(ETA, `since`, приоритет). Ничего не пересоздаётся и не дублируется.

**Почему сухой прогон по умолчанию.** Операция пишет в боевую очередь: как
только задачи вернулись, их начнёт выполнять воркер. Поэтому без `--apply`
скрипт только разбирает содержимое `unacked`.

    python scripts/restore_unacked.py            # разбор: что и в какую очередь
    python scripts/restore_unacked.py --apply    # вернуть задачи в очереди

Ограничения (осознанные):
* возвращаются только задачи из `unacked` — то, что брокер уже отдал воркеру
  и не получил подтверждения; обычные задачи в очередях не трогаются;
* повторный запуск безопасен: возвращённое сообщение уже не в `unacked`;
* если в `unacked` лежат задачи разных юнитов — вернутся все (иначе часть
  хвоста осталась бы висеть), поэтому отчёт печатается целиком.

Канон: P256 (процедура остановки юнитов), P253 (правки в бою), P254 (чтение
живых логов).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Очереди, из которых стоит возвращать хвост сбора. Пусто — все.
DIGEST_QUEUE = "digest"


def _repo_path() -> str:
    return str(REPO_ROOT)


def load_env_files() -> int:
    """Подхватить env-файл локального стенда (на Windows — единственный источник)."""
    env_files = os.environ.get("SETKA_ENV_FILES")
    if not env_files:
        default = REPO_ROOT / "config" / "local-stand.env"
        if default.exists():
            os.environ["SETKA_ENV_FILES"] = str(default)
    try:
        from infra.windows.local_env import load_local_env
    except ImportError:
        return 0
    return load_local_env()


def describe(payload: bytes) -> Tuple[str, str, str]:
    """Разобрать сообщение kombu: (задача, очередь, аргументы)."""
    from kombu.utils.json import loads

    message, _exchange, routing_key = loads(payload)
    headers = message.get("headers") or {}
    task = str(headers.get("task") or "?")
    queue = str(routing_key or headers.get("queue") or "?")
    args = ""
    try:
        body = json.loads(base64.b64decode(message["body"]))
        args = json.dumps(body[1], ensure_ascii=False)[:120]
    except Exception:  # noqa: BLE001 — аргументы не критичны для отчёта
        pass
    return task, queue, args


def summarize(raw: Dict[bytes, bytes]) -> Counter:
    """Счётчик «задача → сколько таких сообщений лежит в unacked»."""
    counter: Counter = Counter()
    for payload in raw.values():
        try:
            task, _queue, _args = describe(payload)
        except Exception:  # noqa: BLE001 — битое сообщение не должно ронять разбор
            counter["<не разобрано>"] += 1
            continue
        counter[task] += 1
    return counter


def restore(raw: Dict[bytes, bytes], only_queue: str = "") -> List[str]:
    """Вернуть сообщения в их очереди. Возвращает список возвращённых задач."""
    from kombu import Connection

    broker_url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL") or ""
    if not broker_url.startswith("redis"):
        broker_url = "redis://127.0.0.1:6379/0"

    restored: List[str] = []
    with Connection(broker_url) as conn:
        qos = conn.default_channel._qos
        for tag, payload in raw.items():
            try:
                task, queue, _args = describe(payload)
            except Exception as exc:  # noqa: BLE001
                print("  не разобрано, пропускаю:", type(exc).__name__, str(exc)[:80])
                continue
            if only_queue and queue != only_queue:
                continue
            try:
                qos.restore_by_tag(tag)
                restored.append(task)
            except Exception as exc:  # noqa: BLE001
                print("  не вернули:", task, type(exc).__name__, str(exc)[:100])
    return restored


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Возврат задач, зависших в redis unacked после жёсткой остановки воркера."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="вернуть задачи в очереди (без флага — только разбор, ничего не пишется)",
    )
    parser.add_argument(
        "--queue",
        default="",
        help="вернуть только задачи этой очереди (например digest); по умолчанию — все",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, _repo_path())
    load_env_files()

    from kombu import Connection

    broker_url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL") or ""
    if not broker_url.startswith("redis"):
        broker_url = "redis://127.0.0.1:6379/0"

    with Connection(broker_url) as conn:
        client = conn.default_channel.client
        raw = client.hgetall("unacked")

    if not raw:
        print("unacked пуст — возвращать нечего")
        return 0

    print("сообщений в unacked:", len(raw))
    print("--- по задачам ---")
    for task, count in summarize(raw).most_common():
        print(f"  {task}: {count}")

    if not args.apply:
        print("сухой прогон: ничего не возвращено (для возврата — --apply)")
        return 0

    restored = restore(raw, only_queue=args.queue)
    print("--- возвращено ---")
    for task, count in Counter(restored).most_common():
        print(f"  {task}: {count}")

    with Connection(broker_url) as conn:
        client = conn.default_channel.client
        print(
            "unacked после: %d | в очередях: digest=%d wave=%d celery=%d"
            % (
                client.hlen("unacked"),
                client.llen("digest"),
                client.llen("wave"),
                client.llen("celery"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
