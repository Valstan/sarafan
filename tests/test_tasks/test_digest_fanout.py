"""Веер вечерней сводки: задача на регион, разведение по минутам, своя очередь.

**Зачем (P234, решение владельца 24.11).** Прежде сводка была **одной** задачей
на 76 регионов: она занимала единственный слот воркера целиком, её снимал общий
лимит 3600 с, и сборник выходил примерно для трети сети (замер 23.11 — 21 регион
из 76), а волна ``admin`` (22:20) ждала конца обхода.

Три свойства веера, которые и проверяются здесь:

* **задача на регион** — у каждого региона свой срок, «хвост» списка не выпадает;
* **разведение по минутам** — выход равномерный, а не сорок bulletins подряд;
* **своя очередь** — 76 задач не занимают голову общей очереди (ADR-0020).

И одно, что легко упустить: **день закреплён в задании.** Кандидаты берутся «с
полуночи», а хвост веера с фото доходит после полуночи — без закрепления он
собрал бы чужой день.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tasks import digest_tasks


def _dispatch(regions, *, spread_env=None):
    """Прогнать веер с подменёнными регионами и собрать отправленные задачи."""
    sent = []

    def _fake_apply_async(args=None, countdown=None, expires=None, **kwargs):
        task = MagicMock()
        task.id = "task-%s" % (args[0] if args else "?")
        sent.append(
            {
                "args": args,
                "countdown": countdown,
                "expires": expires,
                "queue": kwargs.get("queue"),
            }
        )
        return task

    def _fake_run_coro(coro):
        coro.close()
        return list(regions)

    env = {"DAILY_DIGEST_REGION_SPREAD_SECONDS": "600"}
    if spread_env is not None:
        env = {"DAILY_DIGEST_REGION_SPREAD_SECONDS": spread_env}

    with patch.dict("os.environ", env):
        with patch.object(digest_tasks, "run_coro", side_effect=_fake_run_coro):
            with patch.object(
                digest_tasks.publish_daily_digest_region,
                "apply_async",
                side_effect=_fake_apply_async,
            ):
                result = digest_tasks.publish_daily_digest_fanout()
    return result, sent


def test_fanout_schedules_one_task_per_region():
    _, sent = _dispatch(["mi", "ur"])

    assert len(sent) == 2
    assert [item["args"][0] for item in sent] == ["mi", "ur"]


def test_regions_are_sorted_by_code():
    """Порядок фиксируем по коду: слоты тогда не прыгают день ото дня."""
    _, sent = _dispatch(["ur", "mi", "or"])

    assert [item["args"][0] for item in sent] == ["mi", "or", "ur"]


def test_regions_are_spread_over_the_window():
    """Первый слот сразу, последний — на краю окна."""
    _, sent = _dispatch(["mi", "or", "ur"])

    assert sent[0]["countdown"] == 0
    assert sent[-1]["countdown"] == 600
    assert sent[1]["countdown"] == 300


def test_expiry_covers_the_slot_and_the_queue_wait():
    """Задача не должна протухнуть, пока ждёт слот (с фото хвост длинный)."""
    _, sent = _dispatch(["mi", "ur"])

    for item in sent:
        assert item["expires"] == item["countdown"] + digest_tasks.DIGEST_TASK_EXPIRES_SEC


def test_tasks_go_to_the_digest_queue():
    """76 задач на общей очереди занимают голову списка (ADR-0020)."""
    _, sent = _dispatch(["mi"])

    assert sent[0]["queue"] == "digest"


def test_queue_name_comes_from_env_for_a_one_line_rollback():
    """``SETKA_DIGEST_QUEUE=celery`` возвращает веер в дефолтную очередь."""
    with patch.dict("os.environ", {"SETKA_DIGEST_QUEUE": "celery"}):
        _, sent = _dispatch(["mi"])

    assert sent[0]["queue"] == "celery"


def test_day_is_pinned_in_the_task():
    """День закрепляется в момент постановки — хвост не соберёт чужой день."""
    result, sent = _dispatch(["mi"])

    assert result["since"]
    assert sent[0]["args"][1] == result["since"]


def test_beat_calls_the_fanout():
    """Расписание зовёт веер, а не прежнюю задачу на всю сеть."""
    from tasks.celery_app import app

    entry = (app.conf.beat_schedule or {})["daily-digest-evening"]
    assert entry["task"] == "tasks.digest_tasks.publish_daily_digest_fanout"


@pytest.mark.asyncio
async def test_since_is_threaded_to_candidate_collection(monkeypatch):
    """Закреплённый ``since`` доходит до отбора кандидатов.

    Без этого хвост веера (с фото доходит после полуночи) взял бы «с полуночи»
    уже **нового** дня и собрал бы чужой сборник.
    """
    from modules.publisher import daily_digest

    seen: dict = {}
    marker = "2026-11-24T00:00:00"

    async def _fake_collect(session, region_code, *, since=None, limit=None, exclude_lips=()):
        seen["since"] = since
        return []

    async def _fake_collect_photo(session, region_code, *, since=None, limit=None, exclude_lips=()):
        seen["since_photo"] = since
        return []

    class _Scalars:
        def first(self):
            return type("RegionConfig", (), {"text_post_maxsize_simbols": 0})()

        def all(self):
            return []

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        async def execute(self, stmt):
            return _Result()

    class _Region:
        code = "mi"
        name = "Малмыж"
        vk_group_id = -1

    monkeypatch.setattr(daily_digest, "collect_candidates", _fake_collect)
    monkeypatch.setattr(daily_digest, "collect_photo_candidates", _fake_collect_photo)

    report = await digest_tasks._digest_for_region(
        _Session(), _Region(), dry_run=False, since=marker
    )

    assert seen["since"] == marker
    assert seen["since_photo"] == marker
    assert report["region"] == "mi"
