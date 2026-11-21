"""Tests for parsing scheduler region pre-filtering."""

import sys
import types
from unittest.mock import MagicMock, patch


def test_run_all_regions_theme_schedules_only_eligible_regions():
    celery_stub = types.ModuleType("celery")

    def _shared_task_stub(*args, **kwargs):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]

        def _decorator(fn):
            return fn

        return _decorator

    celery_stub.shared_task = _shared_task_stub
    with patch.dict(sys.modules, {"celery": celery_stub}):
        from tasks import parsing_scheduler_tasks as scheduler_tasks

        scheduled = []

        def _fake_apply_async(args=None, countdown=None, expires=None, **kwargs):
            region_code, theme = args
            task = MagicMock()
            task.id = f"task-{region_code}-{theme}"
            scheduled.append((region_code, theme, countdown))
            return task

        parse_task_mock = MagicMock()
        parse_task_mock.apply_async = MagicMock(side_effect=_fake_apply_async)

        def _fake_run_coro(coro):
            coro.close()
            return ["mi", "ur"]

        with patch("tasks.parsing_scheduler_tasks.run_coro", side_effect=_fake_run_coro):
            with patch("tasks.parsing_scheduler_tasks.parse_and_publish_theme", parse_task_mock):
                with patch.object(scheduler_tasks, "_wave_region_spread_seconds", lambda: 0.0):
                    result = scheduler_tasks.run_all_regions_theme("novost")

        # Без разведения все регионы стартуют сразу (прежнее поведение).
        assert scheduled == [("mi", "novost", 0), ("ur", "novost", 0)]
        assert result["regions"] == ["mi", "ur"]
        assert result["theme"] == "novost"
        assert result["tasks"] == ["task-mi-novost", "task-ur-novost"]


def test_run_all_regions_theme_spreads_regions_over_the_window():
    """Регионы волны получают свои минуты: публикация идёт равномерно.

    Раньше все регионы темы стартовали в одну минуту — сорок bulletins
    подряд (замер 15.11: 458 публикаций за сутки, медианный интервал 12 с).
    """
    celery_stub = types.ModuleType("celery")

    def _shared_task_stub(*args, **kwargs):
        if args and callable(args[0]) and len(args) == 1 and not kwargs:
            return args[0]

        def _decorator(fn):
            return fn

        return _decorator

    celery_stub.shared_task = _shared_task_stub
    with patch.dict(sys.modules, {"celery": celery_stub}):
        from tasks import parsing_scheduler_tasks as scheduler_tasks

        scheduled = []

        def _fake_apply_async(args=None, countdown=None, expires=None, **kwargs):
            region_code, theme = args
            task = MagicMock()
            task.id = f"task-{region_code}-{theme}"
            scheduled.append((region_code, countdown, expires))
            return task

        parse_task_mock = MagicMock()
        parse_task_mock.apply_async = MagicMock(side_effect=_fake_apply_async)

        def _fake_run_coro(coro):
            coro.close()
            return ["ur", "mi", "or"]

        with patch("tasks.parsing_scheduler_tasks.run_coro", side_effect=_fake_run_coro):
            with patch("tasks.parsing_scheduler_tasks.parse_and_publish_theme", parse_task_mock):
                with patch.object(scheduler_tasks, "_wave_region_spread_seconds", lambda: 600.0):
                    result = scheduler_tasks.run_all_regions_theme("novost")

        # Порядок по коду (mi, or, ur) — слоты не прыгают день ото дня.
        assert [item[0] for item in scheduled] == ["mi", "or", "ur"]
        assert [item[1] for item in scheduled] == [0, 300, 600]
        # Задача не должна протухнуть, пока ждёт слот.
        assert scheduled[0][2] == 3600
        assert scheduled[2][2] == 4200
        assert result["regions"] == ["mi", "or", "ur"]


def _wave_dispatch(regions):
    """Прогоняет волну и возвращает список (регион, очередь) задач.

    Общий каркас двух тестов ниже: модуль целиком подменяет ``celery`` (в
    тестовой среде его нет), а ``apply_async`` волны — заглушкой, которая
    запоминает, в какую очередь ушла задача.
    """
    celery_stub = types.ModuleType("celery")
    celery_stub.shared_task = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
    dispatched = []

    def _fake_apply_async(args=None, countdown=None, expires=None, queue=None, **kwargs):
        region_code, theme = args
        task = MagicMock()
        task.id = f"task-{region_code}-{theme}"
        dispatched.append((region_code, theme, queue))
        return task

    with patch.dict(sys.modules, {"celery": celery_stub}):
        from tasks import parsing_scheduler_tasks as scheduler_tasks

        parse_task_mock = MagicMock()
        parse_task_mock.apply_async = MagicMock(side_effect=_fake_apply_async)

        def _fake_run_coro(coro):
            coro.close()
            return list(regions)

        with patch("tasks.parsing_scheduler_tasks.run_coro", side_effect=_fake_run_coro):
            with patch("tasks.parsing_scheduler_tasks.parse_and_publish_theme", parse_task_mock):
                with patch.object(scheduler_tasks, "_wave_region_spread_seconds", lambda: 0.0):
                    scheduler_tasks.run_all_regions_theme("novost")

    return dispatched


