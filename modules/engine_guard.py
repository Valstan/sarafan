"""Предохранитель вызовов движка: под лимитом free-тира волна не ждёт впустую.

**Что измерено (21.10.2026, лог воркера за 48 часов).** Цепочка моделей
``deepseek-v4.1-flash-free`` → ``glm-5.3-flash-free`` → ``mimo-v2.6-flash-free``
получила 371 отказ ``http_429`` и 156 ``http_403``. Пересказ одного поста
занимал 30–120 секунд не потому, что модель думает: вызывающий по очереди
пробует три модели, при отказе по IP поворачивает выход ВПН и повторяет всю
цепочку заново. Фаза пересказа — 37–61 % всего времени воркера, а слот у него
один: пока он ждёт заведомо неудачный ответ, волны других районов стоят в
очереди, и посты выходят позже расписания.

**Класс отказа тихий.** Публикация не падает — везде fail-open, в ленту уходит
исходный текст. Поэтому «движок лимитит» выглядит как «всё работает, только
медленно», и заметить это можно лишь по длине очереди.

**Что делает предохранитель.** Считает подряд идущие отказы по лимиту/сети.
После ``ENGINE_BREAKER_FAILS`` (дефолт 3) объявляет движок недоступным на
``ENGINE_BREAKER_COOLDOWN`` секунд (дефолт 300): вызовы сразу возвращают отказ,
не трогая сеть, — волны идут по обычным фильтрам. Первый успешный вызов
закрывает предохранитель досрочно, поэтому после снятия лимита пересказ
возвращается сам, без рестарта.

**Смена выхода ВПН снимает предохранитель** (``rotation_happened``, 29.10).
Гейт free-тира адресуется сетью: тот же ``http_429`` снимается сменой выхода за
1,7–2,6 с, и это подтверждено замерами (28.10: пять прогонов по 200 постов,
в каждом внутри — 429 → поворот → прогон доработал; 29.10 00:41 живая цепочка
«429 на прежнем адресе → поворот KZ→DE → ответ модели за 4 с»). До этой правки
предохранитель про поворот не знал и держал вызовы закрытыми 30 минут — то есть
один 429 в первом чанке глушил остаток прогона, хотя выход к тому моменту был
уже другой. Снаружи это выглядело как «ВПН крутится с ``rc=0``, а фри-тир не
возвращается»: замер 27.10 — 7 из 8 прогонов умерли ``breaker_open``
(``recorded=0``) при 17 удачных поворотах.

**Почему в процессе, а не в Redis.** Воркер — один процесс на слот (prefork),
состояние нужно ровно этому процессу; разделяемое состояние потребовало бы
сетевого вызова на каждый чек — цена больше пользы. Плата: у web-процесса свой
предохранитель, и это нормально (там движок зовут кнопки, а не конвейер).

Env:
  ENGINE_BREAKER_ENABLED=1     # 0 — выключить (аварийный откат без деплоя)
  ENGINE_BREAKER_FAILS=3       # сколько отказов подряд открывают предохранитель
  ENGINE_BREAKER_COOLDOWN=300  # на сколько секунд он открывается
  ENGINE_BREAKER_QUOTA_COOLDOWN=1800
                               # отдельный, длинный предохранитель для класса
                               # «квота аккаунта кончилась»: он открывается
                               # сразу (порог не нужен) и держится долго.
                               # 29.10 на проде 300: квота free-тира снимается
                               # сменой выхода, поэтому полчаса тишины на один
                               # 429 — цена выше пользы
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Отказы, которые означают «движок сейчас недоступен», а не «ответ не по форме».
# ``llm_unparseable``/``shape:*``/``empty_response`` сюда НЕ входят: модель
# ответила, просто ответ негоден — это не повод глушить её на пять минут.
_THROTTLE_REASONS = frozenset(
    {
        "http_429",
        "http_403",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "network",
        "timeout",
        "engine_all_failed",
        "engine_disabled",
        "quota_exhausted",
    }
)

# Квота free-тира кончилась (``Free usage limit reached``). Свой предохранитель:
# он открывается СРАЗУ (порог не нужен — квота общая для всех моделей каталога,
# и второй отказ ничего нового не сообщает) и держится долго.
#
# ⚠️ 24.10 здесь стояло «держит сутками и сменой выхода ВПН не снимается» —
# замер 27–29.10 это опроверг: один и тот же 429 снимается сменой выхода, а
# сутками держался не адрес, а наш же предохранитель. Поэтому поворот выхода
# теперь снимает его досрочно (``rotation_happened``), а длинная пауза осталась
# только на случай, когда повернуть некуда.
_QUOTA_REASON = "quota_exhausted"

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "failures": 0,
    "open_until": 0.0,
    "opens": 0,
    "skipped": 0,
    "rotations": 0,
    "last_reason": "",
}


def _enabled() -> bool:
    raw = (os.getenv("ENGINE_BREAKER_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _threshold() -> int:
    try:
        return max(1, int(os.getenv("ENGINE_BREAKER_FAILS", "3")))
    except ValueError:
        return 3


def _cooldown() -> float:
    try:
        return max(0.0, float(os.getenv("ENGINE_BREAKER_COOLDOWN", "300")))
    except ValueError:
        return 300.0


def _quota_cooldown() -> float:
    """Сколько держать вызовы закрытыми при исчерпанной квоте аккаунта, сек.

    Дефолт 30 минут. Длинная пауза нужна на случай, когда повернуть выход
    некуда (``NEURAL_EGRESS_ROTATE_CMD`` не задан или поворот не удался): тогда
    повтор — это не «попробовать ещё раз», а сожжённые запросы в единственном
    слоте воркера (замер 24.10 — 664 отказа за двое суток при мёртвой квоте).

    ⚠️ Успешный поворот выхода снимает предохранитель досрочно
    (``rotation_happened``), поэтому на боксе с настроенным ВПН полчаса тишины
    на один 429 не наступают. 29.10 на проде стоит 300 с: замеры 27–29.10
    показали, что после поворота ответы возвращаются за секунды, и платить
    получасом простоя за один отказ незачем.
    """
    try:
        return max(0.0, float(os.getenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")))
    except ValueError:
        return 1800.0


def _is_throttle(reason: str) -> bool:
    return (reason or "").strip() in _THROTTLE_REASONS


def allow() -> bool:
    """Можно ли сейчас звать движок. ``False`` — предохранитель открыт."""
    if not _enabled():
        return True
    now = time.monotonic()
    with _lock:
        if _state["open_until"] > now:
            _state["skipped"] += 1
            return False
        if _state["open_until"]:
            # Истёк — закрываем и начинаем считать заново.
            _state["open_until"] = 0.0
            _state["failures"] = 0
        return True


def note_result(ok: bool, reason: str = "") -> None:
    """Учесть исход вызова: успех закрывает, лимит — считает к открытию."""
    if not _enabled():
        return
    now = time.monotonic()
    with _lock:
        if ok:
            if _state["failures"] or _state["open_until"]:
                logger.info(
                    "engine breaker: движок ответил — предохранитель закрыт"
                    " (отказов подряд было %d)",
                    _state["failures"],
                )
            _state["failures"] = 0
            _state["open_until"] = 0.0
            return

        if not _is_throttle(reason):
            return

        _state["last_reason"] = reason
        quota = reason == _QUOTA_REASON
        if not quota:
            _state["failures"] += 1
            if _state["failures"] < _threshold():
                return

        # У квоты свой, длинный предохранитель и порог не нужен: она общая для
        # всего каталога моделей, поэтому второй отказ ничего нового не
        # сообщает, а стоит ещё одного запроса в слоте воркера.
        cooldown = _quota_cooldown() if quota else _cooldown()
        if cooldown <= 0:
            return
        _state["open_until"] = now + cooldown
        _state["opens"] += 1
        _state["failures"] = 0
        if quota:
            logger.warning(
                "engine breaker: квота free-тира кончилась (%s) — на %.0f с вызовы"
                " пропускаются; снимается сбросом идентичности или ожиданием",
                reason,
                cooldown,
            )
            return
        logger.warning(
            "engine breaker: движок лимитит (%s) — на %.0f с вызовы пропускаются,"
            " волны идут по обычным фильтрам",
            reason,
            cooldown,
        )


def rotation_happened(reason: str = "") -> None:
    """Выход ВПН сменён — снять предохранитель, поставленный на прежний адрес.

    Зачем. Гейт free-тира адресуется сетью, а не аккаунтом: тот же
    ``http_429 Free usage limit reached`` снимается поворотом выхода за 1,7–2,6 с
    (замеры 20.10, 28.10, 29.10). Предохранитель этого не знал: после неудачного
    повтора он закрывал вызовы на ``ENGINE_BREAKER_QUOTA_COOLDOWN`` секунд, и
    остаток прогона короткозамыкался — при том что выход уже был другой. Живой
    счёт 27.10: 7 из 8 прогонов движка умерли ``breaker_open`` с ``recorded=0``,
    хотя все 17 поворотов вернули ``rc=0``.

    Что НЕ делает: не выдаёт успех за успех и не трогает сеть. Следующий вызов
    идёт на новый адрес как обычно — если и он упрётся, предохранитель
    откроется снова, уже по честному отказу.
    """
    if not _enabled():
        return
    now = time.monotonic()
    with _lock:
        if not _state["open_until"] and not _state["failures"]:
            return
        held = max(0.0, _state["open_until"] - now)
        was = _state["last_reason"] or "?"
        _state["open_until"] = 0.0
        _state["failures"] = 0
        _state["rotations"] += 1
    logger.warning(
        "engine breaker: выход ВПН сменён (%s) — предохранитель снят досрочно"
        " (оставалось %.0f с, причина была %s)",
        (reason or "?")[:80],
        held,
        was,
    )


def snapshot() -> Dict[str, Any]:
    """Состояние для страницы «Карта конвейера» и диагностики."""
    now = time.monotonic()
    with _lock:
        remaining = max(0.0, _state["open_until"] - now)
        return {
            "enabled": _enabled(),
            "open": _state["open_until"] > now,
            "remaining_seconds": round(remaining, 1),
            "failures_in_row": _state["failures"],
            "opens": _state["opens"],
            "skipped": _state["skipped"],
            "rotations": _state["rotations"],
            "last_reason": _state["last_reason"],
            "threshold": _threshold(),
            "cooldown": _cooldown(),
            "quota_cooldown": _quota_cooldown(),
        }


def reset() -> None:
    """Сбросить состояние (тесты и ручная диагностика)."""
    with _lock:
        _state.update(
            {
                "failures": 0,
                "open_until": 0.0,
                "opens": 0,
                "skipped": 0,
                "rotations": 0,
                "last_reason": "",
            }
        )


__all__ = ["allow", "note_result", "rotation_happened", "snapshot", "reset"]
