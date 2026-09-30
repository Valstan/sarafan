"""
Настройки конвейера сводки (парсинг → сборка поста).

Хранятся в RegionConfig.bulletin_filters (JSON):
{
  "defaults": { ... },
  "by_topic": {
    "sport": { "max_post_age_hours": 48 },
    ...
  }
}
"""

from __future__ import annotations

from typing import Any, Dict, List

# Темы Celery/Postopus (для UI и переопределений)
POSTOPUS_BULLETIN_THEMES: List[str] = [
    "novost",
    "kultura",
    "sport",
    "reklama",
    "admin",
    "union",
    "addons",
    "sosed",
    "detsad",
    "setka",
    "oblast",
    "neighbors",
    # Расширенная повестка для областных сообществ (community-mode oblast,
    # 2026-05). Применимы к любому региону, у которого есть communities с
    # такой category — районам не мешают (просто нет таких сообществ).
    "proisshestviya",
    "molodezh",
    "nauka",
    "promyshlennost",
    "selhoz",
    "zdorovie",
    "zhkh",
    "priroda",
]

# Темы, которым запасной путь «нет своих сообществ → берём весь пул региона»
# НЕ доступен.
#
# Замысел фолбэка — добрать тему, которой у района нет своей ленты (``addons``:
# своих сообществ нет ни у одного района, 1752 публикации за 7 дней). Но в теме
# ``reklama`` он опасен вдвойне: там **выключен рекламный фильтр** (объявления
# ожидаемы), поэтому в «Объявления» района без своей доски попал бы весь его
# новостной поток вместе с чужой коммерческой рекламой — а ВК банит аккаунта
# админа за контент, а не за токен (инцидент Уржум 2026-07-08, P023/P188).
#
# Цена исключения измерена на проде 01.10.2026 (замер
# ``scripts/reklama_source_report.py``): активных районов 58, со своей активной
# доской объявлений — 54; за 30 суток вышло 30 рекламных сводок, **все по
# району ``mi``**, у которого три активные доски, то есть фолбэк по ``reklama``
# не срабатывал ни разу. Исключение стоит ноль измеримых потерь сегодня.
THEMES_WITHOUT_ALL_COMMUNITIES_FALLBACK: tuple = ("reklama",)

# Решения по запасному пути: что делать, когда у темы нет своих сообществ.
FALLBACK_ALL = "all"  # читаем весь активный пул региона
FALLBACK_FORBIDDEN = "forbidden"  # тема исключена осознанно
FALLBACK_DISABLED = "disabled"  # флаг выключен оператором


def fallback_decision(theme: str, *, global_enabled: bool) -> str:
    """Что делать, если у темы нет своих сообществ.

    Вынесено чистой функцией не для красоты: ветка живёт внутри большого
    ``_execute`` парсерного планировщика, и её нечем проверить иначе как
    прогоном по сети. Здесь же видно главное — **запрет темы сильнее флага**:
    иначе ``PARSE_THEME_FALLBACK_ALL_COMMUNITIES=1`` (дефолт) вернул бы ``reklama``
    запрещённый путь обратно, и запрет был бы только записью в докстринге.
    """
    if str(theme) in THEMES_WITHOUT_ALL_COMMUNITIES_FALLBACK:
        return FALLBACK_FORBIDDEN
    return FALLBACK_ALL if global_enabled else FALLBACK_DISABLED


