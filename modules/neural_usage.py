"""Единый журнал вызовов нейросетей: кто, когда, зачем и сколько.

**Зачем он.** До 17.11 учёт вёлся разрозненно: расход MiniMax лежал в
``logs/minimax_usage.jsonl`` (свой формат), а расход Дипсика — только строкой
в логе воркера (``deepseek-usage label=...``). Из-за этого «куда уходит трафик
и на что» приходилось собирать по обрывкам лога: на панели «Движок
классификации» стоял счётчик вердиктов **одного** потока (классификация), а
рядом — «Минимакс за сутки», и цифры смотрелись как противоречие (окно
скользящее против календарных суток, вызовы против вердиктов).

Журнал закрывает это одним потоком: **каждый** вызов любого движка пишет
одну строку JSON с полями «движок · поток · когда · объём · исход». По нему
считаются и расход, и загрузка по потокам, и доля платного движка.

**Сутки — московские.** В проекте время в базе и в журналах лежит в UTC, а
человек думает сутками по Москве: день на панели, начавшийся в 00:00 UTC,
это 03:00 МСК, и «сегодня» в таблице на третьи сутки сдвинуто. Поэтому
``msk_day()`` переводит метку времени в ``Europe/Moscow`` **один раз** — на
границе журнала, а не в каждом потребителе.

**Журнал — диагностика, а не летопись.** Строка пишется best-effort: сбой записи
не должен ронять вызов (деньги потрачены, вердикт нужен). Ротация по объёму
(``NEURAL_USAGE_MAX_BYTES``) — чтобы файл не рос бесконечно; две генерации
хватают на историю, которую смотрит панель.

Формат строки (одна на вызов)::

    {"ts": "2026-11-17T14:29:37.335+00:00", "engine": "deepseek",
     "label": "post_rewrite", "model": "deepseek-flash", "ok": true,
     "reason": "", "posts": 0, "duration_ms": 45123,
     "prompt_tokens": 9441, "cache_hit": 8192, "cache_miss": 1249,
     "completion_tokens": 420, "account": "", "points": 0}
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: Московская зона. Россия с 2014 года живёт без перехода на летнее время, так
#: что ``Europe/Moscow`` — это ровно UTC+3 круглый год.
MSK = ZoneInfo("Europe/Moscow")

#: Куда пишем. Рядом с прочими runtime-файлами, в git не едет.
ENV_USAGE_LOG = "NEURAL_USAGE_LOG"
DEFAULT_USAGE_LOG = "logs/neural_usage.jsonl"

#: После какого объёма файл ротируется (два поколения: текущий + ``.1``).
ENV_MAX_BYTES = "NEURAL_USAGE_MAX_BYTES"
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

#: Человеческие названия потоков. Ключ — значение ``label`` из вызова.
#:
#: Поток — это **зачем** звали нейросеть, а не кто звал: один и тот же движок
#: работает на классификацию, на переписывание постов и на конвейер сайтов, и
#: смешивать их в одну цифру нельзя (замер 16.11: 372 вызова Дипсика на
#: классификацию выглядели как «Дипсик много тратит», хотя это был другой
#: поток).
FLOW_LABELS: Dict[str, str] = {
    "headless": "Классификация постов",
    "post_rewrite": "Переписывание постов",
    "daily_digest": "Вечерний сборник",
    "conveyor": "Конвейер сайтов",
    "ai_drafter": "Черновики уведомлений",
    "distill": "Дистилляция правил",
    "discovery": "Подбор сообществ",
    "quota-probe": "Проба квоты",
    "deepseek": "Дипсик (поток не назван)",
}

#: Названия движков — как их знает человек, а не как они лежат в моделях.
ENGINE_LABELS: Dict[str, str] = {
    "deepseek": "Дипсик (платно)",
    "minimax": "Минимакс (очки)",
    "opencode": "Опенкод (бесплатно)",
    "unknown": "Неизвестно",
}


def flow_label(label: str) -> str:
    """Человеческое название потока; неизвестный — как есть."""
    name = str(label or "").strip()
    return FLOW_LABELS.get(name, name or "поток не назван")


def engine_label(engine: str) -> str:
    """Человеческое название движка."""
    return ENGINE_LABELS.get(str(engine or "").strip(), "Неизвестно")


def usage_log_path() -> Path:
    """Путь к журналу (env перекрывает дефолт — нужно тестам и боксу)."""
    raw = (os.getenv(ENV_USAGE_LOG) or "").strip()
    return Path(raw) if raw else Path(DEFAULT_USAGE_LOG)


def _max_bytes() -> int:
    try:
        return max(1024 * 1024, int(os.getenv(ENV_MAX_BYTES, "") or DEFAULT_MAX_BYTES))
    except ValueError:
        return DEFAULT_MAX_BYTES


def msk_day(ts: datetime) -> str:
    """Московские сутки метки времени как ``YYYY-MM-DD``.

    Наивное время из БД считается **UTC** (конвенция проекта: «в базе UTC без
    зоны»). Метка с зоной — уже в своей зоне, её приводим к Москве.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(MSK).strftime("%Y-%m-%d")


