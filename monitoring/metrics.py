"""
Prometheus Metrics for SETKA
Comprehensive monitoring and observability

При выставленной env ``PROMETHEUS_MULTIPROC_DIR`` метрики хранятся в
shared mmap-файлах и агрегируются через ``MultiProcessCollector`` —
без этого Counter'ы из Celery worker'а не доходят до web-эндпоинта
``/metrics``. См. ``monitoring/README.md`` §Multiprocess.
"""

import logging
import os
import re
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    multiprocess,
)

logger = logging.getLogger(__name__)


def _is_multiproc() -> bool:
    return bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))


# =============================================================================
# API METRICS
# =============================================================================

# Request counters
api_requests_total = Counter(
    "setka_api_requests_total", "Total API requests", ["method", "endpoint", "status"]
)

api_requests_in_progress = Gauge(
    "setka_api_requests_in_progress",
    "API requests currently in progress",
    multiprocess_mode="livesum",
)

# Latency histogram
api_request_duration_seconds = Histogram(
    "setka_api_request_duration_seconds",
    "API request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# =============================================================================
# CACHE METRICS
# =============================================================================

cache_hits_total = Counter("setka_cache_hits_total", "Total cache hits", ["cache_type"])

cache_misses_total = Counter("setka_cache_misses_total", "Total cache misses", ["cache_type"])

cache_size_bytes = Gauge(
    "setka_cache_size_bytes", "Current cache size in bytes", multiprocess_mode="livesum"
)

# =============================================================================
# VK API METRICS
# =============================================================================

vk_api_requests_total = Counter(
    "setka_vk_api_requests_total", "Total VK API requests", ["method", "status"]
)

vk_api_request_duration_seconds = Histogram(
    "setka_vk_api_request_duration_seconds",
    "VK API request duration",
    ["method"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

vk_api_errors_total = Counter("setka_vk_api_errors_total", "Total VK API errors", ["error_code"])

vk_api_rate_limit_hits = Counter("setka_vk_api_rate_limit_hits_total", "VK API rate limit hits")

# =============================================================================
# NOTIFICATIONS METRICS (etap 5)
# =============================================================================

# Outcome counter for the three hourly checks. Lets us alert on:
#   - 3+ runs in a row with result="error"  → token health degraded
#   - sudden drop in "items_found" sum      → VK auth broken silently
notifications_check_total = Counter(
    "setka_notifications_check_total",
    "VK notifications check runs",
    ["check_type", "result"],  # check_type: suggested|messages|comments
    # result: ok|empty|error|denied
)

notifications_check_duration_seconds = Histogram(
    "setka_notifications_check_duration_seconds",
    "Duration of one notifications check run",
    ["check_type"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60),
)

notifications_items_found_total = Counter(
    "setka_notifications_items_found_total",
    "Items found by notification checks (suggested posts / unread / comments)",
    ["check_type"],
)

# Gauge that flips to 1 when the last N consecutive auto-runs of a given type
# all returned ZERO items — useful for alerting on broken-token symptoms.
notifications_zero_streak = Gauge(
    "setka_notifications_zero_streak",
    "Consecutive auto-runs that returned 0 items for the given check_type",
    ["check_type"],
    multiprocess_mode="livesum",
)

# =============================================================================
# DATABASE METRICS
# =============================================================================

db_queries_total = Counter("setka_db_queries_total", "Total database queries", ["operation"])

db_query_duration_seconds = Histogram(
    "setka_db_query_duration_seconds",
    "Database query duration",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5, 1.0),
)

db_connections_active = Gauge(
    "setka_db_connections_active", "Active database connections", multiprocess_mode="livesum"
)

# =============================================================================
# BUSINESS METRICS
# =============================================================================

posts_processed_total = Counter("setka_posts_processed_total", "Total posts processed", ["status"])

posts_published_total = Counter("setka_posts_published_total", "Total posts published", ["channel"])

# Сводки по регионам и темам — для Grafana «состояние сводок».
digest_published_total = Counter(
    "setka_digest_published_total",
    "Опубликованные сводки per region per topic",
    ["region", "topic", "result"],  # result: success | empty | failed
)

digest_last_published_timestamp = Gauge(
    "setka_digest_last_published_timestamp",
    "Unix timestamp последней успешной публикации сводки per region per topic",
    ["region", "topic"],
    # ``max`` корректен для timestamp: значение монотонно растёт, поэтому свежее
    # время публикации всегда «выигрывает» при агрегации из нескольких процессов
    # (web + celery worker) даже если mmap-файл умершего PID не подчистили.
    multiprocess_mode="max",
)

communities_monitored = Gauge(
    "setka_communities_monitored",
    "Number of communities being monitored",
    multiprocess_mode="livesum",
)

regions_active = Gauge(
    "setka_regions_active", "Number of active regions", multiprocess_mode="livesum"
)

# =============================================================================
# SYSTEM METRICS
# =============================================================================

# Info-метрики prometheus_client multiproc-mode не поддерживает — определяем
# только в single-process режиме. Сейчас никем не используется, но оставляем
# для совместимости импорта.
if not _is_multiproc():
    system_info = Info("setka_system", "SETKA system information")
else:

    class _InfoStub:
        def info(self, *args, **kwargs) -> None:
            pass

    system_info = _InfoStub()

errors_total = Counter("setka_errors_total", "Total errors", ["component", "error_type"])

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def track_cache_hit(cache_type: str = "redis"):
    """Record a cache hit"""
    cache_hits_total.labels(cache_type=cache_type).inc()


def track_cache_miss(cache_type: str = "redis"):
    """Record a cache miss"""
    cache_misses_total.labels(cache_type=cache_type).inc()


def publish_result_label(publish_result) -> str:
    """Свести результат ``VKPublisher.publish_bulletin()`` к метке для метрик.

    ``publish_bulletin`` возвращает **dict** ``{"success": bool, ...}``. Call-sites
    исторически обращались к ``publish_result.success`` как к атрибуту объекта,
    из-за чего на КАЖДОЙ успешной публикации падал ``AttributeError: 'dict'
    object has no attribute 'success'`` — до вызова ``track_digest_published``.
    Итог: heartbeat #018 не писался, watchdog был молча мёртв (инцидент
    2026-06-05, вскрыт дашбордом + WARNING-логами). Хелпер терпим к обоим
    форматам (dict и объект с ``.success``), чтобы такого не повторилось.

    Возвращает ``"success"`` | ``"failed"``.
    """
    if isinstance(publish_result, dict):
        return "success" if publish_result.get("success") else "failed"
    return "success" if getattr(publish_result, "success", False) else "failed"


def track_digest_published(region: str, topic: str, result: str = "success") -> None:
    """Зафиксировать факт публикации сводки.

    Args:
        region: ``Region.code`` (например, ``"tuzha"``, ``"mi"``).
        topic: имя темы (``"novost"``, ``"mourning"``, ``"reklama"``, …).
        result: ``"success"`` | ``"empty"`` | ``"failed"``. Для ``"success"`` —
            обновляется ``digest_last_published_timestamp`` (Gauge с unix-ts).
            Для остальных — только Counter (нужно для алёртов «давно не
            публиковали успешно»).
    """
    # ── Redis-heartbeat пишем ПЕРВЫМ и НЕЗАВИСИМО от Prometheus ──────────────
    # Heartbeat — это НАДЁЖНЫЙ сигнал для watchdog'а «давно нет сводок»
    # (#018); Prometheus multiproc-gauge на проде исторически ненадёжен (mmap,
    # mode='max', боль #229) и его вызов может бросить исключение. Раньше
    # heartbeat стоял ПОСЛЕ ``gauge.set()`` в общей обёртке, и сбой Prometheus
    # глушил heartbeat — watchdog молча не получал данных на проде с 2026-06-03
    # (вскрыто дашбордом 2026-06-05). Поэтому: сначала heartbeat в своём
    # try/except, затем — отдельно — Prometheus. Логируем сбой на WARNING
    # (раньше debug → невидимо при LOG_LEVEL=INFO, оттого и не замечали).
    if result == "success":
        try:
            from modules.bulletin_heartbeat import mark_published

            mark_published(topic, region=region)
        except Exception:  # pragma: no cover - наблюдаемость не должна валить публикацию
            logger.warning("bulletin heartbeat write failed (topic=%s)", topic, exc_info=True)

    # ── Prometheus — best-effort, отдельно: его сбой больше не трогает heartbeat ─
    try:
        digest_published_total.labels(region=region, topic=topic, result=result).inc()
        if result == "success":
            digest_last_published_timestamp.labels(region=region, topic=topic).set(time.time())
    except Exception:  # pragma: no cover - метрики никогда не должны валить публикацию
        logger.warning("prometheus bulletin metric failed (topic=%s)", topic, exc_info=True)


def track_error(component: str, error_type: str):
    """
    Record an error

    Args:
        component: Component where error occurred
        error_type: Type of error
    """
    errors_total.labels(component=component, error_type=error_type).inc()

    logger.error(f"Error tracked: {component}.{error_type}")


async def get_cache_metrics():
    """
    Get current cache metrics

    Returns:
        Dict with cache statistics
    """
    try:
        from utils.cache import get_cache

        cache = get_cache()
        stats = await cache.get_stats()

        # Update gauge
        if "memory_used" in stats:
            # Parse memory (e.g., "1.5M" -> bytes)
            memory_str = stats["memory_used"]
            if memory_str != "N/A":
                # Simple parsing (improve if needed)
                cache_size_bytes.set(0)  # Placeholder

        return stats
    except Exception as e:
        logger.error(f"Failed to get cache metrics: {e}")
        return {}


async def update_business_metrics():
    """Update business metrics from database"""
    try:
        from sqlalchemy import func, select

        from database.connection import AsyncSessionLocal
        from database.models import Community, Region

        async with AsyncSessionLocal() as session:
            # Count active communities
            result = await session.execute(
                select(func.count(Community.id)).where(Community.is_active.is_(True))
            )
            count = result.scalar()
            communities_monitored.set(count)

            # Count active regions
            result = await session.execute(
                select(func.count(Region.id)).where(Region.is_active.is_(True))
            )
            count = result.scalar()
            regions_active.set(count)

    except Exception as e:
        logger.error(f"Failed to update business metrics: {e}")


# =============================================================================
# METRICS ENDPOINT
# =============================================================================


async def get_metrics():
    """
    Get Prometheus metrics in text format.

    В multiproc-режиме (``PROMETHEUS_MULTIPROC_DIR`` выставлен) собираем
    данные через ``MultiProcessCollector`` поверх временной ``CollectorRegistry``,
    чтобы видеть счётчики из всех процессов (web + celery worker).

    Returns:
        Tuple of (content, content_type)
    """
    # Update cache metrics before export
    await get_cache_metrics()

    if _is_multiproc():
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST

    return generate_latest(), CONTENT_TYPE_LATEST


# =============================================================================
# MULTIPROC-ЧИСТКА ПРИ СТАРТЕ
# =============================================================================

# Суффикс PID — единственный
# способ отличить живые файлы от трупов, и он врёт в обе стороны, поэтому
# проверяем два условия, а не одно (замер на проде 2026-10-06).
_MULTIPROC_PID_SUFFIX = re.compile(r"_(\d+)\.db$")
# Подстроки командной строки, по которым процесс опознаётся как наш. Проверено
# на проде: `.../SETKA/venv/bin/python ... uvicorn/celery`, `.../SETKA/...bot`.
_OWN_PROCESS_MARKERS = ("SETKA", "setka", "uvicorn", "celery")


def _default_pid_alive(pid: int) -> bool:
    """Жив ли процесс (``kill -0``; чужой UID без прав — считаем живым)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _default_pid_cmdline(pid: int) -> str:
    """Командная строка процесса одной строкой; нет доступа — пусто."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def prune_stale_multiproc_files(
    directory: str | None = None,
    *,
    _pid_alive=None,
    _pid_cmdline=None,
    _own_pid: int | None = None,
) -> dict:
    """Удалить mmap-файлы чужих и мёртвых процессов из multiproc-каталога.

    **Зачем.** ``MultiProcessCollector`` агрегирует ВСЕ ``*.db`` каталога,
    включая файлы процессов, умерших месяцы назад. 2026-10-06: 4 928 файлов с
    мая (в т.ч. до-фиксовые серии ADR-0006 с сырыми путями сканеров) держали
    экспозицию на 7 МБ и ~70 тыс. серий — prometheus ел 440 МБ и умирал от OOM
    вместе с uvicorn. Ручная чистка «жив ли PID» недостаточна: файл мёртвого
    процесса с суффиксом _353 переживает, если PID 353 meanwhile заняла
    grafana (доказано на проде: файлы июня, живые по kill -0, cmdline чужой).

    Поэтому правило двойное: файл живёт, только если PID жив **и** cmdline
    процесса — наш (``_OWN_PROCESS_MARKERS``). Свой собственный PID не трогаем
    никогда. Непарсящийся суффикс — не трогаем (консерватизм: чужой мусор
    лучше, чем снос живого).

    Вызывается на старте web (lifespan в ``main.py``): рестарт при каждом
    деплое не даёт сиротам копиться. Ошибки не роняют старт — возвращается
    счётчик, в лог пишется итог.
    """
    if directory is None:
        directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "")
    summary: dict = {"dir": directory or None, "kept": 0, "removed": 0, "skipped": 0}
    if not directory:
        return summary
    pid_alive = _pid_alive or _default_pid_alive
    pid_cmdline = _pid_cmdline or _default_pid_cmdline
    own_pid = _own_pid if _own_pid is not None else os.getpid()
    try:
        names = os.listdir(directory)
    except OSError as e:
        logger.warning("multiproc prune: cannot list %s: %s", directory, e)
        return summary
    for name in names:
        if not name.endswith(".db"):
            continue
        match = _MULTIPROC_PID_SUFFIX.search(name)
        if not match:
            summary["skipped"] += 1
            continue
        pid = int(match.group(1))
        if pid == own_pid:
            summary["kept"] += 1
            continue
        try:
            alive = pid_alive(pid)
        except Exception:  # noqa: BLE001 — сомневаешься, не трогай
            summary["skipped"] += 1
            continue
        if not alive:
            _remove_quietly(directory, name, summary)
            continue
        try:
            cmdline = pid_cmdline(pid)
        except Exception:  # noqa: BLE001 — сомневаешься, не трогай
            summary["skipped"] += 1
            continue
        if any(marker in cmdline for marker in _OWN_PROCESS_MARKERS):
            summary["kept"] += 1
        else:
            _remove_quietly(directory, name, summary)
    return summary


def _remove_quietly(directory: str, name: str, summary: dict) -> None:
    import os as _os

    try:
        _os.remove(_os.path.join(directory, name))
        summary["removed"] += 1
    except OSError as e:
        logger.warning("multiproc prune: cannot remove %s: %s", name, e)
        summary["skipped"] += 1


if __name__ == "__main__":
    # Test metrics
    print("Testing metrics...")

    # Simulate some metrics
    api_requests_total.labels(method="GET", endpoint="/test", status="success").inc()
    api_request_duration_seconds.labels(method="GET", endpoint="/test").observe(0.123)

    cache_hits_total.labels(cache_type="redis").inc(10)
    cache_misses_total.labels(cache_type="redis").inc(2)

    vk_api_requests_total.labels(method="wall.get", status="success").inc(5)

    # Generate metrics
    metrics_output = generate_latest().decode("utf-8")

    print("\nGenerated metrics:")
    print("=" * 60)
    print(metrics_output[:500])  # First 500 chars
    print("...")
    print("=" * 60)
    print("\n✅ Metrics test completed!")
