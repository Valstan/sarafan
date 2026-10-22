"""Общий батч-фетчер метрик постов (wall.getById).

Единственное место, где парсится ответ ВК по метрикам. Тесты держат три
свойства: NULL не превращается в 0, падение одного батча не уносит остальные,
и параллельный режим не превращает круг в залп (тормоз зовётся на каждый батч).
"""

import threading
from datetime import datetime

import pytest

from modules.vk_monitor.post_metrics import fetch_metrics_for_token, parse_metrics_items


@pytest.fixture(autouse=True)
def _no_real_throttle(monkeypatch):
    """Тормоз подменяется счётчиком: реальный лимитер спит 0.4 с на вызов.

    Подмена — по модулю post_metrics, поэтому сам факт вызова остаётся
    проверяемым (см. test_every_batch_goes_through_the_shared_throttle).
    """
    calls = []
    monkeypatch.setattr(
        "modules.vk_monitor.post_metrics.enforce_token_rate_limit",
        lambda token, method="": calls.append((token, method)),
    )
    return calls


def test_parse_reads_all_four_metrics_and_date():
    items = [
        {
            "owner_id": -100,
            "id": 7,
            "date": 1787136000,
            "views": {"count": 1224},
            "likes": {"count": 7},
            "comments": {"count": 2},
            "reposts": {"count": 1},
        }
    ]
    out = parse_metrics_items(items)
    assert out[(-100, 7)] == {
        "views": 1224,
        "likes": 7,
        "comments": 2,
        "reposts": 1,
        "published_at": datetime(2026, 8, 19, 10, 40),
    }


def test_missing_views_stays_none_not_zero():
    # 10% постов приезжают без поля views. Ноль тут соврал бы: рейтинг делит
    # на (views+1), и «ноль просмотров» подняло бы пост наверх.
    items = [{"owner_id": -1, "id": 2, "likes": {"count": 3}}]
    out = parse_metrics_items(items)
    assert out[(-1, 2)]["views"] is None
    assert out[(-1, 2)]["likes"] == 3
    assert out[(-1, 2)]["comments"] is None


def test_parse_skips_items_without_usable_ids():
    items = [{"likes": {"count": 1}}, {"owner_id": "х", "id": 1}]
    assert parse_metrics_items(items) == {}


def test_fetch_splits_into_batches_of_hundred():
    calls = []

    class FakeApi:
        class wall:
            @staticmethod
            def getById(posts):
                calls.append(posts.split(","))
                return []

    refs = [(-1, i) for i in range(250)]
    fetch_metrics_for_token(FakeApi, refs, token="tok", batch_size=100)
    assert [len(c) for c in calls] == [100, 100, 50]


def test_one_failing_batch_does_not_lose_the_others():
    class FlakyApi:
        class wall:
            seen = 0

            @classmethod
            def getById(cls, posts):
                cls.seen += 1
                if cls.seen == 1:
                    raise RuntimeError("VK упал")
                return [{"owner_id": -1, "id": 200, "likes": {"count": 5}}]

    refs = [(-1, i) for i in range(150)]
    out = fetch_metrics_for_token(FlakyApi, refs, token="tok", batch_size=100)
    assert (-1, 200) in out, "второй батч обязан отработать после падения первого"


def test_response_may_be_dict_with_items():
    class DictApi:
        class wall:
            @staticmethod
            def getById(posts):
                return {"items": [{"owner_id": -5, "id": 1, "likes": {"count": 2}}]}

    out = fetch_metrics_for_token(DictApi, [(-5, 1)], token="tok")
    assert out[(-5, 1)]["likes"] == 2


def test_every_batch_goes_through_the_shared_throttle(_no_real_throttle):
    """Каждый батч тормозится общим per-token лимитером, а не уходит залпом.

    Это не гигиена: круг обновления метрик берёт тот же живой READ-токен, что
    и боевой парсер. 78 вызовов подряд — burst сильно выше 3 req/sec, за
    который ВК ставит на токен cooldown или captcha, и вспомогательная таска
    посадила бы сбор постов. Плюс тот же вызов считается в отчёте по токенам.
    """

    class FakeApi:
        class wall:
            @staticmethod
            def getById(posts):
                return []

    fetch_metrics_for_token(FakeApi, [(-1, i) for i in range(250)], token="tok-A", batch_size=100)

    assert _no_real_throttle == [("tok-A", "wall.getById")] * 3


