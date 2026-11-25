"""Ёмкость бокса под вызовы MiniMax Code: сколько CLI бокс выдерживает одновременно.

**Что это.** Вызов MiniMax — это **процесс** ``minimax-code`` (CLI-агент), а не
HTTP-запрос. На боевом боксе он занимает **~315 МБ RSS** (замер 25.11 05:57–06:04 на
тихом боксе: 318,1 МБ при потолке кучи 160 и 314,9 МБ при 384 — разница 3 МБ, шум;
потолок кучи RSS не ограничивает, см. P239). Бокс при этом — **1 ядро, 1536 МБ,
swap = 0**, а база без CLI (postgres 333, celery 423, uvicorn 109, python 174) —
**≈1000 МБ**. Значит запас под CLI — **≈536 МБ**, а два CLI (2 × 315 = 630 МБ)
**не помещаются**: ядро снимает крупнейший процесс, и это CLI.

**Что из этого выросло (прод, 25.11 04:00–07:40).** Ночной срез discovery поднимал
4 CLI разом (столько одновременных проверок сообществ региона) — 1,26 ГБ поверх базы.
Ядро снимало CLI **125 раз за ночь**, движок после каждого убийства **повторял** вызов
(сброс состояния + ещё один CLI на голодном боксе) — и отказ уходил в **платный
Дипсик**: **122 платных вызова** за ночь, ≈1 в минуту. Утренняя волна 07:00–07:40
показала то же самое уже без ночного среза: ядерные убийства в 07:32:53 и 07:37:55,
таймаут 180 с в 07:36:53 и отказ в 07:38:39 — **все в платный Дипсик**, MiniMax с
**0 % успеха**, очередь ``celery`` выросла 15 → 32 → **72**.

**Что делает этот модуль.** Две проверки, обе **до** запуска процесса:

1. **Слот.** Одновременных вызовов CLI на боксе — не больше
   ``MINIMAX_CLI_MAX_CONCURRENT`` (дефолт **1**: два CLI в запас бокса не влезают,
   а один — влезает). Слот берётся в Redis тем же приёмом, что и замок волны
   (``modules/wave_lock.py``): ``SET … NX PX``, снятие — только своего токена.
   Redis недоступен — **fail-open** (вызов идёт как раньше): конвейер не должен
   вставать из-за хранилища, а жёсткую защиту от OOM даёт проверка памяти ниже.
2. **Память.** Если на боксе нет места под ещё один CLI — вызов не запускается вовсе:
   отказ уходит в платный Дипсик **сразу**, а не через 167 с, которые бокс не
   переживёт. Порог — ``MINIMAX_MIN_AVAILABLE_MB`` (дефолт 384 = замеренный RSS
   живого вызова + запас). Читается ``MemAvailable`` из ``/proc/meminfo``; на
   платформе без него проверка не делается вовсе.

**Почему это не «ещё один ограничитель».** Ограничитель конкуренции у discovery
уже был (``DISCOVERY_RECHECK_MAX_CONCURRENT``), но он покрывает только ночной срез
04:00 — а CLI поднимает **любой** поток: волна, сборник, переписывание. Здесь
ограничение общее для движка, поэтому работает везде.

Env:
  MINIMAX_CLI_CAPACITY_ENABLED=1   # 0 — выключить всё (аварийный откат без деплоя)
  MINIMAX_CLI_MAX_CONCURRENT=1     # сколько вызовов CLI одновременно на боксе
  MINIMAX_CLI_SLOT_TTL_SECONDS=300 # потолок жизни слота (вызов не должен пережить его)
  MINIMAX_CLI_SLOT_WAIT_SECONDS=300 # сколько ждать свободный слот, прежде чем отказать
  MINIMAX_MIN_AVAILABLE_MB=384     # минимум свободной памяти для запуска CLI
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ENV_ENABLED = "MINIMAX_CLI_CAPACITY_ENABLED"
ENV_MAX_CONCURRENT = "MINIMAX_CLI_MAX_CONCURRENT"
ENV_SLOT_TTL_SECONDS = "MINIMAX_CLI_SLOT_TTL_SECONDS"
ENV_SLOT_WAIT_SECONDS = "MINIMAX_CLI_SLOT_WAIT_SECONDS"
ENV_MIN_AVAILABLE_MB = "MINIMAX_MIN_AVAILABLE_MB"

DEFAULT_MAX_CONCURRENT = 1
DEFAULT_SLOT_TTL_SECONDS = 300
DEFAULT_SLOT_WAIT_SECONDS = 300
DEFAULT_MIN_AVAILABLE_MB = 384

KEY_PREFIX = "setka:minimax_cli_slot:"

#: Отказ: на боксе нет памяти под вызов CLI.
REASON_NO_MEMORY = "insufficient_memory"
#: Отказ: все слоты вызовов CLI заняты (кто-то уже работает).
REASON_BUSY = "cli_slot_busy"

# Снимаем только свой слот: чужой token значит, что TTL истёк и слот забрал
# следующий вызов — снять его значило бы разрешить два вызова разом.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_client: Any = None


def _redis() -> Any:
    """Клиент Redis на процесс (prefork-ребёнок), ленивый.

    Импорт ``config.runtime`` — **внутри функции**: он требует ``DATABASE_URL`` и
    ``REDIS_URL`` на импорте, а движок MiniMax импортируется скриптами, которые
    работают и без базы (например, прибор замера потолка кучи).
    """
    global _client
    if _client is None:
        import redis

        from config.runtime import REDIS

        _client = redis.Redis(
            host=REDIS["host"],
            port=int(REDIS["port"]),
            db=int(REDIS.get("db", 0)),
            decode_responses=True,
            socket_timeout=3.0,
        )
    return _client


def _enabled() -> bool:
    return (os.getenv(ENV_ENABLED) or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def max_concurrent() -> int:
    try:
        return max(1, int(float(os.getenv(ENV_MAX_CONCURRENT) or DEFAULT_MAX_CONCURRENT)))
    except ValueError:
        return DEFAULT_MAX_CONCURRENT


def slot_ttl_seconds() -> int:
    try:
        return max(60, int(float(os.getenv(ENV_SLOT_TTL_SECONDS) or DEFAULT_SLOT_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_SLOT_TTL_SECONDS


def slot_wait_seconds() -> int:
    try:
        return max(0, int(float(os.getenv(ENV_SLOT_WAIT_SECONDS) or DEFAULT_SLOT_WAIT_SECONDS)))
    except ValueError:
        return DEFAULT_SLOT_WAIT_SECONDS


def min_available_mb() -> int:
    try:
        return max(0, int(float(os.getenv(ENV_MIN_AVAILABLE_MB) or DEFAULT_MIN_AVAILABLE_MB)))
    except ValueError:
        return DEFAULT_MIN_AVAILABLE_MB


def mem_available_mb() -> Optional[int]:
    """Сколько памяти бокса доступно под новые выделения, МБ.

    ``MemAvailable`` из ``/proc/meminfo`` — та же величина, которой ядро решает,
    хватает ли памяти. ``None`` — платформа без ``/proc`` (или файл не прочитан):
    тогда проверка памяти не делается вовсе, и вызов идёт как раньше.
    """
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


@dataclass
class Lease:
    """Занятое место под вызов CLI — либо отказ с причиной.

    ``ok=False`` означает «вызов не запускай»: ``reason`` уходит в журнал и в
    платный Дипсик, ``details`` — числа, по которым видно, чего не хватило.
    """

    ok: bool
    reason: str = ""
    detail: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    slot: Optional[Tuple[int, str]] = None

    def release(self) -> None:
        """Отпустить слот (если он был взят)."""
        if self.slot is not None:
            release_slot(self.slot)
            self.slot = None


def memory_decision() -> Tuple[bool, Dict[str, Any]]:
    """Хватает ли боксу места под ещё один CLI."""
    avail = mem_available_mb()
    if avail is None:
        return True, {"mem_available_mb": None}
    required = min_available_mb()
    return avail >= required, {
        "mem_available_mb": avail,
        "mem_required_mb": required,
    }


def acquire_slot(
    *,
    ttl_seconds: Optional[int] = None,
    wait_seconds: Optional[int] = None,
) -> Optional[Tuple[int, str]]:
    """Занять слот вызова CLI. ``None`` — слот не достался.

    Возвращает пару ``(номер слота, token)`` — её же надо отдать в
    :func:`release_slot`.
    """
    ttl = max(60, int(ttl_seconds or slot_ttl_seconds()))
    wait = max(0, int(slot_wait_seconds() if wait_seconds is None else wait_seconds))
    slots = max_concurrent()
    deadline = time.monotonic() + wait
    while True:
        for index in range(slots):
            key = KEY_PREFIX + str(index)
            token = uuid.uuid4().hex
            try:
                if _redis().set(key, token, nx=True, px=ttl * 1000):
                    return index, token
            except Exception as exc:  # noqa: BLE001 — Redis недоступен: работаем без слота
                logger.warning(
                    "minimax: Redis недоступен (%s) — вызов CLI идёт без ограничения слотов",
                    exc,
                )
                return -1, ""
        if time.monotonic() >= deadline:
            return None
        time.sleep(2.0)


def release_slot(slot: Optional[Tuple[int, str]]) -> None:
    """Отпустить свой слот. Пустой slot (слота не было) — no-op."""
    if not slot:
        return
    index, token = slot
    if index < 0 or not token:
        return
    try:
        _redis().eval(_RELEASE_LUA, 1, KEY_PREFIX + str(index), token)
    except Exception as exc:  # noqa: BLE001 — слот истечёт по TTL
        logger.warning("minimax: не удалось отпустить слот CLI: %s", exc)


def lease(
    *,
    ttl_seconds: Optional[int] = None,
    wait_seconds: Optional[int] = None,
) -> Lease:
    """Занять место под вызов CLI: слот, затем проверка памяти.

    Порядок важен: **слот берётся первым**, и только потом меряется память. Так
    замер сделан на боксе без других вызовов CLI (слот свободен ⇒ никто не
    работает), и проверка памяти отвечает на вопрос «поместится ли ещё один
    вызов», а не «свободно ли сейчас, пока двое других уже растут».
    """
    if not _enabled():
        return Lease(ok=True, details={"capacity_check": "disabled"})

    wait = max(0, int(slot_wait_seconds() if wait_seconds is None else wait_seconds))
    slot = acquire_slot(ttl_seconds=ttl_seconds, wait_seconds=wait)
    if slot is None:
        avail = mem_available_mb()
        return Lease(
            ok=False,
            reason=REASON_BUSY,
            detail="все слоты вызовов CLI заняты (ждали %s с)" % wait,
            details={
                "mem_available_mb": avail,
                "slots": max_concurrent(),
                "slot_wait_seconds": wait,
            },
        )

    ok, details = memory_decision()
    if not ok:
        release_slot(slot)
        return Lease(
            ok=False,
            reason=REASON_NO_MEMORY,
            detail="на боксе %s МБ свободно, а вызову CLI нужно %s МБ"
            % (details.get("mem_available_mb"), details.get("mem_required_mb")),
            details=details,
        )

    return Lease(ok=True, details=details, slot=slot)
