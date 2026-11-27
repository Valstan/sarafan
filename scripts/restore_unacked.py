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

**Про срок задачи.** У задачи веера срок — ``expires = countdown + 4 ч``
(``DIGEST_TASK_EXPIRES_SEC``). Хвост, провисевший в ``unacked`` дольше
этого срока, **протух**: вернуть его в очередь как есть — значит отбросить
его как просроченный («Discarding revoked task»), то есть не сделать ничего.
Сухой прогон показывает состояние срока по каждой задаче, а `--refresh-expiry`
продлевает срок протухшим задачам (и `--eta-in` сдвигает «срок наступления»,
чтобы они не начали бить все разом).

    python scripts/restore_unacked.py            # разбор: что и в какую очередь
    python scripts/restore_unacked.py --apply    # вернуть задачи в очереди

    # вернуть только сводку, продлив срок и растянув начало на 15 минут:
    python scripts/restore_unacked.py --apply --queue digest \
        --refresh-expiry --eta-in 900

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
import time
from collections import Counter
from datetime import datetime
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


def describe(payload: bytes) -> Tuple[str, str, str, Dict[str, object]]:
    """Разобрать сообщение kombu: (задача, очередь, аргументы, заголовки)."""
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
    return task, queue, args, headers


def parse_moment(value: object) -> float | None:
    """Момент времени из заголовка celery: unix-число или ISO-строка."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def expiry_state(headers: Dict[str, object], now: float | None = None) -> str:
    """«жива» / «протухла» / «срока нет» — по заголовку ``expires``."""
    moment = parse_moment(headers.get("expires"))
    if moment is None:
        return "срока нет"
    if now is None:
        now = time.time()
    return "протухла" if moment < now else "жива"


def summarize(raw: Dict[bytes, bytes]) -> Counter:
    """Счётчик «задача → сколько таких сообщений лежит в unacked»."""
    counter: Counter = Counter()
    for payload in raw.values():
        try:
            task, _queue, _args, _headers = describe(payload)
        except Exception:  # noqa: BLE001 — битое сообщение не должно ронять разбор
            counter["<не разобрано>"] += 1
            continue
        counter[task] += 1
    return counter


def refresh_deadline(headers: Dict[str, object], *, now: float, ttl_sec: int, eta_in: int) -> bool:
    """Продлить срок задачи и сдвинуть её «срок наступления».

    Зачем: срок задачи веера — ``expires = countdown + 4 ч`` (см.
    ``DIGEST_TASK_EXPIRES_SEC``). Хвост, провисевший в ``unacked`` дольше
    этого срока, **протух**: вернуть его в очередь как есть — значит отбросить
    его как просроченный («Discarding revoked task»), то есть не сделать
    ничего. Тут срок продлевается на ``ttl_sec`` от текущего момента, а
    ``eta`` сдвигается на ``eta_in`` секунд вперёд — чтобы задачи не начали
    бить все разом.

    Возвращает True, если заголовки изменены.
    """
    changed = False
    expires = parse_moment(headers.get("expires"))
    if expires is not None and expires < now:
        headers["expires"] = datetime.fromtimestamp(now + ttl_sec).isoformat()
        changed = True
    if eta_in:
        eta = parse_moment(headers.get("eta"))
        base = eta if (eta is not None and eta > now) else now
        headers["eta"] = datetime.fromtimestamp(base + eta_in).isoformat()
        changed = True
    return changed


def write_back(
    client: object,
    unacked_key: str,
    tag: bytes,
    message: Dict[str, object],
    exchange: str,
    routing_key: str,
) -> None:
    """Записать обновлённое тело сообщения **обратно в хеш** ``unacked``.

    Отдельная функция не для красоты: `restore_by_tag` читает тело из хеша
    заново, поэтому правка срока, сделанная в памяти, до брокера не дойдёт —
    и вернётся протухшая задача, которую воркер отбросит. Именно так потерялся
    хвост веера 27.11: возврат прошёл, а задачи отбросились как просроченные.
    """
    from kombu.utils.json import dumps

    client.hset(unacked_key, tag, dumps([message, exchange, routing_key]))


def restore(
    raw: Dict[bytes, bytes],
    only_queue: str = "",
    *,
    refresh_ttl_sec: int = 0,
    eta_in: int = 0,
    only_expired: bool = False,
) -> List[str]:
    """Вернуть сообщения в их очереди. Возвращает список возвращённых задач."""
    from kombu import Connection

    broker_url = os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL") or ""
    if not broker_url.startswith("redis"):
        broker_url = "redis://127.0.0.1:6379/0"

    restored: List[str] = []
    now = time.time()
    with Connection(broker_url) as conn:
        channel = conn.default_channel
        # `channel.qos` — свойство, которое лениво создаёт QoS нужного класса
        # (у транспорта redis это QoS с restore_by_tag); `channel._qos` до
        # первого потребителя остаётся None.
        qos = channel.qos
        client = channel.client
        from kombu.utils.json import loads

        for tag, payload in raw.items():
            try:
                task, queue, _args, headers = describe(payload)
            except Exception as exc:  # noqa: BLE001
                print("  не разобрано, пропускаю:", type(exc).__name__, str(exc)[:80])
                continue
            if only_queue and queue != only_queue:
                continue
            if only_expired and expiry_state(headers, now) != "протухла":
                continue
            if refresh_ttl_sec or eta_in:
                refresh_deadline(headers, now=now, ttl_sec=refresh_ttl_sec, eta_in=eta_in)
                # ⚠️ **Заголовки надо записать обратно в хеш.** `restore_by_tag`
                # читает тело сообщения из хеша `unacked` заново (не из того
                # объекта, который разобрали мы) — поэтому правка срока в
                # памяти ушла бы в никуда, и вернулась бы протухшая задача,
                # которую воркер отбросит как просроченную.
                message, exchange, routing_key = loads(payload)
                message["headers"] = headers
                write_back(client, qos.unacked_key, tag, message, exchange, routing_key)
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
    parser.add_argument(
        "--refresh-expiry",
        action="store_true",
        help=(
            "продлить срок задач, которые протухли (иначе воркер отбросит их "
            "как просроченные); срок = --expiry-ttl от текущего момента"
        ),
    )
    parser.add_argument(
        "--expiry-ttl",
        type=int,
        default=4 * 3600,
        help="на сколько секунд продлевать срок (по умолчанию 4 ч — как DIGEST_TASK_EXPIRES_SEC)",
    )
    parser.add_argument(
        "--only-expired",
        action="store_true",
        help="вернуть только протухшие задачи (живые не трогать — их и так ждёт брокер)",
    )
    parser.add_argument(
        "--eta-in",
        type=int,
        default=0,
        help=(
            "сдвинуть «срок наступления» на N секунд вперёд, "
            "чтобы задачи не начали бить все разом"
        ),
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

    now = time.time()
    print("сообщений в unacked:", len(raw))
    print("--- по задачам ---")
    for task, count in summarize(raw).most_common():
        print(f"  {task}: {count}")

    print("--- по сроку ---")
    states: Counter = Counter()
    for payload in raw.values():
        try:
            _task, _queue, _args, headers = describe(payload)
            states[expiry_state(headers, now)] += 1
        except Exception:  # noqa: BLE001
            states["срока нет"] += 1
    for state, count in states.most_common():
        print(f"  {state}: {count}")
    if states.get("протухла") and not args.refresh_expiry:
        print(
            "⚠️ Протухшие задачи без --refresh-expiry вернутся в очередь и будут "
            "отброшены как просроченные. Продлить срок: --refresh-expiry"
        )

    if not args.apply:
        print("сухой прогон: ничего не возвращено (для возврата — --apply)")
        return 0

    restored = restore(
        raw,
        only_queue=args.queue,
        refresh_ttl_sec=args.expiry_ttl if args.refresh_expiry else 0,
        eta_in=args.eta_in,
        only_expired=args.only_expired,
    )
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
