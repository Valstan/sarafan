"""Сборка реестра инструментов агента.

Одна точка, где решается, **какие инструменты доступны агенту**. Две группы:

* **шлюз VK** (``gateway_mcp.tools``) — read-only обращения к VK через шлюз
  Сарафана; образец инструмента для модели;
* **локальная база** (``gateway_mcp.local_tools``) — read-only выборки из
  базы Сарафана на этой машине.

Шлюз VK требует ``SARAFAN_GATEWAY_KEY``: без ключа его инструменты
зарегистрировать нельзя, и сборка реестра их пропускает (с пометкой в
каталоге), а не падает. Агент без шлюза всё равно работает — с локальными
инструментами.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from gateway_mcp.tool_registry import ToolRegistry


def build_registry(
    *,
    include_gateway: bool = True,
    include_local: bool = True,
    gateway_client: Optional[Any] = None,
) -> ToolRegistry:
    """Собрать реестр инструментов агента.

    ``include_gateway`` — добавить инструменты VK-шлюза (нужен ключ в
    окружении, иначе они пропускаются). ``include_local`` — добавить
    инструменты по локальной базе (импорт базы ленивый, ключ не нужен).
    """
    registry = ToolRegistry()

    if include_gateway:
        if gateway_client is None:
            if os.getenv("SARAFAN_GATEWAY_KEY"):
                from gateway_mcp.client import GatewayClient

                gateway_client = GatewayClient()
            else:
                gateway_client = None
        if gateway_client is not None:
            from gateway_mcp.tools import build_gateway_tools

            build_gateway_tools(registry, gateway_client)

    if include_local:
        from gateway_mcp.local_tools import build_local_tools

        build_local_tools(registry)

    return registry


def registry_catalog(registry: ToolRegistry) -> List[Dict[str, Any]]:
    """Каталог инструментов реестра — то, что видит модель и что лежит в доках."""
    return registry.catalog()


__all__ = ["build_registry", "registry_catalog"]
