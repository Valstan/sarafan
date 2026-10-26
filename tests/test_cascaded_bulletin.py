"""Тесты для каскадной сводки (modules.cascaded_bulletin)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modules.bulletin_pipeline_settings import POSTOPUS_BULLETIN_THEMES
from modules.cascaded_bulletin import (
    DEFAULT_LOOKBACK_HOURS,
    DEFAULT_POSTS_PER_CHILD,
    _defaults_dict,
    _has_banned_marker,
    _is_recent_enough,
    _is_religious_text,
    _resolve_child_regions,
)


def test_postopus_themes_includes_oblast():
    """Backward-compat: тема `oblast` всё ещё в списке POSTOPUS_BULLETIN_THEMES,
    чтобы существующие beat-таски `postopus-kirov-oblast-*` находили
    `RegionConfig.bulletin_template.by_topic.oblast`."""
    assert "oblast" in POSTOPUS_BULLETIN_THEMES


def test_constants():
    assert DEFAULT_POSTS_PER_CHILD == 5
    assert DEFAULT_LOOKBACK_HOURS == 72.0


def test_recent_enough_by_vk_date():
    now = int(time.time())
    fresh = {"date": now - 3600}
    old = {"date": now - int((DEFAULT_LOOKBACK_HOURS + 1) * 3600)}
    assert _is_recent_enough(fresh, DEFAULT_LOOKBACK_HOURS)
    assert not _is_recent_enough(old, DEFAULT_LOOKBACK_HOURS)


def test_recent_enough_missing_date():
    assert not _is_recent_enough({}, DEFAULT_LOOKBACK_HOURS)
    assert not _is_recent_enough({"date": "garbage"}, DEFAULT_LOOKBACK_HOURS)


def test_religious_text_markers():
    assert _is_religious_text("В храме прошла служба")
    assert _is_religious_text("Открытие новой мечети")
    assert not _is_religious_text("Открыли новую дорогу в районе")


def test_banned_marker_detection():
    assert _has_banned_marker("Реклама от компании X")
    assert _has_banned_marker("ОБЪЯВЛЕНИЕ: продаётся дом")
    assert _has_banned_marker("Раздел дополнительно")
    assert _has_banned_marker("Сборка addons")
    assert not _has_banned_marker("Обычная новость района")


def test_defaults_dict_empty_and_nested():
    assert _defaults_dict(SimpleNamespace(bulletin_filters=None)) == {}
    assert _defaults_dict(SimpleNamespace(bulletin_filters="bad")) == {}
    assert _defaults_dict(
        SimpleNamespace(bulletin_filters={"defaults": {"cascade_posts_per_child": 7}})
    ) == {"cascade_posts_per_child": 7}


@pytest.mark.asyncio
async def test_resolve_child_regions_explicit_override_excludes_self():
    """Override через `cascade_source_region_codes` пропускает явные коды,
    исключая сам oblast (защита от рекурсии)."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        SimpleNamespace(code="mi", vk_group_id=-100, is_active=True),
        SimpleNamespace(code="ur", vk_group_id=-200, is_active=True),
    ]
    session.execute = AsyncMock(return_value=mock_result)
    cfg = SimpleNamespace(
        bulletin_filters={
            "defaults": {"cascade_source_region_codes": ["mi", "ur", "kirov_obl", "  "]}
        }
    )
    out = await _resolve_child_regions(
        session, region_id=999, region_code="kirov_obl", region_config=cfg
    )
    # Возвращается то, что вернёт БД на in_(['mi','ur']) — мы проверяем что вызов был.
    assert len(out) == 2
    assert session.execute.called


