"""Unit-тесты для ``scripts/smoke_test.py`` — post-deploy smoke (dry-run).

Скрипт — CLI-утилита вне устанавливаемого пакета, грузим напрямую через
importlib (как ``tests/test_scripts/test_check_commit_msg.py``). Celery не
поднимаем: ``evaluate_result`` чистая, а ``run_smoke`` принимает инжектируемые
``submit``/``poll``/``sleep``/``now``/``log``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "smoke_test", REPO_ROOT / "scripts" / "smoke_test.py"
)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["smoke_test"] = smoke
_spec.loader.exec_module(smoke)


# --------------------------------------------------------------------------- #
# evaluate_result — чистая логика проверки dry_run-результата
# --------------------------------------------------------------------------- #


def _ok_result(posts: int = 3, scanned: int | None = None) -> Dict[str, Any]:
    """Dry-run словарь пайплайна.

    ``posts`` — сколько ОСТАЛОСЬ после отбора (``posts_parsed``);
    ``scanned`` — сколько ПРИНЕСЕНО из ВК (``stats.total_posts_scanned``).
    По умолчанию совпадают: «отбор ничего не выкинул».
    """
    return {
        "success": True,
        "dry_run": True,
        "region_code": "mi",
        "theme": "novost",
        "communities_count": 10,
        "posts_parsed": posts,
        "bulletins_count": 1,
        "stats": {"total_posts_scanned": posts if scanned is None else scanned},
    }


def test_evaluate_passes_on_success_and_enough_posts():
    assert smoke.evaluate_result(_ok_result(posts=5), min_posts=1) == []


def test_evaluate_passes_when_selection_dropped_everything():
    """Живой парсинг + пустой отбор — это ЗЕЛЁНЫЙ гейт.

    Первый из двух источников ложного нуля: редакционные фильтры/чёрный список
    отсеяли всё принесённое. Пайплайн при этом полностью здоров.
    Ложный красный 25.08.
    """
    assert smoke.evaluate_result(_ok_result(posts=0, scanned=42), min_posts=1) == []


def test_evaluate_passes_when_dedup_cursor_ate_the_wave():
    """Второй источник ложного нуля — курсор дедупа.

    Волна той же темы отработала недавно, посты помечены виденными, до отбора
    доходит ноль. PENDING знал только первый источник, поэтому вариант
    ``posts_final_count`` этот случай бы не закрыл: он тоже считается после
    отбора. Порог меряет до-отборный счётчик — и оба случая зелёные.
    """
    res = _ok_result(posts=0, scanned=17)
    res["bulletins_count"] = 0
    assert smoke.evaluate_result(res, min_posts=1) == []


def test_evaluate_fails_when_parsing_is_actually_dead():
    """А вот это гейт обязан красить: из ВК не принесено НИЧЕГО."""
    failures = smoke.evaluate_result(_ok_result(posts=0, scanned=0), min_posts=1)
    assert len(failures) == 1
    assert "принесено из ВК постов: 0" in failures[0]


def test_evaluate_fails_on_none_result():
    failures = smoke.evaluate_result(None, min_posts=1)
    assert len(failures) == 1
    assert "пустой результат" in failures[0]


def test_evaluate_fails_on_unsuccessful_pipeline():
    res = {"success": False, "error": "No active VK READ tokens (all in cooldown?)"}
    failures = smoke.evaluate_result(res, min_posts=1)
    assert len(failures) == 1
    assert "неуспех" in failures[0]
    assert "cooldown" in failures[0]


def test_evaluate_fails_when_posts_below_minimum():
    failures = smoke.evaluate_result(_ok_result(posts=0), min_posts=1)
    assert len(failures) == 1
    assert "принесено из ВК постов: 0" in failures[0]


def test_evaluate_fails_when_no_dry_run_data_and_min_positive():
    # Ранний success-возврат «нет communities для темы» — без posts_parsed.
    res = {
        "success": True,
        "message": "No 'novost' communities for region mi",
        "posts_published": 0,
    }
    failures = smoke.evaluate_result(res, min_posts=1)
    assert len(failures) == 1
    assert "не выполнен парсинг" in failures[0]


def test_evaluate_passes_with_min_zero_only_checks_success():
    res = {"success": True, "message": "No communities", "posts_published": 0}
    assert smoke.evaluate_result(res, min_posts=0) == []


# --------------------------------------------------------------------------- #
# run_smoke — оркестрация с инжектируемыми коллабораторами
# --------------------------------------------------------------------------- #


class _FakePoll:
    """Скриптованный опрос: отдаёт статусы по очереди, последний — повторяется."""

    def __init__(self, statuses: List[Dict[str, Any]]):
        self._statuses = list(statuses)
        self.calls: List[str] = []

    def __call__(self, task_id: str) -> Dict[str, Any]:
        self.calls.append(task_id)
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]


def _run(poll: _FakePoll, *, submit=lambda region, theme: "abc", **overrides) -> int:
    kwargs = dict(
        region="mi",
        theme="novost",
        min_posts=1,
        timeout=30,
        poll_interval=0,
        submit=submit,
        poll=poll,
        # Стаб по умолчанию: настоящий ``worker_progress`` ходит в брокер, а
        # юнит-тест не должен зависеть от живого Celery (и не должен ждать его
        # таймаут). Тесты про классификацию таймаута передают свой прогресс.
        progress=lambda: {},
        sleep=lambda _s: None,
        now=_fake_clock(),
        log=lambda _m: None,
    )
    kwargs.update(overrides)
    return smoke.run_smoke(**kwargs)


def _fake_clock(step: float = 1.0):
    """Монотонные «часы»: каждый вызов +step, чтобы дедлайн в итоге наступал."""
    state = {"t": 0.0}

    def _now() -> float:
        state["t"] += step
        return state["t"]

    return _now


def test_run_smoke_happy_path_returns_zero():
    submitted: List[tuple] = []

    def _submit(region, theme):
        submitted.append((region, theme))
        return "abc"

    poll = _FakePoll(
        [
            {"task_id": "abc", "state": "PROGRESS", "ready": False},
            {"task_id": "abc", "state": "SUCCESS", "ready": True, "result": _ok_result(2)},
        ]
    )
    assert _run(poll, submit=_submit) == 0
    assert submitted == [("mi", "novost")]
    assert poll.calls and all(t == "abc" for t in poll.calls)


def test_run_smoke_returns_2_when_submit_fails():
    def _boom(region, theme):
        raise RuntimeError("Celery недоступен")

    assert _run(_FakePoll([{}]), submit=_boom) == 2


def test_run_smoke_returns_2_when_no_task_id():
    assert _run(_FakePoll([{}]), submit=lambda r, t: "") == 2


def test_run_smoke_returns_1_on_task_failure():
    poll = _FakePoll([{"task_id": "abc", "state": "FAILURE", "ready": True, "error": "boom"}])
    assert _run(poll) == 1


def test_run_smoke_returns_busy_on_timeout_without_worker_stats():
    """Таймаут сам по себе — не провал: без статистики воркера это «не знаю»."""
    # Задача никогда не ready — дедлайн наступает (clock тикает быстрее timeout).
    poll = _FakePoll([{"state": "PROGRESS", "ready": False}])
    assert _run(poll, timeout=5, now=_fake_clock(step=2.0)) == smoke.EXIT_BUSY


# --------------------------------------------------------------------------- #
# Таймаут: «занято» ≠ «сломано» (exit 3 против exit 1)
# --------------------------------------------------------------------------- #


class _FakeProgress:
    """Счётчики воркера: снимок «до» и «после» ожидания."""

    def __init__(self, before: Dict[str, int], after: Dict[str, int]):
        self._before = before
        self._after = after
        self.calls = 0

    def __call__(self) -> Dict[str, int]:
        self.calls += 1
        return self._before if self.calls == 1 else self._after


def _timeout_run(progress, *, log=None, **overrides) -> int:
    poll = _FakePoll([{"state": "PENDING", "ready": False}])
    kwargs = dict(timeout=5, now=_fake_clock(step=2.0), progress=progress)
    if log is not None:
        kwargs["log"] = log
    return _run(poll, **kwargs)


def test_timeout_with_same_task_progress_is_not_proven_but_pipeline_alive():
    """Живой прогон 21.10: очередь из ~80 волн, diagnostics ждал 900 с.

    Прод при этом был здоров: волны публиковали. Гейт красил исправный деплой,
    потому что «PENDING» не отличает «занято» от «сломано». Если за время
    ожидания воркер выполнил задачи ТОГО ЖЕ вида — шов работает, и это exit 3.
    """
    lines: List[str] = []
    progress = _FakeProgress({"any": 10, "same_task": 4}, {"any": 18, "same_task": 7})

    rc = _timeout_run(progress, log=lines.append)

    assert rc == smoke.EXIT_BUSY == 3
    assert any("пайплайн жив" in ln and "3 задач" in ln for ln in lines), lines


def test_timeout_with_only_foreign_tasks_says_seam_not_proven():
    lines: List[str] = []
    progress = _FakeProgress({"any": 10, "same_task": 4}, {"any": 15, "same_task": 4})

    rc = _timeout_run(progress, log=lines.append)

    assert rc == smoke.EXIT_BUSY
    assert any("шов пайплайна этим не подтверждён" in ln for ln in lines), lines


def test_timeout_without_any_progress_is_a_real_failure():
    """Воркер ответил и не выполнил ничего — вот это уже поломка, а не занятость."""
    lines: List[str] = []
    progress = _FakeProgress({"any": 10, "same_task": 4}, {"any": 10, "same_task": 4})

    rc = _timeout_run(progress, log=lines.append)

    assert rc == smoke.EXIT_FAIL == 1
    assert any("не берёт работу" in ln for ln in lines), lines


def test_timeout_without_worker_stats_is_unknown_not_failure():
    """«Не знаю» нельзя выдавать ни за провал, ни за успех."""
    lines: List[str] = []
    progress = _FakeProgress({}, {})

    rc = _timeout_run(progress, log=lines.append)

    assert rc == smoke.EXIT_BUSY
    assert any("состояние не определено" in ln for ln in lines), lines


def test_progress_is_sampled_before_and_after_the_wait():
    progress = _FakeProgress({"any": 1, "same_task": 1}, {"any": 2, "same_task": 1})

    _timeout_run(progress)

    assert progress.calls == 2


def test_count_completed_splits_same_task_from_everything_else():
    stats = {
        "celery@box": {"total": {smoke.TASK_NAME: 3, "tasks.celery_app.dispatch_ad_reposts": 10}},
        "celery@other": {"total": {smoke.TASK_NAME: 2}},
    }

    assert smoke._count_completed(stats) == {"any": 15, "same_task": 5}


def test_run_smoke_returns_1_when_result_fails_evaluation():
    poll = _FakePoll(
        [
            {
                "task_id": "abc",
                "state": "SUCCESS",
                "ready": True,
                "result": {"success": False, "error": "No VK group ID for region mi"},
            }
        ]
    )
    assert _run(poll) == 1


def test_run_smoke_respects_min_posts_threshold():
    poll = _FakePoll(
        [{"task_id": "abc", "state": "SUCCESS", "ready": True, "result": _ok_result(1)}]
    )
    assert _run(poll, min_posts=5) == 1


# --------------------------------------------------------------------------- #
# main — разбор аргументов
# --------------------------------------------------------------------------- #


def test_main_passes_args_through(monkeypatch):
    captured: Dict[str, Any] = {}

    def _fake_run_smoke(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(smoke, "run_smoke", _fake_run_smoke)
    rc = smoke.main(["--region", "vp", "--theme", "sport", "--min-posts", "2"])
    assert rc == 0
    assert captured["region"] == "vp"
    assert captured["theme"] == "sport"
    assert captured["min_posts"] == 2


def test_success_line_names_the_counter_it_measured():
    """Отчёт обязан печатать величину, ПО КОТОРОЙ принято решение.

    Живой прогон 2026-09-01 прошёл гейт и напечатал «OK: posts_parsed=0» — то
    есть отчёт называл счётчик, который гейт уже не смотрит. Читателю остаётся
    заново разгадывать, почему ноль, но OK. Та же болезнь, от которой лечили
    сам порог, только в строке вывода.
    """
    lines: List[str] = []
    poll = _FakePoll(
        [
            {
                "task_id": "abc",
                "state": "SUCCESS",
                "ready": True,
                "result": _ok_result(posts=0, scanned=260),
            }
        ]
    )
    assert _run(poll, log=lines.append) == 0
    ok = [ln for ln in lines if "OK:" in ln]
    assert ok, "успешный прогон ничего не напечатал"
    assert "принесено из ВК=260" in ok[0]
    assert "после отбора=0" in ok[0]
