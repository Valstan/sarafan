"""Dead-code scanner (pool #036): vulture + project-aware allowlists. Report-only.

Находит код «написанный, но не внедрённый»: функции/классы/переменные без
потребителей. Статический анализ через vulture (0 токенов), дальше кандидаты
триажируются вручную по методике #028 (git-история символа: хвост рефактора →
удалить PR'ом / спящая фича → в PENDING re-триаж). НИКОГДА не авто-удаляет.

Главная грабля стека — Celery: таски регистрируются декораторами и дёргаются
по строковым именам из ``beat_schedule`` / ``send_task`` → vulture счёл бы их
мёртвыми. Allowlist собирается динамически (AST, без импорта проекта):

1. имена функций с декоратором ``@*.task(...)`` / ``@shared_task`` в ``tasks/``;
2. последние компоненты строк ``"task": "..."`` из ``beat_schedule``.

FastAPI-роуты / Celery-signals / pydantic-валидаторы регистрируются тоже только
декоратором — гасятся через ``--ignore-decorators``-эквивалент.

Usage (vulture — dev-only зависимость, не в requirements.txt):
    ./venv/Scripts/python.exe -m pip install vulture
    ./venv/Scripts/python.exe scripts/deadcode_scan.py              # дельта
    ./venv/Scripts/python.exe scripts/deadcode_scan.py --all        # полный список
    ./venv/Scripts/python.exe scripts/deadcode_scan.py --min-confidence 80

Режим — report-only (exit 0 всегда): false positives неизбежны, блокирующий
гейт был бы источником боли (#036). Ежемесячный прогон — скилл /deadcode.

Триаженные кандидаты подавляются через ``scripts/deadcode_known.txt``
(строки ``relpath::symbol`` + комментарий с вердиктом) — ежемесячный прогон
показывает только НОВУЮ дельту.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Что сканируем: прикладной код. tests/ намеренно вне скоупа (fixtures/parametrize
# дают сплошной шум), database/migrations — SQL, examples/ — примеры для человека,
# они и не должны иметь потребителя.
#
# Каталог, забытый в этом списке, врёт ДВАЖДЫ и оба раза молча: мёртвый код внутри
# него не ищется вовсе, а живой символ, чей единственный потребитель лежит в нём,
# всплывает кандидатом на удаление — потребителя сканер не видит. Так на прогоне
# 2026-09-04 в кандидаты попал modules/radar_id/vk_upstream.py::host_shares_session,
# которого зовёт middleware/auth_gate.py. Пополняя проект новым каталогом верхнего
# уровня с прикладным кодом, добавляй его сюда.
SCAN_TARGETS = [
    "main.py",
    "celery_app.py",
    "_version.py",
    "config",
    "core",
    "database",
    "gateway_mcp",
    "middleware",
    "modules",
    "monitoring",
    "tasks",
    "utils",
    "web",
    "scripts",
]

# Декораторы, регистрирующие символ во фреймворке (символ жив без явных вызовов).
IGNORE_DECORATORS = [
    "@app.*",  # Celery @app.task + FastAPI @app.get/post/on_event
    "@celery_app.*",
    "@shared_task",
    "@router.*",  # FastAPI APIRouter endpoints
    "@signals.*",  # Celery signals (worker_ready, worker_shutdown, ...)
    "@mcp.*",  # MCP-инструменты шлюза (gateway_mcp/server.py) — зовёт протокол, не код
    "@validator",
    "@field_validator",
    "@model_validator",
    "@root_validator",
]

# Протокольные/магические имена, которые vulture иногда флагует в наших обёртках.
IGNORE_NAMES_STATIC = [
    "cls",
    "args",
    "kwargs",
    # Контракт Starlette BaseHTTPMiddleware: метод зовёт фреймворк на каждом запросе.
    # Гасим по имени, а не через FRAMEWORK_BASE_HINTS: тот механизм собирает только
    # поля и методы не берёт намеренно (неиспользуемый метод на схеме — кандидат).
    "dispatch",
    "Config",  # pydantic inner-Config + его ключи
    "frozen",
    "from_attributes",
    "arbitrary_types_allowed",
]

# Файлы-неймспейсы настроек: модульные переменные читает фреймворк, не код.
# config/celery_config.py потребляется целиком через app.config_from_object().
SETTINGS_NAMESPACE_FILES = {"config/celery_config.py"}

# Корни фреймворковых иерархий: поля этих классов — контракт (pydantic-схемы
# сериализуются динамически, SQLAlchemy-колонки = схема таблицы), не dead code.
FRAMEWORK_BASE_HINTS = {"BaseModel", "BaseSettings", "Base", "DeclarativeBase"}

KNOWN_FILE = ROOT / "scripts" / "deadcode_known.txt"


def _read_source(path: Path) -> str:
    """Прочитать исходник, сняв BOM, если он есть.

    ``utf-8-sig`` читает одинаково и с BOM, и без него. Обычный ``utf-8``
    на файле с BOM роняет ``ast.parse`` в SyntaxError — а парсер ловит её и
    идёт дальше, то есть **файл молча выпадает из сканирования**: его задачи
    не попадают в allowlist, его поля — в allowlist фреймворков. Так
    терялись 9 Celery-задач из ``tasks/parsing_scheduler_tasks.py`` и целый
    модуль каскадной сводки (семь файлов с BOM в зоне сканирования).
    """
    return path.read_text(encoding="utf-8-sig")


def _decorator_root(node: ast.expr) -> str:
    """'@app.task(name=...)' → 'app.task'; '@shared_task' → 'shared_task'."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def collect_celery_task_names() -> set[str]:
    """Имена Celery-тасок: декорированные функции + строки beat_schedule."""
    names: set[str] = set()
    tasks_dir = ROOT / "tasks"
    for py in sorted(tasks_dir.glob("*.py")):
        try:
            tree = ast.parse(_read_source(py))
        except SyntaxError as exc:  # не валим скан из-за одного файла
            print(f"WARN: SyntaxError in {py}: {exc}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    root = _decorator_root(dec)
                    if root.endswith(".task") or root == "shared_task":
                        names.add(node.name)
            # "task": "tasks.module.func" внутри beat_schedule
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "task"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        names.add(value.value.rsplit(".", 1)[-1])
    return names


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for target in SCAN_TARGETS:
        path = ROOT / target
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
    return files


def collect_framework_field_names() -> set[str]:
    """Имена полей классов, унаследованных (транзитивно) от pydantic/SQLAlchemy базы.

    Поля схем заполняются динамически (сериализация / ORM) — vulture их не видит.
    Методы НЕ собираем — неиспользуемый метод на схеме остаётся кандидатом.
    """
    class_bases: dict[str, set[str]] = {}
    class_fields: dict[str, set[str]] = {}
    for py in _iter_scan_files():
        try:
            tree = ast.parse(_read_source(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = set()
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.add(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.add(base.attr)
            class_bases.setdefault(node.name, set()).update(bases)
            fields = class_fields.setdefault(node.name, set())
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
                elif isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            fields.add(tgt.id)

    # Fixpoint: фреймворк-наследник, если база — hint или другой фреймворк-класс.
    framework: set[str] = set()
    changed = True
    while changed:
        changed = False
        for cls, bases in class_bases.items():
            if cls in framework:
                continue
            if bases & FRAMEWORK_BASE_HINTS or bases & framework:
                framework.add(cls)
                changed = True

    names: set[str] = set()
    for cls in framework:
        names.update(class_fields.get(cls, set()))
    return names


def load_known() -> dict[str, str]:
    """deadcode_known.txt → {'relpath::symbol': 'комментарий-вердикт'}."""
    known: dict[str, str] = {}
    if not KNOWN_FILE.exists():
        return known
    for line in KNOWN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entry, _, comment = line.partition("#")
        entry = entry.strip()
        if entry:
            known[entry] = comment.strip()
    return known


def main() -> int:
    parser = argparse.ArgumentParser(description="Dead-code scan (#036), report-only")
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=60,
        help="порог уверенности vulture (default 60)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="показать и уже триаженные (из deadcode_known.txt)",
    )
    args = parser.parse_args()

    try:
        from vulture import Vulture
    except ImportError:
        print("vulture не установлен: ./venv/Scripts/python.exe -m pip install vulture")
        return 0  # report-only: отсутствие инструмента не валит гейты

    celery_names = collect_celery_task_names()
    framework_fields = collect_framework_field_names()
    print(f"Celery allowlist: {len(celery_names)} task-имён (декораторы + beat_schedule)")
    print(f"Framework-поля (pydantic/SQLAlchemy схемы): {len(framework_fields)} имён")

    v = Vulture(
        ignore_names=sorted(celery_names | framework_fields) + IGNORE_NAMES_STATIC,
        ignore_decorators=IGNORE_DECORATORS,
    )
    v.scavenge([str(ROOT / t) for t in SCAN_TARGETS if (ROOT / t).exists()])

    known = load_known()
    fresh: list = []
    suppressed = 0
    for item in v.get_unused_code(min_confidence=args.min_confidence):
        rel = Path(item.filename).resolve().relative_to(ROOT).as_posix()
        if rel in SETTINGS_NAMESPACE_FILES:
            continue
        key = f"{rel}::{item.name}"
        if key in known and not args.all:
            suppressed += 1
            continue
        fresh.append((rel, item, key in known))

    current_file = ""
    for rel, item, was_known in sorted(fresh, key=lambda x: (x[0], x[1].first_lineno)):
        if rel != current_file:
            current_file = rel
            print(f"\n{rel}")
        mark = " [triaged]" if was_known else ""
        typ = item.typ.replace("unused_", "")
        print(f"  :{item.first_lineno} {typ} `{item.name}` ({item.confidence}%){mark}")

    print(
        f"\nИтого: {len(fresh)} кандидатов"
        + (f" (+{suppressed} подавлено как триаженные)" if suppressed else "")
    )
    print("Report-only: триаж по #028 (git-история символа) перед любым удалением.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