def msk_today() -> str:
    """Сегодняшние московские сутки."""
    return msk_day(datetime.now(timezone.utc))


def record(
    *,
    engine: str,
    label: str = "",
    model: str = "",
    ok: bool = True,
    reason: str = "",
    detail: str = "",
    posts: int = 0,
    duration_ms: int = 0,
    prompt_tokens: Optional[int] = None,
    cache_hit: Optional[int] = None,
    cache_miss: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    account: str = "",
    points: int = 0,
) -> None:
    """Записать один вызов в журнал.

    **Сбой записи не должен ломать вызов.** Деньги потрачены, вердикт нужен —
    поэтому любая ошибка здесь гасится и уходит в лог на уровне warning.
    """
    row: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "engine": str(engine or "unknown"),
        "label": str(label or ""),
        "model": str(model or ""),
        "ok": bool(ok),
        "reason": str(reason or "")[:80],
        "posts": int(posts or 0),
        "duration_ms": int(duration_ms or 0),
        "account": str(account or ""),
        "points": int(points or 0),
    }
    if detail:
        row["detail"] = str(detail)[:300]
    for key, value in (
        ("prompt_tokens", prompt_tokens),
        ("cache_hit", cache_hit),
        ("cache_miss", cache_miss),
        ("completion_tokens", completion_tokens),
    ):
        if value is not None:
            row[key] = int(value)
    try:
        _append(row)
    except Exception as exc:  # noqa: BLE001 — учёт не важнее ответа
        logger.warning("neural usage: не удалось записать журнал (%s)", exc)


def _append(row: Dict[str, Any]) -> None:
    path = usage_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rotate_if_needed(path: Path) -> None:
    """Ротация по объёму: ``.1`` — прошлое поколение, ``.2`` — стирается."""
    try:
        if not path.exists() or path.stat().st_size < _max_bytes():
            return
        old = path.with_suffix(path.suffix + ".2")
        if old.exists():
            old.unlink()
        prev = path.with_suffix(path.suffix + ".1")
        if prev.exists():
            prev.replace(old)
        path.replace(prev)
    except OSError as exc:
        logger.warning("neural usage: ротация журнала не удалась (%s)", exc)


def read_records(days: int = 30, max_bytes: int = 0) -> List[Dict[str, Any]]:
    """Прочитать журнал за последние ``days`` московских суток.

    ``days`` — **календарные** сутки, включая сегодняшние: ``days=1`` — это
    «сегодня по Москве», а не «последние 24 часа». Разница видна на стыке
    суток: без неё «сегодня» в панели съезжало на третьи сутки (начало дня в
    00:00 UTC — это 03:00 МСК).

    ``max_bytes`` — читать только хвост файла (столько байт с конца). Файл
    растёт монотонно, а читать его целиком на каждый чек платных вызовов
    нельзя: при лежащем MiniMax это 600 чтений в сутки.

    Битая строка не должна ронять чтение — журнал пишется из разных процессов,
    и оборванная строка (kill во время записи) — обычное дело.
    """
    path = usage_log_path()
    if not path.exists():
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 1)) - 1)).astimezone(
        MSK
    )
    since_day = since.strftime("%Y-%m-%d")
    out: List[Dict[str, Any]] = []
    try:
        for line in _tail_lines(path, max_bytes=max_bytes):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            ts = _row_ts(row)
            if ts is None:
                continue
            if msk_day(ts) < since_day:
                continue
            row["day"] = msk_day(ts)
            out.append(row)
    except OSError as exc:
        logger.warning("neural usage: журнал не прочитан (%s)", exc)
    return out


def _tail_lines(path: Path, max_bytes: int = 0) -> List[str]:
    """Строки файла (или только его хвост, если задан ``max_bytes``)."""
    limit = max(0, int(max_bytes or 0))
    with path.open("rb") as fh:
        if limit and path.stat().st_size > limit:
            fh.seek(-limit, os.SEEK_END)
            fh.readline()  # первая строка хвоста может быть оборвана — выкидываем
        data = fh.read()
    return data.decode("utf-8", "replace").splitlines()


