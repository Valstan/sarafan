"""Конфиг пересборки текста новости перед публикацией (заказ владельца 20.10).

Движок переписывает выбранную новость своими словами: лента перестаёт быть
копипастой чужих текстов и получает наш тон. Переписывание стоит один вызов
модели на пост, поэтому бюджет ограничен числом реально публикуемых постов в
волне (``max_posts_per_bulletin`` + хедлайнер), а не размером волны.

Env vars:
  POST_REWRITE_ENABLED=1        # 1 (дефолт) — включено; 0 — откат без деплоя,
                                # публикуется исходный текст источника
  POST_REWRITE_MIN_SOURCE=300   # короче — не переписываем: в объявлении из двух
                                # строк пересказ только добавит воды
  POST_REWRITE_MIN_CHARS=300    # ответ короче — считаем отказом (fail-open)
  POST_REWRITE_MAX_CHARS=1400   # ответ длиннее — считаем отказом (VK-пост 4096,
                                # но 1400 уже перебор для пересказа новости)
  POST_REWRITE_MAX_POSTS=2      # потолок переписываемых постов на волну
  POST_REWRITE_MODEL            # опц. модель-голова цепочки (дефолт — из
                                # config.opencode.get_model_chain)
"""

from __future__ import annotations

import os

DEFAULT_MIN_SOURCE = 300
DEFAULT_MIN_CHARS = 300
DEFAULT_MAX_CHARS = 1400
DEFAULT_MAX_POSTS = 2


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(value, high))


def rewrite_enabled() -> bool:
    """True → переписывать текст перед публикацией.

    Дефолт — включено (решение владельца 20.10: «включаем на все районы сразу»).
    Откат — ``POST_REWRITE_ENABLED=0`` + рестарт воркера, без деплоя кода.
    """
    raw = (os.getenv("POST_REWRITE_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def get_min_source_chars() -> int:
    return _int_env("POST_REWRITE_MIN_SOURCE", DEFAULT_MIN_SOURCE, 0, 4000)


def get_min_chars() -> int:
    return _int_env("POST_REWRITE_MIN_CHARS", DEFAULT_MIN_CHARS, 50, 4000)


def get_max_chars() -> int:
    return _int_env("POST_REWRITE_MAX_CHARS", DEFAULT_MAX_CHARS, 200, 6000)


def get_max_posts() -> int:
    return _int_env("POST_REWRITE_MAX_POSTS", DEFAULT_MAX_POSTS, 1, 10)


def get_model() -> str:
    return (os.getenv("POST_REWRITE_MODEL") or "").strip()
