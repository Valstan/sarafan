"""HTTP к Bot API Telegram с повторами на сетевой отказ — одна дверь для всех отправок.

Замер Матрицы 04.09 с трёх точек (G307, D-076): ``api.telegram.org`` с боксов
хостера **не заблокирован, а теряет около половины SYN** ко всем DC Telegram
(13/20 с нашего бокса, контроль ``1.1.1.1`` — 20/20); установленное соединение
живёт. Наши отправки шли одним ``requests.post(..., timeout=15)`` без повторов —
каждый второй запрос молча падал ``ConnectTimeout``'ом, и никто их не считал.
В логах воркера за 01–03.09 это 45–59 сетевых отказов Telegram в сутки.

Лечение то же, что у Матрицы (#758): **короткий таймаут на попытку и до шести
повторов — только на сетевой отказ**. Ответ HTTP (4xx/5xx, 429) не повторяем: он
доказывает, что соединение установилось, а повтор ``sendMessage`` по таймауту
чтения задвоил бы сообщение. Поэтому ловим ``ConnectionError`` (в него входит и
``ConnectTimeout``), но **не** ``ReadTimeout``.

Все вызовы идут через ``requests.post``/``requests.get``, причём модуль берётся
**в момент вызова** (локальный импорт): тесты проекта патчат ``requests.post``
глобально или подменяют ``sys.modules["requests"]`` целиком, и с этим помощником
оба приёма продолжают работать без правок. Классы исключений — из настоящего
``requests``, привязаны при импорте: подменённый модуль их не обязан иметь.

**База API настраивается (G63 / #041 мозга).** С 06.11.2026 ``api.telegram.org``
с нашего бокса **не отвечает вовсе** (РКН-блок IP Telegram): 391–655 отказов в
сутки, по ~40 с на попытку (6 попыток × 6 с). Лечится relay'ом вне зоны блока
(Cloudflare Worker, ``infra/tg_relay/worker.js``). Поэтому:

* ``TELEGRAM_API_BASE`` — база Bot API; пусто ⇒ прямое соединение (дефолт,
  как было);
* при базе-relay адрес ``https://api.telegram.org/bot<TOKEN>/<METHOD>``
  переписывается в ``<base>/<METHOD>``, а токен уходит **заголовком**
  ``X-TG-Token`` — не в пути (G239: секрет в URL утекает в лог через текст
  исключения), плюс ``X-Relay-Secret`` для гейта relay'а.

Переписывание живёт здесь, в единственной двери: остальные модули зовут
``tg_http.post("https://api.telegram.org/bot<token>/sendMessage", ...)`` и о
relay'е не знают.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Optional, Tuple

import requests as _real_requests

logger = logging.getLogger(__name__)

#: Прямой хост Bot API — дефолт, когда relay не настроен.
DEFAULT_API_BASE = "https://api.telegram.org"

#: ``https://api.telegram.org/bot<TOKEN>/<METHOD>`` — разбираем на части.
_BOT_URL_RE = re.compile(
    r"^https?://api\.telegram\.org/bot(?P<token>[^/?#]+)/(?P<method>[^?#]+)(?P<query>\?.*)?$",
    re.IGNORECASE,
)


def api_base() -> str:
    """База Bot API: ``TELEGRAM_API_BASE`` или прямой хост.

    Пустое значение (или сам прямой хост) ⇒ идём напрямую, как раньше.
    """
    base = (os.getenv("TELEGRAM_API_BASE") or "").strip().rstrip("/")
    return base or DEFAULT_API_BASE


def relay_secret() -> str:
    """Секрет relay'а (``TG_RELAY_SECRET``) — пусто ⇒ нечего слать."""
    return (os.getenv("TG_RELAY_SECRET") or "").strip()


def rewrite_url(url: str, headers: Optional[dict] = None) -> str:
    """Переписать Bot API-URL на базу relay'а (если он настроен).

    Возвращает итоговый URL и **дополняет** ``headers`` (если передан):
    ``X-TG-Token`` (токен бота) и ``X-Relay-Secret`` (если он есть в env).
    Всё, что не похоже на Bot API-URL, уходит без изменений.
    """
    base = api_base()
    if base == DEFAULT_API_BASE:
        return url
    match = _BOT_URL_RE.match(url)
    if not match:
        return url
    token = match.group("token")
    method = match.group("method")
    query = match.group("query") or ""
    if headers is not None:
        headers["X-TG-Token"] = token
        secret = relay_secret()
        if secret:
            headers["X-Relay-Secret"] = secret
    return f"{base}/{method}{query}"


