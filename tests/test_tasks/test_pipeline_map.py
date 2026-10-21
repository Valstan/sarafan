"""Карта конвейера: расписание волн, состав слота и цена одного поста.

Зачем гейт. Карта — это витрина, а витрина врёт тихо: если расписание начнут
править в beat, а карта останется прежней, владелец будет принимать решения по
вчерашней правде и не узнает об этом. Поэтому проверяется не «страница
нарисовалась», а два свойства источника:

* строки карты выводятся **из самого** ``beat_schedule`` (а не из списка,
  продублированного в шаблоне): новая запись волны обязана появиться сама;
* отбор регионов у карты и у таска — **один и тот же** вызов
  (``modules.wave_targets``): иначе карта покажет не то, что поедет.
"""

from __future__ import annotations

from celery.schedules import crontab

from modules import pipeline_map as pm
from tasks import parsing_scheduler_tasks


def _wave(name: str, theme: str, *, minute: int = 20, hour="6,12", expires: int = 3600):
    return {
        "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
        "schedule": crontab(minute=minute, hour=hour),
        "args": (theme,),
        "options": {"expires": expires},
        "_name": name,
    }


def test_slots_come_from_beat_schedule_and_expand_every_hour():
    schedule = {"postopus-novost-6": _wave("postopus-novost-6", "novost")}

    rows = pm.wave_slots(schedule)

    assert [r["time"] for r in rows] == ["06:20", "12:20"]
    assert {r["theme"] for r in rows} == {"novost"}
    assert {r["expires"] for r in rows} == {3600}


def test_new_beat_entry_appears_without_touching_the_page():
    """Добавили слот в beat — он обязан появиться в карте сам."""
    schedule = {
        "postopus-novost-6": _wave("postopus-novost-6", "novost"),
        "postopus-kultura-19": _wave("postopus-kultura-19", "kultura", minute=20, hour="19"),
    }

    rows = pm.wave_slots(schedule)

    assert ("19:20", "kultura") in {(r["time"], r["theme"]) for r in rows}


def test_non_wave_entries_are_not_drawn_as_waves():
    schedule = {
        "ad-repost-dispatch": {
            "task": "tasks.celery_app.dispatch_ad_reposts",
            "schedule": crontab(minute="*"),
            "options": {"expires": 55},
        },
        "postopus-novost-6": _wave("postopus-novost-6", "novost"),
    }

    rows = pm.wave_slots(schedule)

    assert len(rows) == 2  # только два часа у novost
    assert all(r["task"].endswith(pm.WAVE_TASK) for r in rows)


def test_interval_entries_are_not_pretended_to_be_clock_slots():
    from datetime import timedelta

    schedule = {
        "every-minute": {
            "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
            "schedule": timedelta(seconds=60),
            "args": ("novost",),
        }
    }

    assert pm.wave_slots(schedule) == []


def test_hour_group_counts_regions_that_will_land_in_the_slot():
    schedule = {
        "postopus-admin-8": _wave("postopus-admin-8", "admin", hour="8"),
        "postopus-kultura-8": _wave("postopus-kultura-8", "kultura", minute=50, hour="8"),
    }
    rows = pm.attach_regions(
        pm.wave_slots(schedule),
        {pm.region_key("admin", False): 71, pm.region_key("kultura", False): 73},
    )

    hours = pm.group_by_hour(rows)

    assert len(hours) == 1
    assert hours[0]["hour"] == 8
    assert hours[0]["regions_total"] == 144
    assert hours[0]["themes"] == ["admin", "kultura"]


def test_strict_theme_is_counted_by_its_own_regions():
    """Областная тема идёт строгим гейтом: два региона, а не семьдесят пять.

    Живой промах 21.10: карта посчитала волны ``proisshestviya`` по мягкому
    гейту и показала 2 925 волн в сутки вместо ~1 600. Цифра выглядела
    правдоподобно — ровно поэтому нужен гейт.
    """
    schedule = {
        "postopus-proisshestviya-8": {
            "task": "tasks.parsing_scheduler_tasks.run_all_regions_theme",
            "schedule": crontab(minute=50, hour="8"),
            "kwargs": {"theme": "proisshestviya", "strict": True},
        },
        "postopus-admin-8": _wave("postopus-admin-8", "admin", hour="8"),
    }
    rows = pm.attach_regions(
        pm.wave_slots(schedule),
        {pm.region_key("proisshestviya", True): 2, pm.region_key("admin", False): 71},
    )

    by_theme = {r["theme"]: r for r in rows}

    assert by_theme["proisshestviya"]["strict"] is True
    assert by_theme["proisshestviya"]["regions"] == 2
    assert by_theme["admin"]["strict"] is False
    assert by_theme["admin"]["regions"] == 71


def test_theme_table_shows_what_a_post_costs_in_waves():
    stats = [
        {
            "theme": "novost",
            "runs": 1000,
            "published": 100,
            "avg_seconds": 20.0,
            "max_run_date": "21.10 09:00",
        },
        {
            "theme": "detsad",
            "runs": 400,
            "published": 4,
            "avg_seconds": 5.0,
            "max_run_date": "21.10 08:00",
        },
    ]
    rows = pm.attach_regions(
        pm.wave_slots({"a": _wave("a", "novost", hour="6")})
        + pm.wave_slots({"b": _wave("b", "detsad", hour="7")}),
        {pm.region_key("novost", False): 75, pm.region_key("detsad", False): 59},
    )

    table = {
        t["theme"]: t
        for t in pm.theme_table(
            wave_rows=rows, stats=stats, region_counts={"novost": 75, "detsad": 59}
        )
    }

    assert table["novost"]["waves_per_post"] == 10.0
    assert table["novost"]["yield_pct"] == 10.0
    assert table["novost"]["waves_per_day"] == 75.0
    assert table["detsad"]["waves_per_post"] == 100.0


