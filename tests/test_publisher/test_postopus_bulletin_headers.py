"""postopus_bulletin_headers: шапка выключена по умолчанию, шаблоны живы под флагом.

Шапки сняты решением владельца 2026-09-22 («место занимают, текст новости в
ленте скрыт, читателю не за что зацепиться»). Сами шаблоны не удалены — возврат
одной переменной окружения, поэтому тесты шаблонов включают флаг явно.
"""

from types import SimpleNamespace

from modules.publisher.postopus_bulletin_headers import (
    resolve_bulletin_hashtags,
    resolve_bulletin_header,
    resolve_mourning_bulletin_format,
)


def test_header_is_empty_by_default(monkeypatch):
    """Дефолт — пост без шапки, как оригинал.

    Пустая строка, а не None: ``BulletinBuilder`` читает "" как «без
    заголовка», а None — как «поставь дефолтный». Разница молчаливая.
    """
    monkeypatch.delenv("BULLETIN_HEADER_ENABLED", raising=False)
    rc = SimpleNamespace(zagolovki={}, heshteg_local={"raicentr": "лебяжье"})
    region = SimpleNamespace(name="Лебяжье", code="leb")

    assert resolve_bulletin_header(rc, "sport", region) == ""


def test_flag_beats_a_region_with_its_own_headers(monkeypatch):
    """Район с собственными zagolovki тоже остаётся без шапки.

    Иначе «убрали шапки» означало бы «убрали у всех, кроме тех, кто настраивал
    их руками» — то есть у самых обжитых районов шапки бы и остались.
    """
    monkeypatch.delenv("BULLETIN_HEADER_ENABLED", raising=False)
    rc = SimpleNamespace(zagolovki={"sport": "Свой заголовок"}, heshteg_local={})
    region = SimpleNamespace(name="Лебяжье", code="leb")

    assert resolve_bulletin_header(rc, "sport", region) == ""


def test_resolve_header_fallback_sport_russian(monkeypatch):
    monkeypatch.setenv("BULLETIN_HEADER_ENABLED", "1")
    rc = SimpleNamespace(zagolovki={}, heshteg_local={"raicentr": "лебяжье"})
    region = SimpleNamespace(name="Лебяжье", code="leb")
    h = resolve_bulletin_header(rc, "sport", region)
    assert "Спортивные новости" in h
    assert "Лебяжье" in h


def test_resolve_hashtags_theme_word_and_local():
    """Схема 20.10: тема словом + тег райцентра, без склейки «спортЛебяжье»."""
    rc = SimpleNamespace(heshteg={"sport": "спортЛебяжье"}, heshteg_local={"raicentr": "лебяжье"})
    tags, loc = resolve_bulletin_hashtags(rc, "sport")
    assert tags[0] == "спорт"
    assert all("лебяжье" not in t.lower() for t in tags), "тема и район больше не склеиваются"
    assert loc == "#Лебяжье"


def test_resolve_hashtags_adds_district_tag_from_region():
    rc = SimpleNamespace(heshteg={}, heshteg_local={"raicentr": "малмыж"})
    region = SimpleNamespace(local_hashtags=["#малмыж", "#малмыжский_район"])
    tags, loc = resolve_bulletin_hashtags(rc, "kultura", region)
    assert tags[0] == "культура"
    assert "малмыжский_район" in tags
    assert loc == "#Малмыж"


def test_resolve_hashtags_drops_region_code_from_city():
    """«Киров43» в конфиге — это город Киров, а не тег с цифрами."""
    rc = SimpleNamespace(heshteg={}, heshteg_local={"raicentr": "Киров43"})
    _, loc = resolve_bulletin_hashtags(rc, "oblast")
    assert loc == "#Киров"


def test_resolve_header_oblast_fallback(monkeypatch):
    monkeypatch.setenv("BULLETIN_HEADER_ENABLED", "1")
    rc = SimpleNamespace(zagolovki={}, heshteg_local={"raicentr": "киров"})
    region = SimpleNamespace(name="Кировская область", code="kirov_obl")
    h = resolve_bulletin_header(rc, "oblast", region)
    assert "Главное в области" in h
    assert "Кировская область" in h


def test_resolve_mourning_bulletin_format_is_plain_text_only():
    header, tags, local = resolve_mourning_bulletin_format()
    assert header == ""
    assert tags == []
    assert local == ""


def test_hashtags_and_attribution_are_not_headers(monkeypatch):
    """Снятие шапки не трогает хэштеги — владелец просил оставить их внизу."""
    monkeypatch.delenv("BULLETIN_HEADER_ENABLED", raising=False)
    rc = SimpleNamespace(heshteg={}, heshteg_local={"raicentr": "лебяжье"})

    tags, loc = resolve_bulletin_hashtags(rc, "sport")

    assert tags and loc == "#Лебяжье"
