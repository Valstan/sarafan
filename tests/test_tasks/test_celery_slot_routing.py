"""Публикационные слоты живут в очереди `wave`, а не в общей `celery`.

Замер 25.11 00:20 (живой прод): общая очередь `celery` — 467 сообщений, все
просрочены; в ней молча истекли волны `admin` (22:20) и `sport` (22:50) и встала
лента хаба. Вечерняя сводка выжила — у неё своя очередь `digest` и `expires`
7200 (PR #1247).

Тесты ниже фиксируют именно это решение: две задачи публикационного слота
уходят в `wave`, а мелкая периодика остаётся в `celery` (иначе кладбище
росло бы быстрее).
"""

import importlib

import pytest

from tasks.celery_app import app

FANOUT_TASK = "tasks.parsing_scheduler_tasks.run_all_regions_theme"
PUBLISH_TASK = "tasks.parsing_scheduler_tasks.parse_and_publish_theme"


def _queue_name(task_name: str) -> str:
    """Имя очереди, в которую роутер отправит задачу."""
    routed = app.amqp.router.route({}, task_name, [], {})
    queue = routed.get("queue")
    return str(getattr(queue, "name", queue))


def test_wave_fanout_goes_to_wave_queue():
    """Веер волны — задача-«мама» — должен уйти в `wave`.

    Иначе он стоит в общей очереди и протухает там: замер 25.11 — обе вечерние
    волны (22:20 и 22:50) истекли в `celery` молча, без единой ошибки в логах.
    """
    assert _queue_name(FANOUT_TASK) == "wave"


def test_publish_task_goes_to_wave_queue():
    """Публикация по региону и лента хаба — тоже публикационный слот."""
    assert _queue_name(PUBLISH_TASK) == "wave"


@pytest.mark.parametrize(
    "task_name",
    [
        "tasks.celery_app.dispatch_ad_reposts",
        "tasks.broadcast_tasks.dispatch_broadcasts",
        "tasks.celery_app.classify_pending_posts",
    ],
)
def test_minute_periodic_stays_in_general_queue(task_name):
    """Мелкая периодика остаётся в `celery` — её штатное место.

    Поднимать ей срок в очереди нельзя: тогда кладбище из просроченных
    сообщений росло бы быстрее, а слоты публикации это всё равно не чинит.
    """
    assert _queue_name(task_name) == "celery"


def test_route_uses_same_queue_name_as_runtime_helper():
    """Имя очереди в маршрутах — то же, что у `config.runtime.get_wave_queue()`.

    Страховка от расхождения двух мест: маршруты живут в конфиг-модуле, а
    очередь волны выбирается в runtime. Дефолт и env-имя должны совпадать.
    """
    from config.runtime import get_wave_queue

    routes = app.conf.task_routes or {}
    assert routes[FANOUT_TASK]["queue"] == get_wave_queue()
    assert routes[PUBLISH_TASK]["queue"] == get_wave_queue()


def test_queue_name_follows_env(monkeypatch):
    """`SETKA_WAVE_QUEUE` переименовывает очередь целиком (откат — одна строка)."""
    import config.celery_config as celery_config

    monkeypatch.setenv("SETKA_WAVE_QUEUE", "volna")
    try:
        reloaded = importlib.reload(celery_config)
        assert reloaded.task_routes[FANOUT_TASK]["queue"] == "volna"
        assert reloaded.task_routes[PUBLISH_TASK]["queue"] == "volna"
    finally:
        monkeypatch.undo()
        importlib.reload(celery_config)


def test_beat_sends_evening_waves_through_the_fanout():
    """Бит зовёт веер — тот самый, что уходит в `wave`."""
    schedule = app.conf.beat_schedule or {}
    for entry_name in ("postopus-admin-22", "postopus-sport-22"):
        assert schedule[entry_name]["task"] == FANOUT_TASK
