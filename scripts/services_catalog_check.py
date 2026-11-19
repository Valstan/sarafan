#!/usr/bin/env python3
"""Еженедельная проверка публичного каталога `/services` (строка будильника).

Каталог лежит на домене ЕСА (`вход.вмалмыже.рф`) и собран из карточек соседних
проектов: у каждой карточки — своя ссылка. Проверка отвечает на два вопроса:
все ли карточки живы и не появилось ли мёртвых.

**Где запускать.** С прода (бокса), а не с машины агента: часть доменов
(Сабантуй, Казанская, ПОЗВОНИ, Такси) отдаёт с машины агента TLS-handshake
timeout из-за сломанного IPv6 — DNS резолвится, рукопожатие не идёт. С бокса
те же четыре отвечают 200. Проверка 19.11 так и выглядела: с машины «4 карточки
мёртвые», с бокса — все живы.

    ssh sarafan 'cd ~/SETKA && ./venv/bin/python scripts/services_catalog_check.py'

**Только чтение.** Ни одного POST/PUT/DELETE, никаких токенов и сессий: запросы
анонимные, как их видит обычный посетитель.

Код возврата: 0 — все карточки живы, 1 — есть недоступные (тревога: карточку
убрать или починить), 2 — сам каталог не отвечает.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from typing import List, Tuple

#: Кириллица в выводе: консоль Windows по умолчанию cp1251 — переключаем на UTF-8,
#: иначе подписи карточек превращаются в мусор.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 — старый Python или нестандартный stdout
    pass

#: Каталог: домен ЕСА (punycode) + путь.
CATALOG_HOST = "вход.вмалмыже.рф"
CATALOG_PATH = "/services"

#: Коды, которые для карточки означают «жива»: 200 — открыта, 401/403 — за
#: сессией (страница жива, аноним её не видит), 301/302 — редирект.
ALIVE_CODES = (200, 301, 302, 401, 403)

#: Разметка карточки каталога: ссылка + подпись (title) в блоке.
_CARD_RE = re.compile(r'<a class="card[^"]*" href="([^"]+)">.*?title="([^"]+)"', re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


def _to_ascii(url: str) -> str:
    """Кириллические домены → punycode: urllib такие хосты не резолвит."""
    m = re.match(r"^(https?)://([^/]+)(.*)$", url)
    if not m:
        return url
    scheme, host, rest = m.groups()
    try:
        host = host.encode("idna").decode("ascii")
    except Exception:  # noqa: BLE001 — punycode не нужен, оставляем как есть
        pass
    return "%s://%s%s" % (scheme, host, rest)


def fetch(url: str, *, timeout: int = 25) -> Tuple[int, bytes]:
    """GET без следа: код ответа и тело (тело — пустое при ошибке)."""
    req = urllib.request.Request(_to_ascii(url), headers={"User-Agent": "setka-catalog-check/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""
    except Exception:  # noqa: BLE001 — недоступность равна коду 0
        return 0, b""


def catalog_cards(html: str) -> List[Tuple[str, str]]:
    """Карточки каталога: [(ссылка, подпись)] в порядке страницы, без повторов."""
    cards: List[Tuple[str, str]] = []
    for m in _CARD_RE.finditer(html):
        pair = (m.group(1), " ".join(m.group(2).split()))
        if pair not in cards:
            cards.append(pair)
    return cards


def main() -> int:
    parser = argparse.ArgumentParser(description="проверка публичного каталога /services")
    parser.add_argument("--timeout", type=int, default=25, help="таймаут запроса, с")
    args = parser.parse_args()

    host = CATALOG_HOST.encode("idna").decode("ascii")
    catalog_url = "https://%s%s" % (host, CATALOG_PATH)
    status, body = fetch(catalog_url, timeout=args.timeout)
    if status != 200:
        print("каталог не отвечает: код %s (%s)" % (status, catalog_url))
        return 2

    html = body.decode("utf-8", "replace")
    cards = catalog_cards(html)
    print("каталог: 200, %d Б, карточек: %d" % (len(body), len(cards)))
    m = _TITLE_RE.search(html)
    if m:
        print("title: %s" % " ".join(m.group(1).split()))

    broken: List[Tuple[str, str, int]] = []
    for href, title in cards:
        code, _ = fetch(href, timeout=args.timeout)
        alive = code in ALIVE_CODES
        if not alive:
            broken.append((href, title, code))
        print(
            "  %-4s %-44s %s%s" % (code, title[:42], href[:44], "" if alive else "  <-- НЕДОСТУПНА")
        )

    print("условия услуг на странице: %s" % ("есть" if "Условия услуг" in html else "нет"))
    print("живых: %d, недоступных: %d" % (len(cards) - len(broken), len(broken)))
    if broken:
        print("\nНЕДОСТУПНЫ (убрать карточку или починить ссылку):")
        for href, title, code in broken:
            print("  %s — %s (код %s)" % (title, href, code))
        print(
            "\nПроверь с бокса: с машины агента часть доменов отдаёт TLS-timeout "
            "из-за сломанного IPv6 — это не значит, что карточка мертва."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
