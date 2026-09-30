"""Тесты конфига конвейера — прежде всего умолчаний, которые решают, полетит ли пост наружу."""

from __future__ import annotations

import pytest

from config import content_conveyor as cc


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "CONVEYOR_DISABLED",
        "CONVEYOR_SITES",
        "CONVEYOR_SOURCE_DAYS",
        "CONVEYOR_BATCH_MAX",
    ):
        monkeypatch.delenv(name, raising=False)


class TestActivation:
    def test_no_env_means_no_active_sites(self):
        """Умолчание не публикует: забытая переменная на новом боксе — тишина, не рассылка."""
        assert cc.get_active_sites() == []

    def test_site_activates_by_key(self, monkeypatch):
        monkeypatch.setenv("CONVEYOR_SITES", "vmalmyzhe")
        assert [s["key"] for s in cc.get_active_sites()] == ["vmalmyzhe"]

    def test_kill_switch_beats_allowlist(self, monkeypatch):
        monkeypatch.setenv("CONVEYOR_SITES", "vmalmyzhe")
        monkeypatch.setenv("CONVEYOR_DISABLED", "1")
        assert cc.get_active_sites() == []

    def test_unknown_key_is_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("CONVEYOR_SITES", "opechatka,vmalmyzhe")
        assert [s["key"] for s in cc.get_active_sites()] == ["vmalmyzhe"]

    def test_separators_and_case(self, monkeypatch):
        monkeypatch.setenv("CONVEYOR_SITES", " VMALMYZHE ; ")
        assert [s["key"] for s in cc.get_active_sites()] == ["vmalmyzhe"]


class TestSiteDescriptions:
    def test_keys_are_unique(self):
        keys = [s["key"] for s in cc.SITES]
        assert len(keys) == len(set(keys))

    def test_every_site_has_required_fields(self):
        for s in cc.SITES:
            for field in ("key", "title", "ingest_url", "key_env", "source_region"):
                assert str(s.get(field) or "").strip(), f"{s.get('key')}: пустое {field}"

    def test_ingest_urls_are_https(self):
        for s in cc.SITES:
            assert str(s["ingest_url"]).startswith("https://"), s["key"]

    def test_key_env_never_collides_with_gateway_prefix(self):
        """GATEWAY_KEY_* — ключи ВХОДЯЩИХ в наш VK-шлюз (config/gateway.py).

        Исходящий ключ доставки под тем же префиксом при недоступной БД начинает
        работать как пропуск в наш шлюз (resolve_gateway_key_name падает на env).
        Тест держит границу направлений именами, а не памятью автора.
        """
        for s in cc.SITES:
            assert str(s["key_env"]).endswith("_INGEST_KEY"), s["key"]
            assert not str(s["key_env"]).startswith("GATEWAY_KEY_"), s["key"]

    def test_get_site_by_key_is_case_insensitive(self):
        assert cc.get_site("VMALMYZHE")["title"] == "вМалмыже.РФ"
        assert cc.get_site("нет-такого") is None

    def test_get_site_returns_copy(self):
        first = cc.get_site("vmalmyzhe")
        first["title"] = "ИСПОРЧЕНО"
        assert cc.get_site("vmalmyzhe")["title"] != "ИСПОРЧЕНО"


