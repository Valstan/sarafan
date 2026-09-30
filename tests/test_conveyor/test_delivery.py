"""Тесты доставки на ingest сайта — выход конвейера.

Ветка безлюдная и исходящая: ошибка здесь видна не нам, а читателям сайта.
Поэтому тесты держат три вещи — что мусор не улетает, что ретраим ровно то, что
можно ретраить, и что ключ идемпотентности собран верно (иначе приёмник заведёт
дубль вместо обновления черновика).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modules.conveyor import delivery
from tests.test_conveyor.conftest import SITE

GOOD_TEXT = (
    "В Малмыже отремонтировали участок дороги по улице Ленина. Работы шли две недели, "
    "подрядчик уложил новое покрытие и обновил разметку у школы."
)


def _payload(**over):
    body = {
        "vkPostId": "-158787639_77646",
        "sourceUrl": "https://vk.com/wall-158787639_77646",
        "title": "Отремонтировали улицу Ленина",
        "text": GOOD_TEXT,
    }
    body.update(over)
    return body


# ───────── ключ идемпотентности ─────────


class TestVkPostId:
    def test_adds_minus_sign_for_community_owner(self):
        """lip хранит owner по модулю, приёмник ждёт ВК-нотацию со знаком."""
        assert delivery.lip_to_vk_post_id("158787639_77646") == "-158787639_77646"

    @pytest.mark.parametrize(
        "bad", ["", "   ", "158787639", "abc_12", "158787639_", "_77646", None]
    )
    def test_malformed_lip_is_none(self, bad):
        assert delivery.lip_to_vk_post_id(bad) is None


class TestVkPostIdSign:
    """P186: знак берётся из адреса стены, а не из слепого «всегда минус».

    Пост с личной страницы (положительный owner) иначе уезжает с чужим
    vkPostId — приёмник с собственным импортом его не узнаёт и заводит
    вторую запись (дубль «Дорогие друзья!» id 1325 на Культуре).
    """

    def test_group_wall_keeps_minus(self):
        post = {"lip": "110599037_2997", "url": "https://vk.com/wall-110599037_2997"}
        assert delivery.vk_post_id_for(post) == "-110599037_2997"

    def test_personal_page_keeps_no_minus(self):
        post = {"lip": "234960216_8123", "url": "https://vk.com/wall234960216_8123"}
        assert delivery.vk_post_id_for(post) == "234960216_8123"

    def test_build_payload_uses_wall_address(self):
        body = delivery.build_payload(
            {"lip": "234960216_8123", "url": "https://vk.com/wall234960216_8123", "text": "t"},
            {"title": "З"},
        )
        assert body["vkPostId"] == "234960216_8123"

    @pytest.mark.parametrize("url", ["", "https://vk.com/dk_malmyzh", None])
    def test_unparseable_url_falls_back_to_lip(self, url):
        post = {"lip": "158787639_77646", "url": url}
        assert delivery.vk_post_id_for(post) == "-158787639_77646"


# ───────── инвариант мусора (pool #133) ─────────


class TestInvariant:
    def test_good_payload_passes(self):
        assert delivery.check_invariant(_payload()) is None

    def test_missing_idempotency_key_blocks(self):
        assert delivery.check_invariant(_payload(vkPostId="")) == "no_vk_post_id"

    def test_missing_source_url_blocks(self):
        """Атрибуция обязательна по контракту — без неё пост уходить не должен."""
        assert delivery.check_invariant(_payload(sourceUrl="")) == "no_source_url"

    def test_empty_title_blocks(self):
        assert delivery.check_invariant(_payload(title="  ")) == "empty_title"

    @pytest.mark.parametrize(
        "text",
        [
            "Здравствуйте, {author_name}! " + GOOD_TEXT,
            "{{ name }} " + GOOD_TEXT,
            "Привет, %(user)s. " + GOOD_TEXT,
            "Значение $SECRET_TOKEN " + GOOD_TEXT,
        ],
    )
    def test_unresolved_placeholder_blocks(self, text):
        """Шаблон, уехавший неподставленным, — ровно инцидент 08.08 в другой ветке."""
        assert delivery.check_invariant(_payload(text=text)) == "unresolved_placeholder"

    def test_placeholder_in_title_blocks(self):
        assert (
            delivery.check_invariant(_payload(title="Новость {section}"))
            == "unresolved_placeholder"
        )

    def test_plain_curly_text_is_not_a_placeholder(self):
        """Фигурные скобки в живом тексте — не повод задерживать новость."""
        assert delivery.check_invariant(_payload(text=GOOD_TEXT + " Смайл {} в конце.")) is None

    def test_too_short_blocks(self):
        assert delivery.check_invariant(_payload(text="Коротко.")) == "text_too_short"

    def test_too_long_blocks(self):
        assert delivery.check_invariant(_payload(text="а" * 20001)) == "text_too_long"

    def test_low_letter_ratio_blocks(self):
        """Текст из цифр и знаков — не новость: таблица, мусор или битая кодировка."""
        assert delivery.check_invariant(_payload(text="1234567890 " * 20)) == "low_letter_ratio"

    def test_question_marks_alone_do_not_block(self):
        """Порог «?»×3 из инцидента с кодировкой сюда НЕ переносится — он про другое.

        «Что?? Уже?! Правда?» — нормальный человеческий текст, и задерживать
        новость из-за него значит наказывать за пунктуацию.
        """
        assert delivery.check_invariant(_payload(text="Что?? Уже?! Правда? " + GOOD_TEXT)) is None


# ───────── сборка тела ─────────


class TestBuildPayload:
    def test_maps_post_and_verdict(self):
        body = delivery.build_payload(
            {"lip": "1_10", "url": "https://vk.com/wall-1_10", "text": "исходный"},
            {"title": "Заголовок", "text": "отредактированный", "section": "novosti"},
            date_iso="2026-08-09T10:00:00",
        )
        assert body["vkPostId"] == "-1_10"
        assert body["text"] == "отредактированный"
        assert body["section"] == "novosti"
        assert body["date"] == "2026-08-09T10:00:00"

    def test_falls_back_to_original_text(self):
        """Редактура не вернулась — шлём исходный пост, а не пустоту."""
        body = delivery.build_payload(
            {"lip": "1_10", "url": "u", "text": "исходный"}, {"title": "З"}
        )
        assert body["text"] == "исходный"

    def test_only_photo_and_doc_urls_go_as_images(self):
        body = delivery.build_payload(
            {
                "lip": "1_10",
                "url": "u",
                "text": "t",
                "media": [
                    {"type": "photo", "url": "https://cdn/1.jpg"},
                    {"type": "video", "title": "без url"},
                    {"type": "doc", "url": "https://cdn/2.pdf"},
                    {"type": "photo"},
                ],
            },
            {"title": "З"},
        )
        assert body["images"] == ["https://cdn/1.jpg", "https://cdn/2.pdf"]

    def test_images_capped_at_receiver_limit(self):
        media = [{"type": "photo", "url": f"https://cdn/{i}.jpg"} for i in range(25)]
        body = delivery.build_payload({"lip": "1_10", "url": "u", "text": "t", "media": media}, {})
        assert len(body["images"]) == delivery.MAX_IMAGES

    def test_absent_optional_fields_are_omitted(self):
        body = delivery.build_payload({"lip": "1_10", "url": "u", "text": "t"}, {})
        assert "section" not in body and "images" not in body and "date" not in body
        assert "publish" not in body


# ───────── дата поста и публикация (D-091) ─────────


class TestIsoDate:
    def test_naive_db_time_is_marked_utc(self):
        """Времена в БД наивные и всегда UTC — суффикс обязан быть явным.

        Без ``Z`` приёмник вправе прочитать строку в своём поясе, и новость
        уедет в ленте на три часа. Ошибка при этом не видна ниоткуда: доставка
        успешна, дата есть, просто не та.
        """
        assert delivery.iso_date(datetime(2026, 9, 14, 8, 30, 0)) == "2026-09-14T08:30:00Z"

    def test_aware_time_is_converted_to_utc(self):
        msk = timezone(timedelta(hours=3))
        got = delivery.iso_date(datetime(2026, 9, 14, 11, 30, 0, tzinfo=msk))
        assert got == "2026-09-14T08:30:00Z"

    @pytest.mark.parametrize("value", [None, "", "2026-09-14", 0, 1757836800])
    def test_non_datetime_gives_none_not_now(self, value):
        """Подставленная «сейчас» — то самое враньё, от которого избавляется D-091."""
        assert delivery.iso_date(value) is None


class TestPublishFlag:
    def test_publish_true_lands_in_body(self):
        body = delivery.build_payload(
            {"lip": "1_10", "url": "u", "text": "t"}, {"title": "З"}, publish=True
        )
        assert body["publish"] is True

    def test_publish_key_goes_as_header(self, monkeypatch):
        caller = _Caller([(201, {"id": "d1"})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        delivery.deliver(SITE, "gate", _payload(), publish_key="pub")
        assert caller.publish_keys == ["pub"]

    def test_empty_publish_key_is_not_sent(self, monkeypatch):
        """Пустой ``X-Publish-Key`` приёмник прочтёт как неверный ключ и ответит
        warning'ом — шум на ровном месте вместо отсутствия запроса."""
        sent = {}

        class _Req:
            def __init__(self, url, data=None, method=None, headers=None):
                sent.update(headers or {})

        monkeypatch.setattr(delivery.urllib.request, "Request", _Req)
        monkeypatch.setattr(
            delivery.urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")),
        )
        delivery._post_once("https://x/api", "gate", {"a": 1}, timeout=1, publish_key="")
        assert "X-Publish-Key" not in sent and sent["X-Gateway-Key"] == "gate"


