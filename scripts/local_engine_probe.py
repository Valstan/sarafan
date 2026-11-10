#!/usr/bin/env python3
"""Прогнать золотую выборку через локальный движок (ПК владельца).

**Что это.** Инструмент теневой сверки: бокс выгружает выборку постов с
решением оператора и **теми же промптами**, что получает облако; этот скрипт
считает их локальной моделью и пишет вердикты в JSON; бокс с той же выборкой
считает облачные вердикты и сравнивает оба набора попарно.

Скрипт **ничего не пишет** в корпус: ни одна строка в
``content_classifications`` не появляется — измерение не должно занимать посты.

Запуск (на ПК):

    python scripts/local_engine_probe.py \\
        --sample C:\\temp\\sample.json --out C:\\temp\\local_verdicts.json \\
        --engine opencode --model-url http://127.0.0.1:4097

Только стандартная библиотека: демон и проба живут на ПК и не требуют
зависимостей проекта.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_engine_daemon import _extract_json, _normalize_verdicts  # noqa: E402


def _call_openai(
    model_url: str,
    model_name: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> Dict[str, Any]:
    """OpenAI-совместимый эндпоинт (llama-server, LM Studio, vLLM)."""
    import urllib.request

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
    request = urllib.request.Request(
        model_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    parsed = json.loads(raw)
    choices = parsed.get("choices") or []
    if not choices:
        raise RuntimeError("движок вернул пустой ответ")
    content = (choices[0].get("message") or {}).get("content") or ""
    return {
        "content": content,
        "usage": parsed.get("usage") or {},
        "model": parsed.get("model") or model_name,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Прогон золотой выборки через локальный движок")
    ap.add_argument("--sample", required=True, help="JSON с выборкой (с бокса)")
    ap.add_argument("--out", required=True, help="куда писать вердикты")
    ap.add_argument("--engine", choices=["opencode", "openai"], default="opencode")
    ap.add_argument("--model-url", default="http://127.0.0.1:4097", help="адрес движка")
    ap.add_argument("--model-name", default="", help="модель (для openai-совместимого)")
    ap.add_argument("--model-chain", default="", help="цепочка моделей через запятую (opencode)")
    ap.add_argument("--model-password-file", default="", help="файл с паролем serve (opencode)")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число чанков (0 = все)")
    ap.add_argument("--timeout", type=float, default=180.0, help="потолок одного вызова, с")
    ap.add_argument("--max-tokens-per-post", type=int, default=240)
    ap.add_argument("--show-prompt", action="store_true", help="печатать user-промпт первого чанка")
    args = ap.parse_args()

    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    chunks = sample.get("chunks") or []
    if args.limit:
        chunks = chunks[: args.limit]
    if not chunks:
        print("в выборке нет чанков — нечего считать")
        return 1

    chain = [m.strip() for m in args.model_chain.split(",") if m.strip()] or None

    opencode_client = None
    if args.engine == "opencode":
        import local_engine_opencode

        opencode_client = local_engine_opencode

    out_chunks: List[Dict[str, Any]] = []
    failures = 0
    started = time.time()

    for chunk in chunks:
        system = chunk.get("system") or ""
        user = chunk.get("user") or ""
        posts = chunk.get("posts") or []
        if args.show_prompt and chunk is chunks[0]:
            print("--- user-промпт первого чанка ---")
            print(user[:2000])

        if args.engine == "opencode":
            answer = opencode_client.chat(
                base_url=args.model_url,
                user=user,
                system=system,
                model=args.model_name or None,
                chain=chain,
                timeout=args.timeout,
                password_file=Path(args.model_password_file) if args.model_password_file else None,
            )
            if not answer.get("ok"):
                failures += 1
                print(
                    "[чанк %s] движок не ответил: %s"
                    % (chunk.get("index"), answer.get("detail"))[:200]
                )
                out_chunks.append(
                    {
                        "index": chunk.get("index"),
                        "ok": False,
                        "error": str(answer.get("detail") or answer.get("reason"))[:300],
                        "verdicts": [],
                    }
                )
                continue
            content = answer.get("content") or ""
            usage = answer.get("usage") or {}
            model_name = answer.get("model")
        else:
            try:
                answer = _call_openai(
                    args.model_url,
                    args.model_name or "local",
                    system,
                    user,
                    args.max_tokens_per_post * max(1, len(posts)),
                    args.timeout,
                )
            except Exception as e:  # noqa: BLE001 — движок недоступен = волна в облако
                failures += 1
                print("[чанк %s] модель не ответила: %s" % (chunk.get("index"), e))
                out_chunks.append(
                    {
                        "index": chunk.get("index"),
                        "ok": False,
                        "error": str(e)[:300],
                        "verdicts": [],
                    }
                )
                continue
            content = answer.get("content") or ""
            usage = answer.get("usage") or {}
            model_name = answer.get("model")

        payload = _extract_json(content)
        verdicts = _normalize_verdicts(payload or {}, posts)
        out_chunks.append(
            {
                "index": chunk.get("index"),
                "ok": True,
                "model": model_name,
                "verdicts": verdicts,
                "usage": usage,
            }
        )
        print(
            "[чанк %s] постов %d, вердиктов %d%s"
            % (
                chunk.get("index"),
                len(posts),
                len(verdicts),
                (" (%s)" % model_name) if model_name else "",
            ),
            flush=True,
        )

    result = {
        "engine": args.engine,
        "model_url": args.model_url,
        "chunks": out_chunks,
        "failures": failures,
        "duration_ms": int((time.time() - started) * 1000),
        "sample_posts": len(sample.get("posts") or []),
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    total_verdicts = sum(len(c.get("verdicts") or []) for c in out_chunks)
    print(
        "готово: чанков %d, вердиктов %d, отказов %d → %s"
        % (len(out_chunks), total_verdicts, failures, args.out)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
