"""
Metrics Middleware for automatic API metrics collection
"""

import logging
import time
from typing import Any, Dict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from monitoring.metrics import (
    api_request_duration_seconds,
    api_requests_in_progress,
    api_requests_total,
)

logger = logging.getLogger(__name__)

# Значение лейбла ``endpoint`` становится именем prometheus-серии. Брать его из
# ``request.url.path`` нельзя: сканеры из интернета дают бесконечное число новых
# путей, и каждый превращается в 11 серий гистограммы, живущих retention дней.
#
# Чем это обернулось на боевом боксе (замер 2026-10-01): 4 708 уникальных значений
# ``endpoint``, 52 811 серий в одном семействе histogram, 7 МБ текста на каждый
# скрейп при интервале 30 с. TSDB разрослась до 1 ГБ, а prometheus дважды
# побирался ядром OOM (RSS 418–440 МБ на боксе с 1.5 ГБ RAM и без swap) — и
# съедал память именно тогда, когда её не хватало сервисам. Среди значений были
# ``/%2e%2e/%2e%2e/etc/shadow`` и ``/%2e%2e/%2e%2e/home/admin/.ssh/id_rsa``.
#
# Поэтому ``endpoint`` — это шаблон маршрута, а не путь. Всё, что маршрутом не
# опознано (то есть ровно трафик сканеров), сводится в два фиксированных
# значения. Решения и альтернативы — docs/adr/0006-metrics-endpoint-cardinality.md.

UNMATCHED = "__unmatched__"
LONG_PATH = "__long_path__"

# Потолок длины метки — страховка на будущее, а не починка сегодняшнего бага.
# ``{path:path}`` уже схлопывается правильно (хвост уходит в path_params и
# становится ``{path}``), но маршрут, покрытый параметрами лишь ЧАСТИЧНО,
# воспроизвёл бы длинную метку. Пока такого маршрута нет — проверка остаётся,
# потому что её исчезновение означало бы, что новые маршруты не проверяют.
MAX_LABEL_LEN = 120


def endpoint_label(scope: Dict[str, Any], path: str) -> str:
    """Шаблон маршрута для лейбла метрики.

    Starlette (проверено на 0.48) кладёт в scope только ``endpoint`` и
    ``path_params``, но **не** ``route``, поэтому шаблон собирается обратно из
    ``path_params`` подстановкой значений параметров в сегменты пути.

    - нет ``path_params`` → маршрут не совпал (404/405, сканеры) → ``__unmatched__``;
    - сегмент совпал с параметром → ``{имя}``;
    - получилось длиннее ``MAX_LABEL_LEN`` → ``__long_path__``;
    - хвостовой ``/`` отбрасывается: ``/api/x`` и ``/api/x/`` — один маршрут,
      а для метрики это две серии.
    """
    params = {
        str(name): str(value)
        for name, value in (scope.get("path_params") or {}).items()
        if value is not None and str(value) != ""
    }
    if not params:
        return UNMATCHED

    remaining = dict(params)
    parts = path.split("/")
    replacements: Dict[int, str] = {}

    # Проход 1 — только точное совпадение сегмента. Именно отдельным проходом, а
    # не внутри цикла по сегментам: иначе параметр «1» по подстроке съел бы «1»
    # внутри статического «v1» раньше, чем до сегмента «1» дойдёт очередь.
    for index, segment in enumerate(parts):
        for name, value in remaining.items():
            if value == segment:
                replacements[index] = "{" + name + "}"
                del remaining[name]
                break

    # Проход 2 — частичное совпадение (uuid с префиксом, слаг в составном
    # сегменте). То, что не забрал проход 1, идёт в остаток.
    for index, segment in enumerate(parts):
        if index in replacements:
            continue
        for name, value in remaining.items():
            if value in segment:
                replacements[index] = "{" + name + "}"
                del remaining[name]
                break

    label = "/".join(replacements.get(i, part) for i, part in enumerate(parts))
    if len(label) > MAX_LABEL_LEN:
        return LONG_PATH
    if len(label) > 1 and label.endswith("/"):
        label = label.rstrip("/") or "/"
    return label or UNMATCHED


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware для автоматического сбора метрик API

    Собирает:
    - Количество запросов
    - Латентность запросов
    - Статусы ответов
    - Активные запросы
    """

    def __init__(self, app):
        super().__init__(app)

        # Paths to exclude from metrics
        self.exclude_paths = {
            "/metrics",
            "/health",
            "/favicon.ico",
        }

    def should_track(self, path: str) -> bool:
        """Check if path should be tracked"""
        # Exclude specific paths
        if path in self.exclude_paths:
            return False

        # Exclude static files
        if path.startswith("/static"):
            return False

        return True

    async def dispatch(self, request: Request, call_next):
        """Process request with metrics collection"""

        # Check if should track
        if not self.should_track(request.url.path):
            return await call_next(request)

        # Increment in-progress counter
        api_requests_in_progress.inc()

        # Start timing
        start_time = time.time()
        status_code = 500  # Default to error

        try:
            # Process request
            response = await call_next(request)
            status_code = response.status_code
            return response

        except Exception as e:
            logger.error(f"Request error: {e}")
            raise

        finally:
            # Calculate duration
            duration = time.time() - start_time

            # Decrement in-progress counter
            api_requests_in_progress.dec()

            # Determine status category
            status = "success" if 200 <= status_code < 400 else "error"

            # Метка endpoint = шаблон маршрута, НЕ сырой путь. Сырой путь
            # неограничен: чужие сканеры создают новую серию на каждый запрос.
            endpoint = endpoint_label(request.scope, request.url.path)

            # Record metrics
            api_requests_total.labels(method=request.method, endpoint=endpoint, status=status).inc()

            api_request_duration_seconds.labels(method=request.method, endpoint=endpoint).observe(
                duration
            )

            # Log slow requests
            if duration > 1.0:
                logger.warning(f"Slow request: {request.method} {endpoint} took {duration:.2f}s")


if __name__ == "__main__":
    print("✅ Metrics middleware module ready")
