"""Сжатый правилник (ADR-0016): файл, загрузчик, режим локального контура.

Файл — не копия ради копии: в нём сила ПРАВИЛ, а не только текст. Поэтому тест
держит три вещи, которые ломаются тихо: состав правил (17 базовых + 33
выученных), наши решения, которые при сжатии теряются (негосударственные
опросы), и отсутствие дубликата (вакансии были продублированы в артефакте
Мозга — «правило 9» и «правило 33» говорили одно и то же).
"""

from __future__ import annotations

import re

import pytest

from config import classifier as cfg
from modules.classifier import rules

BASE_RULES = 17
LEARNED_RULES = 33
THEMES = (
    "новости",
    "администрация",
    "объявления",
    "культура",
    "спорт",
    "дети и образование",
    "кругозор",
    "соседи",
    "мусор",
)


def _section(text: str, header: str) -> str:
    """Текст секции до следующего заголовка второго уровня."""
    start = text.index(header)
    rest = text[start:]
    nxt = rest.find("\n## ", 1)
    return rest if nxt < 0 else rest[:nxt]


def _numbered(text: str) -> list:
    return [ln for ln in text.splitlines() if re.match(r"^\d+\. ", ln)]


def test_compact_file_exists_and_readable():
    assert cfg.COMPACT_POSTULATES_PATH.exists()
    text = cfg.read_compact_postulates()
    assert text.strip()
    assert text == cfg.COMPACT_POSTULATES_PATH.read_text(encoding="utf-8")


def test_compact_file_keeps_rule_counts():
    text = cfg.read_compact_postulates()
    base = _numbered(_section(text, "## Стартовые правила"))
    learned = _numbered(_section(text, "## Выученные правила"))
    assert len(base) == BASE_RULES
    assert len(learned) == LEARNED_RULES


def test_compact_file_keeps_all_themes():
    text = cfg.read_compact_postulates()
    for theme in THEMES:
        assert f"**{theme}**" in text, f"тема {theme} потеряна"


def test_our_rule_about_nongov_surveys_survived_compression():
    """Решение владельца про негосударственные опросы не должно молча пропасть.

    При сжатии в артефакте Мозга оговорка выпала из выученного правила id=22 —
    и пропала бы навсегда, если бы никто не сверял сжатое с каноном.
    """
    text = cfg.read_compact_postulates()
    assert "ТПП" in text
    assert "Бизнес-барметр" in text


def test_no_duplicated_vacancy_rule():
    """Дубликат убран: «правило 9 сужено» повторяло правило 9 целиком.

    Смотрим в самом разделе правил, а не во всём файле: в шапке файл честно
    описывает, что именно убрал, — и это упоминание не должно ломать тест.
    """
    text = cfg.read_compact_postulates()
    learned = _section(text, "## Выученные правила")
    assert "Вакансии — правило 9 сужено" not in learned
    # Правило про вакансии осталось одно — в нём же живут опросы.
    vacancy_rules = [ln for ln in _numbered(learned) if ln.startswith("9. ")]
    assert len(vacancy_rules) == 1
    assert "Вакансии" in vacancy_rules[0]


def test_compact_file_documents_its_own_provenance():
    """Файл должен говорить, кто его сжал и что мы поправили."""
    text = cfg.read_compact_postulates()
    assert "Мозг" in text or "Мозга" in text
    assert "ADR-0016" in text


def test_rulebook_mode_default_is_effective(monkeypatch):
    monkeypatch.delenv("LOCAL_ENGINE_RULEBOOK", raising=False)
    assert cfg.get_local_rulebook_mode() == cfg.LOCAL_RULEBOOK_EFFECTIVE


def test_rulebook_mode_reads_compact(monkeypatch):
    monkeypatch.setenv("LOCAL_ENGINE_RULEBOOK", "compact")
    assert cfg.get_local_rulebook_mode() == cfg.LOCAL_RULEBOOK_COMPACT


def test_rulebook_mode_unknown_falls_back_to_effective(monkeypatch):
    """Опечатка в env не должна молча переключить контур на другой промпт."""
    monkeypatch.setenv("LOCAL_ENGINE_RULEBOOK", "compactt")
    assert cfg.get_local_rulebook_mode() == cfg.LOCAL_RULEBOOK_EFFECTIVE


@pytest.mark.asyncio
async def test_resolve_local_postulates_uses_compact_file(monkeypatch):
    monkeypatch.setenv("LOCAL_ENGINE_RULEBOOK", "compact")
    text = await rules.resolve_local_postulates(None)
    assert text == cfg.read_compact_postulates()


@pytest.mark.asyncio
async def test_resolve_local_postulates_falls_back_when_file_missing(monkeypatch, tmp_path):
    """Нет файла — действующий путь, а не пустой промпт (модель без правил)."""
    monkeypatch.setenv("LOCAL_ENGINE_RULEBOOK", "compact")
    monkeypatch.setattr(cfg, "COMPACT_POSTULATES_PATH", tmp_path / "нет-такого.md")
    monkeypatch.setattr(rules, "COMPACT_POSTULATES_PATH", tmp_path / "нет-такого.md")

    called = {}

    async def _fake_effective(_session):
        called["yes"] = True
        return "БАЗА"

    monkeypatch.setattr(rules, "render_effective_postulates", _fake_effective)
    text = await rules.resolve_local_postulates(None)
    assert text == "БАЗА"
    assert called.get("yes")


def test_ab_script_refuses_merged_candidate_in_tail_mode():
    """Слитый кандидат в режиме tail дал бы выученные правила дважды.

    Молчаливый дубль мерял бы не тот промпт, который в бою, — и замер вышел бы
    правдоподобным. Правильнее отказаться.
    """
    from scripts import classifier_postulates_ab as ab

    effective = (
        "БАЗА\n\n## Разрешённые темы (theme)\n\nсловарь\n\n## Выученные правила\n\n1. правило"
    )
    candidate = "СЖАТАЯ БАЗА\n\n## Выученные правила\n\n1. правило"

    with pytest.raises(ValueError):
        ab._variant_b(effective, candidate, "tail")

    merged = ab._variant_b(effective, candidate, "merged")
    assert merged.startswith("СЖАТАЯ БАЗА")
    assert merged.count("## Выученные правила") == 1
