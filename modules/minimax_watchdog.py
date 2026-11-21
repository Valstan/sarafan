"""Сторож «MiniMax молчит — трафик идёт в платный Дипсик».

**Зачем отдельный сторож, если продукт публикуется.** Инцидент 19–21.11: MiniMax не
отвечал двое суток, и **снаружи всё выглядело здоровым** — посты публиковались
(платная страховка), сервисы ``active``, health 200, сторож сводок молчал (сводки
выходили). Ни один существующий сигнал об этом не говорил: сломан был не продукт, а
**бесплатный движок**, а платить пришлось из своего кармана.

Замер (журнал вызовов нейросетей, 21.11): 941 отказ MiniMax и 942 платных вызова
Дипсика — **ровно 1:1**; успешных вызовов MiniMax 4 из 958 (0,4 %). За день до этого
платных вызовов было 324, а в здоровые дни — десятки.

**Сигнал берётся из общего журнала вызовов нейросетей**
(``logs/neural_usage.jsonl``), где у каждой строки есть поле ``engine``: по нему видно,
кто работал в окне — бесплатный MiniMax или платный Дипсик. Это тот же журнал, что
читает панель, — цифры у человека и у сторожа одни и те же.

**Почему не «MiniMax молчит» вообще.** Отказов движка в здоровом состоянии тоже
бывает (аккаунты с нулевым балансом: движок берёт следующий и работает дальше —
в журнале это отказ с успехом следом). Поэтому алёрт срабатывает не на «отказы», а на
**деньги**: платные вызовы идут, а доля успешных вызовов бесплатного движка упала.
Именно это и было незаметно два суток.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

#: Не спамить, пока авария длится: три часа — шесть тиков сторожа.
ALERT_COOLDOWN_SECONDS = 3 * 3600
COOLDOWN_KEY = "setka:minimax:dead_alert_cooldown"

#: Окно, за которое смотрим (минуты).
DEFAULT_WINDOW_MINUTES = 30
#: Сколько платных вызовов в окне считается «деньги пошли».
DEFAULT_MIN_PAID_CALLS = 5
#: Ниже какой доли успешных вызовов MiniMax считаем, что бесплатный движок не работает.
DEFAULT_MIN_SUCCESS_RATE = 0.5
#: Сколько отказов MiniMax подряд при нуле успехов — тоже авария.
DEFAULT_MIN_CONSECUTIVE_FAILURES = 5


def _row_ts(row: Dict[str, Any]) -> Optional[datetime]:
    """Метка времени строки журнала (``ts``) как datetime с зоной."""
    raw = str(row.get("ts") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def summarise_window(
    records: Iterable[Dict[str, Any]],
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Свод по окну: что сделал каждый движок и чем кончил MiniMax.

    Чистая функция (без файлов и сети) — её и проверяют тесты.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    minutes = max(1, int(window_minutes or DEFAULT_WINDOW_MINUTES))
    cutoff = current - timedelta(minutes=minutes)

    attempts = ok = fail = paid = 0
    reasons: Counter = Counter()
    for row in records or ():
        ts = _row_ts(row)
        if ts is None or ts < cutoff:
            continue
        engine = str(row.get("engine") or "").strip().lower()
        if engine == "deepseek":
            paid += 1
            continue
        if engine != "minimax":
            continue
        attempts += 1
        if row.get("ok"):
            ok += 1
        else:
            fail += 1
            reasons[str(row.get("reason") or "?")] += 1

    return {
        "window_minutes": minutes,
        "attempts": attempts,
        "ok": ok,
        "fail": fail,
        "paid": paid,
        "ok_rate": (float(ok) / attempts) if attempts else 0.0,
        "reasons": dict(reasons.most_common(5)),
    }


def should_alert(
    stats: Dict[str, Any],
    *,
    min_paid_calls: int = DEFAULT_MIN_PAID_CALLS,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    min_failures: int = DEFAULT_MIN_CONSECUTIVE_FAILURES,
) -> bool:
    """Тревожиться ли по этому окну.

    Три правила, и все три — про **деньги и остановку**, а не про отказы:

    * в окне ничего не было — тревожиться не о чем (тишина ≠ поломка);
    * MiniMax молчит совсем (отказы есть, успехов нет) — авария движка;
    * платные вызовы идут, а доля успешных вызовов бесплатного движка упала —
      **именно это было незаметно два суток**.

    Отказы с последующим успехом (аккаунт с нулевым балансом → движок берёт
    следующий) алёртом **не** считаются: успех рядом означает, что движок жив.
    """
    attempts = int(stats.get("attempts") or 0)
    ok = int(stats.get("ok") or 0)
    fail = int(stats.get("fail") or 0)
    paid = int(stats.get("paid") or 0)

    if attempts == 0 and paid == 0:
        return False
    if ok == 0 and fail >= int(min_failures):
        return True
    rate = float(stats.get("ok_rate") or 0.0)
    return paid >= int(min_paid_calls) and rate < float(min_success_rate)


def build_alert_text(stats: Dict[str, Any]) -> str:
    """Текст алёрта: что сломалось, сколько стоит и что делать."""
    attempts = int(stats.get("attempts") or 0)
    ok = int(stats.get("ok") or 0)
    fail = int(stats.get("fail") or 0)
    paid = int(stats.get("paid") or 0)
    rate = 100.0 * float(stats.get("ok_rate") or 0.0)
    reasons = stats.get("reasons") or {}
    reasons_text = (
        ", ".join("%s — %d" % (name, count) for name, count in list(reasons.items())[:3])
        or "причины не указаны"
    )
    return "\n".join(
        [
            "🔴 <b>SETKA: MiniMax не работает, трафик идёт в платный Дипсик</b>\n",
            f"За последние <b>{int(stats.get('window_minutes') or 0)} мин</b>: "
            f"MiniMax — вызовов <b>{attempts}</b>, успешных <b>{ok}</b> ({rate:.0f} %), "
            f"отказов <b>{fail}</b>; платных вызовов Дипсика — <b>{paid}</b>.",
            f"\nПричины отказов: {reasons_text}.",
            "\n\nКаждый отказ MiniMax оплачивается отдельным вызовом Дипсика (замер 21.11: "
            "941 отказ = 942 платных вызова).",
            "\n\nЧто делать: <code>du -sh /home/valstan/minimax-accounts/work/data</code> "
            "(состояние CLI копится и поднимает RSS — при 897 МБ ядро убивает процесс), "
            "<code>dmesg -T | grep 'killed process'</code>, "
            "<code>tail -20 logs/minimax_usage.jsonl</code>.",
            "\nРучник денег: <code>CLASSIFIER_PAID_DAILY_CALLS</code> — потолок платных "
            "вызовов в сутки (сейчас 0, то есть без ограничения).",
        ]
    )


def _redis():
    """Redis для cooldown'а — тот же клиент, что у сторожа сводок."""
    try:
        from modules.bulletin_heartbeat import _redis as _shared_redis

        return _shared_redis()
    except Exception as exc:  # noqa: BLE001 — сторож не должен падать
        logger.warning("minimax watchdog: redis недоступен: %s", exc)
        return None


