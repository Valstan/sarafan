#!/usr/bin/env python3
"""Живость MAX-канала и подписка на вебхук (P230).

Канал включается в четыре шага, и три из них требуют секретов, которые не
попадают в чат. Этот скрипт делает шаг «подписаться» и проверку «готов ли
канал» **одной командой на боксе** — секреты берутся из env приложения и
никогда не печатаются.

Запуск на проде:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/max_channel.py --check'

    # подписаться на вебхук (после того как заданы MAX_TOKEN и MAX_WEBHOOK_SECRET):
    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/max_channel.py --subscribe'

**Что печатается.** Только факты «есть / нет» и коды ответов. Значения токена и
секрета не выводятся ни в одном режиме — в том числе в теле ответа MAX.

Коды возврата: 0 — канал готов (или подписка прошла), 1 — чего-то не хватает,
2 — не удалось выполнить проверку.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

#: API MAX (справка R50 мозга).
API_BASE = "https://platform-api2.max.ru"

#: Куда подписываться: вебхук живёт в веб-сервисе, путь публичный
#: (``/api/max/webhook`` в ``PUBLIC_EXACT``), собственная защита — заголовок
#: ``X-Max-Bot-Api-Secret``.
DEFAULT_WEBHOOK_PATH = "/api/max/webhook"
DEFAULT_WEBHOOK_HOST = "сарафан.вмалмыже.рф"

#: Типы событий, которые нам нужны: нажатие кнопки ответа и старт диалога
#: (из него бот узнаёт chat_id).
UPDATE_TYPES = ["message_callback", "bot_started"]

#: Локальный адрес веб-сервиса на боксе — для проверки живости маршрута без
#: выхода в интернет.
LOCAL_WEB = "http://127.0.0.1:8000"


def _has(name: str) -> bool:
    """Задана ли переменная окружения (без печати значения)."""
    return bool((os.environ.get(name) or "").strip())


def _http(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = 20,
) -> Tuple[int, bytes]:
    """HTTP-вызов: (код, тело). Ошибка соединения — код 0."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""
    except Exception:  # noqa: BLE001 — недоступность равна коду 0
        return 0, b""


def subscription_payload(url: str, secret: str) -> bytes:
    """Тело подписки на вебхук (то, что ждёт ``POST /subscriptions``)."""
    return json.dumps(
        {"url": url, "update_types": UPDATE_TYPES, "secret": secret},
        ensure_ascii=False,
    ).encode("utf-8")


def explain_webhook_code(code: int) -> str:
    """Что значит код ответа нашей двери."""
    if code == 200:
        return "дверь отвечает 200 — вебхук живой"
    if code == 503:
        return "503 — секрет подписки на сервере не задан (MAX_WEBHOOK_SECRET пуст)"
    if code == 401:
        return "401 — секрет задан, но не совпал (или не пришёл в заголовке)"
    if code == 404:
        return "404 — маршрута нет: веб-сервис поднят на старом коде (нужен рестарт)"
    if code == 0:
        return "нет соединения — веб-сервис не отвечает"
    return "код %s — неожиданно" % code


def _learned_chat_id() -> Optional[str]:
    """Выученный chat_id (из события bot_started) или None, если проверить не удалось."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from modules.max_http import learned_chat_id  # noqa: PLC0415

        return learned_chat_id()
    except Exception:  # noqa: BLE001 — Redis/импорт могут быть недоступны
        return None


def check(webhook_url: str) -> int:
    """Проверка готовности канала. 0 — готов, 1 — не хватает чего-то."""
    print("=== готовность MAX-канала ===")
    token = _has("MAX_TOKEN")
    secret = _has("MAX_WEBHOOK_SECRET")
    chat_env = _has("MAX_CHAT_ID")
    print("MAX_TOKEN задан: %s" % ("да" if token else "НЕТ"))
    print("MAX_WEBHOOK_SECRET задан: %s" % ("да" if secret else "НЕТ"))
    print("MAX_CHAT_ID задан (вручную): %s" % ("да" if chat_env else "нет — выучится сам"))
    print("AD_NEURAL_MODE: %s" % (os.environ.get("AD_NEURAL_MODE") or "off (дефолт)"))

    learned = _learned_chat_id()
    if learned is None:
        print("чат выучен: не проверено (Redis недоступен)")
    else:
        print(
            "чат выучен из bot_started: %s" % ("да" if learned else "НЕТ — нажмите «Старт» в боте")
        )

    code, _ = _http(
        LOCAL_WEB + DEFAULT_WEBHOOK_PATH,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=b"{}",
    )
    print("маршрут вебхука (локально): %s" % explain_webhook_code(code))
    print("цель подписки: %s" % webhook_url)

    missing = []
    if not token:
        missing.append("MAX_TOKEN")
    if not secret:
        missing.append("MAX_WEBHOOK_SECRET")
    if not learned and not chat_env:
        missing.append("«Старт» в боте (chat_id не выучен)")
    if code == 404:
        missing.append("рестарт веб-сервиса (маршрута нет)")

    if missing:
        print("\nНЕ ХВАТАЕТ: %s" % ", ".join(missing))
        return 1
    print("\nВсё на месте — можно подписываться (--subscribe).")
    return 0


def subscribe(webhook_url: str) -> int:
    """Подписка на вебхук. Токен и секрет — из env, наружу не печатаются."""
    token = (os.environ.get("MAX_TOKEN") or "").strip()
    secret = (os.environ.get("MAX_WEBHOOK_SECRET") or "").strip()
    if not token or not secret:
        print("нужны MAX_TOKEN и MAX_WEBHOOK_SECRET в окружении — одно из них не задано")
        return 1

    code, body = _http(
        API_BASE + "/subscriptions",
        method="POST",
        headers={"Authorization": token, "Content-Type": "application/json"},
        body=subscription_payload(webhook_url, secret),
    )
    print("POST %s/subscriptions -> %s" % (API_BASE, code))
    text = body.decode("utf-8", "replace")
    if secret and secret in text:
        text = text.replace(secret, "<секрет скрыт>")
    if text:
        print(text[:600])
    if code in (200, 201):
        print("\nПодписка оформлена: MAX будет звать %s" % webhook_url)
        return 0
    print("\nПодписка не прошла — разбери ответ выше.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="живость и подписка MAX-канала")
    parser.add_argument("--check", action="store_true", help="проверить готовность (по умолчанию)")
    parser.add_argument("--subscribe", action="store_true", help="подписаться на вебхук")
    parser.add_argument("--url", default="", help="путь вебхука (по умолчанию — домен сети)")
    parser.add_argument("--host", default=DEFAULT_WEBHOOK_HOST, help="домен вебхука")
    args = parser.parse_args()

    host = args.host
    webhook_url = args.url or ("https://%s%s" % (host, DEFAULT_WEBHOOK_PATH))

    if args.subscribe:
        return subscribe(webhook_url)
    return check(webhook_url)


if __name__ == "__main__":
    sys.exit(main())
