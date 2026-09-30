"""Слияние bulletin_filters с дефолтами + решение по запасному пути."""

from types import SimpleNamespace

from modules.bulletin_pipeline_settings import (
    FALLBACK_ALL,
    FALLBACK_DISABLED,
    FALLBACK_FORBIDDEN,
    THEMES_WITHOUT_ALL_COMMUNITIES_FALLBACK,
    fallback_decision,
    get_effective_pipeline_settings,
)


def test_effective_uses_defaults():
    rc = SimpleNamespace(bulletin_filters=None)
    eff = get_effective_pipeline_settings(rc, "novost")
    # Сутки с 2026-09-22 (решение владельца): всё старше в ленту не берём.
    assert eff["max_post_age_hours"] == 24.0
    # 1 с 2026-09-18 (решение владельца по замеру P168): одиночный пост даёт новости
    # 2.11× просмотров против той же новости внутри сводки.
    assert eff["max_posts_per_bulletin"] == 1
    assert eff["text_similarity_threshold"] == 0.90
    assert eff["min_rafinad_len_similarity_dedup"] == 80


def test_by_topic_overrides_age():
    rc = SimpleNamespace(
        bulletin_filters={
            "defaults": {"max_post_age_hours": 72},
            "by_topic": {"sport": {"max_post_age_hours": 48}},
        }
    )
    assert get_effective_pipeline_settings(rc, "novost")["max_post_age_hours"] == 72.0
    assert get_effective_pipeline_settings(rc, "sport")["max_post_age_hours"] == 48.0


def test_stored_region_value_still_wins_over_the_code_default():
    """Настройка, записанная в БД, главнее файла — и это ловушка, а не удобство.

    Сохранение настроек в UI пишет в ``bulletin_filters`` ВЕСЬ блок defaults
    (``web/api/filtration._normalize_bulletin_filters``), то есть замораживает
    тогдашний дефолт. После этого правка ``DEFAULT_PIPELINE`` до такого района
    молча не доезжает. На 2026-09-22 так закреплены четыре района с прежними
    72 ч. Тест фиксирует само поведение, чтобы оно не выглядело случайностью.
    """
    rc = SimpleNamespace(bulletin_filters={"defaults": {"max_post_age_hours": 72}, "by_topic": {}})

    assert get_effective_pipeline_settings(rc, "novost")["max_post_age_hours"] == 72.0


def test_broken_stored_value_falls_back_to_the_code_default():
    """Мусор в БД не должен воскрешать прежние 72 ч — падаем в текущий дефолт."""
    rc = SimpleNamespace(bulletin_filters={"defaults": {"max_post_age_hours": "не число"}})

    assert get_effective_pipeline_settings(rc, "novost")["max_post_age_hours"] == 24.0


# --- Запасной путь «нет своих сообществ → берём весь пул региона» (P188) ---
#
# Главное свойство: **запрет темы сильнее флага**. Иначе дефолтный
# ``PARSE_THEME_FALLBACK_ALL_COMMUNITIES=1`` вернул бы запрещённой теме путь
# обратно, и запрет остался бы строкой в докстринге — тот класс, где намерение
# записано рядом с кодом и не выполняется.


def test_reklama_never_falls_back_to_all_communities():
    """В «Объявлениях» запасной путь запрещён при любом флаге.

    Причина не в «осторожности»: в этой теме рекламный фильтр выключен, а ВК
    банит аккаунта админа за контент, а не за токен (инцидент Уржум, P023).
    """
    for flag in (True, False):
        assert fallback_decision("reklama", global_enabled=flag) == FALLBACK_FORBIDDEN


def test_other_themes_keep_the_fallback():
    """``addons`` без фолбэка не публикует вовсе (своих досок нет ни у одного
    района), поэтому запрет не должен расползтись на темы."""
    assert fallback_decision("addons", global_enabled=True) == FALLBACK_ALL


def test_global_flag_still_disables_fallback_for_others():
    """Флаг оператора работает как и раньше — запрет добавлен, а не вместо него."""
    assert fallback_decision("novost", global_enabled=False) == FALLBACK_DISABLED


def test_excluded_list_is_explicit_and_small():
    """Список исключений виден во весь: «одна тема» и «закрытый класс» —
    разные решения, и молчаливое расширение списка обесценило бы замер."""
    assert THEMES_WITHOUT_ALL_COMMUNITIES_FALLBACK == ("reklama",)
