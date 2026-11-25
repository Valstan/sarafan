"""Инструменты агента: VK-шлюз SARAFAN (read-only) как готовый образец.

Модуль вынесен из ``server.py`` без изменения поведения: тот же клиент, те же
параметры, те же описания. Причина — **одно место, где описано**: JSON Schema
инструмента теперь рождается из pydantic-модели здесь, а MCP-обёртка
(``server.py``) и агентный слой (``agent.py``) читают одно и то же описание.

Схемы — по образцу ``docs/GATEWAY.md``: контракт шлюза read-only, запись
запрещена. Все три инструмента помечены как read-only (``READ_ANNOTATIONS``).
"""

from __future__ import annotations

import json
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field

from gateway_mcp.client import GatewayClient, GatewayError
from gateway_mcp.tool_registry import ToolRegistry


class CommunityInput(BaseModel):
    """Параметры ``vk_get_community``."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group: str = Field(
        ...,
        description="Числовой id или screen_name сообщества (напр. '1' или 'apiclub').",
        min_length=1,
        max_length=200,
    )
    fields: str = Field(
        default="",
        description=(
            "CSV доп. полей groups.getById (опц.). Пусто — дефолт шлюза "
            "(описание, members_count, activity, status, screen_name, фото, сайт, контакты)."
        ),
        max_length=500,
    )


class WallInput(BaseModel):
    """Параметры ``vk_get_wall``."""

    model_config = ConfigDict(extra="forbid")

    owner_id: int = Field(
        ...,
        description="ID владельца стены. Сообщество — со знаком минус (напр. -1 = vk.com/apiclub).",
    )
    count: int = Field(default=20, description="Сколько постов вернуть (1..100).", ge=1, le=100)
    offset: int = Field(default=0, description="Смещение для пагинации.", ge=0)


class CallInput(BaseModel):
    """Параметры ``vk_call`` — универсальный read-вызов."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    method: str = Field(
        ...,
        description=(
            "VK API read-метод из allowlist шлюза (напр. 'users.get', 'groups.getMembers', "
            "'wall.getComments', 'utils.resolveScreenName'). Запись запрещена → 400."
        ),
        min_length=1,
        max_length=64,
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Параметры VK-метода как объект (напр. {'user_ids': '1,2', 'fields': 'city'}).",
    )


def format_payload(result: Dict[str, Any]) -> str:
    """Сериализовать payload шлюза для агента (компактный, читаемый JSON)."""
    return json.dumps(result, ensure_ascii=False, indent=2)


def handle_error(exc: Exception) -> str:
    """Единое actionable-сообщение об ошибке для всех инструментов."""
    if isinstance(exc, GatewayError):
        return f"Error: {exc}"
    if isinstance(exc, TimeoutError):
        return "Error: запрос к шлюзу истёк по таймауту. Повторите позже."
    return f"Error: неожиданная ошибка: {type(exc).__name__}: {exc}"


def build_gateway_tools(registry: ToolRegistry, client: GatewayClient) -> ToolRegistry:
    """Зарегистрировать три read-инструмента VK-шлюза.

    Обработчики возвращают **строку** (JSON шлюза либо ``"Error: ..."``) —
    ровно то, что отдавал прежний MCP-сервер, чтобы потребители ничего не
    потеряли.
    """
    from httpx import HTTPError, TimeoutException

    def _handle(exc: Exception) -> str:
        if isinstance(exc, TimeoutException):
            return "Error: запрос к шлюзу истёк по таймауту. Повторите позже."
        if isinstance(exc, HTTPError):
            return f"Error: сетевая ошибка обращения к шлюзу: {type(exc).__name__}."
        return handle_error(exc)

    async def vk_get_community(arguments: Dict[str, Any]) -> str:
        """Получить информацию о VK-сообществе через шлюз SARAFAN (``groups.getById``).

        Read-only. Удобная обёртка для частого кейса «что это за паблик».
        """
        try:
            group = str(arguments.get("group") or "")
            fields = str(arguments.get("fields") or "")
            return format_payload(await client.community(group, fields or None))
        except Exception as exc:  # noqa: BLE001 — единый actionable-handler
            return _handle(exc)

    async def vk_get_wall(arguments: Dict[str, Any]) -> str:
        """Получить последние посты со стены VK через шлюз SARAFAN (``wall.get``).

        Read-only, с пагинацией (count/offset).
        """
        try:
            owner_id = int(arguments.get("owner_id"))
            count = int(arguments.get("count") or 20)
            offset = int(arguments.get("offset") or 0)
            return format_payload(await client.wall(owner_id, count, offset))
        except Exception as exc:  # noqa: BLE001 — единый actionable-handler
            return _handle(exc)

    async def vk_call(arguments: Dict[str, Any]) -> str:
        """Исполнить произвольный read-метод VK API из allowlist шлюза (``POST /call``).

        Универсальная дверь для методов без удобной обёртки (users.get,
        groups.getMembers, wall.getComments, utils.resolveScreenName и т.д.).
        Запись (wall.post/messages.send/likes.add/…) шлюзом запрещена → ``Error: 400``.
        """
        try:
            method = str(arguments.get("method") or "")
            params = arguments.get("params") or {}
            return format_payload(await client.call(method, params))
        except Exception as exc:  # noqa: BLE001 — единый actionable-handler
            return _handle(exc)

    registry.add(
        "vk_get_community",
        title="VK: инфо о сообществе",
        description=(
            "Получить информацию о VK-сообществе через шлюз SARAFAN " "(groups.getById). Read-only."
        ),
        handler=vk_get_community,
        params_model=CommunityInput,
    )
    registry.add(
        "vk_get_wall",
        title="VK: посты со стены",
        description=(
            "Получить последние посты со стены VK через шлюз SARAFAN (wall.get). "
            "Read-only, с пагинацией (count/offset)."
        ),
        handler=vk_get_wall,
        params_model=WallInput,
    )
    registry.add(
        "vk_call",
        title="VK: произвольный read-метод",
        description=(
            "Исполнить произвольный read-метод VK API из allowlist шлюза (POST /call). "
            "Read-only; запись запрещена."
        ),
        handler=vk_call,
        params_model=CallInput,
    )
    return registry
