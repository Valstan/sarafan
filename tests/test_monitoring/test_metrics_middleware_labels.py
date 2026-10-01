"""Кардинальность лейбла ``endpoint`` в метриках API.

Инвариант, который здесь защищается: **число различных значений лейбла
``endpoint`` не растёт от чужого трафика**. Значение лейбла — это имя
prometheus-серии, а серия живёт retention дней. Если брать его из сырого
``request.url.path``, то каждый запрос интернет-сканера создаёт новую серию
навсегда.

История, из-за которой тест написан (замер на проде 2026-10-01):
4 708 значений endpoint → 52 811 серий в одном histogram-семействе → 7 МБ на
скрейп → TSDB 1 ГБ → prometheus дважды убит ядром OOM на боксе с 1.5 ГБ RAM.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.metrics_middleware import (
    LONG_PATH,
    MAX_LABEL_LEN,
    UNMATCHED,
    MetricsMiddleware,
    endpoint_label,
)


def _scope(**path_params):
    return {"path_params": dict(path_params)} if path_params else {"path_params": {}}


class TestEndpointLabel:
    """Чистая функция: маршрут не совпал → фиксированное значение."""

    def test_unmatched_path_is_one_fixed_value(self):
        # Ровно те строки, из-за которых всё и ломалось.
        for path in (
            "/%2e%2e/%2e%2e/etc/shadow",
            "/%2e%2e/%2e%2e/home/admin/.ssh/id_rsa",
            "/wp-config.php.bak",
            "/wp-json/batch/v1",
            "/.env",
        ):
            assert endpoint_label(_scope(), path) == UNMATCHED

    def test_empty_scope_is_unmatched(self):
        assert endpoint_label({}, "/anything") == UNMATCHED

    def test_numeric_param_becomes_placeholder(self):
        assert endpoint_label(_scope(id="123"), "/api/posts/123") == "/api/posts/{id}"

    def test_non_numeric_param_becomes_placeholder(self):
        # Прежняя чистка умела только `.isdigit()`, поэтому UUID и слаги
        # утекали в лейбл как есть.
        slug = "9f2b1c7a-4d3e-4a1b-8c2d-7e6f5a4b3c2d"
        assert endpoint_label(_scope(id=slug), f"/api/clients/{slug}") == (
            "/api/clients/{id}"
        )
        assert endpoint_label(_scope(slug="abc-def"), "/api/theme-quotas/abc-def") == (
            "/api/theme-quotas/{slug}"
        )

    def test_several_params_in_one_path(self):
        assert endpoint_label(
            _scope(client_id="42", month="2026-09"),
            "/api/ad-crm/clients/42/stats/2026-09",
        ) == "/api/ad-crm/clients/{client_id}/stats/{month}"

    def test_equal_param_values_do_not_eat_each_other(self):
        # Оба параметра равны "x": наивный replace() дал бы "{a}/{a}".
        assert endpoint_label(_scope(a="x", b="x"), "/pair/x/x") == "/pair/{a}/{b}"

    def test_static_segment_is_not_corrupted_by_short_param(self):
        # Параметр "1" не должен съесть "1" внутри статического "v1".
        assert endpoint_label(_scope(id="1"), "/api/v1/items/1") == (
            "/api/v1/items/{id}"
        )

    def test_trailing_slash_does_not_split_the_route(self):
        # На проде было 52 и 78 попаданий на один и тот же маршрут — из-за "/".
        assert endpoint_label(_scope(id="7"), "/api/theme-quotas/7/") == (
            "/api/theme-quotas/{id}"
        )
        assert endpoint_label(_scope(), "/api/theme-quotas/") == UNMATCHED

    def test_catch_all_route_collapses_to_placeholder(self):
        # ``/media/{path:path}`` — маршрут ПОЙМАН, но в path_params уходит весь
        # хвост. Подстановка схлопывает его в {path}, то есть утечку не даёт.
        long_tail = "a" * 400
        path = f"/media/{long_tail}"
        assert endpoint_label(_scope(path=long_tail), path) == "/media/{path}"

    def test_label_never_exceeds_cap_when_params_cover_only_part(self):
        # Страховка на будущее: маршрут с кучей сегментов, покрытых параметрами
        # лишь частично, не должен дать длинную метку. Сейчас такой маршрут
        # представить трудно — именно поэтому проверка остаётся, а не удаляется
        # вместе с правкой.
        path = "/".join(["seg"] * 60) + "/42"
        label = endpoint_label(_scope(id="42"), path)
        assert len(label) <= MAX_LABEL_LEN
        assert label == LONG_PATH

    def test_static_routes_pass_through(self):
        assert endpoint_label(_scope(id="1"), "/api/health/full") == "/api/health/full"


class TestMiddlewareDoesNotGrowCardinality:
    """Настоящий охранитель: чужие пути не плодят серии."""

    def _client(self):
        app = FastAPI()
        app.add_middleware(MetricsMiddleware)

        @app.get("/api/items/{item_id}")
        async def items(item_id: str):
            return {"ok": True}

        return TestClient(app, raise_server_exceptions=False)

    def test_scanner_paths_collapse_into_one_label(self):
        from monitoring import metrics

        before = _endpoint_label_values(metrics.api_requests_total)

        client = self._client()
        for path in (
            "/%2e%2e/%2e%2e/etc/passwd",
            "/%2e%2e/%2e%2e/etc/ssh/sshd_config",
            "/wp-login.php",
            "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php",
        ):
            client.get(path)  # 404 — маршрут не совпал

        after = _endpoint_label_values(metrics.api_requests_total)
        assert set(after) - set(before) == {UNMATCHED}

    def test_declared_route_with_dynamic_param_is_one_label(self):
        from monitoring import metrics

        before = _endpoint_label_values(metrics.api_requests_total)

        client = self._client()
        for item_id in ("1", "2", "abc-def", "9f2b1c7a-4d3e-4a1b-8c2d-7e6f5a4b3c2d"):
            assert client.get(f"/api/items/{item_id}").status_code == 200

        after = _endpoint_label_values(metrics.api_requests_total)
        assert set(after) - set(before) == {"/api/items/{item_id}"}


def _endpoint_label_values(counter) -> set:
    """Значения лейбла ``endpoint``, уже появившиеся у этого counter'а.

    ``prometheus_client`` держит образцы в приватном ``_metrics`` под ключом
    ``(method, endpoint, status)``. Приватность — осознанный выбор: публичного
    «список серий» у библиотеки нет, а тесту нужно увидеть, что новые значения
    не появились.
    """
    return {key[1] for key in counter._metrics.keys()}


@pytest.mark.parametrize(
    "scope,path,expected",
    [
        (_scope(id="123"), "/api/posts/123", "/api/posts/{id}"),
        (_scope(), "/nope", UNMATCHED),
        (_scope(id=""), "/api/x/", UNMATCHED),
        (_scope(id=None), "/api/x", UNMATCHED),
    ],
)
def test_table(scope, path, expected):
    assert endpoint_label(scope, path) == expected
