"""Загрузка env-файла локального стенда (Windows).

**Зачем этот модуль.** На проде env-файл цепляется systemd'ом
(``EnvironmentFile=`` в юните): процесс стартует уже с наполненным
окружением. На Windows такого механизма нет — переменные окружения
никто не наполняет, и приложение падает на
``config.runtime._require("DATABASE_URL")`` ещё на импорте, до первого
запроса.

Поэтому на Windows env-файл читает **сам процесс** — и обязательно **до**
``bootstrap_secrets()``: иначе «комната как источник» не увидит ни локального
файла, ни адреса хранилища, и ключи не доедут.

**Как задать файл.** Переменная ``SETKA_ENV_FILES`` — список путей через
``os.pathsep`` (на Windows — ``;``). Задавать её нужно **явно**: молчаливый
путь по умолчанию означал бы, что приложение подхватывает файл, о котором
никто не знает, — а это ровно тот класс тихой поломки, который канон
запрещает.

**Правила — те же, что у systemd** (модуль ``modules.prod_env``):

- уже заданное окружением **не перебивается** (окружение сильнее файла);
- значения **не печатаются** — только число подхваченных ключей;
- отсутствующий файл — не ошибка (стенд может стартовать и без него).

На проде модуль — no-op: ``SETKA_ENV_FILES`` не задан, ничего не читается.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

#: Переменная со списком env-файлов (пути через ``os.pathsep``).
ENV_FILES_VAR = "SETKA_ENV_FILES"

#: Файлы, которые уже прочитаны этим процессом. Загрузка идёт **один раз**:
#: и ``main.py``, и ``tasks/celery_app.py`` вызывают её на старте (второй
#: импорт происходит транзитивно, через ``web.api``), и без этого флага
#: второй вызов отчитывался бы «подхвачено ключей — 0», хотя на самом деле
#: подхвачено всё: это правда о числе **новых** ключей, но читается как
#: «файл не прочитан». Лог, который врёт, хуже отсутствия лога.
_loaded_paths: List[str] = []


def env_file_paths(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Список env-файлов, заданных переменной ``SETKA_ENV_FILES``.

    Пустой список — значит «читать нечего» (прод: systemd сделал это сам).
    """
    environ = env if env is not None else os.environ
    raw = (environ.get(ENV_FILES_VAR) or "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(os.pathsep) if part.strip()]


def load_local_env(env: Optional[Dict[str, str]] = None) -> int:
    """Подгрузить env-файлы локального стенда в окружение процесса.

    Возвращает, сколько **новых** ключей подхватили. Значения не печатаются.
    Повторный вызов с теми же файлами ничего не делает и не логирует
    INFO-строку (второй вызов — норма для этого проекта: ``main.py`` и
    ``tasks/celery_app.py`` стартуют оба).
    """
    paths = env_file_paths(env)
    if not paths:
        return 0
    fresh = [path for path in paths if path not in _loaded_paths]
    if not fresh:
        logger.debug("локальный env: уже загружен (файлов: %d)", len(paths))
        return 0

    from modules.prod_env import load_env_files

    count = load_env_files(fresh, override=False)
    _loaded_paths.extend(fresh)
    if count:
        logger.info(
            "локальный env: подхвачено ключей — %d (файлов: %d)",
            count,
            len(fresh),
        )
    return count


__all__ = ["ENV_FILES_VAR", "env_file_paths", "load_local_env"]
