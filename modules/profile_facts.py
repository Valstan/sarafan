"""Клиент запроса факта о жителе у сервиса-владельца (пилот #386, шаг 3).

**Зачем эта штука.** План пилота («единый профиль жителя») требует, чтобы
ассистент Сарафана **спрашивал сервис-владельца по конкретному вопросу** и
получал ответ на этот вопрос. Правило, из которого это следует, жёсткое:
**факты не копируются**. В Сарафане нет таблицы фактов и нет зеркала профиля
Казанской — два сервиса не хранят две копии одного факта, иначе «спросили один
раз» рассыплется на второй копии.

Поэтому здесь нет ни хранилища, ни кэша, ни ретрая с записью: есть **один
запрос — один ответ**, который живёт ровно дольше запроса. Факт приходит,
используется в ответе ассистента и уходит вместе с ним.

**Формат запроса — это тот самый «единственный содержательный вопрос шага 3»**
(P225): как именно Сарафан спрашивает факт и как получает ответ. Договорённость
зафиксирована здесь и должна быть реализована на стороне Казанской:

.. code-block:: text

    POST {base}/api/profile/facts/query
    X-API-Key: <ключ приёмника Казанской>
    Content-Type: application/json

    {"sub": "<esa_sub жителя>", "key": "<ключ факта>", "question": "<вопрос жителя>"}

    200 {"key": "benefit_category", "value": "veteran",
         "source": "self", "verified_at": null, "found": true}
    200 {"key": "benefit_category", "found": false}
    401 — ключ не тот
    404 — эндпоинта ещё нет (Казанская не реализовала чтение)

**Почему ``sub``, а не «профиль целиком».** Житель — не вещь, которую можно
передать ссылкой. Запрашивается **один конкретный факт по одному конкретному
вопросу**; получить «профиль целиком» нельзя, и это осознанное ограничение
(план, «Главное правило: факты не копируются»).

**Ключ.** Используется ``KAZANSKAYA_INGEST_KEY`` — тот же grant, что открывает
приёмник конвейера (выдан kazanskayamalmyzh → setka, принят allowlist'ом в
``modules/secrets_grants.py``). Отдельное имя вроде ``KAZANSKAYA_FACTS_KEY``
allowlist не примет: список принимаемых имён — явный, а не «по суффиксу»
(там же, в шапке модуля). Право «прочитать факт» шире права «доставить пост»,
и правильным ходом было бы развести их (урок #124: разные права отзываются
порознь) — но это отдельный grant от КАРМАНА, то есть решение владельца.
Пока берём существующий ключ и не выдаём никому лишнего.

**ПДн в логи не идут.** В журнал попадают **ключ факта и статус**, никогда —
значение. Это прямое требование плана («значения фактов не печатаются в логи и
отчёты — только ключи и статусы»), и оно же — единственная причина, по
которой ``FactOutcome`` не несёт поля ``value`` наружу в лог.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Путь эндпоинта чтения факта у Казанской. Тот же префикс, что и её
#: существующий ``POST /api/profile/facts`` (запись факта), но с ``/query``:
#: чтение и запись — разные права, и смешивать их нельзя.
FACT_QUERY_PATH = "/api/profile/facts/query"

#: Куда ходим. Публичный домен Казанской (он же в ``config/content_conveyor.py``).
DEFAULT_BASE_URL = "https://казанская.вмалмыже.рф"

#: Ключ приёмника Казанской — grant kazanskayamalmyzh → setka.
KEY_ENV = "KAZANSKAYA_INGEST_KEY"

#: Статусы исхода. В лог уходят только они и ключ факта — без значения.
STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_NO_SUBJECT = "no_subject"
STATUS_UNCONFIGURED = "unconfigured"
STATUS_DENIED = "denied"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"

#: Ключи фактов, которые мы вправе спрашивать. Закрытый список: словарь
#: принадлежит Казанской (``web/src/lib/facts.ts``), и новый факт = новая
#: строка здесь + миграция словаря у неё.
FACT_KEYS = ("benefit_category", "children_count")

#: Таймаут запроса. Факт нужен для ответа жителю — ждать долго нельзя, но и
#: мгновенный отказ нечестен: лучше «не знаю» с заявкой, чем таймаут.
TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class FactOutcome:
    """Итог запроса факта.

    ``value`` — значение факта. Оно **не пишется в лог** и не сохраняется:
    единственный его читатель — сборщик ответа ассистента.
    """

    status: str
    key: str = ""
    value: str = ""
    source: str = ""
    verified_at: Optional[str] = None

    @property
    def found(self) -> bool:
        """Есть ли пригодный факт. ``False`` ⇒ ответ «не знаю» + заявка."""
        return self.status == STATUS_OK and bool(self.value)

    @property
    def verified(self) -> bool:
        """Подтверждён ли факт источником. Сам житель факт **не** подтверждает."""
        return bool(self.verified_at) and bool(self.source) and self.source != "self"


def facts_enabled() -> bool:
    """Включён ли запрос фактов (env ``KAZANSKAYA_FACTS_ENABLED``).

    Дефолт — включён: клиент сам решает, можно ли идти (нет ключа/адреса ⇒
    «не настроено»), и выключать его вручную незачем. Выключение оставлено на
    случай, если владелец захочет, чтобы Сарафан вообще не ходил к Казанской.
    """
    raw = (os.getenv("KAZANSKAYA_FACTS_ENABLED", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _base_url() -> str:
    return (os.getenv("KAZANSKAYA_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def _key() -> str:
    return (os.getenv(KEY_ENV) or "").strip()


def _post(url: str, key: str, body: Dict[str, Any]) -> tuple:
    """Один HTTP-вызов. Возвращает ``(status_code, разобранный ответ)``.

    Сетевой сбой и таймаут — это ``(0, {})``: вызывающий превращает это в
    «не знаю» + заявку, а не в ошибку жителю.
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-Key": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = {}
            return int(resp.status), parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as e:
        return int(e.code), {}
    except Exception:  # сеть/таймаут/DNS — не «ошибка жителю», а «не знаю»
        return 0, {}


