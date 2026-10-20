"""Редкий футер «Ленты соседей»: не больше N раз в сутки и с паузой между показами.

Заказ владельца 20.10: «Рубрику "Ленты соседей" не нужно тулить в каждом посте,
это некрасиво… пускайте раза два-три в сутки, не более».

Почему счётчик, а не «час в расписании». У регионов разные сетки волн, и волна,
попавшая в назначенный час, может не дать публикации вовсе. Счётчик же считает
**фактические показы**: он растёт только после успешной публикации сводки с
футером, поэтому «три раза» — это три реальных поста, а не три попытки.

Почему пауза. Без неё три волны подряд (например, 8:40, 9:40, 10:40) выбирают
всю квоту до обеда, и вечером футера нет — при формально соблюдённом лимите
поведение выглядит как «опять в каждом посте».

Состояние живёт в Redis (db=1, как у heartbeat): ключ на регион, TTL трое суток.
Redis недоступен — **показываем футер** (fail-open): пропустить подпись хуже,
чем показать её лишний раз, потому что молчание не отличить от «соседей нет».
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_PER_DAY = 3
DEFAULT_MIN_GAP_HOURS = 4
_KEY_PREFIX = "setka:neighbor_footer"
_TTL_SECONDS = 3 * 24 * 3600


def _redis() -> Any:
    try:
        from modules.notifications.storage import NotificationsStorage

        return NotificationsStorage().redis_client
    except Exception:  # pragma: no cover - инфраструктурный сбой
        logger.warning("redis init failed for neighbor footer", exc_info=True)
        return None


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def max_per_day() -> int:
    return _int_env("NEIGHBOR_FOOTER_MAX_PER_DAY", DEFAULT_MAX_PER_DAY)


def min_gap_hours() -> float:
    return float(_int_env("NEIGHBOR_FOOTER_MIN_GAP_HOURS", DEFAULT_MIN_GAP_HOURS))


def _key(region_code: str) -> str:
    return f"{_KEY_PREFIX}:{region_code}"


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def _load(client: Any, region_code: str) -> Dict[str, Any]:
    raw = client.get(_key(region_code))
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _state(region_code: str, now: float) -> Dict[str, Any]:
    """Прочитать состояние на сегодня (вчерашнее не считается)."""
    client = _redis()
    if client is None:
        return {}
    state = _load(client, region_code)
    if state.get("day") != _today(now):
        return {"day": _today(now), "count": 0, "last": 0}
    return state


def footer_allowed(region_code: str, *, now: Optional[float] = None) -> bool:
    """Пора ли снова показать «Ленты соседей».

    Пустой код региона — не наш случай, но лучше не падать: возвращаем True
    (ограничение — украшение, а не гейт публикации).
    """
    if not region_code:
        return True
    now = time.time() if now is None else now
    try:
        state = _state(region_code, now)
    except Exception:  # pragma: no cover - Redis
        logger.warning("neighbor footer state read failed", exc_info=True)
        return True
    if not state:
        return True
    if int(state.get("count") or 0) >= max_per_day():
        return False
    last = float(state.get("last") or 0)
    if last and now - last < min_gap_hours() * 3600:
        return False
    return True


def register_footer(region_code: str, *, now: Optional[float] = None) -> None:
    """Записать фактический показ футера (после успешной публикации)."""
    if not region_code:
        return
    now = time.time() if now is None else now
    client = _redis()
    if client is None:
        return
    try:
        state = _load(client, region_code)
        if state.get("day") != _today(now):
            state = {"day": _today(now), "count": 0, "last": 0}
        state["count"] = int(state.get("count") or 0) + 1
        state["last"] = now
        client.setex(_key(region_code), _TTL_SECONDS, json.dumps(state))
    except Exception:  # pragma: no cover - Redis
        logger.warning("neighbor footer state write failed", exc_info=True)


def reset(region_code: str) -> None:
    """Сбросить состояние региона (тесты и ручная проверка)."""
    client = _redis()
    if client is None:
        return
    try:
        client.delete(_key(region_code))
    except Exception:  # pragma: no cover
        logger.warning("neighbor footer state reset failed", exc_info=True)
