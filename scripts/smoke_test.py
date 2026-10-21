#!/usr/bin/env python3
"""Post-deploy smoke-test для SETKA: dry-run прогон пайплайна без публикации.

После рестарта сервисов (`/reliz` Шаг 8) полезно за один шаг убедиться, что
пайплайн региона жив: токены валидны, VK отвечает, парсинг → фильтр → сборка
сводки проходят. Возвращает exit-код, пригодный для автоматической проверки
в `/reliz`.

Механика: ставит ``parse_and_publish_theme(dry_run=True)`` **напрямую в Celery**
и опрашивает ``AsyncResult`` — тем же способом, что web-эндпоинт diagnostics
(``web/api/regions.py``), но минуя web-слой: diagnostics-эндпоинты живут под
операторской сессией, и у post-deploy скрипта нет cookie (старый HTTP-путь
падал на 401). Публикация и запись в БД при ``dry_run=True`` не происходят.

Использование (на проде, из venv проекта — скрипт импортирует
``tasks.celery_app``, поэтому чужим интерпретатором не запускается):
    ssh sarafan "cd ~/SETKA && ./venv/bin/python scripts/smoke_test.py"
    ssh sarafan "cd ~/SETKA && ./venv/bin/python scripts/smoke_test.py \\
        --region mi --theme novost --min-posts 1"

Exit 0 — smoke прошёл; exit 1 — провал (детали в stderr); exit 2 — ошибка
аргументов/постановки задачи; exit 3 — **не доказано**: diagnostics-задача ещё
стоит в очереди, но воркер при этом жив (см. ниже). Чистая логика вынесена в
``evaluate_result``, оркестрация ``run_smoke`` принимает инжектируемые
``submit``/``poll``/``progress`` — юнит-тесты не поднимают Celery.

**Почему существует exit 3.** Диагностическая задача встаёт в хвост той же
очереди, что и боевые волны. 21.10.2026 очередь держала ~80 волн по ~35 с, и
smoke честно не дождался своей очереди ни за 180 с, ни за 900 с — при полностью
здоровом проде (health 200, волны публикуют). Гейт при этом краснел **на
исправном деплое**: «PENDING» одинаково выглядит и когда воркер не берёт работу
(поломка), и когда он занят чужой работой (норма). Различить их можно только
снаружи — по счётчикам выполненных задач, и это делает ``worker_progress``.
Exit 1 остаётся за настоящей поломкой: воркер ответил и не выполнил **ничего**.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable, Dict, List, Optional

DEFAULT_REGION = "mi"
DEFAULT_THEME = "novost"
DEFAULT_MIN_POSTS = 1
DEFAULT_TIMEOUT = 180  # сек на весь dry-run (реальный VK-парсинг бывает долгим)
DEFAULT_POLL_INTERVAL = 3  # сек между опросами статуса задачи

# Коды возврата. EXIT_BUSY отделён от EXIT_FAIL сознательно: «не дождались
# очереди» и «воркер не берёт работу» — разные факты, и раньше они были одним
# красным. См. историю в docstring модуля.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SETUP = 2
EXIT_BUSY = 3

TASK_NAME = "tasks.parsing_scheduler_tasks.parse_and_publish_theme"


def evaluate_result(result: Optional[Dict[str, Any]], min_posts: int) -> List[str]:
    """Проверить dry_run-результат. Возвращает список провалов (пусто = OK).

    Чистая функция (без сети) — вся диагностика smoke-теста тут, чтобы её можно
    было покрыть юнит-тестами без поднятия API/Celery.
    """
    failures: List[str] = []
    if result is None:
        return ["задача вернула пустой результат (result=None)"]

    if not result.get("success"):
        reason = result.get("error") or result.get("message") or "success=False без причины"
        failures.append(f"пайплайн вернул неуспех: {reason}")
        # Нет смысла проверять посты, если сам прогон провалился.
        return failures

    # Порог меряется по ``stats.total_posts_scanned`` — счётчику постов, ПРИНЕСЁННЫХ
    # из ВК, до какой-либо фильтрации (``advanced_parser`` инкрементит его перед
    # ``_filter_post``). Гейт спрашивает «жив ли парсинг», и мерить надо именно это.
    #
    # Раньше мерили ``posts_parsed`` — длину списка ПОСЛЕ отбора. У такого нуля
    # два независимых источника, и ни один не является поломкой:
    #   1. отбор отсеял всё (редакционные фильтры, чёрный список, возраст);
    #   2. курсор дедупа — волна той же темы отработала недавно, и посты уже
    #      помечены как виденные (записано в SESSION_HANDOFF 31.08).
    # Оба давали красный гейт на здоровом проде — два ложных красных подряд
    # 25.08 и 26.08. Гейт, который врёт, перестают читать, и он превращается в
    # шум ровно к тому моменту, когда покраснеет по делу.
    #
    # Вариант «а» из PENDING предлагал ``posts_final_count`` — он тоже считается
    # ПОСЛЕ отбора и закрыл бы только первый источник. Поэтому здесь взят
    # до-отборный счётчик: он закрывает оба разом.
    stats = result.get("stats") or {}
    scanned = stats.get("total_posts_scanned")

    if scanned is None:
        # Ранние success-возвраты («нет communities для темы») статистики не
        # содержат вовсе — для эталонного региона это тоже провал smoke.
        if min_posts > 0:
            msg = result.get("message", "посты не парсились (нет dry_run-данных)")
            failures.append(f"не выполнен парсинг постов: {msg}")
    elif scanned < min_posts:
        failures.append(
            f"принесено из ВК постов: {scanned} < ожидаемого минимума {min_posts} "
            f"(после отбора осталось {result.get('posts_parsed', '?')})"
        )

    return failures


def submit_dry_run(region: str, theme: str) -> str:
    """Поставить dry-run задачу пайплайна в Celery, вернуть task_id.

    Импорты внутри: скрипт остаётся импортируемым без Celery (для юнит-тестов
    оркестрации с фейковыми ``submit``/``poll``)."""
    from tasks.celery_app import app as celery_app

    task = celery_app.send_task(
        TASK_NAME, kwargs={"region_code": region, "theme": theme, "dry_run": True}
    )
    return task.id


def poll_task(task_id: str) -> Dict[str, Any]:
    """Статус Celery-задачи — та же форма словаря, что web-эндпоинт
    ``/api/regions/diagnostics/task/{id}/status`` (см. web/api/regions.py)."""
    from celery.result import AsyncResult

    from tasks.celery_app import app as celery_app

    ar = AsyncResult(task_id, app=celery_app)
    state = ar.state
    payload: Dict[str, Any] = {
        "task_id": task_id,
        "state": state,
        "ready": ar.ready(),
        "result": None,
        "error": None,
    }
    if state == "SUCCESS":
        try:
            payload["result"] = ar.result
        except Exception as e:  # pragma: no cover - тонкая обёртка backend'а
            payload["error"] = f"не удалось получить result: {e}"
    elif state == "FAILURE":
        try:
            payload["error"] = str(ar.result)
        except Exception:  # pragma: no cover
            payload["error"] = "задача завершилась с ошибкой"
    return payload


def worker_progress() -> Dict[str, int]:
    """Счётчики выполненных задач воркера — чем «занят» отличается от «сломан».

    ``inspect.stats()['total']`` — сколько задач воркер выполнил с момента
    старта, по именам. Нам нужны две величины:

    * ``same_task`` — выполнено ровно того же таска (``parse_and_publish_theme``).
      Это **сильное** доказательство: шов пайплайна отработал на этом же коде,
      значит он жив, а diagnostics просто не дошёл до головы очереди;
    * ``any`` — выполнено хоть чего-нибудь. Слабее (шов не подтверждён), но
      уже исключает «воркер не берёт работу вовсе».

    Пустой словарь — статистику получить не удалось (воркер не ответил за
    таймаут). Это не ноль: «не знаю» нельзя выдавать за «ничего не делал».
    """
    try:
        from tasks.celery_app import app as celery_app

        stats = celery_app.control.inspect(timeout=2.0).stats() or {}
    except Exception:  # pragma: no cover - зависит от брокера
        return {}

    return _count_completed(stats)


def _count_completed(stats: Dict[str, Any]) -> Dict[str, int]:
    """``inspect.stats()`` → ``{"any": N, "same_task": M}``. Чистая функция."""
    total = 0
    same_task = 0
    for data in (stats or {}).values():
        counters = (data or {}).get("total") or {}
        for name, value in counters.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            total += count
            if name == TASK_NAME:
                same_task += count
    return {"any": total, "same_task": same_task}


def _progress_delta(before: Dict[str, int], after: Dict[str, int], key: str) -> int:
    if key not in before or key not in after:
        return 0
    return max(0, int(after[key]) - int(before[key]))


def _classify_pending(
    last: Dict[str, Any],
    timeout: float,
    before: Dict[str, int],
    progress: Callable[[], Dict[str, int]],
    log: Callable[[str], None],
) -> int:
    """Задача не дождалась очереди: отличить «занято» от «сломано».

    Возвращает ``EXIT_BUSY`` (не доказано, но и не провал деплоя) либо
    ``EXIT_FAIL`` — когда воркер ответил и не выполнил ничего: это уже похоже
    на «он не берёт работу».
    """
    state = last.get("state")
    task_short = TASK_NAME.split(".")[-1]
    try:
        after = progress()
    except Exception:  # pragma: no cover - чужой код
        after = {}

    if not after:
        log(
            f"[smoke] НЕ ДОКАЗАНО: задача не завершилась за {timeout:.0f}s (state={state}), "
            "и статистику воркера получить не удалось — состояние не определено (exit 3)"
        )
        return EXIT_BUSY

    same = _progress_delta(before, after, "same_task")
    any_done = _progress_delta(before, after, "any")

    if same > 0:
        log(
            f"[smoke] НЕ ДОКАЗАНО, но пайплайн жив: diagnostics не дошёл до головы очереди "
            f"за {timeout:.0f}s (state={state}), зато воркер выполнил {same} задач "
            f"{task_short} на этом же коде — шов работает (exit 3)"
        )
        return EXIT_BUSY
    if any_done > 0:
        log(
            f"[smoke] НЕ ДОКАЗАНО: diagnostics не дошёл до головы очереди за {timeout:.0f}s "
            f"(state={state}); воркер выполнил {any_done} других задач, но ни одной "
            f"{task_short} — шов пайплайна этим не подтверждён (exit 3)"
        )
        return EXIT_BUSY

    log(
        f"[smoke] FAIL: задача не завершилась за {timeout:.0f}s (state={state}), и воркер "
        "не выполнил ни одной задачи — похоже, он не берёт работу вовсе"
    )
    return EXIT_FAIL


def run_smoke(
    region: str,
    theme: str,
    min_posts: int,
    timeout: float,
    poll_interval: float,
    *,
    submit: Callable[[str, str], str] = submit_dry_run,
    poll: Callable[[str], Dict[str, Any]] = poll_task,
    progress: Callable[[], Dict[str, int]] = worker_progress,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
) -> int:
    """Оркестрация smoke-теста. Возвращает exit-код (0 OK / 1 fail / 2 ошибка / 3 не доказано).

    Коллабораторы (``submit``/``poll``/``progress``/``sleep``/``now``/``log``)
    инжектируются для тестов.
    """
    log(f"[smoke] dry-run пайплайна region={region} theme={theme} (Celery напрямую)")

    try:
        task_id = submit(region, theme)
    except Exception as e:
        log(f"[smoke] FAIL: не удалось поставить dry-run задачу: {e}")
        return EXIT_SETUP
    if not task_id:
        log("[smoke] FAIL: постановка задачи не вернула task_id")
        return EXIT_SETUP
    log(f"[smoke] задача поставлена: task_id={task_id}, опрос до {timeout:.0f}s")

    # Снимок счётчиков сразу после постановки: интересен прирост ЗА ВРЕМЯ
    # ожидания, а не абсолютное число (воркер мог отработать тысячу задач до нас).
    try:
        before = progress()
    except Exception:  # pragma: no cover - инжектируется, но чужой код
        before = {}

    deadline = now() + timeout
    last: Dict[str, Any] = {}
    while now() < deadline:
        try:
            last = poll(task_id)
        except Exception as e:  # pragma: no cover - transient backend
            log(f"[smoke] предупреждение: опрос статуса упал ({e}), повтор…")
            sleep(poll_interval)
            continue

        if last.get("ready"):
            break
        sleep(poll_interval)
    else:
        return _classify_pending(last, timeout, before, progress, log)

    if last.get("state") == "FAILURE":
        log(f"[smoke] FAIL: задача упала: {last.get('error')}")
        return EXIT_FAIL

    failures = evaluate_result(last.get("result"), min_posts)
    if failures:
        for f in failures:
            log(f"[smoke] FAIL: {f}")
        return EXIT_FAIL

    result = last.get("result") or {}
    # Первым печатаем счётчик, ПО КОТОРОМУ гейт принял решение. Иначе успешный
    # прогон выглядит как «OK: posts_parsed=0» — отчёт называет не ту величину,
    # которую мерил, и читатель заново решает загадку «почему ноль, но OK».
    # Ровно та болезнь, от которой лечили сам порог.
    scanned = (result.get("stats") or {}).get("total_posts_scanned")
    log(
        "[smoke] OK: "
        f"принесено из ВК={scanned}, "
        f"после отбора={result.get('posts_parsed')}, "
        f"would_publish={result.get('bulletins_count')}, "
        f"communities={result.get('communities_count')}"
    )
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default=DEFAULT_REGION, help="код эталонного региона")
    parser.add_argument("--theme", default=DEFAULT_THEME, help="тема сводки")
    parser.add_argument(
        "--min-posts",
        type=int,
        default=DEFAULT_MIN_POSTS,
        help="минимум спарсенных постов (0 — проверять только success)",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="таймаут, сек")
    parser.add_argument(
        "--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="интервал опроса, сек"
    )
    args = parser.parse_args(argv)

    return run_smoke(
        region=args.region,
        theme=args.theme,
        min_posts=args.min_posts,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )


if __name__ == "__main__":
    sys.exit(main())
