"""Редакционная отсечка ``advertisement`` снимается флагом — и только она.

Шаг 3 звена 5 (P091): рейтинг и вердикт движка готовы, и слепой второй
редактор больше не нужен. Флаг ``CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED``
(дефолт — включён, то есть нынешнее поведение) снимает **среднюю** отсечку
шага 5.

Что при этом обязано остаться — проверяется здесь же, а не «по памяти»:

- **5a ``hard_spam``** — бан-защита (инцидент Уржум 2026-07-08);
- **5c ``is_marked_advertisement``** — бан-защита, и движок её структурно не
  видит: в payload классификатора поля ``marked_as_ads`` нет вовсе (P091 §2).

Если флаг снимал бы и их, это был бы не шаг 3, а откат фикса #508.
"""

import time

import pytest

from modules.vk_monitor.advanced_parser import AdvancedVKParser


class _DummyVk:
    pass


class _RegionStub:
    """Минимальный region_config: парсеру из него нужны только эти поля."""

    def __init__(self, region_code: str):
        self.region_code = region_code
        self.black_id = []
        self.delete_msg_blacklist = None
        self.filter_group_by_region_words = None


def _fresh_post(owner_id: int, post_id: int, text: str, marked_as_ads: bool = False):
    return {
        "owner_id": owner_id,
        "id": post_id,
        "text": text,
        "date": int(time.time()),
        "likes": {"count": 0},
        "comments": {"count": 0},
        "reposts": {"count": 0},
        "marked_as_ads": marked_as_ads,
    }


def _bare_parser():
    parser = AdvancedVKParser(_DummyVk())
    parser._batch_lips = set()
    parser._batch_text_fps = set()
    parser._batch_core_fps = set()
    parser._batch_media_sigs = set()
    parser._batch_text_simhashes = set()
    parser._batch_token_sets = []
    parser._blocked_lips = set()
    return parser


#: Частное объявление с ценой и телефоном: по commercial-scoring это реклама,
#: но маркеров рекламодателя («erid:», «#реклама», метка ВК) оно не носит.
_PRIVATE_AD = "Продам мёд свежий качественный, 500 руб литр, тел 89123456789 " * 3


