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
  REWRITE_ENGINE=deepseek       # движок переписывания (см. get_rewrite_engine)
  REWRITE_TIMEOUT=40            # потолок одного вызова, сек
"""

from __future__ import annotations

import os

DEFAULT_MIN_SOURCE = 300
DEFAULT_MIN_CHARS = 300
DEFAULT_MAX_CHARS = 1400
DEFAULT_MAX_POSTS = 2
DEFAULT_REWRITE_ENGINE = "deepseek"
DEFAULT_REWRITE_TIMEOUT = 40


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


def get_rewrite_engine() -> str:
    """Движок переписывания постов (env ``REWRITE_ENGINE``).

    **Отдель от ``CLASSIFIER_ENGINE`` — намеренно.** Переключатель был один на
    всех, и переписывание ушло в MiniMax **не по решению**, а потому что так
    стоял общий переключатель (замер 16.11): 358 вызовов в сутки, то есть
    **85 % всех вызовов MiniMax**, тогда как на классификацию оставалось ~70.

    Считаем честно: переписывание — это оформление одного поста, а не
    «обработка трафика». Промпт у него крошечный (835–1105 токенов, кэш 92 %),
    и платный Дипсик отвечает на него за две секунды. MiniMax же на этом пути —
    запуск агента-CLI: ~16 000 токенов и 30–90 секунд на вызов, а слот воркера
    на боксе **один**. Переписать два поста волны через MiniMax — это минута
    волны, в которую стоят все остальные районы.

    Поэтому дефолт — ``deepseek`` (это то, что было до 12.11), а MiniMax
    включается одной переменной — для тех случаев, когда платить нельзя.
    """
    raw = (os.getenv("REWRITE_ENGINE") or "").strip().lower()
    if raw:
        return raw
    return DEFAULT_REWRITE_ENGINE


def get_rewrite_timeout() -> float:
    """Потолок одного вызова переписывания, сек (env ``REWRITE_TIMEOUT``).

    Дипсик отвечает за секунды, 40 с — с большим запасом. Для MiniMax (агент-
    CLI) 40 с мало: вызов не успевает, и волна уходит по обычным фильтрам —
    там берётся потолок пути публикации движка.
    """
    if get_rewrite_engine() == "minimax":
        from config.opencode import get_publish_timeout_for_engine

        return get_publish_timeout_for_engine()
    try:
        return max(5.0, float(os.getenv("REWRITE_TIMEOUT") or DEFAULT_REWRITE_TIMEOUT))
    except ValueError:
        return DEFAULT_REWRITE_TIMEOUT
