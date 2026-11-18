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
  REWRITE_ENGINE=minimax        # движок переписывания (см. get_rewrite_engine)
  REWRITE_TIMEOUT=40            # потолок одного вызова, сек (для MiniMax —
                                # см. get_rewrite_timeout: берётся потолок пути
                                # публикации движка, 180 с)
  REWRITE_PAID_TOPUP=1          # добивать ли пропущенный пост платным Дипсиком
                                # (страховка по решению владельца 17.11)
"""

from __future__ import annotations

import os

DEFAULT_MIN_SOURCE = 300
DEFAULT_MIN_CHARS = 300
DEFAULT_MAX_CHARS = 1400
DEFAULT_MAX_POSTS = 2
#: Движок переписывания по умолчанию — **MiniMax** (решение владельца 18.11:
#: «передавай всё Минимаксу»). Весь нейро-трафик проекта уходит на бесплатный
#: движок, платный DeepSeek остаётся страховкой на случай отказа MiniMax.
#:
#: Почему раньше было наоборот (решение владельца 16.11): переписывание ушло
#: в MiniMax не по решению, а потому что стоял общий переключатель, и забрало
#: 85 % вызовов MiniMax (замер 16.11). Тогда вывод был «MiniMax слишком
#: дорог для пересказа». С тех пор поменялось три вещи: (1) переписывание
#: батчится (один вызов на два поста вместо одного на пост); (2) MiniMax больше
#: не ест 85 % пула, потому что контур починен и берёт аккаунты у Кармана сам
#: (живой факт 18.11: 111 свободных аккаунтов); (3) платный Дипсик стал
#: заметной долей расхода — 149 вызовов в сутки уходили на переписывание и
#: сборник, то есть ровно половина всех платных вызовов.
DEFAULT_REWRITE_ENGINE = "minimax"
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

    **Дефолт — MiniMax** (решение владельца 18.11: «передавай всё Минимаксу»):
    весь нейро-трафик проекта уходит на бесплатный движок, платный Дипсик
    остаётся страховкой на случай отказа MiniMax.

    Что поменялось с 16.11 (тогда дефолтом был ``deepseek``):

    - переписывание **батчится**: один вызов на два поста, а не на пост;
    - контур MiniMax **починен** и берёт аккаунты у Кармана сам (живой факт
      18.11: 111 свободных аккаунтов), так что переписывание больше не съедает
      85 % пула — оно забирает свою долю;
    - платный Дипсик стал **заметной долей расхода**: 149 вызовов в сутки
      (переписывание + сборник) из 296 платных — ровно половина.

    ⚠️ **Цена, о которой стоит помнить.** На MiniMax переписывание — это запуск
    агента-CLI: ~16 000 токенов и 30–90 секунд на вызов, а слот воркера на
    боксе **один**. Вызов идёт **внутри волны**, то есть эти секунды волна
    стоит. Потолок вызова для MiniMax берётся из потолка пути публикации
    движка (``MINIMAX_PUBLISH_TIMEOUT``, 180 с) — общий 40 с обрывал бы
    вызов прямо на волне. Любой отказ (движок молчит, ответ не по форме,
    пустой текст) — fail-open: публикуется исходный текст источника.
    """
    raw = (os.getenv("REWRITE_ENGINE") or "").strip().lower()
    if raw:
        return raw
    return DEFAULT_REWRITE_ENGINE


def get_rewrite_timeout() -> float:
    """Потолок одного вызова переписывания, сек (env ``REWRITE_TIMEOUT``).

    Дипсик отвечает за секунды, 40 с — с большим запасом. Для MiniMax (агент-
    CLI) 40 с мало: вызов не успевает, и волна уходит по обычным фильтрам —
    там берётся потолок **пути публикации именно этого движка**
    (``MINIMAX_PUBLISH_TIMEOUT``, 180 с).

    ⚠️ Движок передаётся **явно** (``get_publish_timeout_for_engine("minimax")``):
    иначе потолок смотрел бы на ``CLASSIFIER_ENGINE`` и выдал MiniMax 40 с —
    то есть обрыв вызова прямо на волне, потраченные токены без пересказа.
    """
    return get_rewrite_timeout_for_engine(get_rewrite_engine())


def get_rewrite_timeout_for_engine(engine: str) -> float:
    """Потолок вызова переписывания **для конкретного движка**, сек.

    Нужен там, где движок выбирается по ходу: страховочный вызов платным
    Дипсиком не должен получать потолок MiniMax (180 с) — Дипсик отвечает за
    секунды, а лишние 140 с стоят волне (слот воркера на боксе один).
    """
    if (engine or "").strip().lower() == "minimax":
        from config.opencode import get_publish_timeout_for_engine

        return get_publish_timeout_for_engine("minimax")
    try:
        return max(5.0, float(os.getenv("REWRITE_TIMEOUT") or DEFAULT_REWRITE_TIMEOUT))
    except ValueError:
        return DEFAULT_REWRITE_TIMEOUT


#: Env-переключатель страховки: добивать ли пропущенный пост платным Дипсиком.
ENV_PAID_TOPUP = "REWRITE_PAID_TOPUP"


def paid_topup_enabled() -> bool:
    """Разрешено ли добирать пропущенный пост платным Дипсиком.

    Решение владельца 17.11: «Дипсик вернуть на подстраховку, если Минимакс
    проморгал или не смог — то отдавать пропущенное Дипсику». «Проморгал» здесь
    означает и «ответ пришёл не по форме»: блок поста не пришёл — пересказа нет.

    Отключается двумя рубильниками: ``REWRITE_PAID_TOPUP=0`` — только этот путь,
    ``CLASSIFIER_PAID_FALLBACK=0`` — вся платная страховка разом (счёт закрыт).
    """
    raw = (os.getenv(ENV_PAID_TOPUP) or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    from config.opencode import paid_fallback_enabled

    return paid_fallback_enabled()