def test_theme_without_publications_has_no_cost_per_post():
    stats = [{"theme": "priroda", "runs": 20, "published": 0, "avg_seconds": 10.0}]
    rows = pm.wave_slots({"a": _wave("a", "priroda", hour="9")})

    table = pm.theme_table(wave_rows=rows, stats=stats, region_counts={"priroda": 2})

    assert table[0]["waves_per_post"] is None
    assert table[0]["yield_pct"] == 0.0


def test_task_and_map_use_the_same_region_selector(monkeypatch):
    """Таск волны обязан брать регионы общим отбором, а не своей копией SQL."""
    seen = {}

    async def fake_select(session, *, theme, strict=False):
        seen["theme"] = theme
        seen["strict"] = strict
        return ["mi", "ur"]

    dispatched = []

    class _FakeDelay:
        def delay(self, region_code, theme):
            dispatched.append((region_code, theme))

            class _R:
                id = f"task-{region_code}"

            return _R()

    monkeypatch.setattr("modules.wave_targets.select_regions", fake_select)
    monkeypatch.setattr(parsing_scheduler_tasks, "parse_and_publish_theme", _FakeDelay())

    result = parsing_scheduler_tasks.run_all_regions_theme("novost")

    assert seen == {"theme": "novost", "strict": False}
    assert dispatched == [("mi", "novost"), ("ur", "novost")]
    assert result["regions"] == ["mi", "ur"]


def test_schedule_is_read_from_cron_not_from_the_comment():
    """Подпись расписания выводится из самого crontab.

    Живой промах: у ``reconcile-scheduled-publications`` два месяца стоял
    комментарий «каждые 30 мин» при часовом слоте. Карта, повторяющая
    комментарий, повторяет и ошибку — поэтому подпись считается из полей.
    """
    assert pm.describe_schedule(crontab(minute="*"))["label"] == "каждую минуту"
    assert (
        pm.describe_schedule(crontab(minute="*/15", hour="8-23"))["label"] == "каждые 15 мин, 08–23"
    )
    assert pm.describe_schedule(crontab(minute=35, hour="*/3"))["label"] == "каждые 3 ч в :35"
    assert pm.describe_schedule(crontab(minute=20, hour="12,22"))["label"] == "12:20, 22:20"


def test_weekly_entry_is_one_run_a_week_not_one_a_day():
    described = pm.describe_schedule(crontab(minute=40, hour=4, day_of_week=1))

    assert described["label"].startswith("пн")
    assert described["runs_per_day"] == 0.14


def test_interval_schedule_is_described_as_interval_not_as_clock():
    from datetime import timedelta

    described = pm.describe_schedule(timedelta(minutes=30))

    assert described["kind"] == "interval"
    assert described["label"] == "каждые 30 мин"
    assert described["runs_per_day"] == 48.0


def test_periodic_block_does_not_swallow_waves():
    schedule = {
        "postopus-novost-6": _wave("postopus-novost-6", "novost", hour="6"),
        "ad-repost-dispatch": {
            "task": "tasks.celery_app.dispatch_ad_reposts",
            "schedule": crontab(minute="*"),
            "options": {"expires": 55},
        },
    }

    rows = pm.periodic_slots(schedule)

    assert [r["name"] for r in rows] == ["ad-repost-dispatch"]
    assert rows[0]["runs_per_day"] == 1440.0
    assert rows[0]["task_short"] == "dispatch_ad_reposts"


def test_periodic_table_costs_a_task_in_slot_minutes():
    """8 прогонов в сутки по 589 с — это 78.5 минуты слота, а не «мелочь»."""
    schedule = {
        "classifier-headless": {
            "task": "tasks.celery_app.classify_pending_posts",
            "schedule": crontab(minute=35, hour="*/3"),
            "options": {"expires": 3 * 3600},
        }
    }
    rows = pm.periodic_slots(schedule)

    table = pm.periodic_table(
        rows,
        {"tasks.celery_app.classify_pending_posts": {"avg_seconds": 589.0, "samples": 4}},
    )

    assert table[0]["minutes_per_day"] == 78.5
    assert table[0]["samples"] == 4
    assert pm.periodic_totals(table)["minutes_per_day"] == 78.5


def test_unmeasured_task_shows_blank_not_zero():
    """Ноль читается как «ничего не занимает» — поэтому неизмеренное это None."""
    rows = pm.periodic_slots(
        {"x": {"task": "tasks.demo.nothing", "schedule": crontab(minute=0, hour=3)}}
    )

    table = pm.periodic_table(rows, {})

    assert table[0]["minutes_per_day"] is None
    assert table[0]["avg_seconds"] is None
    assert pm.periodic_totals(table) == {
        "tasks": 1,
        "measured": 0,
        "minutes_per_day": 0,
        "top": None,
        "top_minutes": None,
    }


def test_periodic_table_is_sorted_by_what_it_costs_the_worker():
    rows = [
        {"name": "cheap", "task": "t.cheap", "runs_per_day": 1440.0},
        {"name": "expensive", "task": "t.expensive", "runs_per_day": 8.0},
    ]
    timings = {
        "t.cheap": {"avg_seconds": 1.0},
        "t.expensive": {"avg_seconds": 589.0},
    }

    table = pm.periodic_table(rows, timings)

    assert [r["name"] for r in table] == ["expensive", "cheap"]
