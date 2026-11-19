"""HTTP-дверь ассистента единого профиля жителя (``/api/service-assistant``).

Пилот единого профиля жителя (#386), **шаг 3**. Слой условий услуг (шаг 1) уже
отдаёт каталог; ассистент отвечает на вопрос жителя «что мне положено» —
по условиям услуги и, когда нужно, по факту, который назвал сам житель.

**Дверь публичная по пути, но не по данным.** Путь открыт без сессии (его
зовут серверы соседних проектов, у которых cookie нет), и собственная защита
у двери — как у ``/api/gateway/``:

- **без ``esa_sub``** — ответ по каталогу услуг. Это те же данные, что и
  публичный ``/api/service-conditions``: условия, документы, владелец, ссылка
  на публичную страницу. Ничего приватного.
- **с ``esa_sub``** — ответ может содержать **факт о жителе**, и тогда
  обязателен ключ ``X-API-Key`` сервиса-владельца (Казанской). Без ключа —
  401. Это не паранойя: факт о человеке не должен достаться тому, кто назвал
  чужой идентификатор.

**Почему ``POST``, а не ``GET``.** В теле — вопрос жителя и, возможно, его
идентификатор. В URL это попало бы в логи веб-сервера и в историю браузера,
то есть ПДн уехали бы в то, где им не место. Тело — единственное место, где
можно передать вопрос, не оставив его в адресной строке.

**Значения фактов в лог не попадают.** В журнал — ключ факта и статус
(см. ``modules/profile_facts.py``).
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from modules.profile_facts import KEY_ENV
from modules.service_assistant import ask

logger = logging.getLogger(__name__)
router = APIRouter()


class AssistantRequest(BaseModel):
    """Тело запроса ассистента.

    ``esa_sub`` — идентификатор жителя в ЕСА (``sub`` из id_token). Нужен
    только для фактов; без него ассистент отвечает по каталогу.
    """

    question: str = Field(..., min_length=1, max_length=500, description="Вопрос жителя")
    esa_sub: Optional[str] = Field(
        default=None, max_length=200, description="Идентификатор жителя (нужен для фактов)"
    )
    service: Optional[str] = Field(
        default=None, max_length=200, description="Услуга из каталога; пусто — ищем сами"
    )
    request_id: Optional[str] = Field(
        default=None, max_length=100, description="Идентификатор запроса для сопоставления"
    )


def _facts_key() -> str:
    return (os.getenv(KEY_ENV) or "").strip()


def _require_facts_key(x_api_key: Optional[str]) -> None:
    """Проверить ключ сервиса-владельца (constant-time).

    Ключ не задан в env → 503 (фича не настроена), а не 401: «нет ключа» и
    «не тот ключ» — разные вещи, и притворяться, что кто-то подсунул
    неправильный ключ, когда его просто не выдали, нечестно.
    """
    expected = _facts_key()
    if not expected:
        raise HTTPException(status_code=503, detail="запрос фактов не настроен")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-API-Key")


@router.post("/service-assistant")
async def service_assistant(
    payload: AssistantRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """Ответ ассистента по условиям услуги и фактам жителя.

    Форма ответа — контракт плана пилота: ``answer`` (утверждения с
    обязательной цитатой на источник), ``missing`` (что ещё спросить),
    ``owner`` (кто отвечает) и ``request_id``.

    Пустой ``answer`` — законный ответ: значит, данных нет, и ассистент
    говорит об этом прямо, а не выдумывает (правило 3 контракта).
    """
    if payload.esa_sub:
        _require_facts_key(x_api_key)

    return ask(
        payload.question,
        esa_sub=payload.esa_sub,
        service=payload.service,
        request_id=payload.request_id,
    )