def maybe_alert_minimax_dead(
    *,
    stats: Dict[str, Any],
    telegram_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    min_paid_calls: int = DEFAULT_MIN_PAID_CALLS,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    min_failures: int = DEFAULT_MIN_CONSECUTIVE_FAILURES,
) -> str:
    """Алёрт, если бесплатный движок не работает, а платный — работает.

    Возвращает статус-строку: ``fresh`` | ``unknown:no-calls`` | ``skipped:no-telegram-config``
    | ``skipped:cooldown`` | ``alert-sent`` | ``error:…``.
    """
    if not should_alert(
        stats,
        min_paid_calls=min_paid_calls,
        min_success_rate=min_success_rate,
        min_failures=min_failures,
    ):
        return "fresh" if (stats.get("attempts") or stats.get("paid")) else "unknown:no-calls"

    if not telegram_token or not chat_id:
        logger.error(
            "minimax watchdog: движок молчит, но алёрт послать некуда "
            "(нет TELEGRAM_ALERT_CHAT_ID/токена)",
        )
        return "skipped:no-telegram-config"

    client = _redis()
    try:
        if client is not None and client.get(COOLDOWN_KEY):
            return "skipped:cooldown"
    except Exception:  # noqa: BLE001 — cooldown не критичен
        pass

    message = build_alert_text(stats)
    try:
        from modules import telegram_http as tg_http

        resp = tg_http.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "minimax watchdog: алёрт не отправлен: %s %s", resp.status_code, resp.text[:200]
            )
            return "error:http-" + str(resp.status_code)
        if client is not None:
            client.setex(COOLDOWN_KEY, ALERT_COOLDOWN_SECONDS, "1")
        logger.warning(
            "minimax watchdog: алёрт отправлен (платных вызовов %s, успешных MiniMax %s)",
            stats.get("paid"),
            stats.get("ok"),
        )
        return "alert-sent"
    except Exception as exc:  # noqa: BLE001
        logger.error("minimax watchdog: алёрт не отправлен: %s", exc)
        return "error:" + str(exc)


__all__ = [
    "ALERT_COOLDOWN_SECONDS",
    "DEFAULT_MIN_CONSECUTIVE_FAILURES",
    "DEFAULT_MIN_PAID_CALLS",
    "DEFAULT_MIN_SUCCESS_RATE",
    "DEFAULT_WINDOW_MINUTES",
    "build_alert_text",
    "maybe_alert_minimax_dead",
    "should_alert",
    "summarise_window",
]
