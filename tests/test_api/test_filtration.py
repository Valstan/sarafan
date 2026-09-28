"""Unit tests for web.api.filtration helpers (без FastAPI/DB)."""

import pytest

from modules.bulletin_pipeline_settings import DEFAULT_PIPELINE
from web.api.filtration import (
    FiltrationPutBody,
    _normalize_bulletin_filters,
    _normalize_localities,
    _sparse_bulletin_filters,
)


class TestNormalizeLocalities:
    def test_empty_returns_empty(self):
        assert _normalize_localities(None) == []
        assert _normalize_localities([]) == []

    def test_strips_whitespace(self):
        assert _normalize_localities(["  Цепочкино  "]) == ["Цепочкино"]

    def test_drops_empty_strings(self):
        assert _normalize_localities(["", "  ", "Гоньба"]) == ["Гоньба"]

    def test_dedupes_case_insensitive(self):
        # Сохраняется первая встретившаяся форма
        result = _normalize_localities(["Цепочкино", "цепочкино", "ЦЕПОЧКИНО"])
        assert result == ["Цепочкино"]

    def test_dedupes_yo_e(self):
        # «Лебяжье» и «Лебяжье» (без ё) — один и тот же населённый пункт
        result = _normalize_localities(["Лебяжье", "Лебяжье"])
        assert len(result) == 1

    def test_preserves_order(self):
        result = _normalize_localities(["Калинино", "Гоньба", "Цепочкино"])
        assert result == ["Калинино", "Гоньба", "Цепочкино"]

    def test_skips_non_strings(self):
        # На случай если фронт пришлёт мусор (число, None) внутри списка
        result = _normalize_localities(["Гоньба", None, 42, "Цепочкино"])  # type: ignore[list-item]
        assert result == ["Гоньба", "Цепочкино"]


class TestSparseBulletinFilters:
    """P178: запись хранит только отличия от дефолта, показ — слитый блок."""

    def test_full_block_of_defaults_stores_empty(self):
        """Главная ловушка: UI прислал слитый блок — в БД ложится пусто."""
        data = {"defaults": dict(DEFAULT_PIPELINE), "by_topic": {}}
        assert _sparse_bulletin_filters(data) == {"defaults": {}, "by_topic": {}}

    def test_int_one_equals_float_default(self):
        """`1` из формы и `1.0` из кода — одно значение, замораживать нечего."""
        out = _sparse_bulletin_filters({"defaults": {"max_posts_per_bulletin": 1}})
        assert out["defaults"] == {}

    def test_real_override_survives(self):
        out = _sparse_bulletin_filters(
            {
                "defaults": {"max_post_age_hours": 72, "max_posts_per_bulletin": 1},
                "by_topic": {"sport": {"max_post_age_hours": 48}},
            }
        )
        assert out == {
            "defaults": {"max_post_age_hours": 72},
            "by_topic": {"sport": {"max_post_age_hours": 48}},
        }

    def test_unknown_keys_and_garbage_are_kept(self):
        """Чужое и мусорное не выбрасываем: effective разберётся сам."""
        out = _sparse_bulletin_filters(
            {"defaults": {"custom_flag": True, "max_post_age_hours": "не число"}}
        )
        assert out["defaults"] == {"custom_flag": True, "max_post_age_hours": "не число"}

    def test_empty_input_stores_empty(self):
        assert _sparse_bulletin_filters(None) == {"defaults": {}, "by_topic": {}}
        assert _sparse_bulletin_filters({}) == {"defaults": {}, "by_topic": {}}

    def test_display_still_merged(self):
        """Показ не меняется: из разреженной записи UI видит эффективные."""
        stored = {"defaults": {"max_post_age_hours": 72}, "by_topic": {}}
        shown = _normalize_bulletin_filters(stored)
        assert shown["defaults"]["max_post_age_hours"] == 72
        assert shown["defaults"]["max_posts_per_bulletin"] == (
            DEFAULT_PIPELINE["max_posts_per_bulletin"]
        )


class TestFiltrationPutBodySchema:
    def test_localities_field_optional(self):
        # Все поля optional — пустой body должен валидироваться
        body = FiltrationPutBody()
        assert body.localities is None

    def test_localities_accepts_list_of_strings(self):
        body = FiltrationPutBody(localities=["Цепочкино", "Гоньба"])
        assert body.localities == ["Цепочкино", "Гоньба"]

    def test_localities_rejects_non_list(self):
        # Pydantic должен забраковать строку вместо списка
        with pytest.raises(Exception):
            FiltrationPutBody(localities="not a list")  # type: ignore[arg-type]