# Значения по умолчанию, если в БД пусто
#
# ``max_posts_per_bulletin = 1`` — решение владельца 2026-09-18 (запись P168).
# Замер по сети (55 регионов, 12 039 постов, 940 дней, где обе формы вышли в один
# день одного района): новость, положенная в сводку третьей, получает 7.7 просмотров
# вместо 17 у одиночной — на элемент одиночный пост выигрывает в 2.11 раза и в 84 %
# дней. Виральность — свойство отдельной новости, а не поста-контейнера: сводка
# выигрывает «на пост» только потому, что в неё сложили втрое больше материала.
#
# Цена решения названа явно: конвейер строит ОДНУ сводку за прогон, поэтому потолок
# в 1 ограничивает и число новостей за слот. В районах с тонким потоком (63 %
# сводок и так несли один элемент) не меняется ничего; в богатых часть кандидатов
# уедет в следующий прогон и может состариться по ``max_post_age_hours``.
#
# ``max_post_age_hours = 24`` — решение владельца 2026-09-22: всё старше суток в
# ленту не берём, чтобы у сегодняшних постов был шанс пробиться в повестку.
# Подробности и почему одного рейтинга для этого мало — у константы
# ``BULLETIN_MAX_POST_AGE_HOURS`` в ``modules/vk_monitor/advanced_parser.py``.
#
# ⚠️ Этот дефолт доезжает НЕ до всех районов, у которых в БД лежит
# замороженный блок (legacy: до PR со _sparse_bulletin_filters сохранение
# настроек в UI писало весь блок defaults целиком — см. запись P178).
# Новые сохранения пишут только отличия, но старые строки сами не худеют:
# кто закреплён — смотреть запросом из P178.
DEFAULT_PIPELINE: Dict[str, Any] = {
    "max_post_age_hours": 24.0,
    "max_posts_per_bulletin": 1,
    "min_rafinad_len_core_dedup": 50,
    "text_similarity_threshold": 0.90,
    "min_rafinad_len_similarity_dedup": 80,
    "posts_per_community_fetch": 20,
}


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def get_effective_pipeline_settings(region_config: Any, theme: str) -> Dict[str, Any]:
    """
    Сливает defaults → defaults из bulletin_filters → переопределение темы.
    Возвращает плоский словарь с числовыми ключами конвейера.
    """
    raw = getattr(region_config, "bulletin_filters", None)
    if not isinstance(raw, dict):
        raw = {}
    base_defaults = {**DEFAULT_PIPELINE, **(raw.get("defaults") or {})}
    by_topic = raw.get("by_topic") or {}
    topic_ov: Dict[str, Any] = {}
    if isinstance(by_topic, dict) and theme in by_topic and isinstance(by_topic[theme], dict):
        topic_ov = by_topic[theme]
    merged = {**base_defaults, **topic_ov}
    merged["max_post_age_hours"] = _coerce_float(
        merged.get("max_post_age_hours"), DEFAULT_PIPELINE["max_post_age_hours"]
    )
    merged["max_posts_per_bulletin"] = _coerce_int(merged.get("max_posts_per_bulletin"), 1)
    merged["min_rafinad_len_core_dedup"] = _coerce_int(merged.get("min_rafinad_len_core_dedup"), 50)
    merged["text_similarity_threshold"] = _coerce_float(
        merged.get("text_similarity_threshold"), 0.90
    )
    merged["min_rafinad_len_similarity_dedup"] = _coerce_int(
        merged.get("min_rafinad_len_similarity_dedup"), 80
    )
    merged["posts_per_community_fetch"] = _coerce_int(merged.get("posts_per_community_fetch"), 20)
    # разумные границы
    merged["max_post_age_hours"] = max(1.0, min(merged["max_post_age_hours"], 8760.0))
    merged["max_posts_per_bulletin"] = max(1, min(merged["max_posts_per_bulletin"], 10))
    merged["posts_per_community_fetch"] = max(1, min(merged["posts_per_community_fetch"], 100))
    merged["min_rafinad_len_core_dedup"] = max(10, min(merged["min_rafinad_len_core_dedup"], 500))
    merged["text_similarity_threshold"] = max(0.70, min(merged["text_similarity_threshold"], 0.99))
    merged["min_rafinad_len_similarity_dedup"] = max(
        20, min(merged["min_rafinad_len_similarity_dedup"], 1000)
    )
    return merged


def empty_bulletin_filters_template() -> Dict[str, Any]:
    """Шаблон для сохранения в БД."""
    return {
        "defaults": dict(DEFAULT_PIPELINE),
        "by_topic": {},
    }
