#!/usr/bin/env python3
"""MCP-сервер: VK-шлюз SARAFAN как инструменты для AI-сессий проектов @valstan.

Обёртка над read-only HTTP-шлюзом SARAFAN (``docs/GATEWAY.md``). Запускается в
среде ПОТРЕБИТЕЛЯ (не в SARAFAN): даёт его Claude-сессии инструменты «сходить в
VK» без своей VK-инфры. Токен VK наружу не уходит — шлюз исполняет read-вызов
своим токеном и возвращает JSON.

Конфиг (env потребителя):
  SARAFAN_GATEWAY_KEY   API-ключ проекта (обязателен) — выдаёт владелец SARAFAN;
  SARAFAN_GATEWAY_URL   базовый URL шлюза (опц.; дефолт — прод-хост).

Запуск (stdio):
  SARAFAN_GATEWAY_KEY=... python -m gateway_mcp.server

Пример .mcp.json / claude config — в gateway_mcp/README.md.

**Описания инструментов живут в одном месте.** Схемы и обработчики — в
``gateway_mcp/tools.py`` (общий реестр с агентным слоем), этот модуль только
оборачивает их в MCP. Раньше описание, схема и вызов были продублированы в
двух файлах, и правка одного из них молча расходилась с другим.
"""

from __future__ import annotations

from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from gateway_mcp.client import GatewayClient
from gateway_mcp.tool_registry import READ_ANNOTATIONS, ToolRegistry
from gateway_mcp.tools import CallInput, CommunityInput, WallInput, build_gateway_tools

mcp = FastMCP("vk_gateway_mcp")

#: Реестр — источник истины по описаниям, схемам и вызовам.
_registry: ToolRegistry = build_gateway_tools(ToolRegistry(), GatewayClient())


async def _call(name: str, arguments: Dict[str, Any]) -> str:
    """Вызвать инструмент реестра и отдать модели его текстовый результат.

    Реестр не бросает: ошибка приходит строкой ``Error: ...`` — ровно как
    прежде, когда каждый инструмент сам ловил исключение.
    """
    payload = await _registry.call(name, arguments)
    if payload.get("ok"):
        result = payload.get("result")
        return result if isinstance(result, str) else str(result)
    error = payload.get("error")
    detail = payload.get("detail")
    tail = f" ({detail})" if detail else ""
    return f"Error: {error}{tail}"


@mcp.tool(
    name="vk_get_community", annotations={"title": "VK: инфо о сообществе", **READ_ANNOTATIONS}
)
async def vk_get_community(params: CommunityInput) -> str:
    """Получить информацию о VK-сообществе через шлюз SARAFAN (``groups.getById``).

    Read-only. Удобная обёртка для частого кейса «что это за паблик».

    Args:
        params (CommunityInput):
            - group (str): числовой id или screen_name сообщества;
            - fields (str): CSV доп. полей (опц.).

    Returns:
        str: JSON ответа шлюза. Успех — ``{"ok": true, "response": [...]}``
        (массив groups.getById). Доменная VK-ошибка — ``{"ok": false, "error": {...}}``.
        Транспорт/auth/квота — строка ``"Error: ..."`` с подсказкой.
    """
    return await _call("vk_get_community", params.model_dump())


@mcp.tool(name="vk_get_wall", annotations={"title": "VK: посты со стены", **READ_ANNOTATIONS})
async def vk_get_wall(params: WallInput) -> str:
    """Получить последние посты со стены VK через шлюз SARAFAN (``wall.get``).

    Read-only, с пагинацией (count/offset).

    Args:
        params (WallInput):
            - owner_id (int): владелец стены (сообщество — со знаком минус);
            - count (int): сколько постов (1..100, дефолт 20);
            - offset (int): смещение пагинации (дефолт 0).

    Returns:
        str: JSON ответа шлюза. Успех — ``{"ok": true, "response": {"count": N, "items": [...]}}``.
        Доменная VK-ошибка — ``{"ok": false, "error": {...}}``. Иначе ``"Error: ..."``.
    """
    return await _call("vk_get_wall", params.model_dump())


@mcp.tool(name="vk_call", annotations={"title": "VK: произвольный read-метод", **READ_ANNOTATIONS})
async def vk_call(params: CallInput) -> str:
    """Исполнить произвольный read-метод VK API из allowlist шлюза (``POST /call``).

    Универсальная дверь для методов без удобной обёртки (users.get,
    groups.getMembers, wall.getComments, utils.resolveScreenName и т.д.).
    Запись (wall.post/messages.send/likes.add/…) шлюзом запрещена → ``Error: 400``.

    Args:
        params (CallInput):
            - method (str): VK read-метод из allowlist;
            - params (dict): параметры метода.

    Returns:
        str: JSON ответа шлюза. Успех — ``{"ok": true, "response": <payload метода>}``.
        Доменная VK-ошибка — ``{"ok": false, "error": {...}}``. Иначе ``"Error: ..."``
        (в т.ч. 400, если метод вне read-allowlist).
    """
    return await _call("vk_call", params.model_dump())


def main() -> None:
    """Запустить сервер по stdio (для локального подключения у потребителя)."""
    mcp.run()


if __name__ == "__main__":
    main()
