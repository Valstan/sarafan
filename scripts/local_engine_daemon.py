#!/usr/bin/env python3
"""Демон локального движка: тянет батч с бокса по SSH, считает локально, отдаёт вердикты.

**Что это.** Схема Pull (мандат мозга 09.11): демон на ПК владельца **сам**
обращается к боксу по SSH, забирает посты без вердикта, считает их локальной
моделью и отдаёт вердикты обратно. Домашний ПК не нужно делать видимым из
интернета.

**Требования к ПК:** Python 3.9+ (только стандартная библиотека) и
OpenAI-совместимый локальный движок (`llama-server`, `opencode serve` и т.п.) на
`--model-url`. Ключи от бокса не нужны — только SSH.

**Запуск:**

    python scripts/local_engine_daemon.py --model-url http://127.0.0.1:8081/v1 \\
        --model-name local-model --interval 300

Проверка без модели (только выгрузка батча):

    python scripts/local_engine_daemon.py --dry-run --once

**Фолбэк.** Если демон молчит, ПК выключен или модель не отвечает — посты
остаются без вердикта, и их забирает облачный headless на боксе. DeepSeek
подключается ровно тогда, когда локальный движок не справился, — это и
задумано.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Каталог скриптов — чтобы рядом лежащий клиент opencode импортировался
#: независимо от текущего каталога.
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_SSH_HOST = "sarafan"
DEFAULT_REMOTE = "~/SETKA/scripts/local_bridge.sh"
DEFAULT_MODEL_URL = "http://127.0.0.1:8081/v1"
#: Журнал прогонов демона — в ``logs/`` рядом с остальными runtime-файлами
#: (каталог в git не едет).
LOG_NAME = "local_engine_daemon.jsonl"
DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / LOG_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(path: Path, row: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — журнал демона не должен ронять цикл
        pass


def _ssh(host: str, remote_cmd: str, *, stdin_data: Optional[str] = None) -> str:
    """Выполнить команду на боксе по SSH и вернуть stdout."""
    cmd = ["ssh", "-o", "ConnectTimeout=20", host, remote_cmd]
    proc = subprocess.run(
        cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "ssh %s вернул %d: %s" % (host, proc.returncode, (proc.stderr or "")[-400:])
        )
    return proc.stdout


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Достать JSON-объект из ответа модели (терпимо к ```json-обёрткам)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _call_model(
    model_url: str, model_name: str, system: str, user: str, max_tokens: int, timeout: float
) -> Dict[str, Any]:
    """Позвать локальный движок (OpenAI-совместимый контракт)."""
    body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        model_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    parsed = json.loads(raw)
    choices = parsed.get("choices") or []
    if not choices:
        raise RuntimeError("движок вернул пустой ответ: %s" % raw[:200])
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = parsed.get("usage") or {}
    return {"content": content, "usage": usage, "model": parsed.get("model") or model_name}


def _normalize_verdicts(
    payload: Dict[str, Any], posts: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Привести ответ модели к списку вердиктов по нашим постам."""
    raw_list = payload.get("verdicts")
    if not isinstance(raw_list, list):
        return []
    known = {str(p.get("lip")) for p in posts}
    out: List[Dict[str, Any]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        lip = str(raw.get("lip") or "").strip()
        if lip not in known:
            continue  # чужой lip — в запись не идёт (то же правило, что на боксе)
        action = str(raw.get("action") or "").strip().lower()
        if action not in ("publish", "delete", "hold"):
            action = "hold"
        merge_with = raw.get("merge_with")
        if not isinstance(merge_with, list):
            merge_with = []
        out.append(
            {
                "lip": lip,
                "theme": str(raw.get("theme") or "").strip() or "новости",
                "action": action,
                "merge_with": [str(m) for m in merge_with if str(m) in known],
                "split": bool(raw.get("split")),
                "confidence": _clamp(raw.get("confidence")),
                "urgent": bool(raw.get("urgent")),
                "importance": _clamp(raw.get("importance")),
                "reasoning": str(raw.get("reasoning") or "")[:200],
            }
        )
    return out


def _clamp(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def run_once(args: argparse.Namespace, log_path: Path) -> int:
    """Один круг: выгрузка → модель → приём."""
    started = time.time()
    export_cmd = "sudo -n %s export --limit %d" % (args.remote, args.limit)
    if args.regions:
        export_cmd += " --regions %s" % args.regions
    if args.regions_per_chunk:
        export_cmd += " --regions-per-chunk %d" % args.regions_per_chunk
    try:
        raw = _ssh(args.ssh_host, export_cmd)
    except Exception as e:  # noqa: BLE001 — демон не должен падать из-за SSH
        _log(log_path, {"ts": _now(), "ok": False, "stage": "export", "error": str(e)[:300]})
        print("[%s] не смог забрать батч: %s" % (_now(), e), flush=True)
        return 0

    try:
        batch = json.loads(raw)
    except json.JSONDecodeError:
        _log(log_path, {"ts": _now(), "ok": False, "stage": "export", "error": "не JSON"})
        return 0
    if batch.get("empty"):
        print("[%s] очередь пуста" % _now(), flush=True)
        # Пустой круг — тоже признак жизни демона: без него не отличить
        # «демон молчит» от «очереди нет».
        _log(log_path, {"ts": _now(), "ok": True, "stage": "empty", "posts": 0})
        return 0

    posts = batch.get("posts") or []
    if args.dry_run:
        print(
            "[%s] батч %s: постов %d, регионы %s, системный промпт %d симв., user %d симв."
            % (
                _now(),
                batch.get("batch_id"),
                len(posts),
                ",".join(batch.get("regions_in_batch") or []),
                len(batch.get("system") or ""),
                len(batch.get("user") or ""),
            ),
            flush=True,
        )
        if args.show_prompt:
            print(batch.get("user") or "", flush=True)
        _log(
            log_path,
            {
                "ts": _now(),
                "ok": True,
                "stage": "dry-run",
                "batch_id": batch.get("batch_id"),
                "posts": len(posts),
                "regions": batch.get("regions_in_batch"),
            },
        )
        return 0

    try:
        if args.engine == "opencode":
            # OpenCode v2 — не OpenAI-совместимый сервер, у него сессионная
            # схема (/api/session → prompt → wait → message). Клиент — рядом,
            # только стандартная библиотека.
            import local_engine_opencode

            chain = [m.strip() for m in (args.model_chain or "").split(",") if m.strip()] or None
            answer = local_engine_opencode.chat(
                base_url=args.model_url,
                user=batch.get("user") or "",
                system=batch.get("system") or "",
                model=args.model_name or None,
                chain=chain,
                timeout=args.model_timeout,
                password_file=Path(args.model_password_file) if args.model_password_file else None,
            )
            if not answer.get("ok"):
                raise RuntimeError(
                    "opencode не ответил: %s (%s)"
                    % (answer.get("reason"), str(answer.get("detail"))[:200])
                )
            answer = {
                "content": answer.get("content") or "",
                "usage": answer.get("usage") or {},
                "model": answer.get("model"),
            }
        else:
            answer = _call_model(
                args.model_url,
                args.model_name,
                batch.get("system") or "",
                batch.get("user") or "",
                args.max_tokens_per_post * max(1, len(posts)),
                args.model_timeout,
            )
    except Exception as e:  # noqa: BLE001 — недоступная модель = волна уйдёт в облако
        _log(
            log_path,
            {
                "ts": _now(),
                "ok": False,
                "stage": "model",
                "batch_id": batch.get("batch_id"),
                "posts": len(posts),
                "error": str(e)[:300],
            },
        )
        print("[%s] модель не ответила: %s" % (_now(), e), flush=True)
        return 0

    payload = _extract_json(answer["content"])
    verdicts = _normalize_verdicts(payload or {}, posts)
    usage = answer.get("usage") or {}
    report = {
        "ts": _now(),
        "ok": True,
        "stage": "submit",
        "batch_id": batch.get("batch_id"),
        "model": answer.get("model"),
        "posts": len(posts),
        "verdicts_sent": len(verdicts),
        "duration_ms": int((time.time() - started) * 1000),
        "tokens_prompt": usage.get("prompt_tokens"),
        "tokens_completion": usage.get("completion_tokens"),
        "regions": batch.get("regions_in_batch"),
    }
    try:
        # ``--shadow``: вердикты НЕ пишутся в корпус — только отчёт о прогоне.
        # Так демон может работать, не занимая посты и не вытесняя облачные
        # вердикты, пока качество локальной модели не измерено.
        submit_cmd = "sudo -n %s submit%s" % (args.remote, " --shadow" if args.shadow else "")
        out = _ssh(
            args.ssh_host,
            submit_cmd,
            stdin_data=json.dumps({"verdicts": verdicts, "run": report}, ensure_ascii=False),
        )
        result = json.loads(out)
        report.update(
            {
                "recorded": result.get("recorded"),
                "skipped_existing": result.get("skipped_existing"),
                "skipped_invalid": result.get("skipped_invalid"),
                "skipped_missing": result.get("skipped_missing"),
            }
        )
        print(
            "[%s] батч %s: постов %d, вердиктов %d, записано %s"
            % (_now(), batch.get("batch_id"), len(posts), len(verdicts), result.get("recorded")),
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        report["ok"] = False
        report["error"] = str(e)[:300]
        print("[%s] приём не прошёл: %s" % (_now(), e), flush=True)
    _log(log_path, report)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Демон локального движка (Pull)")
    ap.add_argument("--ssh-host", default=DEFAULT_SSH_HOST, help="алиас SSH до бокса")
    ap.add_argument("--remote", default=DEFAULT_REMOTE, help="путь к обёртке моста на боксе")
    ap.add_argument("--model-url", default=DEFAULT_MODEL_URL, help="база OpenAI-совместимого API")
    ap.add_argument("--model-name", default="local", help="имя модели в запросе")
    ap.add_argument(
        "--engine",
        choices=["openai", "opencode"],
        default="openai",
        help="движок: openai — OpenAI-совместимый (llama-server, LM Studio); "
        "opencode — сессионный протокол opencode serve v2",
    )
    ap.add_argument(
        "--model-chain",
        default="",
        help="цепочка моделей через запятую (opencode): первая живая закрывает вызов",
    )
    ap.add_argument(
        "--model-password-file",
        default="",
        help="файл с паролем opencode serve (по умолчанию ~/.config/opencode/service.json)",
    )
    ap.add_argument(
        "--shadow",
        action="store_true",
        help="режим тени: вердикты НЕ пишутся в корпус, только отчёт о прогоне",
    )
    ap.add_argument("--limit", type=int, default=20, help="постов в батч")
    ap.add_argument("--regions", default="", help="коды районов через запятую (пусто = все)")
    ap.add_argument(
        "--regions-per-chunk", type=int, default=0, help="регионов в вызов (0 = как на боксе)"
    )
    ap.add_argument("--interval", type=int, default=300, help="пауза между кругами, с")
    ap.add_argument("--max-tokens-per-post", type=int, default=240, help="потолок ответа на пост")
    ap.add_argument("--model-timeout", type=float, default=300.0, help="потолок ответа модели, с")
    ap.add_argument("--once", action="store_true", help="один круг и выход")
    ap.add_argument("--dry-run", action="store_true", help="только выгрузка, без модели")
    ap.add_argument("--show-prompt", action="store_true", help="печатать user-промпт (dry-run)")
    ap.add_argument(
        "--log",
        default=str(DEFAULT_LOG_PATH),
        help="локальный журнал прогонов (JSONL)",
    )
    args = ap.parse_args()

    log_path = Path(args.log)
    print(
        "локальный движок: ssh=%s движок=%s модель=%s (%s) интервал=%d с%s"
        % (
            args.ssh_host,
            args.engine,
            args.model_url,
            args.model_name,
            args.interval,
            " РЕЖИМ ТЕНИ" if args.shadow else "",
        ),
        flush=True,
    )
    while True:
        run_once(args, log_path)
        if args.once:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