def _row_ts(row: Dict[str, Any]) -> Optional[datetime]:
    raw = str(row.get("ts") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def aggregate_by_day(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Свести журнал в таблицу «сутки × поток × движок».

    Строка на тройку (день, поток, движок): сколько вызовов, сколько успешных,
    сколько постов в них было, сколько токенов ушло на промпт и ответ.
    """
    buckets: Dict[Tuple[str, str, str], Dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "ok": 0,
            "failed": 0,
            "posts": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "duration_ms": 0,
            "reasons": Counter(),
        }
    )
    for row in records or []:
        day = str(row.get("day") or "")
        if not day:
            continue
        key = (day, str(row.get("label") or ""), str(row.get("engine") or "unknown"))
        bucket = buckets[key]
        bucket["calls"] += 1
        if row.get("ok"):
            bucket["ok"] += 1
        else:
            bucket["failed"] += 1
            reason = str(row.get("reason") or "").strip()
            if reason:
                bucket["reasons"][reason] += 1
        bucket["posts"] += int(row.get("posts") or 0)
        bucket["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(row.get("completion_tokens") or 0)
        bucket["duration_ms"] += int(row.get("duration_ms") or 0)

    out: List[Dict[str, Any]] = []
    for (day, label, engine), bucket in sorted(
        buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        out.append(
            {
                "day": day,
                "label": label,
                "flow": flow_label(label),
                "engine": engine,
                "engine_label": engine_label(engine),
                "calls": bucket["calls"],
                "ok": bucket["ok"],
                "failed": bucket["failed"],
                "posts": bucket["posts"],
                "prompt_tokens": bucket["prompt_tokens"],
                "completion_tokens": bucket["completion_tokens"],
                "duration_ms": bucket["duration_ms"],
                "reasons": dict(bucket["reasons"].most_common(5)),
            }
        )
    return out


#: Сколько байт журнала читаем, когда считаем платные вызовы. Сутки журнала —
#: порядка 300 КБ; 4 МБ хватает с запасом, а файл не растёт в памяти.
_COUNT_TAIL_BYTES = 4 * 1024 * 1024

#: Кэш счётчика в процессе: пересчёт раз в минуту. Счётчик нужен на каждый
#: отказ движка, а читать журнал на каждом — лишний I/O в самый горячий момент.
_count_cache: Dict[Tuple[str, str], Tuple[float, int]] = {}


def summarize(
    records: Iterable[Dict[str, Any]],
    *,
    engine: str = "",
    day: str = "",
    label: str = "",
) -> Dict[str, Any]:
    """Свод по журналу: вызовы, успех, отказы, посты, токены, причины.

    Пустые фильтры означают «всё» — так один и тот же счётчик обслуживает и
    блок «Минимакс сегодня», и «платные вызовы за сутки».
    """
    calls = 0
    ok = 0
    failed = 0
    posts = 0
    prompt_tokens = 0
    completion_tokens = 0
    duration_ms = 0
    reasons: Counter = Counter()
    for row in records or []:
        if engine and str(row.get("engine") or "") != engine:
            continue
        if day and str(row.get("day") or "") != day:
            continue
        if label and str(row.get("label") or "") != label:
            continue
        calls += 1
        if row.get("ok"):
            ok += 1
        else:
            failed += 1
            reason = str(row.get("reason") or "").strip()
            reasons[reason or "без причины"] += 1
        posts += int(row.get("posts") or 0)
        prompt_tokens += int(row.get("prompt_tokens") or 0)
        completion_tokens += int(row.get("completion_tokens") or 0)
        duration_ms += int(row.get("duration_ms") or 0)
    return {
        "calls": calls,
        "ok": ok,
        "failed": failed,
        "ok_pct": round(100.0 * ok / calls, 1) if calls else None,
        "posts": posts,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "duration_ms": duration_ms,
        "reasons": dict(reasons.most_common(6)),
    }


def count_paid_calls(day: str = "", label: str = "headless", max_age: float = 60.0) -> int:
    """Сколько платных вызовов ушло за московские сутки.

    Считается по журналу, а не по своему счётчику: значит, что видит человек в
    панели, и что реально потрачено, — одна и та же величина.
    """
    target = day or msk_today()
    key = (target, str(label or ""))
    cached = _count_cache.get(key)
    now = time.monotonic()
    if cached and (now - cached[0]) < max_age:
        return cached[1]
    n = 0
    for row in read_records(days=2, max_bytes=_COUNT_TAIL_BYTES):
        if str(row.get("day") or "") != target:
            continue
        if str(row.get("engine") or "") != "deepseek":
            continue
        if label and str(row.get("label") or "") != label:
            continue
        n += 1
    _count_cache[key] = (now, n)
    return n


__all__ = [
    "ENGINE_LABELS",
    "FLOW_LABELS",
    "aggregate_by_day",
    "count_paid_calls",
    "engine_label",
    "flow_label",
    "msk_day",
    "msk_today",
    "read_records",
    "record",
    "usage_log_path",
]
