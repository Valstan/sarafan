"""Общий клиент DeepSeek — один HTTP-вызов на всю экосистему (D-024).

Мандат brain 2026-08-10 (решение владельца D-024): нейро-движок экосистемы —
DeepSeek API напрямую. Всё, что ходило в Groq или в облачную рутину, переводится
сюда тем же паттерном, что конвейер: ключ из комнаты КАРМАНа, прямой вызов.

**Почему отдельный модуль, а не копия вызова в каждом потребителе.** Первый
потребитель (``modules/conveyor/classify.py``) нёс собственный ``_call_api``;
второй и третий скопировали бы его вместе с обработкой ошибок, таймаутом и
разбором тела. Дальше они расходятся молча — ровно тот класс, из-за которого в
проекте сводили VK-токены к единому источнику. Здесь один вызов, один набор
кодов отказа, одно место для правки.

**Чистый HTTP вместо SDK** — тот же выбор, что в конвейере и в доставке: тело
запроса OpenAI-совместимо и умещается в несколько строк, а лишняя зависимость в
рантайме — это то, что придётся обновлять и чинить. Groq-SDK, который отсюда
убран, добавлял к каждому потребителю ветку «а вдруг пакет не установлен».

**Никогда не бросает.** Все потребители — фоновые рутины и UI-хелперы, где
падение вызова не должно ронять вызывающего. Отказ возвращается кодом причины.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from config.deepseek import get_api_key, get_base_url, get_max_tokens, get_model, get_timeout

logger = logging.getLogger(__name__)

# Предохранитель «нет денег на счету».
#
# ``http_402`` (Insufficient Balance) — единственный отказ DeepSeek, который НЕ
# лечится повтором: ни пауза, ни другой промпт, ни уменьшение пачки его не
# снимут, снимет только платёж. При этом он выглядит как обычная ошибка вызова,
# поэтому каждый потребитель честно пробует снова — и платит за это слотом
# воркера. Живой замер 26.10: еженедельный recheck discovery сделал 2448
# вызовов подряд, все 402, и занял 27 минут слота при нулевом результате.
#
# Поэтому первый же 402 открывает предохранитель на уровне клиента: до истечения
# кулдауна ``chat`` отвечает локально, не ходя в сеть. Кулдаун конечный
# намеренно — пополнили счёт, и следующий вызов после окна снова настоящий;
# вечная блокировка потребовала бы рестарта воркера, а это худший вид поломки:
# тихий.
_BALANCE_COOLDOWN_DEFAULT = 1800.0
_balance_blocked_until: float = 0.0


def _balance_cooldown() -> float:
    """Сколько секунд не звонить после ``402`` (env ``DEEPSEEK_BALANCE_COOLDOWN``)."""
    try:
        return max(0.0, float(os.getenv("DEEPSEEK_BALANCE_COOLDOWN", _BALANCE_COOLDOWN_DEFAULT)))
    except ValueError:
        return _BALANCE_COOLDOWN_DEFAULT


def reset_balance_breaker() -> None:
    """Снять предохранитель немедленно (ручная проверка после пополнения, тесты)."""
    global _balance_blocked_until
    _balance_blocked_until = 0.0


def balance_blocked_for() -> float:
    """Сколько секунд ещё действует предохранитель; ``0.0`` — не действует."""
    return max(0.0, _balance_blocked_until - time.monotonic())


def finish_reason(response: Dict[str, Any]) -> str:
    """``finish_reason`` первого варианта ответа; пусто, если провайдер его не вернул.

    ``length`` значит «модель упёрлась в ``max_tokens`` и замолчала на полуслове».
    Это отдельная поломка, а не «пустой» или «неразбираемый» ответ: лечится она
    бюджетом вывода, а не промптом. Слитая с ними в одну причину, она
    неотличима от молчания модели — у соседнего проекта так две недели терялось
    47 % вердиктов (G334, письмо brain 2026-09-10).
    """
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def log_cache_usage(label: str, response: Dict[str, Any]) -> None:
    """Записать долю префикс-кэша одной строкой (мандат brain 2026-08-30, R29).

    DeepSeek кэширует СОВПАВШЕЕ НАЧАЛО запроса и считает его по сниженной цене.
    Включать нечего — кэш работает сам; но работает он только пока префикс
    байт-в-байт стабилен, а сломать эту стабильность может правка, которая
    выглядит безобидно: переставленный ключ словаря, новая строка с датой,
    сортировка без вторичного ключа.

    **Отказ здесь бесшумный.** Ответы модели остаются правильными, вызовы
    проходят, в логах ни одной ошибки — меняется только счёт. Поэтому доля
    кэша логируется на каждом вызове: это единственный сигнал, по которому
    видно, что префикс поехал.

    ``hit=- miss=-`` означает, что провайдер полей НЕ ВЕРНУЛ, и это не то же
    самое, что ``hit=0``. Первое — «померить нечем», второе — «кэш не сработал».
    Сложить их в один ноль значит получить приёмку, которая врёт.

    Та же граница у бюджета вывода (письмо brain 2026-09-10, G334):
    ``finish=length`` — вызов упёрся в ``max_tokens``; ``reasoning=`` — сколько
    из ``completion`` съели раздумья модели. ``reasoning=-`` значит «поля нет»
    (нерассуждающая модель его не присылает), а не «раздумий было ноль».
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return

    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    prompt = usage.get("prompt_tokens")

    if isinstance(hit, int) and isinstance(miss, int) and (hit + miss) > 0:
        share = f"{100.0 * hit / (hit + miss):.1f}"
    else:
        share = "-"

    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None

    logger.info(
        "deepseek-usage label=%s model=%s prompt=%s hit=%s miss=%s hit_pct=%s completion=%s "
        "reasoning=%s finish=%s",
        label,
        response.get("model") or "-",
        prompt if prompt is not None else "-",
        hit if hit is not None else "-",
        miss if miss is not None else "-",
        share,
        usage.get("completion_tokens") if usage.get("completion_tokens") is not None else "-",
        reasoning if reasoning is not None else "-",
        finish_reason(response) or "-",
    )