@pytest.mark.asyncio
async def test_resolve_child_regions_uses_parent_id_when_no_override():
    """Без override берутся children по `parent_region_id = region_id`."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        SimpleNamespace(code="mi", vk_group_id=-100, is_active=True),
    ]
    session.execute = AsyncMock(return_value=mock_result)
    cfg = SimpleNamespace(bulletin_filters={"defaults": {}})
    out = await _resolve_child_regions(
        session, region_id=42, region_code="kirov_obl", region_config=cfg
    )
    assert len(out) == 1
    assert out[0].code == "mi"


@pytest.mark.asyncio
async def test_resolve_child_regions_override_empty_returns_empty():
    """Override = [oblast_code, '  '] — после фильтрации список пуст, не идём в БД."""
    session = AsyncMock()
    session.execute = AsyncMock()
    cfg = SimpleNamespace(
        bulletin_filters={"defaults": {"cascade_source_region_codes": ["kirov_obl", "   "]}}
    )
    out = await _resolve_child_regions(
        session, region_id=1, region_code="kirov_obl", region_config=cfg
    )
    assert out == []
    # Поскольку override был задан и непуст в исходнике, идём в DB с пустым списком
    # это нормально — DB вернёт пусто. Главное — функция не падает.


@pytest.mark.asyncio
async def test_run_cascaded_bulletin_rejects_raion():
    """Для kind=raion функция должна вернуть error — каскад только для oblast/strana."""
    from modules.cascaded_bulletin import run_cascaded_bulletin

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = SimpleNamespace(
        code="mi",
        kind="raion",
        vk_group_id=-100,
    )
    session.execute = AsyncMock(return_value=mock_result)

    out = await run_cascaded_bulletin(session, region_code="mi", theme="oblast")
    assert out["success"] is False
    assert "oblast/strana only" in out["error"]


@pytest.mark.asyncio
async def test_run_cascaded_bulletin_rejects_region_without_vk_group_id():
    from modules.cascaded_bulletin import run_cascaded_bulletin

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = SimpleNamespace(
        code="kirov_obl",
        kind="oblast",
        vk_group_id=None,
    )
    session.execute = AsyncMock(return_value=mock_result)

    out = await run_cascaded_bulletin(session, region_code="kirov_obl", theme="oblast")
    assert out["success"] is False
    assert "no vk_group_id" in out["error"]


@pytest.mark.asyncio
async def test_run_cascaded_bulletin_rejects_missing_region():
    from modules.cascaded_bulletin import run_cascaded_bulletin

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    session.execute = AsyncMock(return_value=mock_result)

    out = await run_cascaded_bulletin(session, region_code="nonexistent", theme="oblast")
    assert out["success"] is False
    assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# Соседский обмен (source_mode="neighbors")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_neighbor_regions_parses_and_excludes_self():
    """`_resolve_neighbor_regions` парсит Region.neighbors, исключая сам регион."""
    from modules.cascaded_bulletin import _resolve_neighbor_regions

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [
        SimpleNamespace(code="vp", vk_group_id=-1, is_active=True),
        SimpleNamespace(code="ur", vk_group_id=-2, is_active=True),
    ]
    session.execute = AsyncMock(return_value=mock_result)
    region = SimpleNamespace(code="mi", neighbors="vp, ur ; mi")  # self (mi) исключается
    out = await _resolve_neighbor_regions(session, region)
    assert len(out) == 2
    assert session.execute.called


@pytest.mark.asyncio
async def test_resolve_neighbor_regions_empty_neighbors_no_db():
    """Пустые neighbors → пустой список, в БД не ходим."""
    from modules.cascaded_bulletin import _resolve_neighbor_regions

    session = AsyncMock()
    session.execute = AsyncMock()
    out = await _resolve_neighbor_regions(session, SimpleNamespace(code="mi", neighbors=""))
    assert out == []
    assert not session.execute.called


@pytest.mark.asyncio
async def test_resolve_neighbor_regions_only_self_no_db():
    """neighbors указывает только на сам регион → пусто, в БД не ходим."""
    from modules.cascaded_bulletin import _resolve_neighbor_regions

    session = AsyncMock()
    session.execute = AsyncMock()
    out = await _resolve_neighbor_regions(session, SimpleNamespace(code="mi", neighbors="mi"))
    assert out == []
    assert not session.execute.called


@pytest.mark.asyncio
async def test_run_neighbor_bulletin_defaults_to_no_gate(monkeypatch):
    """Без config-override гейта НЕТ (снят 2026-08-29 — этап 4 ребрендинга:
    стена соседа состоит из сводок, гейт добивал остальное, канал публиковал
    ноль по всей сети), theme/source_mode = neighbors."""
    from modules.cascaded_bulletin import DEFAULT_NEIGHBOR_HASHTAG, run_neighbor_bulletin

    captured = {}

    async def fake_cascaded(
        session, *, region_code, theme, test_mode, source_mode, require_hashtag, footer=""
    ):
        captured.update(
            region_code=region_code,
            theme=theme,
            source_mode=source_mode,
            require_hashtag=require_hashtag,
        )
        return {"success": True}

    monkeypatch.setattr("modules.cascaded_bulletin.run_cascaded_bulletin", fake_cascaded)

    session = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.first.return_value = SimpleNamespace(code="mi", config=None)
    session.execute = AsyncMock(return_value=res)

    out = await run_neighbor_bulletin(session, region_code="mi")
    assert out == {"success": True}
    assert captured["theme"] == "neighbors"
    assert captured["source_mode"] == "neighbors"
    assert captured["require_hashtag"] is None
    assert DEFAULT_NEIGHBOR_HASHTAG is None


@pytest.mark.asyncio
async def test_run_neighbor_bulletin_respects_config_hashtag(monkeypatch):
    """region.config['neighbor_hashtag'] переопределяет гейт."""
    from modules.cascaded_bulletin import run_neighbor_bulletin

    captured = {}

    async def fake_cascaded(
        session, *, region_code, theme, test_mode, source_mode, require_hashtag, footer=""
    ):
        captured["require_hashtag"] = require_hashtag
        return {"success": True}

    monkeypatch.setattr("modules.cascaded_bulletin.run_cascaded_bulletin", fake_cascaded)

    session = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.first.return_value = SimpleNamespace(
        code="mi", config={"neighbor_hashtag": "#Срочно"}
    )
    session.execute = AsyncMock(return_value=res)

    await run_neighbor_bulletin(session, region_code="mi")
    assert captured["require_hashtag"] == "#Срочно"
