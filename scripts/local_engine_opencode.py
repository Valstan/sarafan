#!/usr/bin/env python3
"""Клиент opencode v2 для локального движка: сессионный протокол serve.

**Что это.** OpenCode v2 — не OpenAI-совместимый сервер: у него своя схема
(`/api/session` → prompt → wait → message). Этот модуль — тонкий клиент к ней,
**только стандартная библиотека**: демон живёт на ПК владельца и поднимается
без зависимостей проекта.

Контракт (проверен на этой машине 10.11 против opencode v2.1.4 — тот же, что
использовался на боксе до ADR-0014):

1. ``POST /api/session`` — сессия сразу с моделью
   (``{"model": {"id", "providerID", "variant"}}``) и запретами инструментов
   (текст постов — недоверенный вход, вердикту инструменты не нужны);
2. ``POST /api/session/{id}/prompt`` — сообщение в очередь (асинхронно);
3. ``POST /api/experimental/session/{id}/wait`` — дождаться тишины сессии;
4. ``GET /api/session/{id}/message`` — собрать ответ: текст в
   ``content[type=text]``, счётчики в ``tokens``, отказ — ``idle.outcome`` !=
   ``succeeded``;
5. ``DELETE /api/session/{id}`` — сессия удаляется за собой, иначе пул и данные
   сервера растут молча.

**Ролевых частей у serve нет** — постулаты и посты уходят одним сообщением
(``system + "\\n\\n" + user``). ``response_format`` сервер не принимает:
строгий JSON обеспечивается формулировкой промпта.

**Пароль.** ``opencode serve`` генерирует его один раз и кладёт в
``~/.config/opencode/service.json``; он же принимает basic-auth с логином
``opencode``. Ключ на ПК лежит локально и никуда не уходит.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Логин serve (совпадает с тем, что ждёт opencode).
DEFAULT_USER = "opencode"

#: Файл с паролем, который serve пишет при старте.
DEFAULT_PASSWORD_FILE = Path.home() / ".config" / "opencode" / "service.json"

#: Цепочка моделей-фолбэков: первая живая закрывает вызов. Порядок — тот же,
#: что на боксе до 06.11 (ADR-0014, пункт «цепочка»).
DEFAULT_CHAIN = [
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "mimo-v2.6-flash-free",
    "flint-40b-free",
]

#: Уровень рассуждений по модели (решение владельца 29.10 — ``high``).
DEFAULT_VARIANTS = {
    "deepseek-v4.1-flash-free": "high",
    "glm-5.3-flash-free": "high",
}

#: Запреты инструментов для сессии классификатора: посты — недоверенный вход.
#: Узкие запреты на «read» (точечные) — широкий запрет ломает сам вызов
#: free-тира (проверено на боксе 17–18.10).
PERMISSIONS: List[Dict[str, str]] = [
    {"action": action, "resource": "*", "effect": "deny"}
    for action in (
        "shell",
        "edit",
        "webfetch",
        "websearch",
        "subagent",
        "browser",
        "grep",
        "glob",
        "todowrite",
        "skill",
        "question",
        "execute",
    )
] + [
    {"action": "read", "resource": "*anon.env*", "effect": "deny"},
    {"action": "read", "resource": "*/.ssh/*", "effect": "deny"},
]


class OpencodeError(RuntimeError):
    """Отказ контура opencode (с причиной)."""


def read_password(password_file: Optional[Path] = None) -> str:
    """Пароль serve: из файла, который он сам пишет при старте."""
    path = Path(password_file) if password_file else DEFAULT_PASSWORD_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OpencodeError("не читается пароль opencode (%s): %s" % (path, e))
    password = str(payload.get("password") or "").strip()
    if not password:
        raise OpencodeError("в %s нет пароля" % path)
    return password


def _auth_header(user: str, password: str) -> str:
    raw = ("%s:%s" % (user, password)).encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _request(
    base_url: str,
    path: str,
    *,
    auth: str,
    body: Optional[Dict[str, Any]] = None,
    method: Optional[str] = None,
    timeout: float = 120.0,
) -> Tuple[int, str]:
    """Один HTTP-вызов к serve. Никогда не бросает: отказ — (0, текст)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — сеть, таймаут, TLS
        return 0, str(e)


