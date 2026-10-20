"""Редкость футера «Ленты соседей»: не больше N в сутки и с паузой (20.10)."""

from __future__ import annotations

import pytest

from modules import neighbor_footer


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        assert ttl > 0
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(neighbor_footer, "_redis", lambda: fake)
    monkeypatch.setenv("NEIGHBOR_FOOTER_MAX_PER_DAY", "3")
    monkeypatch.setenv("NEIGHBOR_FOOTER_MIN_GAP_HOURS", "4")
    return fake


def _at(hour: int) -> float:
    """Время внутри суток 2026-10-20 (UTC)."""
    from datetime import datetime, timezone

    return datetime(2026, 10, 20, hour, 0, tzinfo=timezone.utc).timestamp()


def test_first_footer_of_the_day_is_allowed(redis):
    assert neighbor_footer.footer_allowed("mi", now=_at(8))


def test_gap_between_footers_is_respected(redis):
    neighbor_footer.register_footer("mi", now=_at(8))

    assert not neighbor_footer.footer_allowed("mi", now=_at(9)), "час спустя — рано"
    assert neighbor_footer.footer_allowed("mi", now=_at(12))


def test_daily_cap_is_three(redis):
    neighbor_footer.register_footer("mi", now=_at(7))
    neighbor_footer.register_footer("mi", now=_at(12))
    neighbor_footer.register_footer("mi", now=_at(18))

    assert not neighbor_footer.footer_allowed("mi", now=_at(23))


def test_counter_resets_next_day(redis):
    for hour in (7, 12, 18):
        neighbor_footer.register_footer("mi", now=_at(hour))

    next_day = _at(8) + 24 * 3600
    assert neighbor_footer.footer_allowed("mi", now=next_day)


def test_regions_do_not_share_the_counter(redis):
    neighbor_footer.register_footer("mi", now=_at(8))

    assert neighbor_footer.footer_allowed("kilmez", now=_at(8))


def test_redis_failure_does_not_hide_the_footer(monkeypatch):
    """Redis лёг — показываем: молчание не отличить от «соседей нет»."""

    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(neighbor_footer, "_redis", boom)
    assert neighbor_footer.footer_allowed("mi")


def test_zero_cap_disables_footer(redis, monkeypatch):
    monkeypatch.setenv("NEIGHBOR_FOOTER_MAX_PER_DAY", "0")
    assert not neighbor_footer.footer_allowed("mi", now=_at(8))
