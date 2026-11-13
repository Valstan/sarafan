"""Машинный доступ к пулу аккаунтов MiniMax в Кармане (13.11).

**Что это.** Пул аккаунтов MiniMax и журнал активаций живут в Кармане (85 аккаунтов,
``accounts_usage`` с уникальным индексом ``UNIQUE (account_id, used_on)``, недельная
квота 7 активаций, окно простоя 5 часов). По решению владельца (13.11) **подбор
делает Карман**: он выбирает аккаунт по своим правилам и говорит, сколько очков
у него активируется. Сарафан — исполнитель: забирает очки чекином, работает на
аккаунте и отчитывается.

**Контракт Кармана** (ручки живые на проде; авторизация — токен комнаты **с правом
записи**, ``Bearer skm_…``; проект и дата определяются сервером по токену и
московскому дню — клиент их не присылает):

.. code-block:: text

    GET {base}/api/minimax/accounts/available
         → 200 {"account": {"id": 67, "title": "…", "email": "…", "password": "…",
                            "activation_index": 1, "points_next": 400, "streak": 0,
                            "next_bonus_in_days": 4, "used_on": null,
                            "quota_exhausted": false},
                "reason": null, "tier": "points_400", "free": 66, "soon": 0,
                "project": "SARAFAN", "today": "2026-11-13"}
         → 200 с account=null и reason="no_free_accounts" — честная пустота
           (не 404 и не 500: «сегодня аккаунтов нет» и «сервис сломался» — разное);
         → 403 — токен только для чтения; 401 — недействительный токен.

    POST {base}/api/minimax/activations
         {"account_id": 67, "points": 400, "activation_index": 1}
         → 200 {"account_id": 67, "used_on": "2026-11-13", "activation_index": 1,
                "points": 400, "points_expected": 400, "mismatch": false,
                "source": "client_index", "updated": false, "project": "SARAFAN"}
         → 409 — аккаунт сегодня уже записан за ДРУГИМ проектом или занят окном
           в 5 часов (бери другой).

**Про `password` в ответе.** Карман отдаёт учётные данные (ему они нужны, чтобы
владелец входил в браузере). **Мы их не используем и не храним:** у MiniMax нет
программного логина (проверено 13.11: ``grant_type=password`` в
``account.minimax.io/oauth2/token`` → HTTP 400), вход только device-flow в браузере
владельца. Поэтому аккаунт у нас — это локальный профиль CLI, а сопоставление с
выданным идёт **по почте**.

**Адрес и токен** — из окружения (в коде их нет: адрес хранилища это инфра-деталь,
репозиторий публичный). По умолчанию база выводится из ``SECRETS_VAULT_URL``
(``{хост}/api/secrets`` → ``{хост}/api/minimax``), токен — ``SECRETS_TOKEN``
(пишущий токен комнаты). Переопределяется ``KARMAN_MINIMAX_API_URL`` и
``KARMAN_MINIMAX_TOKEN``.

**Пока ручек не настроено** — клиент выключен (нет адреса или токена), и движок
работает своим пулом и своей ротацией.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Базовый адрес ручек Кармана (полный, до ``/api/minimax`` включительно).
ENV_API_URL = "KARMAN_MINIMAX_API_URL"
#: Токен комнаты с правом записи. По умолчанию — общий токен комнаты setka.
ENV_TOKEN = "KARMAN_MINIMAX_TOKEN"
#: Хвост URL хранилища секретов: по нему восстанавливаем корень приложения.
SECRETS_URL_SUFFIX = "/api/secrets"

_TIMEOUT_SEC = 20
#: Москва — UTC+3 без перехода на летнее время (с 2014 года), поэтому фиксируем
#: смещение, а не IANA-зону: на боксе зона в tzdata может не быть.
MOSCOW_OFFSET = timedelta(hours=3)


def moscow_today(now: Optional[datetime] = None) -> str:
    """Московская дата 'YYYY-MM-DD' — как у Кармана (его ``used_on``)."""
    moment = now or datetime.now(timezone.utc)
    return (moment.astimezone(timezone(MOSCOW_OFFSET))).date().isoformat()


def api_base() -> str:
    """База ручек пула (пустая строка — «адрес не задан»)."""
    explicit = (os.getenv(ENV_API_URL) or "").strip().rstrip("/")
    if explicit:
        return explicit
    vault = (os.getenv("SECRETS_VAULT_URL") or "").strip().rstrip("/")
    if not vault:
        return ""
    if vault.endswith(SECRETS_URL_SUFFIX):
        return "%s/api/minimax" % vault[: -len(SECRETS_URL_SUFFIX)]
    return "%s/minimax" % vault


def token() -> str:
    """Токен комнаты для ручек пула (пустая строка — «токен не задан»)."""
    for name in (ENV_TOKEN, "SECRETS_TOKEN"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def enabled() -> bool:
    """Ручки пула доступны? Пока нет — Сарафан работает своим пулом."""
    return bool(api_base() and token())


@dataclass
class PoolOffer:
    """Ответ Кармана «дай аккаунт»."""

    account: Optional[Dict[str, Any]] = None
    reason: str = ""
    tier: str = ""
    free: int = 0
    soon: int = 0
    project: str = ""
    today: str = ""

    @property
    def empty(self) -> bool:
        """Сегодня брать нечего — честная пустота, а не поломка."""
        return self.account is None


def _call(
    method: str,
    url: str,
    token_value: str,
    *,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Один HTTP-вызов → ``(status, json)``; сеть/таймаут → ``(0, {...})``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + token_value, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return int(resp.status), (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover — тело ошибки может не читаться
            pass
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {"error": raw[:300]}
        return int(exc.code), parsed
    except Exception as exc:  # noqa: BLE001 — сеть/DNS/таймаут
        return 0, {"error": ("%s: %s" % (type(exc).__name__, exc))[:300]}


def take_account(*, call=_call) -> PoolOffer:
    """Попросить у Кармана следующий аккаунт (его выбор, его правила).

    Пустой пул — это **не** ошибка: Карман отвечает 200 с ``account: null`` и
    ``reason: no_free_accounts``. Ошибка связи или отказ — ``RuntimeError``:
    молча уходить на свой пул нельзя, это вернуло бы две правды об одном пуле.
    """
    base = api_base()
    token_value = token()
    if not base or not token_value:
        raise RuntimeError("ручки пула не настроены (адрес или токен не задан)")
    status, payload = call("GET", "%s/accounts/available" % base, token_value)
    if status != 200:
        raise RuntimeError(
            "Карман не ответил: %s %s" % (status, str(payload.get("error") or "")[:200])
        )
    account = payload.get("account")
    return PoolOffer(
        account=account if isinstance(account, dict) else None,
        reason=str(payload.get("reason") or ""),
        tier=str(payload.get("tier") or ""),
        free=int(payload.get("free") or 0),
        soon=int(payload.get("soon") or 0),
        project=str(payload.get("project") or ""),
        today=str(payload.get("today") or ""),
    )


def report_activation(
    account_id: Any,
    *,
    points: Optional[int] = None,
    activation_index: Optional[int] = None,
    call=_call,
) -> Dict[str, Any]:
    """Отметить активацию в журнале Кармана.

    ``activation_index`` — клетка, которую **мы увидели на панели** MiniMax: у
    Кармана это истина (номер, названный проектом), а расхождение с их
    предсказанием помечается флагом ``mismatch`` — их правило клетки от чужого
    показания не меняется.

    ``RuntimeError`` при 409 — аккаунт сегодня записан за другим проектом или
    занят окном в 5 часов: бери другой.
    """
    base = api_base()
    token_value = token()
    if not base or not token_value:
        raise RuntimeError("ручки пула не настроены (адрес или токен не задан)")
    body: Dict[str, Any] = {"account_id": int(account_id)}
    if points is not None:
        body["points"] = int(points)
    if activation_index is not None:
        body["activation_index"] = int(activation_index)
    status, payload = call("POST", "%s/activations" % base, token_value, body=body)
    if status == 409:
        raise RuntimeError(str(payload.get("error") or "аккаунт занят"))
    if status != 200:
        raise RuntimeError(
            "Карман не записал активацию: %s %s" % (status, str(payload.get("error") or "")[:200])
        )
    return payload


def account_identity(account: Optional[Dict[str, Any]]) -> str:
    """Человекочитаемое имя аккаунта для логов (без токенов и паролей)."""
    if not isinstance(account, dict):
        return "-"
    title = str(account.get("title") or "").strip()
    email = str(account.get("email") or "").strip()
    ident = str(account.get("id") or "?").strip()
    if title and email:
        return "%s (%s)" % (title, email)
    return title or email or ident


__all__: Sequence[str] = (
    "ENV_API_URL",
    "ENV_TOKEN",
    "PoolOffer",
    "account_identity",
    "api_base",
    "enabled",
    "moscow_today",
    "report_activation",
    "take_account",
    "token",
)
