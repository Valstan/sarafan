#!/usr/bin/env python3
"""Обвязка облачной рутины HITL-классификатора: HTTP-механика отдельно от разметки.

Рутина-агент в облаке раньше сама ходила к ``/api/classifier`` с ключом прямо в
тексте промпта — модель рутины стала отказываться (ключ в инструкции + автономные
HTTP-вызовы с ним читаются как небезопасный паттерн, см. Troubleshooting в
``docs/ops/hitl-classifier-routine.md``). Скрипт забирает всю обвязку в
детерминированный код: ключ — из env облачного окружения
(``CLASSIFIER_INGEST_KEY``), запросы — stdlib ``urllib`` (в облачном checkout'е
зависимости проекта не установлены — ничего не импортировать из проекта!).
Модели остаётся чистая редакторская задача: прочитать ``pending.json`` +
``postulates.md``, вынести вердикты, сохранить ``verdicts.json``.

Подкоманды:
  fetch  [--limit N] [--out DIR] [--no-media] [--region CODE ...]
                                   GET /postulates + /pending →
                                   DIR/postulates.md + DIR/pending.json,
                                   сводка JSON в stdout (DIR дефолт classifier_run).
                                   Без --region батч берёт allowlist сервера
                                   (CLASSIFIER_REGION_CODES; пусто = все регионы).
                                   С --region (можно несколько раз) — только эти
                                   регионы, по одному запросу на регион, посты
                                   склеиваются в один pending.json (у каждого
                                   поста своё region_code). Пилот без DeepSeek:
                                   --region mi --region ur --region klz.
                                   Постам БЕЗ текста скачивает фото/PDF через
                                   media-прокси в DIR/media/ (пути — в поле
                                   media_files поста), чтобы модель посмотрела
                                   вложения и вынесла вердикт с media_summary.
  submit FILE                      локальная валидация + POST /verdicts из FILE
                                   ({"verdicts": [...]}), ответ сервера в stdout

Петля обучения (ADR-0005, рутина-дистиллятор — те же хост/ключ):
  corrections [--limit N] [--days D] [--out DIR]
                                   GET /postulates + /corrections →
                                   DIR/postulates.md + DIR/corrections.json,
                                   сводка JSON в stdout (DIR дефолт distill_run)
  propose FILE                     локальная валидация + POST /rule-proposals из
                                   FILE ({"proposals": [...]}), ответ в stdout

Env: CLASSIFIER_INGEST_KEY (обязателен; секрет облачного окружения рутины),
CLASSIFIER_API_BASE (опционально; дефолт — ASCII-хост VPS, см. docs/GATEWAY.md).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ASCII-хост VPS (punycode-IDN спотыкается об egress-прокси облака чаще).
DEFAULT_API_BASE = "https://3931b3fe50ab.vps.myjino.ru"
DEFAULT_OUT_DIR = "classifier_run"
DEFAULT_DISTILL_DIR = "distill_run"
DEFAULT_LIMIT = 200  # синхронно с CLASSIFIER_PENDING_MAX на проде (2026-07-16)
DEFAULT_CORRECTIONS_LIMIT = 100
DEFAULT_CORRECTIONS_DAYS = 30
TIMEOUT_S = 30
MEDIA_TIMEOUT_S = 60  # картинки/PDF качаются дольше JSON
MEDIA_MAX_PER_POST = 3  # потолок вложений на пост (токены)
MEDIA_FETCH_TYPES = ("photo", "doc")  # видео/аудио модель не «смотрит»
VALID_ACTIONS = ("publish", "delete", "hold")
# Зеркало границ RuleProposal (modules/classifier/schema.py) — ранняя диагностика вместо 422.
RULE_TEXT_MIN, RULE_TEXT_MAX = 3, 600
RATIONALE_MAX = 500


def api_base() -> str:
    return os.environ.get("CLASSIFIER_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _ingest_key() -> str:
    key = os.environ.get("CLASSIFIER_INGEST_KEY", "").strip()
    if not key:
        sys.exit(
            "CLASSIFIER_INGEST_KEY не задан: добавь его в переменные облачного "
            "окружения рутины (claude.ai/code → рутина → Update cloud environment)."
        )
    return key


def _request(method: str, path: str, body: dict | None = None) -> str:
    """HTTP к ingest-API; понятная смерть вместо трейсбека при сетевых бедах."""
    url = f"{api_base()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"X-API-Key": _ingest_key()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        sys.exit(f"HTTP {exc.code} {method} {path}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(
            f"Сетевая ошибка {method} {url}: {exc.reason}. "
            "403 на CONNECT = egress-прокси окружения (Network access → Custom, "
            "см. Troubleshooting в docs/ops/hitl-classifier-routine.md)."
        )


def validate_verdicts(payload: object) -> list[str]:
    """Проверить пакет вердиктов ДО отправки; список ошибок (пустой = валидно).

    Ловим то, что модель чаще всего портит в JSON: нет lip, action вне словаря,
    confidence вне 0..100, merge_with не списком. Сервер валидирует ещё раз
    (pydantic) — тут ранняя внятная диагностика вместо HTTP 422.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        return ['ожидается объект {"verdicts": [...]}']
    items = payload["verdicts"]
    if not items:
        return ["пустой список verdicts — нечего отправлять"]
    errors: list[str] = []
    for i, v in enumerate(items):
        if not isinstance(v, dict):
            errors.append(f"[{i}] не объект")
            continue
        if not str(v.get("lip") or "").strip():
            errors.append(f"[{i}] нет lip")
        if v.get("action") not in VALID_ACTIONS:
            errors.append(f"[{i}] action={v.get('action')!r} не из {list(VALID_ACTIONS)}")
        # Зеркало к проверке выше: у action словарь есть, у theme его нет, и
        # модель кладёт туда название действия. Сервер такую тему обнулит
        # молча — здесь она названа до отправки.
        if str(v.get("theme") or "").strip().casefold() in VALID_ACTIONS:
            errors.append(f"[{i}] theme={v.get('theme')!r} — это действие, а не тема")
        conf = v.get("confidence")
        if conf is not None and not (
            isinstance(conf, (int, float)) and not isinstance(conf, bool) and 0 <= conf <= 100
        ):
            errors.append(f"[{i}] confidence={conf!r} вне 0..100")
        merge_with = v.get("merge_with")
        if merge_with is not None and not isinstance(merge_with, list):
            errors.append(f"[{i}] merge_with не список")
    return errors