def test_wave_regions_go_to_the_wave_queue():
    """Веер волны уходит в отдельную очередь (P233, ADR-0020).

    На общей очереди 81 задача занимает **голову** списка Redis, и всё, что
    beat отправляет позже, стоит за ней. Замер 21.11 (волна 16:40):
    `dispatch_ad_reposts` не выполнился ни разу за час (16:41–17:40) при том,
    что beat отправлял его каждую минуту, а сторож сводок опоздал на
    20 минут.
    """
    dispatched = _wave_dispatch(["mi", "ur"])

    assert dispatched == [
        ("mi", "novost", "wave"),
        ("ur", "novost", "wave"),
    ]


def test_wave_queue_name_comes_from_env_for_a_one_line_rollback(monkeypatch):
    """Имя очереди живёт в env: откат на проде — одна строка, без отката кода.

    ``SETKA_WAVE_QUEUE=celery`` возвращает веер в дефолтную очередь; юнит
    воркера продолжает слушать обе (лишняя очередь без задач просто пуста).
    """
    monkeypatch.setenv("SETKA_WAVE_QUEUE", "celery")

    dispatched = _wave_dispatch(["mi"])

    assert dispatched == [("mi", "novost", "celery")]


class _CapturingSession:
    """Async session double that records executed statements."""

    def __init__(self, captured):
        self._captured = captured

    async def execute(self, stmt):
        self._captured.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result


class _CapturingSessionCM:
    def __init__(self, captured):
        self._captured = captured

    async def __aenter__(self):
        return _CapturingSession(self._captured)

    async def __aexit__(self, *exc):
        return False


def _compiled_region_gate_sql(theme, strict):
    """Run the real ``_get_regions`` query and return its compiled SQL.

    Patches ``AsyncSessionLocal`` so the coroutine executes for real (via
    ``run_coro``) but against a session double that just records the statement.
    """
    celery_stub = types.ModuleType("celery")
    celery_stub.shared_task = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda f: f))
    with patch.dict(sys.modules, {"celery": celery_stub}):
        from tasks import parsing_scheduler_tasks as scheduler_tasks

        captured = []
        with patch(
            "database.connection.AsyncSessionLocal",
            lambda: _CapturingSessionCM(captured),
        ):
            with patch("tasks.parsing_scheduler_tasks.parse_and_publish_theme", MagicMock()):
                result = scheduler_tasks.run_all_regions_theme(theme, strict=strict)

    assert result["regions"] == []
    assert captured, "expected the region-selection query to be executed"
    # Default (dialect-agnostic) compiler — не дёргает psycopg2/asyncpg DBAPI;
    # оператор ``->>`` и ex()-подзапрос рендерятся как есть.
    return str(captured[0].compile(compile_kwargs={"literal_binds": True}))


def test_region_gate_admits_community_mode_oblast_without_region_config():
    """community-mode регионы (config.bulletin_mode='communities') должны попадать
    в тематическую волну даже без строки RegionConfig (баг kirov_obl, 2026-05).

    Без этого область, переведённая на собственный пул, молча выпадала из
    каждой волны: каскад снят, а RegionConfig-гейт её не пускал.
    """
    for strict in (True, False):
        sql = _compiled_region_gate_sql("nauka", strict=strict).lower()
        # OR-ветка community-mode присутствует
        assert "bulletin_mode" in sql
        assert "communities" in sql
        # старая ветка (наличие RegionConfig) сохранена
        assert "region_configs" in sql


def test_region_gate_admits_region_with_community_pool_without_region_config():
    """Район с активным пулом communities попадает в волну даже без строки
    RegionConfig и без bulletin_mode (онбординг-фикс 2026-06, баг Тужи).

    ``config_gate`` теперь содержит OR-ветку ``has_any_communities`` — поэтому в
    скомпилированном SQL появляется дополнительный EXISTS по ``communities``
    (помимо ``community_gate``): для strict-темы их становится 2, для non-strict
    — 3. До фикса было 1 и 2 соответственно.
    """
    strict_sql = _compiled_region_gate_sql("nauka", strict=True).lower()
    nonstrict_sql = _compiled_region_gate_sql("novost", strict=False).lower()
    # strict:    community_gate=has_theme (1) + config_gate.has_any (1) = 2
    assert strict_sql.count("from communities") == 2
    # non-strict: community_gate=has_theme|has_any (2) + config_gate.has_any (1) = 3
    assert nonstrict_sql.count("from communities") == 3
    # старые ветки config_gate сохранены (RegionConfig + bulletin_mode)
    assert "region_configs" in strict_sql
    assert "bulletin_mode" in strict_sql
