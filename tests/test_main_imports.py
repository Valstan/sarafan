"""Smoke import test for main.py.

Hot-fix 2026-05-22: при удалении modules/scheduler/scheduler.py я оставил
dangling `from .scheduler import ContentScheduler` в `__init__.py`. Локальный
pytest не поймал, потому что unit-тесты импортируют только узкие модули —
main.py с цепочкой `web.api.*` импортов проходит мимо.

Этот тест просто `import main` — если хоть один файл в цепочке роутеров
или модулей не импортируется, тест упадёт.
"""

import importlib


def test_main_module_imports():
    """`import main` должен пройти без ошибок (включая всю цепочку
    `web.api.*` роутеров и `modules.*` зависимостей)."""
    import main  # noqa: F401

    assert main.app is not None


def test_all_api_routers_importable():
    """Каждый роутер в `web/api/__init__.py`-стиле должен импортироваться
    отдельно — это быстрее находит виновника, чем падение `import main`."""
    for mod_name in [
        "web.api.health",
        "web.api.regions",
        "web.api.communities",
        "web.api.posts",
        "web.api.notifications",
        "web.api.scheduler",
        "web.api.vk_monitoring",
        "web.api.token_management",
        "web.api.service_notifications",
        "web.api.schedule_management",
        "web.api.system_monitoring",
        "web.api.task_monitoring",
        "web.api.publisher",
        "web.api.parsing",
        "web.api.parsing_stats",
        "web.api.pipeline",
        "web.api.filtration",
        "web.api.templates",
        "web.api.promotion",
    ]:
        importlib.import_module(mod_name)
