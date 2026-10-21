"""Замок волны по району: один район — одна волна одновременно (модуль, не процесс).

Зачем тесты: с ``--concurrency=2`` две волны одного района (разные темы, один
пул сообществ и один курсор дедупа в ``work_tables``) иначе увидят один пост
новым обе и выпустят дубль. Молчаливая поломка здесь — это дубль в ленте,
поэтому проверяем и «берёт», и «не берёт», и «Redis умер — не останавливаемся».
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import wave_lock  # noqa: E402


class FakeRedis:
    def __init__(self, taken: bool = False, fail: bool = False):
        self.taken = taken
        self.fail = fail
        self.sets: list[tuple] = []
        self.evals: list[tuple] = []

    def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise ConnectionError("redis лежит")
        self.sets.append((key, value, nx, px))
        if self.taken:
            return None
        self.taken = True
        return True

    def eval(self, script, numkeys, key, token):
        if self.fail:
            raise ConnectionError("redis лежит")
        self.evals.append((key, token))
        return 1

    def scan_iter(self, match=None, count=None):
        return iter([])


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch):
    monkeypatch.setattr(wave_lock, "_client", None, raising=False)
    monkeypatch.delenv("WAVE_LOCK_ENABLED", raising=False)
    monkeypatch.delenv("WAVE_LOCK_WAIT_SECONDS", raising=False)
    yield
    wave_lock._client = None


def test_acquire_takes_lock_and_release_removes_own(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(wave_lock, "_client", fake)

    token = wave_lock.acquire("mi")

    assert token  # непустой token = замок наш
    key, value, nx, px = fake.sets[0]
    assert key == "setka:wave_lock:mi"
    assert value == token
    assert nx is True
    assert px and px > 0

    wave_lock.release("mi", token)
    assert fake.evals == [("setka:wave_lock:mi", token)]


def test_second_wave_leaves_immediately_when_lock_is_held(monkeypatch):
    """Ждать дольше WAVE_LOCK_WAIT_SECONDS волна не должна: её тему подхватит
    следующий слот, а дубль в ленте не откатить."""
    fake = FakeRedis(taken=True)
    monkeypatch.setattr(wave_lock, "_client", fake)
    monkeypatch.setenv("WAVE_LOCK_WAIT_SECONDS", "0")

    assert wave_lock.acquire("mi") is None


def test_redis_failure_does_not_stop_the_pipeline(monkeypatch):
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(wave_lock, "_client", fake)

    # Пустая строка = «замка нет, но волна идёт»: риск дубля тот же, что до замка,
    # а остановить публикации из-за Redis нельзя.
    assert wave_lock.acquire("mi") == ""


def test_release_without_token_is_noop(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(wave_lock, "_client", fake)

    wave_lock.release("mi", "")

    assert fake.evals == []


def test_lock_can_be_disabled_by_env(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(wave_lock, "_client", fake)
    monkeypatch.setenv("WAVE_LOCK_ENABLED", "0")

    assert wave_lock.acquire("mi") == ""
    assert fake.sets == []


def test_task_skips_region_when_another_wave_holds_it(monkeypatch):
    """Волна района, занятого другой волной, не публикует и честно говорит об этом."""
    from modules import wave_lock as lock_module
    from tasks import parsing_scheduler_tasks as st

    monkeypatch.setattr(lock_module, "acquire", lambda region_code: None)

    out = st.parse_and_publish_theme.apply(args=("mi", "novost")).get()

    assert out["skipped"] == "region_lock"
    assert out["success"] is False
