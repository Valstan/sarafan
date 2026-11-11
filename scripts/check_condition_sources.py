#!/usr/bin/env python3
"""Проверка слоя условий: источник жив и говорит то, что мы ему приписываем.

Пилот #386, шаг 1. Это **гейт приёмки**, а не разовый прогон: условие, которое
разошлось с источником (владелец переписал страницу, ссылка повела в 404, мы
сформулировали своё вместо его слов), — это обещание, которое человек проверит и
не найдёт. Поэтому проверка живёт в репозитории и запускается по кнопке.

**Что именно проверяется.** Вопрос условия — это **наша** формулировка
(«Как добавить свою службу в справочник?»), и на странице владельца её дословно
нет: там лежит правило своими словами. Поэтому сверяется не вопрос, а **суть**
(``then``): доля значимых слов условия, встречающаяся в видимом тексте
источника. Порог по умолчанию 75 % — ниже условие считается не подтверждённым.

Тонкости, из-за которых это не работало с первого раза:

- **IDN-хост** надо перевести в punycode: urllib умеет latin-1 только, кириллица
  в URL даёт «latin-1 codec can't encode characters».
- **Видимый текст**, а не исходник: скрипты и стили убираются, сущности
  разворачиваются, иначе счёт идёт по мусору.

Запуск:

    python scripts/check_condition_sources.py            # порог 75 %
    python scripts/check_condition_sources.py --threshold 0.9
    python scripts/check_condition_sources.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.service_conditions import CONDITIONS  # noqa: E402

# Обычные слова — не носители смысла условия.
STOP = {
    "и",
    "в",
    "на",
    "с",
    "по",
    "для",
    "не",
    "что",
    "как",
    "это",
    "от",
    "до",
    "а",
    "но",
    "или",
    "если",
    "то",
    "за",
    "у",
    "же",
    "бы",
    "их",
    "мы",
    "вы",
    "он",
    "она",
    "они",
    "все",
    "вот",
    "где",
    "к",
    "о",
    "из",
}

DEFAULT_THRESHOLD = 0.75
CTX = ssl.create_default_context()


def visible(raw: str) -> str:
    """Видимый текст страницы: без скриптов, стилей и тегов."""
    raw = re.sub(r"<script\b[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style\b[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", raw)
    for entity, char in (
        ("&nbsp;", " "),
        ("&mdash;", "—"),
        ("&laquo;", "«"),
        ("&raquo;", "»"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&ndash;", "–"),
        ("&rsquo;", "’"),
        ("&laquo;", "«"),
        ("&#8212;", "—"),
    ):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text)


def meaningful(text: str) -> List[str]:
    """Значимые слова текста: длиннее 3 символов и не стоп-слова."""
    return [
        w
        for w in re.findall(r"[0-9A-Za-zА-Яа-яЁё-]+", text.lower())
        if len(w) > 3 and w not in STOP
    ]


def fetch(url: str, timeout: int = 25) -> Tuple[int, str]:
    """Страница по URL. IDN-хост переводится в punycode (urllib не умеет кириллицу)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = host + (f":{parts.port}" if parts.port else "")
    real = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    req = urllib.request.Request(real, headers={"User-Agent": "Mozilla/5.0 (source-check)"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def check(threshold: float) -> Tuple[List[dict], int]:
    """Проверить каждый источник. Возвращает (отчёт, число непроверенных)."""
    pages: Dict[str, List[tuple]] = {}
    for title, items in CONDITIONS.items():
        for item in items:
            pages.setdefault(item["source_ref"], []).append((title, item))

    report: List[dict] = []
    failed = 0
    for url, uses in sorted(pages.items()):
        try:
            code, body = fetch(url)
        except (urllib.error.URLError, OSError) as exc:
            report.append({"url": url, "error": str(exc), "conditions": []})
            failed += len(uses)
            continue
        page_words = set(re.findall(r"[0-9a-zа-яё-]{4,}", visible(body).lower()))
        rows = []
        for title, item in uses:
            cond_words = meaningful(item["then"])
            hit = sum(1 for w in cond_words if w in page_words)
            share = hit / len(cond_words) if cond_words else 0.0
            missing = [w for w in cond_words if w not in page_words]
            ok = share >= threshold
            if not ok:
                failed += 1
            rows.append(
                {
                    "service": title,
                    "question": item["question"],
                    "coverage": round(share, 3),
                    "ok": ok,
                    "missing_words": missing[:12],
                }
            )
        report.append({"url": url, "status": code, "conditions": rows})
    return report, failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=(
            "минимальная доля слов условия, найденных в источнике "
            f"(по умолчанию {DEFAULT_THRESHOLD})"
        ),
    )
    parser.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    args = parser.parse_args()

    report, failed = check(args.threshold)

    if args.json:
        print(
            json.dumps(
                {"threshold": args.threshold, "failed": failed, "sources": report},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if failed else 0

    for entry in report:
        if entry.get("error"):
            print(f"FAIL {entry['url']} -> {entry['error']}")
            continue
        print(f"[{entry['status']}] {entry['url']}")
        for row in entry["conditions"]:
            mark = "OK " if row["ok"] else "!! "
            print(f"  {mark} {row['coverage']:5.0%}  {row['service']}: {row['question']}")
            if not row["ok"]:
                print(f"        не найдены на странице: {row['missing_words']}")
    total = sum(len(e["conditions"]) for e in report if not e.get("error"))
    print(f"\nстраниц-источников: {len(report)}; условий: {total}; не подтверждено: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
