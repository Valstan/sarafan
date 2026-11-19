"""Гейт: каждое строковое имя Celery-задачи имеет зарегистрированный обработчик.

Потребитель и поставщик задачи знакомы **только по строке**: beat-расписание и
``send_task("tasks.module.func")`` пересекают границу процессов, где компилятор
и статический анализатор бессильны — они рассуждают о символах, а канала для
них не существует. Отсюда два молчаливых отказа:

1. **Опечатка или переезд функции** — beat каждый час зовёт имя, которого никто
   не обслуживает. Worker пишет ``No handler registered for '…'`` в лог, а
   наружу это выглядит как «данные почему-то перестали обновляться».
2. **Удаление «мёртвого» кода** — MatricaRMZ 25.07 уронил так целый раздел на
   проде: анализатор, линт, тесты и ревью сочли файл мёртвым, а в нём жили
   ~52 задачи, адресуемые строками (G184, письмо brain 2026-07-26).

Первую половину защиты у нас уже несёт ``scripts/deadcode_scan.py``
(``collect_celery_task_names`` вносит декорированные функции и строки
``"task"`` из beat в whitelist, поэтому vulture не предлагает удалить задачу,
которую зовут строкой). Здесь — **обратная сверка**, которой не было: не
«символ жив, потому что есть строка», а «строка ведёт к живому обработчику».
Она же ловит опечатки в именах, которые сегодня не ловит ничто.

Сверяются только **исполняемые** ссылки (beat + ``send_task``/``signature``).
Человекочитаемые словари подписей вроде ``_format_task_name`` не сверяем: там
имя — ключ поиска с fallback'ом, устаревшая запись безобидна.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

REPO_ROOT = Path(__file__).resolve().parent.parent

# Где ищем строковые вызовы задач. tests/ исключены намеренно: там встречаются
# заведомо фиктивные имена.
_SCAN_DIRS = ("tasks", "web", "modules", "scripts", "config")

# Вызовы, чей первый строковый аргумент — имя задачи.
_NAME_BY_STRING_CALLS = {"send_task", "signature"}


def _load_app():
    """Celery-app с импортированными task-модулями (как их грузит worker)."""
    from tasks.celery_app import app

    # ``include=[...]`` сам по себе ничего не импортирует: модули подтягивает
    # loader при старте worker'а. Без этого вызова app.tasks знает только
    # задачи, объявленные в tasks/celery_app.py.
    app.loader.import_default_modules()
    return app


def _string_constants(module_tree: ast.Module) -> dict[str, str]:
    """Модульные строковые константы — чтобы разрешать ``send_task(TASK_NAME)``."""
    constants: dict[str, str] = {}
    for node in module_tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def collect_string_task_references() -> dict[str, str]:
    """{имя задачи: где встретилось} по всем ``send_task``/``signature``-вызовам."""
    references: dict[str, str] = {}
    for directory in _SCAN_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            try:
                # utf-8-sig: файл с BOM иначе падает в SyntaxError и молча
                # выпадает из сверки — а в нём могут быть вызовы задач.
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError:  # синтаксис ловит flake8, гейт из-за него не валим
                continue
            constants = _string_constants(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called not in _NAME_BY_STRING_CALLS:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    name = first.value
                elif isinstance(first, ast.Name) and first.id in constants:
                    name = constants[first.id]
                else:
                    continue  # имя вычисляется в рантайме — статически не сверить
                if name.startswith("tasks."):
                    location = f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}"
                    references.setdefault(name, location)
    return references


def test_beat_schedule_names_have_handlers():
    """Ни одна beat-запись не зовёт имя без обработчика.

    Именно этот отказ у нас самый тихий: задача просто перестаёт запускаться, и
    заметно это не сразу, а когда кто-то обратит внимание на несвежие данные.
    """
    app = _load_app()
    schedule = app.conf.beat_schedule
    assert len(schedule) > 50, f"beat-расписание подозрительно короткое: {len(schedule)}"

    orphans = sorted(
        f"{key} → {entry['task']}"
        for key, entry in schedule.items()
        if entry["task"] not in app.tasks
    )
    assert not orphans, "beat зовёт задачи без зарегистрированного обработчика:\n" + "\n".join(
        orphans
    )


def test_send_task_names_have_handlers():
    """Каждое имя из ``send_task``/``signature`` ведёт к живому обработчику."""
    app = _load_app()
    references = collect_string_task_references()
    assert references, "не найдено ни одного строкового вызова задачи — сканер сломался"

    orphans = sorted(
        f"{name} ({location})" for name, location in references.items() if name not in app.tasks
    )
    assert not orphans, (
        "строковый вызов задачи не находит обработчика (опечатка или функция "
        "переехала/удалена):\n" + "\n".join(orphans)
    )


def test_beat_entries_are_well_formed():
    """У каждой записи есть строковое имя задачи — опечатка в ключе тоже отказ."""
    app = _load_app()
    malformed = sorted(
        key
        for key, entry in app.conf.beat_schedule.items()
        if not isinstance(entry.get("task"), str) or not entry["task"]
    )
    assert not malformed, f"beat-записи без строкового 'task': {malformed}"
