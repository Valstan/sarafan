"""Клиент раздела промтов КАРМАНА (D-190: мандат brain 2026-11-08 + erratum 09.11).

**Что это.** Раздел проекта в библиотеке КАРМАНА = промт (задание владельца) и
готовый план рядом с ним (заголовок начинается на ``План: ``). Проект **сам**
забирает свой раздел, уточняет план под себя, выполняет и отправляет промт в
архив. Архив = «сделано»; не выполнил — не архивируй (промт остаётся живым, и
это видно владельцу).

**Контракт** (адрес сервиса приходит из ``SECRETS_VAULT_URL`` — в коде его нет
намеренно, как и у остальных клиентов хранилища):

```
GET  {vault}/api/prompts/section?project=<РАЗДЕЛ>[&archived=1]
POST {vault}/api/prompts/section   {"project": ..., "id": N, "archived": true}
```

Ответы: ``200`` ок · ``400`` плохой id · ``403`` токен не от того раздела ·
``404`` такого промта в разделе нет · ``503`` канал не настроен (сервис не
видит токен раздела — грант не принят).

**Токен раздела** — единственное право на раздел: ``PROMPTS_TOKEN_<РАЗДЕЛ>``,
секрет **нашей** комнаты. Он лежит в комнате и доезжает до процесса обычным
bootstrap'ом (``modules/secrets_bootstrap.py``, allowlist). Значение токена
никогда не печатается и не попадает в лог.

**Читаемый раздел — свой.** Токен другого раздела не сработает (403), а
использовать чужой раздел незачем: «виден только свой» — правило канона
(D-190).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

_TIMEOUT_SEC = 15

#: Поля, в которых может лежать текст промта — контракт отдаёт их по-разному.
_TEXT_FIELDS = ("title", "text", "body", "content", "prompt", "message", "name")

#: Заголовок плана в разделе начинается с этого префикса (канон D-190).
PLAN_PREFIX = "План: "


def section_base(vault_url: Optional[str] = None) -> str:
    """``…/api/secrets`` → ``…`` (база сервиса, где живёт ``/api/prompts``)."""
    base = (vault_url or os.getenv("SECRETS_VAULT_URL") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "адрес хранилища не задан: передай vault_url или задай SECRETS_VAULT_URL"
        )
    return base.rsplit("/api/secrets", 1)[0]


def section_url(project: str, *, archived: bool = False, vault_url: Optional[str] = None) -> str:
    """Адрес своего раздела: ``{base}/api/prompts/section?project=<РАЗДЕЛ>``."""
    query = {"project": project}
    if archived:
        query["archived"] = "1"
    return f"{section_base(vault_url)}/api/prompts/section?{urllib.parse.urlencode(query)}"


def token_name(project: str) -> str:
    """Каноническое имя секрета раздела: ``SARAFAN`` → ``PROMPTS_TOKEN_SARAFAN``."""
    slug = "".join(ch for ch in str(project or "").upper() if ch.isalnum())
    if not slug:
        raise ValueError(f"имя раздела пустое: {project!r}")
    return f"PROMPTS_TOKEN_{slug}"


def _call(
    method: str, url: str, token: str, *, body: Optional[Dict[str, Any]] = None
) -> Tuple[int, Any]:
    """Один HTTP-вызов → ``(код, разобранный ответ)``; сеть/сбой → ``(0, {...})``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return int(resp.status), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover — тело ошибки не обязано читаться
            pass
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"error": raw[:300]}
        return int(e.code), parsed
    except Exception as e:  # noqa: BLE001 — сеть/DNS/таймаут
        return 0, {"error": f"{type(e).__name__}: {e}"[:300]}


def _items(payload: Any) -> List[Dict[str, Any]]:
    """Разобрать ответ раздела в список промтов (форма ответа может меняться)."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("prompts", "items", "results", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [p for p in rows if isinstance(p, dict)]
    return []


def prompt_text(item: Dict[str, Any]) -> str:
    """Текст промта/плана: первое непустое текстовое поле."""
    for field in _TEXT_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_plan(item: Dict[str, Any]) -> bool:
    """План ли это (заголовок начинается с ``План: ``)?"""
    return prompt_text(item).startswith(PLAN_PREFIX)


def fetch_section(
    token: str,
    project: str,
    *,
    archived: bool = False,
    vault_url: Optional[str] = None,
    call=_call,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Забрать свой раздел → ``(код, промты)``.

    ``код`` — статус API: 200 ок, 403 чужой токен, 503 канал не настроен.
    """
    url = section_url(project, archived=archived, vault_url=vault_url)
    status, payload = call("GET", url, token)
    if status != 200:
        return status, []
    return status, _items(payload)


def archive_prompt(
    token: str,
    project: str,
    prompt_id: int,
    *,
    archived: bool = True,
    vault_url: Optional[str] = None,
    call=_call,
) -> Tuple[int, Any]:
    """Отправить промт в архив (или вернуть) → ``(код, ответ)``.

    Архив = «сделано». Вернуть можно тем же вызовом (``archived=False``).
    """
    url = f"{section_base(vault_url)}/api/prompts/section"
    body = {"project": project, "id": int(prompt_id), "archived": bool(archived)}
    return call("POST", url, token, body=body)


def resolve_section_token(
    project: str,
    *,
    env: Optional[Dict[str, str]] = None,
    room_token: Optional[str] = None,
    vault_url: Optional[str] = None,
    call=_call,
) -> Optional[str]:
    """Токен раздела: из окружения, иначе из своей комнаты.

    Путь «комната → процесс» — штатный (bootstrap доставляет секреты из
    комнаты в env). Запасной путь нужен там, где env ещё не обновлён: токен
    берётся из комнаты комнатным токеном (``SECRETS_TOKEN``).

    Значение токена наружу не отдаётся — ни в лог, ни в исключение.
    """
    environ = env if env is not None else os.environ
    name = token_name(project)
    value = (environ.get(name) or "").strip()
    if value:
        return value

    room = (room_token or environ.get("SECRETS_TOKEN") or "").strip()
    if not room:
        return None
    base = section_base(vault_url)
    status, payload = call("GET", f"{base}/api/secrets", room)
    if status != 200 or not isinstance(payload, dict):
        return None
    secrets = payload.get("secrets")
    if not isinstance(secrets, dict):
        secrets = payload
    value = (secrets.get(name) or "").strip()
    return value or None
