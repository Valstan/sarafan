#!/usr/bin/env python3
"""Забрать свой раздел промтов КАРМАНА (D-190) и отправить выполненное в архив.

Задание владельца лежит в библиотеке КАРМАНА в разделе проекта; рядом с каждым
промтом лежит готовый план (заголовок начинается на ``План: ``). Проект видит
**только свой** раздел, уточняет план под себя и выполняет; выполненное
отправляется в архив тем же токеном.

Запуск на боксе под env приложения:

    sudo bash -c 'set -a; . /etc/setka/setka.env; . /etc/setka/secrets-token.env; set +a;
                  cd ~/SETKA && venv/bin/python scripts/prompts_fetch.py'

Опции: ``--archived`` — архивные промты; ``--json`` — сырой ответ API;
``--archive ID`` — отправить промт в архив («сделано»); ``--restore ID`` —
вернуть из архива.

Коды выхода: 0 — ок, 1 — нет токена раздела или канал не настроен (503),
2 — операция с промтом не удалась. Значение токена наружу не идёт.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import prompts_section as ps  # noqa: E402

DEFAULT_PROJECT = (os.getenv("PROMPTS_PROJECT") or "SARAFAN").strip()


def _print_prompts(prompts, *, show_plan: bool = True) -> None:
    for item in prompts:
        text = ps.prompt_text(item)
        plan = ps.is_plan(item)
        head = f"id={item.get('id')} archived={item.get('archived')}"
        if show_plan:
            head += " план" if plan else " промт"
        print(head)
        if text:
            body = text if len(text) <= 600 else text[:600] + " …"
            for line in body.splitlines() or [""]:
                print(f"    {line}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--project", default=DEFAULT_PROJECT, help="имя раздела (по умолчанию SARAFAN)"
    )
    parser.add_argument("--archived", action="store_true", help="показать архивные промты")
    parser.add_argument("--json", action="store_true", help="сырой ответ API")
    parser.add_argument("--archive", type=int, metavar="ID", help="отправить промт в архив")
    parser.add_argument("--restore", type=int, metavar="ID", help="вернуть промт из архива")
    args = parser.parse_args(argv)

    token = ps.resolve_section_token(args.project)
    if not token:
        print(
            f"нет токена раздела {ps.token_name(args.project)}: " "ни в env, ни в комнате setka",
            file=sys.stderr,
        )
        return 1

    if args.archive is not None or args.restore is not None:
        prompt_id = args.archive if args.archive is not None else args.restore
        archived = args.archive is not None
        status, payload = ps.archive_prompt(token, args.project, prompt_id, archived=archived)
        if status != 200:
            print(
                f"не удалось {'отправить в архив' if archived else 'вернуть'} "
                f"промт {prompt_id}: статус {status} {str(payload)[:200]}",
                file=sys.stderr,
            )
            return 2
        print(f"промт {prompt_id}: {'в архиве' if archived else 'вернут из архива'}")
        return 0

    status, prompts = ps.fetch_section(token, args.project, archived=args.archived)
    if status == 503:
        print(
            "канал промтов раздела не настроен: сервис не видит токен раздела "
            "(грант не принят) — 503",
            file=sys.stderr,
        )
        return 1
    if status == 403:
        print("токен не от того раздела (403)", file=sys.stderr)
        return 1
    if status != 200:
        print(f"раздел недоступен: статус {status}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(prompts, ensure_ascii=False, indent=2))
        return 0

    if not prompts:
        print("раздел пуст — живых заданий нет" + (" (среди архивных)" if args.archived else ""))
        return 0

    print(f"раздел {args.project}: записей {len(prompts)}")
    _print_prompts(prompts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
