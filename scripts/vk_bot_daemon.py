"""Демон ВК-бота кабинета: постоянный Bots Long Poll сообщества САРАФАН.

Зачем отдельный процесс. Celery-воркер на проде однопроцессный, а Long Poll
раз в минуту из beat давал клиенту до минуты (а при пропуске тика — минуты)
ожидания на каждый шаг диалога: «бот завис» (жалоба владельца 2026-09-02).
Здесь опрос непрерывный (``wait=25``), ответ приходит за секунды, и ни один
воркер не занят.

Запуск — systemd-юнит ``setka-vk-bot`` (шаблон ``config/setka-vk-bot.service.template``,
установка — ``scripts/install_vk_bot_service.sh``). Без ``SARAFAN_VK_COMMUNITY_ID``
или community-токена демон спит и проверяет конфиг раз в минуту.

Логика диалога и отправки — та же, что у beat-тика (``vk_bot.intake``): демон
только крутит цикл и держит ``key``/``ts`` Long Poll в памяти, переинициализируя
их по кодам ``failed`` из документации ВК (1 — обновить ts, 2 — новый key,
3 — новый key и ts).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- Окружение и секреты. На проде их даёт systemd (EnvironmentFile=), на
#     Windows-стенде env-файл читает сам процесс, а ключи приезжают из комнаты
#     КАРМАНА тем же путём, что и на проде. На проде обе строки — no-op:
#     окружение целое, сеть не трогается.
#     Почему это здесь, а не в main.py: демон запускается как отдельный
#     процесс и main.py не импортирует — без этих строк он падал с
#     «DATABASE_URL is not set» ещё до первого запроса.
from infra.windows.local_env import load_local_env  # noqa: E402
from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402
from utils.log_redaction import install_log_redaction  # noqa: E402

load_local_env()
bootstrap_secrets()
install_log_redaction()

logger = logging.getLogger("vk_bot_daemon")

LP_WAIT = 25  # секунд ожидания a_check; ВК держит до 90
CONFIG_RECHECK = 60  # секунд между проверками env/токена, когда бот выключен
TOKEN_REFRESH = 300  # секунд — перечитать токен из /tokens (ротация без рестарта)


async def _config():
    from modules.ad_cabinet.vk_bot.notify import community

    return await community()


async def main() -> None:
    from database.connection import AsyncSessionLocal
    from modules.ad_cabinet.vk_bot import heartbeat, intake
    from modules.bulletin_heartbeat import _redis
    from modules.radar.vk_intake import lp_fetch, vk_api_call

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    r = _redis()
    if r is None:
        logger.error("no redis — dialog state needs it; exiting")
        return
    state_get, state_set = intake.redis_state_store(r)
    photo_fetch = intake.make_photo_fetch()  # без состояния — один на процесс

    conf = None
    conf_at = 0.0
    server = key = ts = None
    while not stop.is_set():
        now = time.monotonic()
        if conf is None or now - conf_at > TOKEN_REFRESH:
            new_conf = await _config()
            if new_conf != conf:
                server = key = ts = None  # токен сменился — переинициализация
            conf, conf_at = new_conf, now
        if conf is None:
            logger.info("vk_bot daemon: off (no SARAFAN_VK_COMMUNITY_ID or token); sleeping")
            try:
                await asyncio.wait_for(stop.wait(), CONFIG_RECHECK)
            except asyncio.TimeoutError:
                pass
            continue

        group_id, token = conf
        if not server:
            srv = await asyncio.to_thread(
                vk_api_call, token, "groups.getLongPollServer", group_id=group_id
            )
            resp = srv.get("response") if isinstance(srv, dict) else None
            if not resp or not resp.get("server"):
                err = (srv or {}).get("error") or {}
                logger.warning("getLongPollServer failed: %s", err.get("error_msg", srv))
                # Ждём минуту, а не крутим цикл: сброс conf сам по себе паузы не
                # даёт (перечитанный конфиг тот же), и отозванный токен давал бы
                # десятки запросов к БД и ВК в секунду с WARNING на каждый.
                conf, conf_at = None, 0.0
                try:
                    await asyncio.wait_for(stop.wait(), CONFIG_RECHECK)
                except asyncio.TimeoutError:
                    pass
                continue
            server, key = resp["server"], resp["key"]
            ts = ts or resp["ts"]
            logger.info("long poll connected: group %s", group_id)

        data = await asyncio.to_thread(lp_fetch, server, key, ts, LP_WAIT)
        if not data:
            await asyncio.sleep(3)  # сеть мигнула — не молотить
            continue
        # Живой ответ ВК (события, пустой или failed) — демон реально опрашивает.
        heartbeat.touch(r)
        if "failed" in data:
            code = data.get("failed")
            if code == 1 and data.get("ts") is not None:
                ts = str(data["ts"])
            elif code == 2:
                server = None  # новый key, ts сохраняем
            else:
                server, ts = None, None
            logger.info("long poll failed=%s → reinit", code)
            continue

        sender = intake.notify._make_sender(token, group_id)
        name_fetch = intake.make_name_fetch(token, vk_api_call)
        submit = intake.make_real_submitter()
        for upd in data.get("updates") or []:
            inc = intake.extract_incoming(upd)
            if inc is None:
                continue
            try:
                events = await intake.handle_one(
                    inc,
                    session_factory=AsyncSessionLocal,
                    state_get=state_get,
                    state_set=state_set,
                    submit=submit,
                    name_fetch=name_fetch,
                    sender=sender,
                    photo_fetch=photo_fetch,
                )
                logger.info("peer %s: handled, events=%s", inc.peer_id, events)
            except Exception:  # noqa: BLE001
                logger.exception("handle_one failed for peer %s", inc.peer_id)
                try:
                    await sender(
                        inc.peer_id,
                        "Что-то пошло не так, попробуйте ещё раз или напишите владельцу.",
                        intake.dialog.MAIN_KEYBOARD,
                    )
                except Exception:  # noqa: BLE001
                    pass
        if data.get("ts") is not None:
            ts = str(data["ts"])
            try:
                r.set(intake.TS_KEY, ts)
            except Exception:  # noqa: BLE001 - обрыв Redis не должен ронять демон в crash-loop
                logger.warning("vk_bot daemon: ts not persisted", exc_info=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx на INFO печатает URL запроса целиком — с access_token в query.
    # Секрет в логе недопустим (инцидент 2026-09-02: ключ сообщества уехал в
    # vk-bot.log при первом же запуске). Только WARNING и выше.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Второй слой: process-wide маскирование секретов (utils/log_redaction) —
    # на случай, если что-то ещё выведет ключ текстом исключения.
    from utils.log_redaction import install_log_redaction

    install_log_redaction()
    asyncio.run(main())