class TestResponseWarnings:
    def test_warnings_are_returned_on_success(self, monkeypatch):
        """Приёмник принял, но не опубликовал — узнать об этом можно только
        из warning'а: статус 2xx, журнал «delivered», расхождение молчит."""
        caller = _Caller([(201, {"id": "d1", "warnings": ["publish ignored: bad key"]})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "k", _payload())
        assert out["ok"] and out["warnings"] == ["publish ignored: bad key"]

    @pytest.mark.parametrize("raw", [None, {}, {"warnings": None}, {"warnings": 5}, "строка"])
    def test_garbage_shapes_give_empty_list(self, raw):
        assert delivery.response_warnings(raw) == []

    def test_single_string_is_wrapped(self):
        assert delivery.response_warnings({"warnings": "date missing"}) == ["date missing"]


# ───────── доставка и ретраи ─────────


class _Caller:
    """Подменяет один HTTP-вызов заранее заданной цепочкой ответов."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.headers = None
        self.publish_keys = []

    def __call__(self, url, key, body, *, timeout, publish_key=""):
        self.calls += 1
        self.headers = key
        self.publish_keys.append(publish_key)
        return self.results[min(self.calls - 1, len(self.results) - 1)]


@pytest.fixture
def slept():
    return []


class TestDeliver:
    def test_success_returns_remote_id(self, monkeypatch):
        caller = _Caller([(201, {"id": "doc-42"})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "s3cret", _payload())
        assert out["ok"] and out["status"] == 201 and out["remote_id"] == "doc-42"
        assert out["attempts"] == 1
        assert caller.headers == "s3cret"

    @pytest.mark.parametrize("code", [502, 503, 504])
    def test_retries_gateway_errors_then_succeeds(self, monkeypatch, slept, code):
        """G234: пятисотки на этом маршруте будут, и ретрай безопасен — приёмник
        идемпотентен по vkPostId."""
        caller = _Caller([(code, {}), (200, {"id": "x"})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "k", _payload(), sleep=slept.append)
        assert out["ok"] and out["attempts"] == 2 and slept == [2]

    def test_network_failure_is_retried(self, monkeypatch, slept):
        caller = _Caller([(0, {"error": "timeout"}), (0, {"error": "timeout"}), (200, {})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "k", _payload(), sleep=slept.append)
        assert out["ok"] and out["attempts"] == 3 and slept == [2, 4]

    def test_gives_up_after_attempts(self, monkeypatch, slept):
        caller = _Caller([(503, {})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "k", _payload(), attempts=3, sleep=slept.append)
        assert not out["ok"] and out["attempts"] == 3 and out["reason"] == "http_503"

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retried(self, monkeypatch, code):
        """4xx — наша ошибка в теле или ключе; повтор её не исправит."""
        caller = _Caller([(code, {"error": "no"})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "k", _payload())
        assert not out["ok"] and caller.calls == 1 and out["reason"] == f"http_{code}"

    def test_missing_key_does_not_call(self, monkeypatch):
        caller = _Caller([(200, {})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(SITE, "", _payload())
        assert not out["ok"] and out["reason"] == "no_key" and caller.calls == 0

    def test_missing_url_does_not_call(self, monkeypatch):
        caller = _Caller([(200, {})])
        monkeypatch.setattr(delivery, "_post_once", caller)
        out = delivery.deliver(dict(SITE, ingest_url=""), "k", _payload())
        assert not out["ok"] and out["reason"] == "no_ingest_url" and caller.calls == 0


# ───────── обратная связь по рубрикам ─────────


class TestUnknownSections:
    def test_reports_unknown_slugs(self):
        site = dict(SITE, sections=("novosti", "afisha"))
        assert delivery.unknown_sections(site, ["novosti", "sport", "sport", "eda"]) == [
            "sport",
            "eda",
        ]

    def test_known_only_yields_empty(self):
        site = dict(SITE, sections=("novosti",))
        assert delivery.unknown_sections(site, ["novosti", "NOVOSTI "]) == []

    def test_site_without_section_list_reports_nothing(self):
        assert delivery.unknown_sections(dict(SITE, sections=()), ["что-угодно"]) == []


# ───────── IDN-домены ─────────


class TestIdnaUrl:
    def test_cyrillic_host_becomes_punycode(self):
        """Кириллический хост роняет urllib на latin-1 ещё до сети — кодируем заранее."""
        assert (
            delivery.idna_url("https://вмалмыже.рф/api/ingest/posts")
            == "https://xn--80adkdyec4j.xn--p1ai/api/ingest/posts"
        )

    def test_ascii_url_untouched(self):
        url = "https://example.com/api/ingest/posts?x=1"
        assert delivery.idna_url(url) == url

    def test_path_and_query_are_not_encoded(self):
        """Punycode относится только к имени хоста; путь трогать нельзя."""
        out = delivery.idna_url("https://вмалмыже.рф/api/ingest/posts?tag=новости")
        assert out.startswith("https://xn--80adkdyec4j.xn--p1ai/api/ingest/posts")
        assert "tag=новости" in out

    def test_port_is_preserved(self):
        assert (
            delivery.idna_url("https://вмалмыже.рф:8443/x")
            == "https://xn--80adkdyec4j.xn--p1ai:8443/x"
        )

    @pytest.mark.parametrize("bad", ["", "   ", "не-урл-вовсе"])
    def test_garbage_input_does_not_raise(self, bad):
        delivery.idna_url(bad)  # не бросает — худший исход виден на самом вызове