def _requests():
    """Модуль ``requests`` на момент вызова (см. докстринг про тесты)."""
    import requests

    return requests


# Таймаут одной попытки: при потере SYN ждать 15 с бессмысленно — повтор быстрее.
ATTEMPT_TIMEOUT = 6.0
# Шесть попыток при p(потери)≈0.5 дают p(все упали)≈1.5% вместо 50%.
ATTEMPTS = 6
# Пауза между попытками — линейная и короткая: это не rate-limit, а лотерея SYN.
_BACKOFF_STEP = 0.5

# То, что считается «сети нет»: ConnectionError покрывает и ConnectTimeout
# (он наследует от обоих), ReadTimeout сюда намеренно не входит.
NETWORK_ERRORS = (_real_requests.exceptions.ConnectionError,)


def _retrying(
    do: Callable[[float], Any],
    *,
    what: str,
    attempts: int,
    timeout: float,
    sleep: Optional[Callable[[float], None]],
) -> Any:
    tries = max(1, attempts)
    last: Optional[BaseException] = None
    for i in range(tries):
        try:
            return do(timeout)
        except NETWORK_ERRORS as e:
            last = e
            if i + 1 < tries:
                logger.debug("telegram %s: сеть (попытка %d/%d): %s", what, i + 1, tries, e)
                if sleep is not None:
                    sleep(_BACKOFF_STEP * (i + 1))
    assert last is not None
    logger.warning("telegram %s: сеть не ответила за %d попыток: %s", what, tries, last)
    raise last


def _prepare(url: str, kw: dict) -> Tuple[str, dict]:
    """Переписать URL на базу relay'а (если настроена) и добавить заголовки.

    Заголовки, заданные вызывающим, не теряются: relay-заголовки дописываются
    поверх них (у вызывающих своих ``X-TG-Token`` не бывает).
    """
    headers = dict(kw.pop("headers", None) or {})
    return rewrite_url(url, headers), {**kw, "headers": headers}


def post(
    url: str,
    *,
    attempts: int = ATTEMPTS,
    timeout: float = ATTEMPT_TIMEOUT,
    sleep: Optional[Callable[[float], None]] = time.sleep,
    **kw: Any,
) -> Any:
    """``requests.post`` с повторами на сетевой отказ. Исключение — после всех попыток."""
    target, call_kw = _prepare(url, kw)
    return _retrying(
        lambda t: _requests().post(target, timeout=t, **call_kw),
        what="POST",
        attempts=attempts,
        timeout=timeout,
        sleep=sleep,
    )


def get(
    url: str,
    *,
    attempts: int = ATTEMPTS,
    timeout: float = ATTEMPT_TIMEOUT,
    sleep: Optional[Callable[[float], None]] = time.sleep,
    **kw: Any,
) -> Any:
    """``requests.get`` с повторами на сетевой отказ."""
    target, call_kw = _prepare(url, kw)
    return _retrying(
        lambda t: _requests().get(target, timeout=t, **call_kw),
        what="GET",
        attempts=attempts,
        timeout=timeout,
        sleep=sleep,
    )


async def apost(url: str, *, attempts: int = ATTEMPTS, timeout: float = ATTEMPT_TIMEOUT, **kw: Any):
    """Асинхронный POST через httpx с теми же правилами повтора."""
    import asyncio

    import httpx

    target, call_kw = _prepare(url, kw)
    tries = max(1, attempts)
    last: Optional[BaseException] = None
    for i in range(tries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.post(target, **call_kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last = e
            if i + 1 < tries:
                await asyncio.sleep(_BACKOFF_STEP * (i + 1))
    assert last is not None
    logger.warning("telegram POST: сеть не ответила за %d попыток: %s", tries, last)
    raise last


def get_httpx(url: str, *, attempts: int = ATTEMPTS, timeout: float = ATTEMPT_TIMEOUT, **kw: Any):
    """Синхронный GET через httpx (для мест, где уже httpx) — повторы те же."""
    import httpx

    target, call_kw = _prepare(url, kw)
    tries = max(1, attempts)
    last: Optional[BaseException] = None
    for i in range(tries):
        try:
            return httpx.get(target, timeout=timeout, **call_kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last = e
            if i + 1 < tries:
                time.sleep(_BACKOFF_STEP * (i + 1))
    assert last is not None
    logger.warning("telegram GET: сеть не ответила за %d попыток: %s", tries, last)
    raise last