def call_api(
    body: Dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    timeout: float,
    label: str = "deepseek",
) -> Tuple[int, Dict[str, Any]]:
    """Один вызов ``/chat/completions``. Сетевой сбой → ``(0, {"error": ...})``.

    HTTP-код возвращается сырым, чтобы вызывающий мог различить 401 (ключ) и
    429 (квота) — для рутины это разные решения: первое чинит человек, второе
    проходит само.

    ``label`` попадает в строку учёта префикс-кэша: потребителей несколько, и
    доля кэша у массового конвейера и у разовой кнопки в UI — разные величины,
    которые нельзя складывать в одно среднее.
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                # Учёт не должен ронять уже оплаченный вызов: тело ответа
                # разобрано, деньги потрачены, вердикт вызывающему нужен даже
                # если метрика почему-то не сложилась.
                try:
                    log_cache_usage(label, parsed)
                except Exception:  # pragma: no cover — учёт не важнее ответа
                    logger.debug("deepseek-usage: не удалось записать метрику", exc_info=True)
            return int(resp.status), parsed
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover — тело ошибки не обязано читаться
            pass
        return int(e.code), {"error": detail}
    except Exception as e:
        # Текст исключения может содержать URL; ключ у нас в заголовке, не в
        # пути (в отличие от Telegram Bot API — инцидент 2026-08-12), но обрезаем
        # всё равно: маскирование логов ловит остальное.
        return 0, {"error": f"{type(e).__name__}: {e}"[:300]}


def chat(
    *,
    user: str,
    system: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    json_object: bool = False,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    label: str = "deepseek",
) -> Dict[str, Any]:
    """Один диалоговый вызов. Возвращает ``{ok, content, model, usage}`` либо
    ``{ok: False, reason, detail?}``.

    Коды отказа: ``no_api_key`` | ``empty_prompt`` | ``network`` |
    ``http_<код>`` | ``truncated`` | ``empty_response``. Они попадают в логи и
    в ответы UI, поэтому стабильны — по ним же различаются «ключа нет» (чинит
    владелец), «упёрлись в ``max_tokens``» (чинит бюджет вывода) и «модель
    промолчала» (повторить).

    ``truncated`` проверяется РАНЬШЕ пустоты: обрезанный ответ бывает и
    непустым (оборванный JSON — наш случай), и пустым (раздумья съели весь
    бюджет — случай соседа). Оба — одна поломка. Пустой ответ без ``length``
    остаётся ``empty_response``.

    ``json_object=True`` включает у DeepSeek режим строгого JSON. Он не
    отменяет разбор на стороне вызывающего: режим гарантирует синтаксис, а не
    схему.
    """
    global _balance_blocked_until

    key = api_key if api_key is not None else get_api_key()
    if not key:
        return {"ok": False, "reason": "no_api_key"}
    if not (user or "").strip():
        return {"ok": False, "reason": "empty_prompt"}
    blocked = balance_blocked_for()
    if blocked > 0:
        # Форма отказа та же, что у настоящего 402: потребители уже ветвятся по
        # ``http_402``, и локальный ответ не должен выглядеть новой поломкой.
        return {
            "ok": False,
            "reason": "http_402",
            "detail": f"insufficient_balance (cooldown {int(blocked)}s, no network call)",
        }

    messages: List[Dict[str, str]] = []
    if system and system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    body: Dict[str, Any] = {
        "model": model or get_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens if max_tokens is not None else get_max_tokens(),
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}

    status, response = call_api(
        body,
        api_key=key,
        base_url=get_base_url(),
        timeout=timeout if timeout is not None else get_timeout(),
        label=label,
    )
    if status == 0:
        return {"ok": False, "reason": "network", "detail": response.get("error")}
    if status == 402:
        # Первый 402 стоит одного вызова, остальные — ноль: см. комментарий у
        # ``_balance_blocked_until``.
        cooldown = _balance_cooldown()
        _balance_blocked_until = time.monotonic() + cooldown
        logger.warning(
            "deepseek: 402 Insufficient Balance — вызовы модели приостановлены на %d с "
            "(label=%s)",
            int(cooldown),
            label,
        )
    if not (200 <= status < 300):
        return {"ok": False, "reason": f"http_{status}", "detail": response.get("error")}

    choices = response.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        content = str((choices[0].get("message") or {}).get("content") or "").strip()
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else None
    if finish_reason(response) == "length":
        # Вызов оплачен — usage отдаём, чтобы расход не выпал из учёта.
        return {
            "ok": False,
            "reason": "truncated",
            "detail": f"max_tokens={body['max_tokens']}",
            "usage": usage,
        }
    if not content:
        return {"ok": False, "reason": "empty_response"}

    return {"ok": True, "content": content, "model": body["model"], "usage": usage}
