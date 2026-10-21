"""Сколько слота воркера съедает каждая задача — измерение, а не оценка.

**Зачем.** Замер P208 (21.10) показал, что единственный слот воркера занят
72 % суток, и что периодика — 31 % этого времени. Числа тогда были получены
разбором ``celery-worker.log`` за 26 часов: разово это сработало, но у
страницы ``/pipeline`` такого источника нет, а цифра, живущая в логе, стареет
молча — лог ротируется, разбор никто не повторяет, и через неделю число
продолжает выглядеть правдоподобно. Это ровно тот класс отказа, ради которого
карта и строилась (см. ``modules/pipeline_map``).

**Что измеряется.** ``task_prerun`` → ``task_postrun`` в процессе-ребёнке:
время от начала исполнения до конца. Ожидание в очереди сюда не входит — и
это правильно, потому что занимает слот именно исполнение, а не стояние.

**Где живёт.** Redis, список на имя задачи, ``SAMPLE_LIMIT`` последних
прогонов. Средняя за всё время скрыла бы, что задача стала вдвое дольше
вчера; последние прогоны — это то, что нужно, когда смотришь, «почему сейчас
очередь». Ключ протухает за ``TTL_SECONDS``: выключенная из расписания задача
перестаёт показывать вчерашнюю правду через две недели, а не навсегда.

**Право на отказ.** Прибор не имеет права уронить задачу: любая ошибка Redis
уходит в debug, запись молча пропускается. Пустая таблица на странице честнее
упавшего воркера.

Env:
  TASK_TIMINGS_ENABLED=0   # выключить прибор, не трогая воркер
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

import redis

from config.runtime import REDIS

logger = logging.getLogger(__name__)

KEY_PREFIX = "setka:task_timings:"

# Сколько последних прогонов держим на задачу. 60 хватает, чтобы средняя не
# прыгала от одного выброса, и мало, чтобы список не рос.
SAMPLE_LIMIT = 60

# Две недели: задача, выключенная из расписания, обязана исчезнуть из карты
# сама — иначе карта показывает работу, которой нет.
TTL_SECONDS = 14 * 24 * 3600

_client: Optional[redis.Redis] = None


def _redis() -> redis.Redis:
    """Один клиент на процесс, ленивый (тот же приём, что в ``wave_lock``)."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=REDIS["host"],
            port=int(REDIS["port"]),
            db=int(REDIS.get("db", 0)),
            decode_responses=True,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
        )
    return _client


def reset_client() -> None:
    """Сбросить ленивый клиент — для тестов и для смены REDIS в рантайме."""
    global _client
    _client = None


def enabled() -> bool:
    return (os.getenv("TASK_TIMINGS_ENABLED") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def key(task_name: str) -> str:
    return f"{KEY_PREFIX}{task_name}"


def record(task_name: str, seconds: Any) -> None:
    """Записать длительность одного прогона. Никогда не бросает."""
    if not task_name or not enabled():
        return
    try:
        value = round(float(seconds), 3)
    except (TypeError, ValueError):
        return
    if value < 0:
        return
    try:
        client = _redis()
        redis_key = key(task_name)
        pipe = client.pipeline()
        pipe.rpush(redis_key, value)
        pipe.ltrim(redis_key, -SAMPLE_LIMIT, -1)
        pipe.expire(redis_key, TTL_SECONDS)
        pipe.execute()
    except Exception:  # noqa: BLE001 — прибор молчит, задача продолжается
        logger.debug("task timings: запись %s не удалась", task_name, exc_info=True)


def _summary(values: Iterable[str]) -> Optional[Dict[str, float]]:
    durations: List[float] = []
    for raw in values or []:
        try:
            durations.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not durations:
        return None
    return {
        "samples": len(durations),
        "avg_seconds": round(sum(durations) / len(durations), 2),
        "max_seconds": round(max(durations), 2),
        "last_seconds": round(durations[-1], 2),
    }


def snapshot(task_names: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Сводка по задачам: ``{имя: {samples, avg_seconds, max_seconds, last_seconds}}``.

    ``task_names=None`` — прочитать всё, что есть в Redis (страница так не
    делает: она спрашивает только то, что печатает).

    Пустой словарь означает «измерений ещё нет» — это не ошибка и не ноль:
    страница обязана показать прочерк, а не выдать «0 с» за факт.
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        client = _redis()
        names: List[str]
        if task_names is None:
            names = [
                str(raw)[len(KEY_PREFIX) :]
                for raw in client.scan_iter(match=f"{KEY_PREFIX}*", count=200)
            ]
        else:
            names = [str(n) for n in task_names if n]
        if not names:
            return out
        pipe = client.pipeline()
        for name in names:
            pipe.lrange(key(name), 0, -1)
        for name, values in zip(names, pipe.execute()):
            summary = _summary(values)
            if summary:
                out[name] = summary
    except Exception:  # noqa: BLE001 — без Redis страница покажет прочерки
        logger.debug("task timings: чтение не удалось", exc_info=True)
    return out


__all__ = [
    "KEY_PREFIX",
    "SAMPLE_LIMIT",
    "TTL_SECONDS",
    "enabled",
    "key",
    "record",
    "reset_client",
    "snapshot",
]