def fetch_fact(*, sub: str, key: str, question: str) -> FactOutcome:
    """Спросить один конкретный факт у сервиса-владельца.

    Возвращает ``FactOutcome``. **Значение факта в журнал не пишется** — только
    ключ и статус.
    """
    if key not in FACT_KEYS:
        # Неизвестный ключ — это наша ошибка в вызывающем коде, а не «нет факта».
        logger.warning("profile facts: неизвестный ключ факта: %s", key)
        return FactOutcome(status=STATUS_ERROR, key=key)

    subject = (sub or "").strip()
    if not subject:
        # Без идентификатора жителя спрашивать некого. Молчание здесь ввело бы
        # в заблуждение: «нет факта» и «некого спрашивать» — разные вещи.
        return FactOutcome(status=STATUS_NO_SUBJECT, key=key)

    api_key = _key()
    if not api_key:
        logger.info("profile facts: запрос факта пропущен — ключ приёмника не задан")
        return FactOutcome(status=STATUS_UNCONFIGURED, key=key)

    url = f"{_base_url()}{FACT_QUERY_PATH}"
    status, payload = _post(
        url,
        api_key,
        {"sub": subject, "key": key, "question": (question or "").strip()},
    )

    if status == 0:
        logger.warning("profile facts: сервис-владелец недоступен, ключ факта %s", key)
        return FactOutcome(status=STATUS_UNAVAILABLE, key=key)
    if status == 401:
        logger.warning("profile facts: сервис-владелец отклонил ключ, ключ факта %s", key)
        return FactOutcome(status=STATUS_DENIED, key=key)
    if status == 404:
        logger.info("profile facts: эндпоинт чтения факта ещё не реализован")
        return FactOutcome(status=STATUS_UNAVAILABLE, key=key)
    if status != 200:
        logger.warning("profile facts: неожиданный ответ сервиса-владельца, ключ факта %s", key)
        return FactOutcome(status=STATUS_ERROR, key=key)

    if not payload.get("found"):
        return FactOutcome(status=STATUS_NOT_FOUND, key=key)

    value = str(payload.get("value") or "").strip()
    if not value:
        return FactOutcome(status=STATUS_NOT_FOUND, key=key)

    return FactOutcome(
        status=STATUS_OK,
        key=str(payload.get("key") or key),
        value=value,
        source=str(payload.get("source") or ""),
        verified_at=payload.get("verified_at") or None,
    )


__all__ = (
    "DEFAULT_BASE_URL",
    "FACT_KEYS",
    "FACT_QUERY_PATH",
    "KEY_ENV",
    "STATUS_DENIED",
    "STATUS_ERROR",
    "STATUS_NOT_FOUND",
    "STATUS_NO_SUBJECT",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "STATUS_UNCONFIGURED",
    "FactOutcome",
    "facts_enabled",
    "fetch_fact",
)
