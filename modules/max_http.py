"""HTTP-клиент Bot API мессенджера MAX — единая дверь для всех отправок.

**Что это.** MAX (мессенджер VK) — канал доставки уведомлений владельцу:
бот «САРАФАН» живёт в MAX, туда приходят карточки новых рекламных заявок, и
оттуда же владелец отвечает автору в один тап.

**Почему отдельная дверь, а не «просто requests».** Три причины, каждая
закрывает свой класс отказа:

1. **Авторизация.** Токен передаётся заголовком ``Authorization`` — query-параметры
   платформа уже не поддерживает, а токен в URL утекает в лог (урок G239 из
   мозга, ровно тот же, что мы уже сделали для Telegram-релея).
2. **Base URL переписывается один раз.** ``MAX_API_BASE`` позволяет увести вызовы
   на прокси или другую площадку, не трогая вызывающий код.
3. **Повтор только на сетевой отказ.** HTTP-ответ (4xx/5xx, 429) не повторяем:
   он доказывает, что соединение было, а повтор ``sendMessage`` задвоил бы
   сообщение. То же правило, что в ``modules/telegram_http.py``.

**Лимит.** Платформа разрешает не больше двух сообщений в секунду в один диалог
(``MAX_MESSAGES_PER_SECOND``). Троттлер — в процессе: этого хватает, потому
что за раз уходит единицы карточек, а не тысячи.

**Чего тут нет и почему.** Нет long polling и нет вебхуков: приём событий
живёт в ``web/api/max_webhook.py`` (входящий HTTP — снаружи), а чтение
истории — отдельно и не нужно для уведомлений.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

#: Официальный домен Bot API MAX. Старый ``platform-api.max.ru`` платформа
#: больше не рекомендует; оставляем его только как осознанный оверрайд.
DEFAULT_API_BASE = "https://platform-api2.max.ru"

#: Не больше двух сообщений в секунду в один диалог (документация MAX).
MAX_MESSAGES_PER_SECOND = 2

#: Таймаут одной попытки. Отправка карточки с фото дольше обычного JSON-поста.
DEFAULT_TIMEOUT = 30.0

#: Сколько раз повторяем сетевой отказ.
ATTEMPTS = 3


class MaxError(Exception):
    """Ответ MAX с кодом ошибки.

    ``code`` — код из тела ответа (MAX отдаёт его в ``error_code``),
    ``description`` — человекочитаемый текст. Отличать «сеть не ответила» от
    «MAX сказал 403» обязательно: первое лечится повтором, второе — нет.
    """

    def __init__(self, code: int, description: str, *, status_code: int = 0):
        super().__init__(f"MAX {code}: {description}")
        self.code = code
        self.description = description
        self.status_code = status_code


def api_base() -> str:
    """База Bot API (``MAX_API_BASE``), без слеша на конце."""
    return (os.getenv("MAX_API_BASE") or DEFAULT_API_BASE).strip().rstrip("/")


def token() -> str:
    """Токен бота (``MAX_TOKEN``). Пусто — MAX не настроен, отправки выключены."""
    return (os.getenv("MAX_TOKEN") or "").strip()


#: Ключ Redis, где лежит выученный chat_id. Chat_id узнаётся сам: когда
#: владелец нажимает «Старт» в диалоге с ботом, MAX присылает событие
#: ``bot_started`` с идентификатором чата. Копировать число руками не надо.
_CHAT_ID_KEY = "setka:notifications:max_chat_id"


def _redis() -> Any:
    """Клиент Redis (db=1, как у хранилища уведомлений). ``None`` — если он недоступен."""
    try:
        from modules.notifications.storage import NotificationsStorage

        return NotificationsStorage().redis_client
    except Exception:  # pragma: no cover - инфраструктурный сбой
        logger.warning("MAX: redis недоступен — chat_id придётся задать вручную")
        return None


def remember_chat_id(chat_id: Any) -> bool:
    """Запомнить chat_id, полученный от MAX. ``True`` — записали."""
    if chat_id in (None, ""):
        return False
    client = _redis()
    if client is None:
        return False
    try:
        client.set(_CHAT_ID_KEY, str(chat_id))
        return True
    except Exception as e:  # noqa: BLE001 - чат не критичен для работы вебхука
        logger.warning("MAX: не удалось сохранить chat_id: %s", e)
        return False


def learned_chat_id() -> str:
    """Chat_id, выученный из событий MAX (пусто — если событий не было)."""
    client = _redis()
    if client is None:
        return ""
    try:
        return str(client.get(_CHAT_ID_KEY) or "").strip()
    except Exception:  # pragma: no cover - инфраструктурный сбой
        return ""


def chat_id() -> str:
    """Куда слать уведомления: явный env важнее выученного.

    Env задаётся руками, выученный приходит от самого MAX (событие
    ``bot_started``). Пока env не задан — берём выученный.
    """
    return (os.getenv("MAX_CHAT_ID") or "").strip() or learned_chat_id()


def enabled() -> bool:
    """Настроен ли канал MAX.

    Включён, если задан токен и известен чат-получатель (явный ``MAX_CHAT_ID``
    или выученный из событий MAX): без чата отправлять некуда, а «молча
    отправленное в никуда» — худший вид доставки.
    """
    return bool(token() and chat_id())


# ── Троттлер: не больше N сообщений в секунду на один диалог ────────────────
_throttle_lock = threading.Lock()
_last_send_at: Dict[str, float] = {}


def _throttle(chat_key: str) -> None:
    """Подождать, если этот диалог только что получил сообщение.

    Счётчик — в процессе: воркер на несколько процессов, но карточек за раз
    единицы, и лимит платформы (2/с) держится с большим запасом.
    """
    interval = 1.0 / max(1, MAX_MESSAGES_PER_SECOND)
    with _throttle_lock:
        last = _last_send_at.get(chat_key, 0.0)
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _last_send_at[chat_key] = time.monotonic()


def _headers() -> Dict[str, str]:
    """Заголовки авторизации. Токен — только в заголовке, никогда в URL."""
    return {
        "Authorization": token(),
        "Content-Type": "application/json; charset=utf-8",
    }


def _raise_for_status(resp: Any) -> None:
    """Превратить HTTP-ответ MAX в ``MaxError`` (или ``None``, если всё хорошо)."""
    if resp.status_code == 200:
        return
    code = 0
    description = ""
    try:
        payload = resp.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        code = int(payload.get("error_code") or 0)
        description = str(payload.get("description") or payload.get("message") or "")
    if not description:
        description = (resp.text or "")[:300]
    raise MaxError(code, description, status_code=int(resp.status_code))


def request(
    method: str,
    path: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = ATTEMPTS,
    throttle_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Один вызов Bot API MAX с повтором только на сетевой отказ.

    Возвращает разобранный JSON. Бросает ``MaxError`` на HTTP-ответ с ошибкой и
    ``requests.RequestException`` — если сеть не ответила даже после попыток.
    """
    if not token():
        raise MaxError(0, "MAX не настроен: нет MAX_TOKEN")

    url = f"{api_base()}{path}"
    last_exc: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        if throttle_key:
            _throttle(throttle_key)
        try:
            resp = requests.request(
                method,
                url,
                json=json,
                params=params,
                headers=_headers(),
                timeout=timeout,
            )
        except requests.exceptions.RequestException as e:
            # Сеть не ответила: соединения не было — повтор безопасен.
            last_exc = e
            logger.warning(
                "MAX %s %s: сеть не ответила (попытка %d): %s", method, path, attempt + 1, e
            )
            continue
        _raise_for_status(resp)
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {"raw": payload}

    assert last_exc is not None
    raise last_exc


