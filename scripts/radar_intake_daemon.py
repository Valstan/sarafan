"""Демон интейков радара: VK Long Poll, Telegram getUpdates и поллер источников
вне Celery-воркера.

**Зачем отдельный процесс.** Оба поллинга почти всё время ЖДУТ события, которого
обычно нет, и в однопроцессном воркере это ожидание занимает единственный слот:
замер 21.10 — 1.7 ч и 1.6 ч в сутки (13 % слота). Хуже того, когда очередь волн
переполнена, минутные тики стоят в ней дольше своего ``expires`` и выбрасываются
вовсе: за день 1 526 выброшенных периодических задач, из них 692 — интейки. То
есть днём радар не принимает сообщения вообще.

Здесь ожидание бесплатное: ``wait=25`` у ВК и ``timeout=25`` у Telegram держат
соединение, событие приходит за секунды, слот воркера не занят. Ровно так уже
работает ``setka-vk-bot`` (``scripts/vk_bot_daemon.py``).

**Третий цикл — поллер источников** (21.10). Он не ждёт события, но тоже жил в
слоте: 144 прогона в сутки по 78 с = 187 мин, больше любой другой периодики, и
это при пяти источниках — они поллились по очереди. Теперь прогон идёт здесь по
фиксированному интервалу (``RADAR_POLL_INTERVAL_SECONDS``, дефолт 600 с), а
фетчи внутри прогона параллельны.

**Что осталось в beat.** Сами таски ``poll_radar_bot``/``poll_radar_vk_intake``
живы (ручной прогон, диагностика), но из ``beat_schedule`` убраны: два
потребителя на один ``ts``/``offset`` дали бы дубли событий. Так же убрана
``poll_radar_sources``: два потребителя на один heartbeat и двойные походы в
источники. Watchdog ``check_radar_poll_heartbeat`` остаётся в beat — он смотрит
на ``setka:radar_last_polled``, который пишет поллер, где бы тот ни жил.

Запуск — systemd-юнит ``setka-radar-intake`` (шаблон
``config/setka-radar-intake.service.template``, установка —
``scripts/install_radar_intake_service.sh``). Без конфига (нет
``RADAR_VK_COMMUNITY_ID``/токена сообщества, нет ``RADAR_BOT_NAME`` или пустой
allowlist) соответствующий поллинг спит и перепроверяет конфиг раз в минуту:
все три цикла бегут в одном процессе независимо.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("radar_intake_daemon")

# Секунды ожидания события. ВК держит до 90, Telegram — до 50; 25 оставляет
# запас на прокси и не рвёт соединение на границе.
LP_WAIT = 25
# Пауза, когда интейк выключен конфигом (не найден токен/allowlist): крутить
# цикл вхолостую значит писать в лог по строке в секунду.
SKIPPED_SLEEP = 60
# Пауза после ошибки сети/БД: не молотить упавший внешний сервис.
ERROR_SLEEP = 5
# Шаг поллера источников. 600 с — тот же, что был у beat-таски (каждые 10 мин):
# личная лента не публикация в сеть, чаще незачем. Пол — минута, чтобы опечатка
# в env не превратила демон в молотилку.
SOURCES_INTERVAL = max(60, int(os.getenv("RADAR_POLL_INTERVAL_SECONDS", "600")))

Runner = Callable[[], Awaitable[Dict[str, Any]]]


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Пауза с уважением к остановке: SIGTERM не ждёт конца сна."""
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def _loop(name: str, runner: Runner, stop: asyncio.Event) -> None:
    """Бесконечный цикл одного интейка. Живёт до ``stop``."""
    last_skip = None
    while not stop.is_set():
        try:
            out = await runner()
        except Exception:  # noqa: BLE001 - одна ошибка не должна ронять демон
            logger.exception("%s: тик упал", name)
            await _pause(stop, ERROR_SLEEP)
            continue

        skipped = out.get("skipped")
        if skipped:
            # Одинаковую причину печатаем один раз: иначе лог за сутки — 1 440
            # одинаковых строк «RADAR_VK_COMMUNITY_ID not set».
            if skipped != last_skip:
                logger.info(
                    "%s: выключен — %s (проверяю конфиг раз в %d с)",
                    name,
                    skipped,
                    SKIPPED_SLEEP,
                )
                last_skip = skipped
            await _pause(stop, SKIPPED_SLEEP)
            continue
        last_skip = None

        if not out.get("ok", True):
            logger.warning("%s: тик без результата — %s", name, out.get("error"))
            await _pause(stop, ERROR_SLEEP)
            continue

        if out.get("processed") or out.get("linked") or out.get("added") or out.get("replied"):
            logger.info("%s: %s", name, out)
        # Успешный тик: следующий сразу. Для long poll это и есть ожидание —
        # отдельный sleep только уменьшил бы частоту событий.


async def _interval_loop(name: str, runner: Runner, stop: asyncio.Event, interval: float) -> None:
    """Цикл с фиксированным шагом: тик, затем пауза до конца интервала.

    Нужен там, где «сразу следующий тик» бессмысленно: поллер источников
    ходит в сеть сам, события не ждёт, и чаще раза в интервал ему незачем.
    Отрицательный остаток (тик длиннее интервала) не превращается в вечный
    цикл — минимум секунда паузы.
    """
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        started = loop.time()
        try:
            out = await runner()
        except Exception:  # noqa: BLE001 - одна ошибка не должна ронять демон
            logger.exception("%s: тик упал", name)
            await _pause(stop, ERROR_SLEEP)
            continue

        if not out.get("ok", True):
            logger.warning("%s: тик без результата — %s", name, out.get("error"))
        elif out.get("new_items") or out.get("failed"):
            # Пустой прогон не печатаем: раз в 10 минут «0 новых» — это шум.
            logger.info("%s: %s", name, out)
        elapsed = loop.time() - started
        await _pause(stop, max(1.0, interval - elapsed))


async def main() -> None:
    # Секреты живут в комнате КАРМАНа, а не в env-файле: TELEGRAM_TOKEN_*, VK-токены
    # и ключи движка доезжают до процесса только через bootstrap — ровно так это
    # делает воркер (`tasks/celery_app.py`, строка с ``bootstrap_secrets()``).
    # Без этого демон стартовал с «no TELEGRAM_TOKEN for AFONYA» при живом боте.
    from modules.secrets_bootstrap import bootstrap_secrets

    bootstrap_secrets()

    from modules.radar.intake_runners import sources_poll_once, telegram_intake_once, vk_intake_once

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows (локальная разработка)
            pass

    vk: Runner = functools.partial(vk_intake_once, lp_wait=LP_WAIT)
    tg: Runner = functools.partial(telegram_intake_once, long_poll=LP_WAIT)
    logger.info(
        "radar intake daemon: старт (wait=%d c, поллер источников каждые %d c)",
        LP_WAIT,
        SOURCES_INTERVAL,
    )
    await asyncio.gather(
        _loop("vk-intake", vk, stop),
        _loop("tg-intake", tg, stop),
        _interval_loop("sources-poll", sources_poll_once, stop, SOURCES_INTERVAL),
    )
    logger.info("radar intake daemon: остановлен")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx на INFO печатает URL запроса целиком — у Telegram-бота токен в пути,
    # у ВК — в query. Секрет в логе недопустим (инцидент 2026-09-02).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from utils.log_redaction import install_log_redaction

    install_log_redaction()
    asyncio.run(main())
