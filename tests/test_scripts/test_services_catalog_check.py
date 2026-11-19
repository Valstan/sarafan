"""Тесты скрипта проверки каталога `/services` (строка будильника).

Сеть в тестах не трогаем: проверяются чистые функции — разбор карточек из HTML и
перевод кириллических доменов в punycode. Именно punycode важен: без него
`urllib` не резолвит кириллические хосты и молча даёт код 0 вместо HTTP-ответа.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "services_catalog_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("services_catalog_check", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


HTML = """
<div class="row row-cols-1 row-cols-md-2 g-3">
  <div class="col">
    <a class="card text-decoration-none shadow-sm h-100" href="https://вмалмыже.рф">
      <div class="card-body d-flex align-items-center gap-3 p-3">
        <div class="fw-semibold text-truncate" title="вМалмыже.РФ">вМалмыже.РФ</div>
      </div>
    </a>
  </div>
  <div class="col">
    <a class="card text-decoration-none shadow-sm h-100" href="https://радар.вмалмыже.рф">
      <div class="card-body d-flex align-items-center gap-3 p-3">
        <div class="fw-semibold text-truncate" title="Радар">Радар</div>
      </div>
    </a>
  </div>
  <div class="col">
    <a class="card text-decoration-none shadow-sm h-100" href="https://вмалмыже.рф">
      <div class="card-body d-flex align-items-center gap-3 p-3">
        <div class="fw-semibold text-truncate" title="вМалмыже.РФ">вМалмыже.РФ</div>
      </div>
    </a>
  </div>
</div>
"""


def test_catalog_cards_parses_href_and_title(mod):
    """Карточка = ссылка + подпись из блока; порядок страницы сохраняется."""
    cards = mod.catalog_cards(HTML)
    assert cards == [
        ("https://вмалмыже.рф", "вМалмыже.РФ"),
        ("https://радар.вмалмыже.рф", "Радар"),
    ]


def test_catalog_cards_deduplicates(mod):
    """Одна и та же карточка дважды не попадает в список (счёт не раздувается)."""
    assert len(mod.catalog_cards(HTML)) == 2


def test_to_ascii_converts_idn_host(mod):
    """Кириллический хост → punycode: иначе urllib его не резолвит."""
    assert mod._to_ascii("https://позвони.вмалмыже.рф/x") == (
        "https://xn--b1ajetccd.xn--80adkdyec4j.xn--p1ai/x"
    )


def test_to_ascii_keeps_already_ascii(mod):
    """Обычный домен не трогаем (и не ломаем при исключении punycode)."""
    assert mod._to_ascii("https://example.com/a") == "https://example.com/a"


def test_alive_codes_include_session_pages(mod):
    """401/403 — это «живая страница за сессией», а не мёртвая карточка."""
    assert 200 in mod.ALIVE_CODES
    assert 401 in mod.ALIVE_CODES
    assert 403 in mod.ALIVE_CODES