def parse_messages(messages: Any) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Собрать из ленты сессии ``(text, usage, error)``.

    Лента приходит новейшим вперёд (``order=desc``); текст ответа лежит в
    ``content[]`` (``{"type": "text"}``), счётчики — в ``tokens``, отказ — в
    ``idle.outcome`` != ``succeeded``.
    """
    if not isinstance(messages, list):
        return "", None, "лента сессии не список"
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        error = info.get("error")
        if error:
            return "", None, str(error)
        parts = message.get("parts")
        if not isinstance(parts, list):
            parts = message.get("content") if isinstance(message.get("content"), list) else []
        text = ""
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                text = str(part["text"])
                break
        if text:
            usage = info.get("tokens") if isinstance(info.get("tokens"), dict) else None
            return text, usage, None
    return "", None, None


def _close_session(base_url: str, session_id: str, auth: str) -> None:
    _request(base_url, "/api/session/" + session_id, auth=auth, method="DELETE", timeout=30.0)


def chat(
    *,
    base_url: str,
    user: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    chain: Optional[List[str]] = None,
    timeout: float = 120.0,
    user_name: str = DEFAULT_USER,
    password_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Спросить opencode. Контракт ответа — как у DeepSeek-клиента.

    ``{"ok": True, "content", "model", "usage"}`` либо
    ``{"ok": False, "reason", "detail"}``. Никогда не бросает.
    """
    if not (user or "").strip():
        return {"ok": False, "reason": "empty_prompt"}

    try:
        password = read_password(password_file)
    except OpencodeError as e:
        return {"ok": False, "reason": "no_password", "detail": str(e)}

    auth = _auth_header(user_name, password)
    models = list(chain or DEFAULT_CHAIN)
    if model:
        models = [model] + [m for m in models if m != model]

    # У serve v2 ролевых частей нет: постулаты и посты — одним сообщением.
    prompt = "%s\n\n%s" % (system.strip(), user.strip()) if (system or "").strip() else user.strip()

    reasons: List[str] = []
    detail = ""
    for model_id in models:
        model_ref: Dict[str, Any] = {"id": model_id, "providerID": "opencode"}
        variant = DEFAULT_VARIANTS.get(model_id)
        if variant:
            model_ref["variant"] = variant

        status, body = _request(
            base_url,
            "/api/session",
            auth=auth,
            body={
                "title": "classify",
                "model": model_ref,
                "permissions": PERMISSIONS,
            },
            timeout=min(timeout, 30.0),
        )
        if status == 0:
            reasons.append("%s: network" % model_id)
            detail = body
            continue
        if status >= 400:
            reasons.append("%s: http_%s" % (model_id, status))
            detail = body[:300]
            continue
        try:
            session_id = str((json.loads(body).get("data") or {}).get("id") or "")
        except (json.JSONDecodeError, AttributeError):
            session_id = ""
        if not session_id:
            reasons.append("%s: no_session" % model_id)
            detail = body[:300]
            continue

        status, prompt_body = _request(
            base_url,
            "/api/session/%s/prompt" % session_id,
            auth=auth,
            body={"text": prompt},
            timeout=min(timeout, 30.0),
        )
        if status == 0 or status >= 400:
            _close_session(base_url, session_id, auth)
            reasons.append("%s: prompt_%s" % (model_id, status or "network"))
            detail = prompt_body[:300]
            continue

        status, wait_body = _request(
            base_url,
            "/api/experimental/session/%s/wait" % session_id,
            auth=auth,
            body=None,
            method="POST",
            timeout=timeout,
        )
        if status == 0 or status >= 400:
            _close_session(base_url, session_id, auth)
            reasons.append("%s: wait_%s" % (model_id, status or "network"))
            detail = wait_body[:300]
            continue

        status, messages_body = _request(
            base_url,
            "/api/session/%s/message?limit=20&order=desc" % session_id,
            auth=auth,
            timeout=min(timeout, 30.0),
        )
        _close_session(base_url, session_id, auth)
        if status == 0 or status >= 400:
            reasons.append("%s: messages_%s" % (model_id, status or "network"))
            detail = messages_body[:300]
            continue

        try:
            raw = json.loads(messages_body).get("data")
        except (json.JSONDecodeError, AttributeError):
            raw = None
        text, usage, error = parse_messages(raw if isinstance(raw, list) else [])
        if error:
            reasons.append("%s: %s" % (model_id, error[:120]))
            detail = error[:300]
            continue
        if not text:
            reasons.append("%s: empty_response" % model_id)
            detail = messages_body[:200]
            continue

        prompt_tokens = int((usage or {}).get("input") or 0)
        completion_tokens = int((usage or {}).get("output") or 0)
        return {
            "ok": True,
            "content": text,
            "model": model_id,
            "usage": {
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }

    return {
        "ok": False,
        "reason": "engine_all_failed",
        "detail": "; ".join(reasons)[:400] or detail,
    }
