"""VK-интейк радара: Bots Long Poll сообщества → привязка VK-лички («Радиоточка»).

Аналог `bot_intake` (Telegram), но для VK: пользователь пишет нашему сообществу
код привязки → ловим его ``from_id`` через Bots Long Poll → создаём vk_dm-вывод и
радар шлёт ему новости в личку через ``messages.send``. Токен пользователя НЕ
нужен (community-токен + захват vk_id), IP-привязки/бан-риска нет (решение
владельца 2026-06-14).

Probe (#020, scripts/probe_vk_messaging.py) подтвердил Long Poll на сообществе-точке
(env ``RADAR_VK_COMMUNITY_ID``). Чистые функции (extract_message/handle_update)
тестируемы без сети; сетевые вызовы инъектируются.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method/{method}"
VK_V = "5.199"
# Секунд ожидания в a_check. Было 10, и это оказалось дорого не токенами, а
# временем единственного слота воркера (замер 21.10: 677 прогонов × 9.1 с =
# 6 137 с за сутки, 6.6 % всего времени слота, почти всё — ожидание события,
# которого нет). Три секунды не меняют отзывчивость: тик всё равно минутный,
# и событие ловится в пределах того же или следующего тика, — но перестают
# занимать слот впустую. Идеал — вынести поллинг в демон (как setka-vk-bot),
# это отдельная работа.
LP_WAIT = 3


def vk_api_call(token: str, method: str, **params) -> Dict[str, Any]:
    """Синхронный вызов VK API. Возвращает распарсенный JSON или {}."""
    params.update({"access_token": token, "v": VK_V})
    try:
        r = httpx.get(VK_API.format(method=method), params=params, timeout=20)
        return r.json()
    except Exception as e:  # noqa: BLE001 - сеть; не валим beat
        logger.warning("vk intake %s failed: %s", method, e)
        return {}


def lp_fetch(server: str, key: str, ts, wait: int = LP_WAIT) -> Dict[str, Any]:
    """Запрос a_check к Long Poll серверу сообщества. JSON или {}."""
    url = server if server.startswith("http") else f"https://{server}"
    try:
        r = httpx.get(
            url,
            params={"act": "a_check", "key": key, "ts": ts, "wait": wait, "mode": 2, "version": 3},
            timeout=wait + 15,
        )
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("vk intake long-poll fetch failed: %s", e)
        return {}


def extract_message(update: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Из события Long Poll вытащить (from_id, text) входящего сообщения. Чистая.

    Bots Long Poll v5.199: {"type":"message_new","object":{"message":{from_id,text}}}.
    Не message_new / нет from_id → (None, "")."""
    if not isinstance(update, dict) or update.get("type") != "message_new":
        return None, ""
    obj = update.get("object") or {}
    msg = obj.get("message") if isinstance(obj, dict) else None
    if not isinstance(msg, dict):
        return None, ""
    from_id = msg.get("from_id")
    if not isinstance(from_id, int) or from_id <= 0:
        return None, ""
    return from_id, (msg.get("text") or "").strip()


def _extract_code(text: str) -> str:
    """Код привязки из текста сообщения (голый код или `/start <код>`)."""
    t = (text or "").strip()
    if t.lower().startswith("/start"):
        parts = t.split(maxsplit=1)
        t = parts[1].strip() if len(parts) > 1 else ""
    return t.split()[0].strip() if t else ""


async def handle_update(
    update: Dict[str, Any],
    *,
    link_account: Callable[[str, int, str], Any],
) -> Optional[Tuple[int, str]]:
    """Обработать одно событие → (peer_id, reply_text) или None (молчим).

    `link_account(code, vk_user_id, display_name)` — привязка по коду; возвращает
    {"status": "linked"|"exists"|"invalid"}. Код сам авторизует — allowlist не нужен
    (написать сообществу = явное намерение)."""
    from_id, text = extract_message(update)
    if from_id is None:
        return None
    code = _extract_code(text)
    if not code:
        # Сообщение без кода (просто написали сообществу) — молчим, не спамим.
        return None
    res = await link_account(code, from_id, "")
    status = (res or {}).get("status")
    if status == "linked":
        return from_id, (
            "✅ Подключено! Радар будет присылать сюда найденные новости. "
            "Отключить — в кабинете «Радиоточка»."
        )
    if status == "exists":
        return from_id, "ℹ️ Эта переписка уже подключена к вашему радару."
    return from_id, (
        "❌ Код не найден или истёк. Сгенерируйте новый в кабинете "
        "«Радиоточка» → «Подключить ВКонтакте»."
    )


async def poll_vk_intake_once(
    *,
    token: str,
    group_id: int,
    link_account: Callable[[str, int, str], Any],
    reply: Callable[[int, str], Any],
    ts_get: Callable[[], Optional[str]],
    ts_set: Callable[[Optional[str]], None],
    api_call: Optional[Callable[..., Dict[str, Any]]] = None,
    lp_get: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Один тик VK-интейка: getLongPollServer → a_check → привязки → ответы → ts.

    `api_call`/`lp_get`/`reply` инъектируются (тест/прод)."""
    api_call = api_call or vk_api_call
    lp_get = lp_get or lp_fetch

    srv = api_call(token, "groups.getLongPollServer", group_id=group_id)
    resp = srv.get("response") if isinstance(srv, dict) else None
    if not resp or not resp.get("server"):
        err = (srv or {}).get("error") or {}
        return {"ok": False, "error": err.get("error_msg", "getLongPollServer failed")}

    server, key, fresh_ts = resp["server"], resp["key"], resp["ts"]
    ts = ts_get() or fresh_ts

    data = lp_get(server, key, ts)
    if "failed" in data:
        # 1: ts устарел → берём новый; 2/3: ключ/сервер протух → переинициализация.
        if data.get("failed") == 1 and data.get("ts") is not None:
            ts_set(str(data["ts"]))
        else:
            ts_set(None)
        return {"ok": True, "reinit": data.get("failed"), "processed": 0, "linked": 0}

    updates = data.get("updates") or []
    processed, linked = 0, 0
    for upd in updates:
        processed += 1
        try:
            out = await handle_update(upd, link_account=link_account)
        except Exception:  # noqa: BLE001 - один битый апдейт не валит остальные
            logger.exception("vk intake: handle_update failed")
            continue
        if out is None:
            continue
        peer_id, text = out
        if text.startswith("✅"):
            linked += 1
        try:
            await reply(peer_id, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("vk intake reply failed: %s", e)

    if data.get("ts") is not None:
        ts_set(str(data["ts"]))
    return {"ok": True, "updates": len(updates), "processed": processed, "linked": linked}