def validate_proposals(payload: object) -> list[str]:
    """Проверить пакет черновиков правил ДО отправки; список ошибок (пустой = валидно).

    Зеркалит границы pydantic-схемы ``RuleProposal``: rule_text 3..600, rationale
    до 500, evidence списком. Сервер валидирует ещё раз — тут ранняя внятная
    диагностика вместо HTTP 422.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
        return ['ожидается объект {"proposals": [...]}']
    items = payload["proposals"]
    if not items:
        return ["пустой список proposals — нечего отправлять"]
    errors: list[str] = []
    for i, p in enumerate(items):
        if not isinstance(p, dict):
            errors.append(f"[{i}] не объект")
            continue
        rule_text = p.get("rule_text")
        if not isinstance(rule_text, str) or not (
            RULE_TEXT_MIN <= len(rule_text.strip()) <= RULE_TEXT_MAX
        ):
            errors.append(f"[{i}] rule_text не строка {RULE_TEXT_MIN}..{RULE_TEXT_MAX} символов")
        rationale = p.get("rationale")
        if rationale is not None and not (
            isinstance(rationale, str) and len(rationale) <= RATIONALE_MAX
        ):
            errors.append(f"[{i}] rationale не строка до {RATIONALE_MAX} символов")
        evidence = p.get("evidence")
        if evidence is not None and not isinstance(evidence, list):
            errors.append(f"[{i}] evidence не список")
    return errors


def _media_ext(item: dict, content_type: str) -> str:
    """Расширение файла вложения: из ext документа, иначе из Content-Type."""
    ext = str(item.get("ext") or "").strip().lstrip(".").lower()
    if ext:
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "application/pdf": "pdf",
    }.get(ct, "bin")


def _fetch_media_for_textless(posts: list, out: Path) -> int:
    """Скачать фото/PDF постов БЕЗ текста через media-прокси → DIR/media/.

    Модель классификатора не видит VK напрямую (egress облака пускает только наш
    хост) — прокси ``/api/classifier/media`` отдаёт вложение с VK. У поста с
    текстом медиа не качаем (текста достаточно, токены бережём). Каждому посту
    проставляется ``media_files`` — локальные пути для чтения моделью. Ошибка
    скачивания не валит fetch: файл просто пропускается.
    """
    media_dir = out / "media"
    downloaded = 0
    for post in posts:
        if (post.get("text") or "").strip():
            continue
        items = [
            m
            for m in (post.get("media") or [])
            if isinstance(m, dict) and m.get("type") in MEDIA_FETCH_TYPES and m.get("url")
        ][:MEDIA_MAX_PER_POST]
        if not items:
            continue
        media_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for i, item in enumerate(items):
            q = urllib.parse.quote(item["url"], safe="")
            url = f"{api_base()}/api/classifier/media?url={q}"
            req = urllib.request.Request(url, headers={"X-API-Key": _ingest_key()})
            try:
                with urllib.request.urlopen(req, timeout=MEDIA_TIMEOUT_S) as resp:
                    data = resp.read()
                    ext = _media_ext(item, resp.headers.get("Content-Type", ""))
            except (urllib.error.URLError, OSError) as exc:
                print(f"media skip {post.get('lip')}[{i}]: {exc}", file=sys.stderr)
                continue
            safe_lip = str(post.get("lip", "post")).replace("/", "_")
            path = media_dir / f"{safe_lip}_{i}.{ext}"
            path.write_bytes(data)
            files.append(str(path))
            downloaded += 1
        if files:
            post["media_files"] = files
    return downloaded


def _fetch_merged_pending(codes: list[str], limit: int) -> dict:
    """Забрать /pending по каждому региону и склеить посты в один батч.

    У каждого поста ответа уже есть своё ``region_code``, поэтому склейка
    безопасна: чанкирование по регионам делает классифицирующая сторона.
    Отдельная функция ради юнит-теста без сети (см.
    tests/test_scripts/test_classifier_routine.py).
    """
    posts: list[dict] = []
    for code in codes:
        q = urllib.parse.quote(code, safe="")
        batch = json.loads(_request("GET", f"/api/classifier/pending?region={q}&limit={limit}"))
        posts.extend(batch.get("posts") or [])
    return {"region_filter": list(codes), "count": len(posts), "posts": posts}


def cmd_fetch(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    postulates = _request("GET", "/api/classifier/postulates")
    regions = [str(r).strip() for r in (args.region or []) if str(r).strip()]
    if regions:
        pending = _fetch_merged_pending(regions, args.limit)
    else:
        pending = json.loads(_request("GET", f"/api/classifier/pending?limit={args.limit}"))
    media_files = 0
    if not args.no_media:
        media_files = _fetch_media_for_textless(pending.get("posts") or [], out)
    (out / "postulates.md").write_text(postulates, encoding="utf-8")
    (out / "pending.json").write_text(
        json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "count": pending.get("count", 0),
                "region_filter": pending.get("region_filter"),
                "media_files": media_files,
                "out": str(out),
            },
            ensure_ascii=False,
        )
    )


def cmd_submit(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    errors = validate_verdicts(payload)
    if errors:
        sys.exit("Невалидные вердикты (исправь и повтори submit):\n" + "\n".join(errors))
    print(_request("POST", "/api/classifier/verdicts", body=payload))


def cmd_corrections(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    postulates = _request("GET", "/api/classifier/postulates")
    corrections = json.loads(
        _request("GET", f"/api/classifier/corrections?limit={args.limit}&days={args.days}")
    )
    (out / "postulates.md").write_text(postulates, encoding="utf-8")
    (out / "corrections.json").write_text(
        json.dumps(corrections, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"count": corrections.get("count", 0), "days": args.days, "out": str(out)},
            ensure_ascii=False,
        )
    )


def cmd_propose(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    errors = validate_proposals(payload)
    if errors:
        sys.exit("Невалидные черновики правил (исправь и повтори propose):\n" + "\n".join(errors))
    print(_request("POST", "/api/classifier/rule-proposals", body=payload))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="забрать постулаты + батч постов в файлы")
    p_fetch.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p_fetch.add_argument("--out", default=DEFAULT_OUT_DIR)
    p_fetch.add_argument(
        "--no-media",
        action="store_true",
        help="не качать вложения постов без текста (медиа-анализ выключен)",
    )
    p_fetch.add_argument(
        "--region",
        action="append",
        default=[],
        help="код региона (можно несколько раз); без флага — allowlist сервера",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_submit = sub.add_parser("submit", help="отправить verdicts.json на сервер")
    p_submit.add_argument("file")
    p_submit.set_defaults(func=cmd_submit)

    p_corr = sub.add_parser(
        "corrections", help="забрать постулаты + коррекции оператора в файлы (дистилляция)"
    )
    p_corr.add_argument("--limit", type=int, default=DEFAULT_CORRECTIONS_LIMIT)
    p_corr.add_argument("--days", type=int, default=DEFAULT_CORRECTIONS_DAYS)
    p_corr.add_argument("--out", default=DEFAULT_DISTILL_DIR)
    p_corr.set_defaults(func=cmd_corrections)

    p_prop = sub.add_parser("propose", help="отправить proposals.json на сервер (черновики правил)")
    p_prop.add_argument("file")
    p_prop.set_defaults(func=cmd_propose)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
