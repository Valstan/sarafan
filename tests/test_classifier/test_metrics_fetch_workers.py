"""Бюджет параллельных батчей у круга обновления метрик (env ``METRICS_FETCH_WORKERS``).

Геттер маленький, но у него ровно два способа навредить молча: поднять потоки
выше тормоза (тогда круг превращается в залп по живому READ-токену боевого
парсера) и упасть на мусоре в env вместо внятного дефолта.
"""

import pytest

from config.classifier import get_metrics_fetch_workers


def test_default_is_three(monkeypatch):
    monkeypatch.delenv("METRICS_FETCH_WORKERS", raising=False)
    assert get_metrics_fetch_workers() == 3


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("METRICS_FETCH_WORKERS", "1")
    assert get_metrics_fetch_workers() == 1


@pytest.mark.parametrize(
    ("raw", "expected"), [("0", 1), ("-5", 1), ("9", 4), ("мусор", 3), ("", 3)]
)
def test_bounds_and_garbage(monkeypatch, raw, expected):
    monkeypatch.setenv("METRICS_FETCH_WORKERS", raw)
    assert get_metrics_fetch_workers() == expected
