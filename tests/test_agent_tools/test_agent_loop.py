"""Приёмка каркаса «модель выдаёт задание — инструмент выполняет».

Главный тест промта: **агент получает задание, вызывает инструмент и
возвращает результат в конвейер**. Модель — заглушка с заготовленными
ответами, инструмент — счётчик вызовов: сеть и деньги не нужны, поведение
проверяется целиком.

См. [ADR-0024](../../../docs/adr/0024-agentnyy-sloy-zadanie-instrument.md).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from gateway_mcp.agent import AgentRunner, extract_json_object
from gateway_mcp.model_adapters import FailingModel, ScriptedModel
from gateway_mcp.tool_registry import ToolRegistry

#: Ответы заглушки: сначала вызов инструмента, затем финальный ответ.
SCRIPT = (
    '{"tool": "sarafan_region_stats", "arguments": {"region_code": "mi"}}',
    '{"final": "Регион mi: 1204 поста, 40 сообществ"}',
)


@pytest.fixture
def calls() -> List[Dict[str, Any]]:
    return []


@pytest.fixture
def registry(calls: List[Dict[str, Any]]) -> ToolRegistry:
    async def region_stats(arguments: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(arguments)
        return {"region_code": arguments.get("region_code"), "posts_total": 1204}

    reg = ToolRegistry()
    reg.add(
        "sarafan_region_stats",
        title="Сарафан: счётчики региона",
        description="Счётчики региона из локальной базы.",
        handler=region_stats,
    )
    return reg


async def test_agent_receives_task_calls_tool_and_returns_result(
    registry: ToolRegistry,
    calls: List[Dict[str, Any]],
) -> None:
    """Приёмка: задание → вызов инструмента → результат на руках."""
    model = ScriptedModel(SCRIPT)
    runner = AgentRunner(registry=registry, model=model, task_name="test")

    result = await runner.run("Собери сводку по региону mi")

    # 1. инструмент вызван — с аргументами из задания модели
    assert calls == [{"region_code": "mi"}]
    # 2. результат инструмента вернулся в конвейер
    assert result.ok is True
    assert result.output == "Регион mi: 1204 поста, 40 сообществ"
    assert result.tool_calls == 1
    assert result.reason == "final"
    # 3. шаги записаны: вызов инструмента и финальный ответ
    actions = [step.action for step in result.steps]
    assert actions == ["tool", "final"]
    assert result.steps[0].tool == "sarafan_region_stats"
    assert result.steps[0].ok is True


async def test_tool_result_is_visible_to_the_model(
    registry: ToolRegistry,
    calls: List[Dict[str, Any]],
) -> None:
    """Результат инструмента уходит обратно в промт — иначе модель его не видела."""
    model = ScriptedModel(SCRIPT)
    runner = AgentRunner(registry=registry, model=model, task_name="test")

    await runner.run("Собери сводку по региону mi")

    assert len(model.calls) == 2
    second_prompt = model.calls[1]["user"]
    assert "1204" in second_prompt
    assert "sarafan_region_stats" in second_prompt


async def test_result_is_serialisable_for_the_pipeline(
    registry: ToolRegistry,
    calls: List[Dict[str, Any]],
) -> None:
    """Результат возвращается в конвейер как словарь, а не как текст."""
    import json

    model = ScriptedModel(SCRIPT)
    runner = AgentRunner(registry=registry, model=model, task_name="test")

    payload = (await runner.run("Собери сводку по региону mi")).to_dict()

    assert payload["ok"] is True
    assert payload["tool_calls"] == 1
    assert payload["reason"] == "final"
    assert isinstance(payload["steps"], list)
    # В Celery-задачу это уходит как JSON — значит, должно сериализоваться.
    assert json.loads(json.dumps(payload, ensure_ascii=False))["output"]


async def test_failed_tool_is_reported_and_agent_continues(
    registry: ToolRegistry,
    calls: List[Dict[str, Any]],
) -> None:
    """Упавший инструмент не роняет агента: модель видит ошибку и решает сама."""

    async def boom(arguments: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError("база недоступна")

    registry.add("boom", title="Ломается", description="Падает.", handler=boom)

    model = ScriptedModel(
        (
            '{"tool": "boom", "arguments": {}}',
            '{"final": "Не смог: база недоступна"}',
        )
    )
    runner = AgentRunner(registry=registry, model=model, task_name="test")

    result = await runner.run("Что угодно")

    assert result.ok is True
    assert result.output == "Не смог: база недоступна"
    assert result.steps[0].ok is False


async def test_engine_failure_returns_reason_not_exception(
    registry: ToolRegistry,
    calls: List[Dict[str, Any]],
) -> None:
    """Отказ движка — честная причина в результате, а не исключение."""
    runner = AgentRunner(
        registry=registry,
        model=FailingModel(reason="no_api_key"),
        task_name="test",
    )

    result = await runner.run("Что угодно")

    assert result.ok is False
    assert result.reason == "no_api_key"
    assert result.tool_calls == 0


async def test_empty_task_is_rejected(registry: ToolRegistry) -> None:
    """Пустое задание — отказ без обращения к модели."""
    runner = AgentRunner(registry=registry, model=ScriptedModel(SCRIPT), task_name="test")

    result = await runner.run("   ")

    assert result.ok is False
    assert result.reason == "empty_task"


async def test_max_steps_stops_the_loop(registry: ToolRegistry) -> None:
    """Модель, которая только зовёт инструменты, не крутит контур вечно."""
    model = ScriptedModel(('{"tool": "sarafan_region_stats", "arguments": {}}',))
    runner = AgentRunner(registry=registry, model=model, task_name="test", max_steps=3)

    result = await runner.run("Зациклись")

    assert result.ok is False
    assert result.reason == "max_steps"
    assert result.tool_calls == 3


def test_extract_json_object_handles_code_fences() -> None:
    """Модели оборачивают JSON в блок кода — разбор должен это пережить."""
    text = 'Вот ответ:\n```json\n{"final": "готово"}\n```'
    assert extract_json_object(text) == {"final": "готово"}


def test_extract_json_object_returns_none_on_garbage() -> None:
    """Нет JSON — нет действия: агент обязан это распознать."""
    assert extract_json_object("просто текст") is None
    assert extract_json_object("") is None
