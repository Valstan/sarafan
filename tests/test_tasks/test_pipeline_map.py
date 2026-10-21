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
    rows = pm.attach_regions(pm.wave_slots(schedule), {"admin": 71, "kultura": 73})

    hours = pm.group_by_hour(rows)

    assert len(hours) == 1
    assert hours[0]["hour"] == 8
    assert hours[0]["regions_total"] == 144
    assert hours[0]["themes"] == ["admin", "kultura"]


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
    rows = pm.wave_slots({"a": _wave("a", "novost", hour="6")}) + pm.wave_slots(
        {"b": _wave("b", "detsad", hour="7")}
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
