"""Инварианты слоя условий услуг (пилот #386, шаг 1).

**Что здесь защищается.** Пилот держится на одном правиле: **нет источника —
нет утверждения**. Условие без ссылки на страницу, где оно написано, — это
выдумка, выданная за правило; человек откроет источник и не найдёт того, что
ему обещали. Правило должно ломать сборку, а не напоминание в комментарии,
поэтому оно здесь проверяется машинно.

Что ещё держим:
- **условия не появляются из ниоткуда** — только для услуг каталога, и каждая
  ссылка ведёт на страницу того владельца, который эти условия опубликовал;
- **«не знаю» — валидный ответ** (жёлтый пример приёмки): вопрос без
  условия даёт пустой ответ со ссылкой на того, кто отвечает, а не
  правдоподобную выдумку;
- **факты жителя не копируются** — цитируются со ссылкой и помечаются «по
  вашим данным», а самопровозглашённый факт без источника в ответ не идёт;
- **публичная поверхность узкая** — новые пути не попадают в allowlist молча.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from config.services_catalog import SERVICES
from modules.service_conditions import (
    CONDITION_FIELDS,
    CONDITIONS,
    SERVICES_WITHOUT_CONDITIONS,
    answer,
    build_service_cards,
    get_conditions,
    known_services,
    service_for_condition,
)

CATALOG_TITLES = {s["title"] for s in SERVICES}

# Домены владельцев услуг. Условие может ссылаться только на страницу
# владельца самой услуги или на страницу соседнего сервиса экосистемы
# (справочник «ПОЗВОНИ» обслуживает и полку «Такси»). Ссылка на наш
# собственный домен — запрещена: условие отвечает «кто отвечает и что
# положено», и наш лендинг не источник условий соседнего сервиса.
ALLOWED_HOST_SUFFIXES = (
    "вмалмыже.рф",
    "гоньба.рф",
)


class TestConditionSchema:
    def test_every_condition_has_all_contract_fields(self):
        """Все поля контракта заполнены — иначе ответчик не сможет собрать ответ."""
        for title, items in CONDITIONS.items():
            assert items, f"{title}: пустой список условий — услуга без условий в CONDITIONS"
            for item in items:
                missing = set(CONDITION_FIELDS) - set(item)
                assert not missing, f"{title}: нет полей {missing}"
                for field in CONDITION_FIELDS:
                    if field == "documents":
                        assert isinstance(item[field], list), f"{title}: documents — не список"
                        continue
                    assert str(item[field]).strip(), f"{title}: пустое поле {field!r}"

    def test_source_ref_is_mandatory_and_non_empty(self):
        """Главный инвариант пилота: условие без источника не существует."""
        for title, items in CONDITIONS.items():
            for item in items:
                assert item["source_ref"].strip(), f"{title}: пустой source_ref"

    def test_source_ref_is_absolute_https_url(self):
        """Источник должен открываться по ссылке — значит, абсолютный https."""
        for title, items in CONDITIONS.items():
            for item in items:
                parts = urlsplit(item["source_ref"])
                assert parts.scheme == "https", f"{title}: источник не https: {item['source_ref']}"
                assert parts.netloc, f"{title}: у источника нет хоста: {item['source_ref']}"

    def test_source_ref_points_to_service_owner_domain(self):
        """Ссылка ведёт на домен владельца услуги, а не куда попало."""
        for title, items in CONDITIONS.items():
            for item in items:
                host = urlsplit(item["source_ref"]).hostname or ""
                assert any(
                    host == suffix or host.endswith("." + suffix)
                    for suffix in ALLOWED_HOST_SUFFIXES
                ), f"{title}: источник {host} — не домен владельца услуги"

    def test_documents_is_list_of_non_empty_strings(self):
        for title, items in CONDITIONS.items():
            for item in items:
                for doc in item["documents"]:
                    assert isinstance(doc, str) and doc.strip(), f"{title}: пустой документ"

    def test_owner_is_filled(self):
        """Кто отвечает — обязательное поле: «не знаю, вот кто отвечает»."""
        for title, items in CONDITIONS.items():
            for item in items:
                assert item["owner"].strip(), f"{title}: не указан owner"


class TestCatalogAlignment:
    def test_conditions_reference_existing_catalog_services(self):
        """Условия — для услуг каталога. Условие про несуществующую услугу —
        витрина врёт."""
        for title in CONDITIONS:
            assert title in CATALOG_TITLES, f"условия для несуществующей услуги: {title}"

    def test_services_without_conditions_are_explained(self):
        """Молчание — тоже ответ: у услуги без условий должно быть сказано почему."""
        for title in CATALOG_TITLES:
            if title not in CONDITIONS:
                assert title in SERVICES_WITHOUT_CONDITIONS, (
                    f"{title}: условий нет и не сказано почему — ассистент не сможет "
                    "ответить «не знаю, вот кто отвечает»"
                )

    def test_services_without_conditions_have_no_entries(self):
        """Услуга без условий не должна числиться обслуженной."""
        for title in SERVICES_WITHOUT_CONDITIONS:
            assert title not in CONDITIONS, f"{title}: помечен без условий, но условия есть"

    def test_source_ref_belongs_to_exactly_one_service(self):
        """Одна страница-источник — один владелец. Иначе цитата уедет."""
        refs = [item["source_ref"] for items in CONDITIONS.values() for item in items]
        for ref in set(refs):
            owners = {
                title
                for title, items in CONDITIONS.items()
                if any(item["source_ref"] == ref for item in items)
            }
            assert len(owners) == 1, f"источник {ref} принадлежит нескольким услугам: {owners}"

    def test_service_for_condition_resolves(self):
        for title, items in CONDITIONS.items():
            for item in items:
                assert service_for_condition(item["source_ref"]) == title

    def test_known_services_sorted(self):
        assert known_services() == sorted(CONDITIONS)

    def test_get_conditions_returns_copy(self):
        """Вызывающий не должен суметь испортить конфиг (страница мутирует свой
        список — тот же класс, что в get_services)."""
        first = get_conditions(CATALOG_TITLES & set(CONDITIONS) and known_services()[0])
        if first:
            first[0]["then"] = "ИСПОРЧЕНО"
            assert get_conditions(known_services()[0])[0]["then"] != "ИСПОРЧЕНО"


class TestServiceCards:
    def test_cards_cover_whole_catalog(self):
        cards = build_service_cards()
        assert len(cards) == len(SERVICES)
        assert {c["title"] for c in cards} == CATALOG_TITLES

    def test_card_carries_conditions_with_source(self):
        """Карточка отдаёт условие ВМЕСТЕ с источником: условие без ссылки
        снаружи неотличимо от «условий нет»."""
        for card in build_service_cards():
            for condition in card["conditions"]:
                assert condition["source_ref"].strip(), card["title"]
                assert condition["owner"].strip(), card["title"]
                assert condition["question"].strip(), card["title"]

    def test_card_without_conditions_says_why(self):
        for card in build_service_cards():
            if not card["conditions"]:
                assert card["conditions_note"], f"{card['title']}: нет условий и нет пояснения"


class TestAnswer:
    def test_matching_question_returns_condition_with_source(self):
        result = answer("Культура Малмыжского района", "Как записаться в кружок или студию?")
        assert result["answer"], "условие по точному вопросу не нашлось"
        first = result["answer"][0]
        assert first["source"]["kind"] == "service_condition"
        assert first["source"]["ref"].startswith("https://")
        assert first["source"]["verified"] is True
        assert first["owner"].strip()
        assert first["statement"].strip()

    def test_unknown_question_answers_dont_know_with_owner(self):
        """Жёлтый пример приёмки: вопрос вне каталога ⇒ «не знаю» + кто
        отвечает. Не тишина и не выдумка."""
        result = answer("Культура Малмыжского района", "Как получить субсидию на жильё?")
        assert result["answer"] == [], "на вопрос без условия появился ответ"
        assert result["missing"], "не сказано, чего не хватает"
        assert result["owner"].strip(), "не сказано, кто отвечает"

    def test_service_without_conditions_answers_dont_know(self):
        result = answer("Радар", "Как подписаться на рассылку?")
        assert result["answer"] == []
        assert result["owner"].strip()

    def test_answer_shape_matches_contract(self):
        result = answer("ПОЗВОНИ", "Как добавить свою службу в справочник?")
        assert set(result) >= {"answer", "missing", "owner", "request_id", "version"}
        for item in result["answer"]:
            assert set(item) >= {"statement", "source", "note"}
            assert set(item["source"]) >= {"kind", "ref", "verified"}

    def test_every_statement_carries_a_source(self):
        """Правило 1 контракта проверяется на каждом заявленном ответе."""
        for title in CONDITIONS:
            for item in CONDITIONS[title]:
                result = answer(title, item["question"])
                assert result["answer"], f"{title}: условие есть, а ответ пуст"
                for stmt in result["answer"]:
                    assert stmt["source"]["ref"].strip()
                    assert stmt["source"]["kind"] in (
                        "service_condition",
                        "profile_fact",
                        "canon",
                    )

    def test_profile_fact_without_source_is_not_an_answer(self):
        """Факт без источника — не факт (план пилота, «Модель факта»)."""
        result = answer(
            "Культура Малмыжского района",
            "Как записаться в кружок или студию?",
            profile_facts=[{"value": "ветеран", "source": "", "ref": ""}],
        )
        for stmt in result["answer"]:
            assert stmt["source"]["kind"] != "profile_fact"

    def test_self_declared_fact_marked_as_such(self):
        """Правило 2: самопровозглашённый факт помечается «по вашим данным»."""
        result = answer(
            "Культура Малмыжского района",
            "Как записаться в кружок или студию?",
            profile_facts=[
                {"value": "инвалид II группы", "source": "self", "ref": "profile: disability"}
            ],
        )
        facts = [s for s in result["answer"] if s["source"]["kind"] == "profile_fact"]
        assert facts, "факт с источником не попал в ответ"
        assert facts[0]["note"] == "по вашим данным"
        assert facts[0]["source"]["verified"] is False


class TestPublicSurface:
    def test_condition_paths_are_public_but_narrow(self):
        """Слой условий читают серверы соседних проектов без cookie — но
        поверхность обязана остаться узкой (точный путь, не префикс)."""
        from middleware.auth_gate import PUBLIC_EXACT, PUBLIC_PREFIXES

        for path in ("/api/service-conditions", "/api/service-answer"):
            assert path in PUBLIC_EXACT, f"{path} не открыт публично"
            matched = [p for p in PUBLIC_PREFIXES if path.startswith(p)]
            assert not matched, f"{path} накрыт публичным префиксом: {matched}"

    def test_operator_only_surface_stays_closed(self):
        """Условия — публичные; операторские страницы — нет. Регрессия на
        тихое расширение allowlist'а."""
        from middleware.auth_gate import PUBLIC_EXACT

        for path in ("/api/advertiser/me", "/api/regions", "/api/local-engine/status"):
            assert path not in PUBLIC_EXACT, f"{path} попал в PUBLIC_EXACT"


@pytest.mark.parametrize("title", sorted(CONDITIONS))
def test_each_service_with_conditions_has_at_least_one(title):
    assert len(CONDITIONS[title]) >= 1