class TestKeysAndLimits:
    def test_ingest_key_read_from_named_env(self, monkeypatch):
        monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "s3cret")
        assert cc.get_ingest_key(cc.get_site("vmalmyzhe")) == "s3cret"

    def test_missing_key_is_empty_not_exception(self, monkeypatch):
        monkeypatch.delenv("VMALMYZHE_INGEST_KEY", raising=False)
        assert cc.get_ingest_key(cc.get_site("vmalmyzhe")) == ""

    @pytest.mark.parametrize(
        "raw,expected", [("7", 7), ("0", 1), ("999", 30), ("мусор", 3), (None, 3)]
    )
    def test_source_days_bounds(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv("CONVEYOR_SOURCE_DAYS", raising=False)
        else:
            monkeypatch.setenv("CONVEYOR_SOURCE_DAYS", raw)
        assert cc.get_source_days() == expected

    @pytest.mark.parametrize("raw,expected", [("5", 5), ("0", 1), ("500", 100), ("мусор", 20)])
    def test_batch_max_bounds(self, monkeypatch, raw, expected):
        monkeypatch.setenv("CONVEYOR_BATCH_MAX", raw)
        assert cc.get_batch_max() == expected


class TestKazanskaya:
    """Второй приёмник (mandate brain 2026-09-02): тематический сайт, свой ключ, свои паблики."""

    def test_site_is_described_but_not_active_by_default(self):
        site = cc.get_site("kazanskaya")
        assert site is not None
        assert cc.get_active_sites() == []

    def test_ingest_key_is_receiver_side_name(self):
        assert cc.get_site("kazanskaya")["key_env"] == "KAZANSKAYA_INGEST_KEY"

    def test_sections_match_receiver_contract(self):
        assert cc.get_site("kazanskaya")["sections"] == (
            "festival",
            "prep",
            "crafts",
            "culture",
            "history",
            "other",
        )

    def test_source_is_the_district_feed_filtered_by_topic(self):
        """Сужение по пабликам снято 2026-09-14 — тему решает слово, а не источник.

        Прежде сайт брал ленту ДК (217788511) целиком. Владелец заменил это
        правилом по теме для всех источников сразу: «со всех пабликов… если
        попадёт что по теме, публиковать, но не всё одинаково, а отсеивать».
        """
        site = cc.get_site("kazanskaya")
        assert site["source_region"] == "mi"
        assert not site.get("source_owner_ids")
        assert site["source_keywords"]

    def test_sabantuy_is_not_caught_by_the_fair_site(self):
        """«Про сабантуй — в сабантуй, про казанскую — в казанскую, мешать не надо».

        У Сабантуя свой сайт; слово не должно стоять в ловле ярмарки, иначе его
        новости уедут не туда — а это ровно то, что владелец запретил.
        """
        words = cc.get_site("kazanskaya")["source_keywords"]
        assert not any("сабантуй" in w.lower() for w in words)

    def test_owner_ids_are_ints_everywhere(self):
        for s in cc.SITES:
            for x in s.get("source_owner_ids") or ():
                assert isinstance(x, int), s["key"]

    def test_both_sites_can_be_active_together(self, monkeypatch):
        monkeypatch.setenv("CONVEYOR_SITES", "vmalmyzhe,kazanskaya")
        assert [s["key"] for s in cc.get_active_sites()] == ["vmalmyzhe", "kazanskaya"]


class TestPublishKey:
    """D-091: право публиковать выражено ключом, а не флагом в конфиге."""

    def test_publish_key_read_from_named_env(self, monkeypatch):
        monkeypatch.setenv("VMALMYZHE_PUBLISH_KEY", "pub")
        site = cc.get_site("vmalmyzhe")
        assert cc.get_publish_key(site) == "pub" and cc.wants_publish(site) is True

    def test_no_key_means_draft_only(self, monkeypatch):
        monkeypatch.delenv("VMALMYZHE_PUBLISH_KEY", raising=False)
        site = cc.get_site("vmalmyzhe")
        assert cc.get_publish_key(site) == "" and cc.wants_publish(site) is False

    def test_kazanskaya_has_no_publish_key_at_all(self, monkeypatch):
        """Ключа публикации Казанская не выдавала — даже выставленный в env
        посторонний секрет не должен включить ей автопубликацию."""
        monkeypatch.setenv("KAZANSKAYA_PUBLISH_KEY", "подброшено")
        site = cc.get_site("kazanskaya")
        assert "publish_key_env" not in site
        assert cc.wants_publish(site) is False


class TestSabantuy:
    """Третий приёмник (mandate brain 2026-09-22, D-093): сайт Сабантуя."""

    def test_site_is_described_but_not_active_by_default(self):
        site = cc.get_site("sabantuy")
        assert site is not None
        assert cc.get_active_sites() == []

    def test_ingest_key_is_receiver_side_name(self):
        assert cc.get_site("sabantuy")["key_env"] == "SABANTUY_INGEST_KEY"

    def test_single_news_section(self):
        """У приёмника одна рубрика — news (письмо 22.09)."""
        assert cc.get_site("sabantuy")["sections"] == ("news",)

    def test_source_is_the_district_feed_filtered_by_topic(self):
        site = cc.get_site("sabantuy")
        assert site["source_region"] == "mi"
        assert not site.get("source_owner_ids")
        assert site["source_keywords"]

    def test_keywords_catch_inflections(self):
        """Совпадение — подстрока без морфологии: «сабантуй» не ловит
        «сабантуя», поэтому формы перечислены явно."""
        from modules.conveyor import source as source_mod

        words = cc.get_site("sabantuy")["source_keywords"]
        for text in (
            "Сабантуй в Малмыже",
            "программа сабантуя",
            "готовимся к сабантую",
            "сабантуем довольны",
            "на сабантуе",
            "сабантуи районов",
            "сабантуйский праздник",
        ):
            assert source_mod.matches_keywords(text, words), text
        assert not source_mod.matches_keywords("ярмарка казанская", words)

    def test_kazanskaya_does_not_catch_sabantuy_either(self):
        """Разводка владельца действует в обе стороны: ярмарка не забирает
        Сабантуй, Сабантуй не забирает ярмарку."""
        from modules.conveyor import source as source_mod

        sab_words = cc.get_site("sabantuy")["source_keywords"]
        assert not source_mod.matches_keywords("ярмарка казанская карнавал", sab_words)

    def test_no_publish_key_at_all(self, monkeypatch):
        """Ключа публикации Сабантуй не выдавал — только черновики."""
        monkeypatch.setenv("SABANTUY_PUBLISH_KEY", "подброшено")
        site = cc.get_site("sabantuy")
        assert "publish_key_env" not in site
        assert cc.wants_publish(site) is False


class TestKultura:
    """Четвёртый приёмник (письмо brain 2026-09-30): портал «Культура»."""

    def test_site_is_described_but_not_active_by_default(self):
        site = cc.get_site("kultura")
        assert site is not None
        assert cc.get_active_sites() == []

    def test_ingest_key_is_receiver_side_name(self):
        assert cc.get_site("kultura")["key_env"] == "KULTURA_INGEST_KEY"

    def test_ingest_url_is_the_portal_receiver(self):
        assert cc.get_site("kultura")["ingest_url"] == (
            "https://культура.вмалмыже.рф/api/ingest/posts"
        )

    def test_sections_are_institution_slugs(self):
        """Рубрика приёмника — slug учреждения; список растёт на их стороне."""
        assert cc.get_site("kultura")["sections"] == ("rckd", "kalinino")

    def test_source_is_the_district_feed_filtered_by_topic(self):
        site = cc.get_site("kultura")
        assert site["source_region"] == "mi"
        assert not site.get("source_owner_ids")
        assert site["source_keywords"]

    def test_keywords_catch_forms_but_not_lookalikes(self):
        """Голые корни «дк»/«хор» ловят и «будку»/«хорошо» (пробелы срезает
        `_norm_keywords`) — это осознанная плата префильтра, лишнее отклонит
        классификатор. Здесь проверяем: профильное ловится, заведомо чужое —
        нет."""
        from modules.conveyor import source as source_mod

        words = cc.get_site("kultura")["source_keywords"]
        for text in (
            "культура Малмыжа",
            "о культуре района",
            "культурный центр",
            "РЦКД приглашает",
            "в СДК Калинино концерт",
            "вечер в ДК",
            "ДК Малмыж",
            "школьный хор выступил",
            "выставка в музее",
            "кружок вышивки",
        ):
            assert source_mod.matches_keywords(text, words), text
        for text in (
            "отключение воды",
            "розыгрыш призов",
            "урожай зерновых",
        ):
            assert not source_mod.matches_keywords(text, words), text

    def test_no_publish_key_at_all(self, monkeypatch):
        """Ключа публикации нет вовсе — только черновики."""
        monkeypatch.setenv("KULTURA_PUBLISH_KEY", "подброшено")
        site = cc.get_site("kultura")
        assert "publish_key_env" not in site
        assert cc.wants_publish(site) is False
