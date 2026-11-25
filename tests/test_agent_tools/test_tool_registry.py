"""Реестр инструментов: схема, валидация аргументов, вызов.

Каждый инструмент обязан иметь **схему** (иначе модель не знает, что
передавать) и **вызов** (иначе схема — украшение). Реестр проверяет оба.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from pydantic import BaseModel, ConfigDict, Field

from gateway_mcp.tool_registry import ToolRegistry, schema_from_model


class EchoParams(BaseModel):
    """Параметры тестового инструмента."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(..., description="Что вернуть.", min_length=1)
    times: int = Field(default=1, description="Сколько раз повторить.", ge=1, le=10)


@pytest.fixture
def echo_calls() -> List[Dict[str, Any]]:
    """Счётчик вызовов обработчика — им проверяем, что вызов состоялся."""
    return []


@pytest.fixture
def registry_with_echo(echo_calls: List[Dict[str, Any]]) -> ToolRegistry:
    async def echo(arguments: Dict[str, Any]) -> Dict[str, Any]:
        echo_calls.append(arguments)
        return {"echo": arguments.get("value"), "times": arguments.get("times")}

    registry = ToolRegistry()
    registry.add(
        "echo",
        title="Эхо",
        description="Возвращает переданное значение.",
        handler=echo,
        params_model=EchoParams,
    )
    return registry


def test_catalog_carries_schema_and_description(registry_with_echo: ToolRegistry) -> None:
    """У инструмента есть имя, описание и JSON Schema аргументов."""
    catalog = registry_with_echo.catalog()
    assert len(catalog) == 1
    entry = catalog[0]
    assert entry["name"] == "echo"
    assert entry["description"]
    schema = entry["input_schema"]
    assert schema["type"] == "object"
    assert "value" in schema["properties"]
    assert schema["required"] == ["value"]


async def test_call_executes_handler(
    registry_with_echo: ToolRegistry,
    echo_calls: List[Dict[str, Any]],
) -> None:
    """Вызов инструмента доходит до обработчика и возвращает его результат."""
    payload = await registry_with_echo.call("echo", {"value": "привет"})
    assert payload["ok"] is True
    assert payload["result"] == {"echo": "привет", "times": 1}
    assert echo_calls == [{"value": "привет", "times": 1}]


async def test_call_rejects_arguments_outside_schema(registry_with_echo: ToolRegistry) -> None:
    """Аргументы вне схемы отклоняются — и понятно, что именно не так."""
    payload = await registry_with_echo.call("echo", {"value": ""})
    assert payload["ok"] is False
    assert "схем" in payload["error"]

    payload = await registry_with_echo.call("echo", {"value": "x", "times": 99})
    assert payload["ok"] is False


async def test_unknown_tool_is_reported_not_raised(registry_with_echo: ToolRegistry) -> None:
    """Неизвестный инструмент — ответ с перечнем доступных, а не исключение."""
    payload = await registry_with_echo.call("нет_такого", {})
    assert payload["ok"] is False
    assert payload["available"] == ["echo"]


async def test_failing_tool_does_not_break_the_call(registry_with_echo: ToolRegistry) -> None:
    """Падение инструмента — ответ с ошибкой, а не исключение наружу."""

    async def boom(arguments: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError("инструмент сломался")

    registry_with_echo.add(
        "boom",
        title="Ломается",
        description="Инструмент, который падает.",
        handler=boom,
    )
    payload = await registry_with_echo.call("boom", {})
    assert payload["ok"] is False
    assert "инструмент сломался" in payload["error"]


def test_duplicate_name_is_rejected(registry_with_echo: ToolRegistry) -> None:
    """Повторное имя — ошибка: молча перетирать описание инструмента нельзя."""

    async def noop(arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    with pytest.raises(ValueError):
        registry_with_echo.add("echo", title="Ещё раз", description="Дубль", handler=noop)


def test_schema_from_model_without_model() -> None:
    """Инструмент без аргументов получает пустую объектную схему."""
    schema = schema_from_model(None)
    assert schema["type"] == "object"
    assert schema["properties"] == {}
