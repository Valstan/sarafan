"""Приём входящих выдач (grant'ов) в комнате КАРМАНа по allowlist — вторая рука D-061.

Выдача секрета между комнатами **двусторонняя** (мандат brain 2026-09-03): источник
предлагает ``POST /api/secrets/grants`` → выдача ``pending``; получатель принимает
``POST /api/secrets/grants/<id>/accept`` своим пишущим токеном → ``active``, и
только после этого имя появляется в его ``GET /api/secrets``. До принятия чужое
имя ничего у нас не занимает и в окружение не попадает.

**Принять — это согласие цели**, поэтому принимаем **только имена из своего
allowlist**. Иначе токен любой комнаты мог бы подставить произвольную переменную в
наш процесс — ровно тот сценарий, из-за которого КАРМАН закрыл одностороннюю выдачу
через два часа после её появления. Всё, что вне списка, — в лог как «предложено,
не принято», по именам, не по значениям (значений у pending-выдачи мы и не видим).

Список принимаемых имён — здесь, явным перечислением, а не «всё с суффиксом
``_INGEST_KEY``»: allowlist bootstrap'а (``modules/secrets_bootstrap``) отвечает на
вопрос «что из комнаты пускать в env», этот — «кому позволено класть имя в нашу
комнату». Второй вопрос строже: суффикс совпадает и у сайта, которого мы не знаем.

Шаг конвейера/деплоя: ``scripts/accept_secret_grants.py`` (под env приложения).
Идемпотентен — повторный прогон при нуле pending ничего не делает.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from modules.secrets_bootstrap import VAULT_URL

log = logging.getLogger(__name__)

# Имена, которые мы согласны принять в свою комнату, и от кого. Пустой набор
# источников = от любой комнаты; иначе — только от перечисленных slug'ов.
# Первый — ключ приёмника Казанской (mandate brain 2026-09-02/03, D-015).
#
# ``VMALMYZHE_PUBLISH_KEY`` — ключ ПУБЛИКАЦИИ портала (D-091, mandate brain
# 2026-09-14). Он не заменяет ``VMALMYZHE_INGEST_KEY``, а ходит рядом с ним:
# ingest-ключ открывает приёмник, publish-ключ поднимает черновик до
# публикации. Два имени, потому что права разные и отзываются порознь — портал
# может закрыть автопубликацию одним ``DELETE`` на grant, не разрывая доставку
# (решение #124, повторено в D-091).
GRANT_ALLOWLIST: Dict[str, frozenset] = {
    "KAZANSKAYA_INGEST_KEY": frozenset({"kazanskayamalmyzh"}),
    "VMALMYZHE_PUBLISH_KEY": frozenset({"vmalmyzhe"}),
    # Третий приёмник конвейера (mandate brain 2026-09-22, D-093): ключ сайта
    # Сабантуя, выдача sabantuymalmyzh → setka под именем SABANTUY_INGEST_KEY.
    # Ключа публикации Сабантуй не выдавал — доставка только черновиком.
    "SABANTUY_INGEST_KEY": frozenset({"sabantuymalmyzh"}),
    # Четвёртый приёмник (письмо brain 2026-09-30, эшелон зафиксирован
    # единственным): ключ портала «Культура Малмыжского района», выдача
    # dkmalmyzh → setka под именем KULTURA_INGEST_KEY. Ключа публикации нет
    # вовсе — приёмник всегда игнорирует publish с warning, всё черновики.
    "KULTURA_INGEST_KEY": frozenset({"dkmalmyzh"}),
}

_TIMEOUT_SEC = 10


def grants_url(vault_url: Optional[str] = None) -> str:
    """``…/api/secrets`` → ``…/api/secrets/grants`` (тот же хост, что у bootstrap'а).

    Адрес приходит аргументом или из ``SECRETS_VAULT_URL`` — в коде его нет
    намеренно: технический домен в отслеживаемом файле это инфра-деталь, а
    репозиторий публичный (AGENTS.md, «Что не публикуется»). Без адреса падаем
    с внятной ошибкой, а не собираем URL из пустоты.
    """
    base = (vault_url or os.getenv("SECRETS_VAULT_URL") or VAULT_URL).rstrip("/")
    if not base:
        raise RuntimeError(
            "адрес хранилища не задан: передай vault_url или задай SECRETS_VAULT_URL"
        )
    return f"{base}/grants"


def _call(
    method: str,
    url: str,
    token: str,
    *,
    body: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Один HTTP-вызов → ``(status, json)``; сетевой сбой → ``(0, {"error": …})``."""
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


def list_pending(
    token: str, *, vault_url: Optional[str] = None, call=_call
) -> List[Dict[str, Any]]:
    """Входящие предложения выдачи (``received`` со статусом pending). Ошибка → raise."""
    url = grants_url(vault_url) + "?" + urllib.parse.urlencode({"pending": "1"})
    status, payload = call("GET", url, token)
    if status != 200:
        raise RuntimeError(f"grants: GET pending → {status} {payload.get('error', '')}".strip())
    received = payload.get("received") or []
    if not isinstance(received, list):
        raise RuntimeError("grants: received is not a list")
    return [g for g in received if isinstance(g, dict)]


def _grant_name(grant: Dict[str, Any]) -> str:
    """Имя, под которым секрет появится у нас (``aliasKey`` в ответе КАРМАНа).

    Запасные поля — на случай, если контракт переименует ключи: имя у получателя
    всегда alias, и только его сверяем с allowlist; ``sourceKey`` — имя в чужой
    комнате, для нас это не идентичность.
    """
    for field in ("aliasKey", "alias", "key", "name"):
        v = grant.get(field)
        if v:
            return str(v).strip()
    return ""


def _grant_source(grant: Dict[str, Any]) -> str:
    """Slug комнаты-источника (``sourceSlug`` в ответе КАРМАНа)."""
    for field in ("sourceSlug", "source_slug", "from_slug", "source", "from"):
        v = grant.get(field)
        if v:
            return str(v).strip().lower()
    return ""


def decide(grant: Dict[str, Any], allowlist: Optional[Dict[str, frozenset]] = None) -> str:
    """``accept`` / ``skip_name`` / ``skip_source`` — чистое решение по одной выдаче."""
    rules = allowlist if allowlist is not None else GRANT_ALLOWLIST
    name = _grant_name(grant)
    if name not in rules:
        return "skip_name"
    sources = rules[name]
    if sources and _grant_source(grant) not in sources:
        return "skip_source"
    return "accept"


def accept_pending(
    token: str,
    *,
    vault_url: Optional[str] = None,
    allowlist: Optional[Dict[str, frozenset]] = None,
    dry_run: bool = False,
    call=_call,
) -> Dict[str, Any]:
    """Принять входящие выдачи по allowlist. Возвращает сводку без значений секретов.

    ``{"accepted": [names], "skipped": [{name, source, reason}], "failed": [...],
    "pending": N}``. Не бросает на отказе одной выдачи: остальные обрабатываются,
    итог — по строкам сводки.
    """
    pending = list_pending(token, vault_url=vault_url, call=call)
    out: Dict[str, Any] = {"pending": len(pending), "accepted": [], "skipped": [], "failed": []}
    for grant in pending:
        name, source = _grant_name(grant), _grant_source(grant)
        state = str(grant.get("state") or "pending").lower()
        if state != "pending":
            # ``?pending=1`` уже фильтрует, но принимать отозванное или принятое
            # повторно — 409 у КАРМАНа и шум у нас; отсекаем до POST.
            continue
        verdict = decide(grant, allowlist)
        if verdict != "accept":
            out["skipped"].append({"name": name, "source": source, "reason": verdict})
            log.warning(
                "grants: предложено, не принято — %s от %s (%s)", name, source or "?", verdict
            )
            continue
        gid = grant.get("id")
        if dry_run:
            out["accepted"].append(name)
            continue
        status, payload = call("POST", f"{grants_url(vault_url)}/{gid}/accept", token)
        if status == 200 and payload.get("ok"):
            out["accepted"].append(name)
            log.warning("grants: принята выдача %s от %s (id %s)", name, source, gid)
        else:
            out["failed"].append(
                {
                    "name": name,
                    "source": source,
                    "id": gid,
                    "status": status,
                    "error": payload.get("error", ""),
                }
            )
            log.error(
                "grants: не удалось принять %s (id %s): %s %s",
                name,
                gid,
                status,
                payload.get("error", ""),
            )
    return out


def format_summary(summary: Dict[str, Any]) -> str:
    lines = [f"pending: {summary.get('pending', 0)}"]
    for n in summary.get("accepted") or []:
        lines.append(f"accepted: {n}")
    for s in summary.get("skipped") or []:
        lines.append(f"skipped ({s['reason']}): {s['name']} от {s['source'] or '?'}")
    for f in summary.get("failed") or []:
        lines.append(f"FAILED: {f['name']} id={f['id']} → {f['status']} {f['error']}")
    return "\n".join(lines)


__all__: Sequence[str] = (
    "GRANT_ALLOWLIST",
    "accept_pending",
    "decide",
    "format_summary",
    "grants_url",
    "list_pending",
)
