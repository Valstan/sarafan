"""Инварианты публичной витрины на валентин.вмалмыже.рф.

Заказ владельца 2026-11-27: витрина САРАФАНА дублируется на этот публичный
адрес, данные идут с локальной машины (обратный SSH-туннель), а заявка
создаётся прямо на странице.

Здесь зафиксировано то, что при невнимательной правке утечёт в интернет:

1. **В приложение уходит только белый список путей.** На локальной машине
   гейт авторизации выключен (``WEB_AUTH_ENABLED=0``) — приложение отдаёт
   операторский интерфейс без сессии (дашборд, ``/tokens``, ``/ad``, ключи).
   Если бы в приложение проксировался catch-all ``location /``, весь САРАФАН
   стал бы публичным. Поэтому проксировать можно только ``location = ...``
   (точное совпадение), и ровно те пути, что нужны витрине.

2. **Реальный IP посетителя доезжает до приложения.** Edge-прокси Джино
   присылает ``X-Forwarded-For`` с настоящим IP клиента; если nginx допишет
   к нему свой (``$proxy_add_x_forwarded_for``), первым элементом окажется
   адрес edge'а — и гейт/лимиты склеят всех посетителей в одного клиента.

3. **Туннель слушает только loopback на VPS.** Наружу у машины владельца не
   открывается ни одного порта: ``-R 127.0.0.1:<порт>:...``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "config" / "nginx_valentin.conf"
TUNNEL = REPO_ROOT / "infra" / "windows" / "start-tunnel.ps1"

#: Пути витрины, которые уходят в приложение. Ровно этот набор — и ничего
#: больше: всё остальное отдаёт статика визитки.
ALLOWED_APP_PATHS = {
    "/",
    "/regions/links",
    "/api/regions/vk-links",
    "/api/landing/orders",
    "/static/js/region_links.js",
}


def _blocks(text: str) -> list[tuple[str, str]]:
    """Все блоки ``location``: (селектор, тело). Учитывает вложенность."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"location\s+([^\{]+)\{", text):
        depth = 1
        i = m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out.append((m.group(1).strip(), text[m.end() : i - 1]))
    return out


def _text() -> str:
    return CONFIG.read_text(encoding="utf-8")


class TestAppProxyIsNarrow:
    def test_only_exact_paths_are_proxied(self) -> None:
        """В приложение проксируется только точный список путей."""
        text = _text()
        proxied = {sel for sel, body in _blocks(text) if "proxy_pass" in body}
        assert proxied, "nginx_valentin.conf: ни одного проксирования в приложение"
        for sel in proxied:
            assert sel.startswith("="), (
                f"nginx_valentin.conf: в приложение проксируется '{sel}' — "
                "это не точное совпадение, и под него попадёт лишнее "
                "(на стенде гейт выключен — наружу уехал бы весь САРАФАН)"
            )
        exact = {sel.split("=", 1)[1].strip() for sel in proxied}
        assert exact == ALLOWED_APP_PATHS, (
            f"nginx_valentin.conf: набор путей витрины изменился: {sorted(exact)} "
            f"ожидались {sorted(ALLOWED_APP_PATHS)}"
        )

    def test_catch_all_is_not_the_app(self) -> None:
        """Catch-all ``location /`` — это статика визитки, а не приложение."""
        text = _text()
        catch_all = [body for sel, body in _blocks(text) if sel == "/"]
        assert catch_all, "nginx_valentin.conf: нет catch-all location /"
        for body in catch_all:
            assert "proxy_pass" not in body, (
                "nginx_valentin.conf: catch-all location / проксирует в приложение — "
                "это публикует операторский интерфейс целиком"
            )
            assert (
                "root /home/valstan/valentin/current" in body
            ), "nginx_valentin.conf: catch-all location / должен отдавать статику визитки"

    def test_portfolio_path_is_served(self) -> None:
        """Визитка владельца живёт под /portfolio/ (решение владельца)."""
        text = _text()
        assert "location /portfolio/" in text
        assert "alias /home/valstan/valentin/current/" in text


class TestClientIp:
    def test_forwarded_for_passed_as_is(self) -> None:
        """Реальный IP клиента должен доехать до приложения первым элементом."""
        text = _text()
        # Комментарии не считаются директивами — смотрим только строки кода.
        code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
        assert "$proxy_add_x_forwarded_for" not in code, (
            "nginx_valentin.conf: $proxy_add_x_forwarded_for допишет адрес edge'а — "
            "первым элементом X-Forwarded-For станет edge, и все посетители "
            "склеятся в одного клиента (гейт и лимиты берут первый элемент)"
        )
        assert "proxy_set_header X-Forwarded-For $http_x_forwarded_for;" in code

    def test_forwarded_proto_uses_the_map(self) -> None:
        """Схему знает только заголовок от edge — карта $setka_forwarded_proto."""
        text = _text()
        assert "proxy_set_header X-Forwarded-Proto $setka_forwarded_proto;" in text


class TestTunnel:
    def test_tunnel_binds_loopback_only(self) -> None:
        """Наружу у машины владельца не открывается ни одного порта."""
        script = TUNNEL.read_text(encoding="utf-8")
        assert "127.0.0.1:${RemotePort}" in script, (
            "start-tunnel.ps1: -R обязан binds'ить на 127.0.0.1 — иначе на VPS "
            "появится публичный порт, а это новая поверхность (D-038)"
        )
        assert "ExitOnForwardFailure=yes" in script, (
            "start-tunnel.ps1: без ExitOnForwardFailure туннель молча живёт без "
            "форварда, и nginx отдаёт 502 с невнятной причиной"
        )

    def test_tunnel_is_registered_as_a_task(self) -> None:
        registrar = (REPO_ROOT / "infra" / "windows" / "register-scheduled-tasks.ps1").read_text(
            encoding="utf-8"
        )
        assert "setka-public-tunnel" in registrar, (
            "туннель должен переживать перезагрузку машины и разрыв сети — "
            "значит, он живёт как задача планировщика"
        )
        assert "start-tunnel.ps1" in registrar
