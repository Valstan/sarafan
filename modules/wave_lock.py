"""Замок волны по району: два слота воркера не выпускают один пост дважды.

**Зачем.** С 21.10 воркер идёт с ``--concurrency=2`` (см. P208): волны разных
районов ждут движок параллельно, и очередь перестаёт расти. Но волны **одного**
района под разными темами идут в расписании вплотную — 11:30 union и 11:40
novost, 12:20 admin / 12:30 sport / 12:40 novost — и при переполненной очереди
накладываются друг на друга.

**Почему это небезопасно без замка.** Дедуп читается в начале волны
(``build_region_dedup_sets`` по ``work_tables.lip``) и пишется в конце. Две
одновременные волны одного района обе видят пост новым и обе его публикуют.
Фолбэк «все сообщества района» (18 % чтений VK) делает совпадение реальным:
одна и та же стена попадает и в свою тему, и в фолбэк чужой.

**Что делает замок.** ``SET setka:wave_lock:<район> <token> NX PX <ttl>``.
Взявший работает; второй ждёт освобождения не дольше ``WAVE_LOCK_WAIT_SECONDS``
и уходит, не публикуя: его тему подхватит следующий слот того же района.
Потерять волну дешевле, чем выпустить дубль — дубль видит читатель.

Замок по району, а не по (район, тема): темы одного района делят один пул
сообществ и один курсор дедупа.

Env:
  WAVE_LOCK_ENABLED=1          # 0 — выключить (аварийный откат без деплоя)
  WAVE_LOCK_TTL_SECONDS=900    # потолок жизни замка (волна + запас)
  WAVE_LOCK_WAIT_SECONDS=90    # сколько ждать освобождения, прежде чем уйти
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

import redis

from config.runtime import REDIS

logger = logging.getLogger(__name__)

KEY_PREFIX = "setka:wave_lock:"

# Снимаем только свой замок: чужой token значит, что TTL истёк и район уже
# забрала следующая волна — снять её замок значило бы разрешить дубль.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_client: Optional[redis.Redis] = None


def _redis() -> redis.Redis:
    """Один клиент на процесс (prefork-ребёнок), ленивый."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS["host"],
            port=int(REDIS["port"]),
            db=int(REDIS.get("db", 0)),
            decode_responses=True,
            socket_timeout=3.0,
        )
    return _client


def _enabled() -> bool:
    return (os.getenv("WAVE_LOCK_ENABLED") or "1").strip() not in {"0", "false", "no"}


def _ttl() -> int:
    try:
        return max(60, int(float(os.getenv("WAVE_LOCK_TTL_SECONDS") or 900)))
    except ValueError:
        return 900


def _wait() -> int:
    try:
        return max(0, int(float(os.getenv("WAVE_LOCK_WAIT_SECONDS") or 90)))
    except ValueError:
        return 90


def acquire(region_code: str) -> Optional[str]:
    """Взять замок района. Возвращает token или ``None`` (занято/выключено/ошибка).

    Ошибка Redis трактуется как «замок взят» с token ``None`` — конвейер не
    должен останавливаться из-за недоступного Redis, а риск дубля в этот момент
    тот же, что был до замка.
    """
    if not _enabled():
        return ""
    token = uuid.uuid4().hex
    deadline = time.monotonic() + _wait()
    key = KEY_PREFIX + region_code
    while True:
        try:
            if _redis().set(key, token, nx=True, px=_ttl() * 1000):
                return token
        except Exception as exc:  # noqa: BLE001 - Redis недоступен: работаем без замка
            logger.warning("wave lock: Redis недоступен (%s), волна идёт без замка", exc)
            return ""
        if time.monotonic() >= deadline:
            return None
        time.sleep(2.0)


def release(region_code: str, token: Optional[str]) -> None:
    """Снять свой замок. Пустой token (замок не брали) — no-op."""
    if not token:
        return
    try:
        _redis().eval(_RELEASE_LUA, 1, KEY_PREFIX + region_code, token)
    except Exception as exc:  # noqa: BLE001 - замок истечёт по TTL
        logger.warning("wave lock: не удалось снять замок района %s: %s", region_code, exc)


def snapshot() -> dict[str, Any]:
    """Кто сейчас держит замки (для диагностики на проде)."""
    try:
        keys = list(_redis().scan_iter(match=KEY_PREFIX + "*", count=200))
        return {"locked": sorted(k[len(KEY_PREFIX) :] for k in keys)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
