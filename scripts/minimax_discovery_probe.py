#!/usr/bin/env python3
"""Проба пути «подбор сообществ → MiniMax»: отвечает ли движок на текущем коде.

**Зачем этот скрипт.** 24.11 ночной срез discovery отработал целиком на платном
Дипсике: 385 вызовов MiniMax упали с ``--system-prompt-file ... is empty`` —
discovery звал CLI без системного промта. Фича «перевести подбор на MiniMax»
была помечена сделанной по факту выката, а по факту путь не отработал **ни разу**
с переключения 23.11, и это молча съедало деньги на платной страховке.

Скрипт проверяет **тот же путь**, что и ночной срез
(``modules.discovery.ai_categorizer.categorize_candidate``), на **синтетической**
карточке сообщества. Проверять надо после каждого выката движка: код может
лежать в ``main`` и на боксе, а путь при этом не работать.

**Что не делает:** не пишет в БД, не ходит в VK, не меняет состояние
сообществ, не отправляет Telegram. Единственное последствие — **один настоящий
вызов MiniMax** (несколько очков чекина).

Запуск на проде::

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
        cd /home/valstan/SETKA && ./venv/bin/python scripts/minimax_discovery_probe.py'

    --json                          вывод в JSON (по умолчанию — тот же JSON)
    --name/--description/--region  своя карточка вместо синтетической
    --allow-paid-fallback           не выключать платную страховку Дипсика.
                                   По умолчанию страховка **выключена**: отказ
                                   MiniMax должен быть виден, а не замаскирован
                                   платным вызовом (именно так 24.11 откат на
                                   Дипсик и остался незамеченным).

**Как читать вердикт.**

* ``success: true`` + ``engine: minimax`` — путь живой;
* ``success: true`` + ``engine: deepseek`` — MiniMax не ответил, срез закрыл
  платный Дипсик (со ``--allow-paid-fallback`` это возможно и ожидаемо);
* ``success: false`` — отказ, причина в поле ``error``.

Код возврата: ``0`` — MiniMax ответил, ``1`` — нет.
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
    categorize_candidate,
)

#: Синтетическая карточка: официальная группа района — вердикт очевиден
#: (``admin``), и ошибка движка не спрячется в спорной категории.
DEFAULT_NAME = "Тужинский район | Официальное сообщество"
DEFAULT_DESCRIPTION = (
    "Официальная группа администрации Тужинского района Кировской области. "
    "Новости района, афиша, нормативные документы, объявления администрации."
)
DEFAULT_REGION = "Тужинский"
DEFAULT_POSTS = (
    "Публикуем график работы администрации района на следующую неделю",
    "В ДК «Заря» состоится концерт ансамбля «Вдохновение»",
    "Напоминаем жителям о сдаче показаний счётчиков до 25 числа",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проба пути «подбор сообществ → MiniMax» (без БД и без VK).",
    )
    parser.add_argument("--name", default=DEFAULT_NAME, help="название сообщества")
    parser.add_argument(
        "--description",
        default=DEFAULT_DESCRIPTION,
        help="описание сообщества",
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

    result = asyncio.run(
        categorize_candidate(
            name=args.name,
            description=args.description,
            members_count=1234,
            recent_posts=list(DEFAULT_POSTS),
            region_name=args.region,
        )
    )

    payload = {
        "success": bool(result.get("success")),
        "engine": result.get("engine"),
        "model": result.get("model"),
        "category": result.get("category"),
        "confidence": result.get("confidence"),
        "is_info_page": result.get("is_info_page"),
        "reasoning": result.get("reasoning"),
        "tokens_prompt": result.get("tokens_prompt"),
        "tokens_completion": result.get("tokens_completion"),
        "error": result.get("error"),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    ok = payload["success"] and payload["engine"] == "minimax"
    if not ok:
        print(
            "ПРОБА НЕ ПРОШЛА: путь «подбор сообществ → MiniMax» не ответил "
            "(см. engine/error выше).",
            file=sys.stderr,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