def post(
    path: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = ATTEMPTS,
    throttle_key: Optional[str] = None,
) -> Dict[str, Any]:
    """POST в Bot API MAX."""
    return request(
        "POST",
        path,
        json=json,
        params=params,
        timeout=timeout,
        attempts=attempts,
        throttle_key=throttle_key,
    )


def get(
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = ATTEMPTS,
) -> Dict[str, Any]:
    """GET в Bot API MAX."""
    return request("GET", path, params=params, timeout=timeout, attempts=attempts)


def send_message(
    chat_id: Any,
    text: str,
    *,
    attachments: Optional[list] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Отправить сообщение в чат.

    ``attachments`` — список вложений MAX. Клавиатура в MAX приходит
    **вложением**, а не отдельным полем как в Telegram: ``{"type":
    "inline_keyboard", "payload": {"buttons": [[...]]}}``.
    """
    body: Dict[str, Any] = {"text": text}
    if attachments:
        body["attachments"] = attachments
    return post(
        "/messages",
        json=body,
        params={"chat_id": chat_id},
        timeout=timeout,
        throttle_key=str(chat_id),
    )


def answer_callback(callback_id: str, text: Optional[str] = None) -> Dict[str, Any]:
    """Подтвердить нажатие кнопки (``callback``).

    Тело — обёрнуто в ``message`` (схема ``CallbackAnswer``), а не плоский
    объект. Без подтверждения MAX показывает пользователю «часик», и нажатие
    выглядит сломанным, даже если действие выполнилось.
    """
    body: Dict[str, Any] = {}
    if text:
        body["message"] = {"text": text}
    return post("/answers", json=body, params={"callback_id": callback_id})


__all__ = (
    "ATTEMPTS",
    "DEFAULT_API_BASE",
    "DEFAULT_TIMEOUT",
    "MAX_MESSAGES_PER_SECOND",
    "MaxError",
    "answer_callback",
    "api_base",
    "chat_id",
    "enabled",
    "get",
    "learned_chat_id",
    "post",
    "remember_chat_id",
    "request",
    "send_message",
    "token",
)
