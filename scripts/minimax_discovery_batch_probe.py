#!/usr/bin/env python3
"""Проба пути «подбор сообществ **партией**» → MiniMax: разбирает ли движок ответ.

**Зачем этот скрипт.** 25.11 ночной срез звал движок по одному разу на
сообщество: 129 вызовов, 450 минут слота, 7 успешных, 122 платных вызова
DeepSeek — и при 233 сообществах срез не влезал в собственную трёхчасовую
рамку (``time_limit=3*3600``), то есть умирал без отчёта. С 25.11 вердикты
считаются **партией**: один вызов движка на N карточек.

Новый путь отличается от прежнего не только экономией, но и **разбором
ответа**: модель должна вернуть массив вердиктов, а сопоставка идёт по ``id``.
Ошибка здесь молча оставила бы сеть без вердиктов на цикл — поэтому путь надо
проверять после каждого выката движка: код может лежать в ``main`` и на боксе,
а путь при этом не работать.

Скрипт проверяет **тот же путь**, что и ночной срез
(``modules.discovery.ai_categorizer.categorize_candidates_bulk``), на
**синтетических** карточках сообществ.

**Что не делает:** не пишет в БД, не ходит в VK, не меняет состояние
сообществ, не отправляет Telegram. Единственное последствие — **один настоящий
вызов MiniMax** (несколько очков чекина).

Запуск на проде::

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/minimax_discovery_batch_probe.py'

**Как читать вердикт.**

* ``success: true`` у всех карточек + ``engine: minimax`` — партия работает;
* ``success: false`` — путь не отработал, причина в поле ``error``;
* ``parsed: N`` меньше числа карточек — модель потеряла часть вердиктов
  (тогда срез доберёт их одиночными вызовами, но это уже деградация).

Код возврата: **0** — MiniMax ответил и вердикты разобрались, **1** — нет.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402

bootstrap_secrets()

from modules.discovery.ai_categorizer import (  # noqa: E402
    ENV_ENGINE,
    ENV_PAID_FALLBACK,
    categorize_candidates_bulk,
)

#: Синтетические карточки: официальная группа района и районный паблик —
#: вердикты очевидны, и ошибка движка не спрячется в спорной категории.
DEFAULT_CARDS = [
    {
        "name": "Тужинский район | Официальное сообщество",
        "description": (
            "Официальная группа администрации Тужинского района Кировской области. "
            "Новости района, афиша, нормативные документы, объявления администрации."
        ),
        "members_count": 1234,
        "recent_posts": [
            "Публикуем график работы администрации района на следующую неделю",
            "В ДК «Заря» состоится концерт ансамбля «Вдохновение»",
            "Напоминаем жителям о сдаче показаний счётчиков до 25 числа",
        ],
    },
    {
        "name": "Тужинский район | Объявления",
        "description": (
            "Объявления тужинцев: продажа, аренда, услуги, работа. "
            "Объявления жителей и организаций района."
        ),
        "members_count": 567,
        "recent_posts": [
            "Продам садовый участок в СНЗ, есть свет и вода",
            "Ищу помощницу по хозяйству на полставки",
            "Сдаю гараж в центре, звоните после 18",
        ],
    },
]
DEFAULT_REGION = "Тужинский"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проба пути «подбор сообществ партией → MiniMax» (без БД и без VK)."
    )
    parser.add_argument("--region", default=DEFAULT_REGION, help="район (регион)")
    parser.add_argument(
        "--allow-paid-fallback",
        action="store_true",
        help="не выключать платную страховку Дипсика (по умолчанию — выключена)",
    )
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    # Движок и страховка задаются здесь, а не в env-файле: проба обязана
    # проверять **MiniMax**, иначе «успех» на платном Дипсике выглядел бы как
    # работа движка.
    os.environ[ENV_ENGINE] = "minimax"
    if not args.allow_paid_fallback:
        os.environ[ENV_PAID_FALLBACK] = "0"

    cards = [dict(card) for card in DEFAULT_CARDS]
    results = asyncio.run(categorize_candidates_bulk(cards, region_name=args.region))

    payload = {
        "cards": len(cards),
        "parsed": sum(1 for item in results if item.get("success")),
        "results": [
            {
                "name": card.get("name"),
                "success": bool(item.get("success")),
                "engine": item.get("engine"),
                "category": item.get("category"),
                "confidence": item.get("confidence"),
                "is_info_page": item.get("is_info_page"),
                "reasoning": item.get("reasoning"),
                "error": item.get("error"),
            }
            for card, item in zip(cards, results)
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    ok = payload["parsed"] == len(cards) and all(
        (item.get("engine") == "minimax") for item in payload["results"] if item.get("success")
    )
    if not ok:
        print(
            "ПРОБА НЕ ПРОШЛА: путь «подбор сообществ партией → MiniMax» не ответил "
            "или вердикты разобрались не все (см. parsed/error выше).",
            file=sys.stderr,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