def test_throttle_happens_even_when_the_batch_fails(_no_real_throttle):
    """Отказ батча не отменяет тормоз: следующий батч всё равно ждёт свой слот.

    Иначе на серии отказов (а именно там ВК и злится) вызовы посыпались бы
    вплотную — ровно в тот момент, когда токен и так под подозрением.
    """

    class DeadApi:
        class wall:
            @staticmethod
            def getById(posts):
                raise RuntimeError("VK упал")

    out = fetch_metrics_for_token(DeadApi, [(-1, i) for i in range(150)], token="t", batch_size=100)

    assert out == {}
    assert len(_no_real_throttle) == 2


def _factory_recording(clients):
    """Фабрика «своя копия api на поток», как в проде (vk_api + requests.Session)."""

    class Client:
        class wall:
            @staticmethod
            def getById(posts):
                ids = [int(p.split("_")[1]) for p in posts.split(",")]
                return [{"owner_id": -1, "id": i, "likes": {"count": 1}} for i in ids]

    def make():
        client = Client
        clients.append(client)
        return client

    return make


def test_workers_run_batches_in_parallel():
    """``workers`` > 1 действительно распараллеливает, а не идёт по очереди.

    Доказательство без опоры на секунды: два потока встречаются на барьере.
    Если второй батч запустился бы только после первого, барьер не собрался бы
    и тест упал бы по таймауту — то есть «параллельно» здесь проверяется
    детерминированно, а не замером времени.
    """
    barrier = threading.Barrier(2, timeout=10)
    seen = []

    class Client:
        class wall:
            @staticmethod
            def getById(posts):
                if len(seen) < 2:
                    seen.append(posts)
                    barrier.wait()
                return [{"owner_id": -1, "id": int(posts.split("_")[-1]), "likes": {"count": 1}}]

    def factory():
        return Client

    out = fetch_metrics_for_token(
        None,
        [(-1, i) for i in range(200)],
        token="tok",
        batch_size=100,
        workers=2,
        api_factory=factory,
    )

    assert barrier.broken is False
    assert len(seen) == 2
    assert len(out) == 2, "метрики обоих батчей обязаны доехать"


def test_workers_keep_every_batch_on_the_shared_throttle(_no_real_throttle):
    """Параллельность не превращает круг в залп: тормоз зовётся на каждый батч.

    Это и есть причина, по которой втрое больше потоков не значит втрое больше
    запросов в секунду для ВК — частоту держит тот же per-token лимитер.
    """
    out = fetch_metrics_for_token(
        None,
        [(-1, i) for i in range(350)],
        token="tok-B",
        batch_size=100,
        workers=3,
        api_factory=_factory_recording([]),
    )

    assert len(out) == 350
    assert sorted(_no_real_throttle) == [("tok-B", "wall.getById")] * 4


def test_workers_do_not_lose_the_other_batches_on_failure():
    """Один упавший батч в параллельном режиме не уносит соседние."""
    lock = threading.Lock()
    seen = {"n": 0}

    class Client:
        class wall:
            @staticmethod
            def getById(posts):
                with lock:
                    seen["n"] += 1
                    n = seen["n"]
                if n == 1:
                    raise RuntimeError("VK упал")
                return [{"owner_id": -1, "id": n, "likes": {"count": n}}]

    out = fetch_metrics_for_token(
        None,
        [(-1, i) for i in range(300)],
        token="tok",
        batch_size=100,
        workers=3,
        api_factory=lambda: Client,
    )

    assert len(out) == 2, "два живых батча должны пережить отказ третьего"


def test_workers_are_capped_by_the_number_of_batches():
    """Потоков не больше, чем батчей: на двух пачках третий не поднимается."""
    clients = []
    out = fetch_metrics_for_token(
        None,
        [(-1, i) for i in range(150)],
        token="tok",
        batch_size=100,
        workers=4,
        api_factory=_factory_recording(clients),
    )

    assert len(out) == 150
    assert len(clients) <= 2, "лишний поток = лишняя копия api на пустом месте"


def test_workers_above_one_does_not_touch_the_passed_api():
    """В параллельном пути переданный ``api`` не используется вовсе.

    Так исключается общий ``requests.Session`` на несколько потоков: если
    потоки начнут ходить через переданный объект, тест это увидит.
    """

    class Shared:
        class wall:
            @staticmethod
            def getById(posts):  # pragma: no cover - сюда попадать не должны
                raise AssertionError("общий api в параллельном режиме трогать нельзя")

    out = fetch_metrics_for_token(
        Shared,
        [(-1, i) for i in range(200)],
        token="tok",
        batch_size=100,
        workers=2,
        api_factory=_factory_recording([]),
    )

    assert len(out) == 200
