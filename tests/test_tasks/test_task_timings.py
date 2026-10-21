"""Прибор длительности задач: что он пишет, что читает и как молчит при отказе.

Зачем гейт. Прибор стоит на пути КАЖДОЙ задачи воркера, а его вывод владелец
читает как факт («периодика съедает N минут слота»). Ошибка здесь дороже, чем
отсутствие прибора: пустой словарь выглядит как «ничего не занимает», а
сломанный Redis — как «задач нет». Поэтому проверяется не «записалось», а три
свойства:

* запись обрезается до ``SAMPLE_LIMIT`` — иначе список растёт вместе с сутками;
* отказ Redis не роняет задачу и не превращается в ноль на странице;
* чтение без измерений возвращает ПУСТО, а не нулевую длительность.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from modules import task_timings as tt


class FakePipeline:
    def __init__(self, store: "FakeRedis"):
        self._store = store
        self._queue: List[tuple] = []

    def rpush(self, key: str, value: Any):
        self._queue.append(("rpush", key, value))
        return self

    def ltrim(self, key: str, start: int, stop: int):
        self._queue.append(("ltrim", key, start, stop))
        return self

    def expire(self, key: str, ttl: int):
        self._queue.append(("expire", key, ttl))
        return self

    def lrange(self, key: str, start: int, stop: int):
        self._queue.append(("lrange", key, start, stop))
        return self

    def execute(self):
        out = []
        for command in self._queue:
            name, key = command[0], command[1]
            values = self._store.lists.setdefault(key, [])
            if name == "rpush":
                values.append(command[2])
                out.append(len(values))
            elif name == "ltrim":
                start, stop = command[2], command[3]
                if start < 0:
                    start = max(0, len(values) + start)
                if stop < 0:
                    stop = len(values) + stop
                self._store.lists[key] = values[start : stop + 1]
                out.append(True)
            elif name == "expire":
                self._store.ttl[key] = command[2]
                out.append(True)
            elif name == "lrange":
                out.append(list(values))
        return out


class FakeRedis:
    def __init__(self):
        self.lists: Dict[str, List[Any]] = {}
        self.ttl: Dict[str, int] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


@pytest.fixture()
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(tt, "_client", client, raising=False)
    monkeypatch.delenv("TASK_TIMINGS_ENABLED", raising=False)
    return client


def test_record_keeps_last_runs_and_reports_average(fake_redis):
    for value in (2.0, 4.0, 6.0):
        tt.record("tasks.demo.task", value)

    summary = tt.snapshot(["tasks.demo.task"])["tasks.demo.task"]

    assert summary["samples"] == 3
    assert summary["avg_seconds"] == 4.0
    assert summary["max_seconds"] == 6.0
    assert summary["last_seconds"] == 6.0
    assert fake_redis.ttl[tt.key("tasks.demo.task")] == tt.TTL_SECONDS


def test_list_is_trimmed_to_the_sample_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(tt, "SAMPLE_LIMIT", 3)
    for value in range(10):
        tt.record("tasks.demo.task", value)

    values = fake_redis.lists[tt.key("tasks.demo.task")]

    assert values == [7, 8, 9]


def test_snapshot_without_measurements_is_empty_not_zero(fake_redis):
    """Прочерк на странице обязан быть прочерком: ноль читается как «не занимает»."""
    assert tt.snapshot(["tasks.never.ran"]) == {}


def test_broken_redis_does_not_break_the_task(monkeypatch):
    class Broken:
        def pipeline(self):
            raise RuntimeError("redis down")

    monkeypatch.setattr(tt, "_client", Broken(), raising=False)
    monkeypatch.delenv("TASK_TIMINGS_ENABLED", raising=False)

    tt.record("tasks.demo.task", 1.0)  # не бросает

    assert tt.snapshot(["tasks.demo.task"]) == {}


def test_instrument_can_be_switched_off_without_touching_the_worker(fake_redis, monkeypatch):
    monkeypatch.setenv("TASK_TIMINGS_ENABLED", "0")

    tt.record("tasks.demo.task", 1.0)

    assert fake_redis.lists == {}


def test_bad_value_is_ignored_not_guessed(fake_redis):
    tt.record("tasks.demo.task", None)
    tt.record("tasks.demo.task", "не число")

    assert fake_redis.lists == {}