class TestAdvertisementFilter:
    @pytest.mark.asyncio
    async def test_enabled_by_default(self, monkeypatch):
        """Дефолт — текущее поведение: отсечка работает, флаг не выключен."""
        monkeypatch.delenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", raising=False)
        parser = _bare_parser()
        post = _fresh_post(-100, 601, _PRIVATE_AD)
        assert await parser._filter_post(post, "novost", None, [], set(), set()) is None
        assert parser.stats["posts_filtered_advertisement"] == 1

    @pytest.mark.asyncio
    async def test_flag_off_lets_the_post_through(self, monkeypatch):
        """С флагом выключенным средняя отсечка не режет."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", "0")
        parser = _bare_parser()
        post = _fresh_post(-100, 602, _PRIVATE_AD)
        assert await parser._filter_post(post, "novost", None, [], set(), set()) is not None
        assert parser.stats["posts_filtered_advertisement"] == 0

    @pytest.mark.asyncio
    async def test_flag_off_does_not_touch_hard_spam(self, monkeypatch):
        """5a — бан-защита. Её снятие было бы откатом фикса, а не шагом 3."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", "0")
        parser = _bare_parser()
        post = _fresh_post(-100, 603, "Удалённая работа, отклик в личку: 89123456789 " * 3)
        await parser._filter_post(post, "novost", None, [], set(), set())
        assert (
            parser.stats["posts_filtered_hard_spam"] + parser.stats["posts_filtered_marked_ad"] > 0
        ), "бан-защита перестала работать при снятой отсечке"

    @pytest.mark.asyncio
    async def test_flag_off_does_not_touch_marked_ads(self, monkeypatch):
        """5c — чужая размеченная реклама: движок её не видит, фильтр нужен."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", "0")
        parser = _bare_parser()
        post = _fresh_post(-100, 604, "Открылся новый салон связи в центре города " * 3, True)
        assert await parser._filter_post(post, "novost", None, [], set(), set()) is None
        assert parser.stats["posts_filtered_marked_ad"] == 1


class TestAuditMirrorsTheFlag:
    def test_audit_reports_advertisement_only_when_filter_is_on(self, monkeypatch):
        """Аудит обязан пере-выводить ПРИЧИНУ настоящую: снятая отсечка не
        должна числиться в журнале как «отброшено фильтром рекламы»."""
        from modules.curation.collection_audit import _derive_drop_reason

        post = {"owner_id": -100, "id": 605, "text": _PRIVATE_AD}

        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", "1")
        assert _derive_drop_reason(post, "novost", None) == "advertisement"

        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", "0")
        assert _derive_drop_reason(post, "novost", None) is None


class TestRegionalExemption:
    """Проба на одном районе (P091): средняя отсечка снимается только там.

    Глобальный флаг снимает отсечку сразу везде, а P091 требует снять её
    **пробой на одном районе** — «цена шага мерится фактической причиной», а не
    разом на сети. Поэтому нужен отдельный список исключений по кодам регионов.
    """

    @pytest.mark.asyncio
    async def test_exempt_region_keeps_private_ad(self, monkeypatch):
        """В исключённом районе средняя отсечка не режет."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", "mi")
        parser = _bare_parser()
        post = _fresh_post(-100, 611, _PRIVATE_AD)
        result = await parser._filter_post(post, "novost", _RegionStub("mi"), [], set(), set())
        assert result is not None
        assert parser.stats["posts_filtered_advertisement"] == 0

    @pytest.mark.asyncio
    async def test_other_region_still_filtered(self, monkeypatch):
        """Соседний район не задет: исключение точечное."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", "mi")
        parser = _bare_parser()
        post = _fresh_post(-100, 612, _PRIVATE_AD)
        assert (
            await parser._filter_post(post, "novost", _RegionStub("ur"), [], set(), set()) is None
        )
        assert parser.stats["posts_filtered_advertisement"] == 1

    @pytest.mark.asyncio
    async def test_exemption_does_not_touch_hard_spam(self, monkeypatch):
        """5a — бан-защита. Исключение снимает только среднюю отсечку."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", "mi")
        parser = _bare_parser()
        post = _fresh_post(-100, 613, "Удалённая работа, отклик в личку: 89123456789 " * 3)
        await parser._filter_post(post, "novost", _RegionStub("mi"), [], set(), set())
        assert parser.stats["posts_filtered_hard_spam"] > 0

    @pytest.mark.asyncio
    async def test_exemption_does_not_touch_marked_ads(self, monkeypatch):
        """5c — чужая размеченная реклама: движок её структурно не видит."""
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", "mi")
        parser = _bare_parser()
        post = _fresh_post(-100, 614, "Открылся новый салон связи в центре города " * 3, True)
        assert (
            await parser._filter_post(post, "novost", _RegionStub("mi"), [], set(), set()) is None
        )
        assert parser.stats["posts_filtered_marked_ad"] == 1

    def test_audit_mirrors_the_regional_exemption(self, monkeypatch):
        """Аудит обязан пере-выводить ПРИЧИНУ настоящую: в пробном районе
        отсечки нет — значит, и в журнале её быть не должно."""
        from modules.curation.collection_audit import _derive_drop_reason

        post = {"owner_id": -100, "id": 615, "text": _PRIVATE_AD}
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", "mi")
        assert _derive_drop_reason(post, "novost", _RegionStub("mi")) is None
        assert _derive_drop_reason(post, "novost", _RegionStub("ur")) == "advertisement"


class TestFlagHelper:
    def test_default_is_enabled(self, monkeypatch):
        from config.classifier import advertisement_filter_enabled

        monkeypatch.delenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", raising=False)
        assert advertisement_filter_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_off_values(self, monkeypatch, value):
        from config.classifier import advertisement_filter_enabled

        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", value)
        assert advertisement_filter_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_on_values(self, monkeypatch, value):
        from config.classifier import advertisement_filter_enabled

        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", value)
        assert advertisement_filter_enabled() is True

    def test_exempt_regions_are_off_the_filter(self, monkeypatch):
        """Исключение снимает отсечку только в перечисленных регионах."""
        from config.classifier import advertisement_filter_enabled

        monkeypatch.delenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", raising=False)
        monkeypatch.setenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", "mi, ur")
        assert advertisement_filter_enabled("mi") is False
        assert advertisement_filter_enabled("UR") is False  # регистр не важен
        assert advertisement_filter_enabled("klz") is True

    def test_empty_exempt_list_keeps_current_behaviour(self, monkeypatch):
        """Пустой список — нынешнее поведение: отсечка работает везде."""
        from config.classifier import advertisement_filter_enabled

        monkeypatch.delenv("CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED", raising=False)
        monkeypatch.delenv("CLASSIFIER_ADVERTISEMENT_FILTER_EXEMPT_REGIONS", raising=False)
        assert advertisement_filter_enabled("mi") is True
