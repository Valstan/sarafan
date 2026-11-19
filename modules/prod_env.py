"""Чтение env-файлов приложения в окружение процесса — для операционных скриптов.

**Зачем.** Файлы приложения на боксе — ``600 root:root``, а systemd-окружение
процессу не отдаёт (и не должен: ``systemd show`` печатал бы секреты). Скрипту,
который работает с приложением от root, нужен тот же env: без него он не видит
``DATABASE_URL``, ``REDIS_URL`` и комнату КАРМАНА.

Правила:

- **Значения не печатаются** — ни одно. Скрипт читает их в ``os.environ`` и
  говорит только «есть / нет» либо использует сам;
- **Уже заданное не перебивается**: systemd-окружение сильнее файла (свойство 3
  спецификации секретов), поэтому переменная из окружения процесса остаётся;
- **Комментарии и пустые строки** пропускаются, кавычки вокруг значения
  снимаются, ``KEY=value`` разбирается по первому ``=`` (значение может
  содержать ``=``).

Путь к env-файлу — не секрет (он и в памятках лежит literal'ом), но читать его
может только root: поэтому скрипты с этим модулем запускаются от root.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Tuple

#: Стандартные env-файлы прод-бокса: приложение и токен комнаты.
PROD_ENV_FILES: Tuple[str, ...] = (
    "/etc/setka/setka.env",
    "/etc/setka/secrets-token.env",
)


def parse_env_file(text: str) -> List[Tuple[str, str]]:
    """Разобрать содержимое env-файла в список пар ``(имя, значение)``."""
    out: List[Tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            out.append((name, value))
    return out


def load_env_files(paths: Iterable[str], *, override: bool = False) -> int:
    """Прочитать env-файлы в ``os.environ``. Возвращает, сколько ключей подхватили.

    ``override=False`` (по умолчанию): уже заданные переменные не трогаются —
    systemd сильнее файла.
    """
    count = 0
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for name, value in parse_env_file(text):
            if not override and name in os.environ:
                continue
            os.environ[name] = value
            count += 1
    return count


def load_prod_env(*, override: bool = False) -> int:
    """Подхватить стандартные env-файлы прод-бокса. Значения не печатаются."""
    return load_env_files(PROD_ENV_FILES, override=override)


__all__ = ["PROD_ENV_FILES", "load_env_files", "load_prod_env", "parse_env_file"]
