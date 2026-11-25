"""Агентный контур: модель ставит задачу — инструмент выполняет.

Цикл один и явный ([ADR-0024](../docs/adr/0024-agentnyy-sloy-zadanie-instrument.md)):

1. агенту дают **задание** и **каталог инструментов** (схемы аргументов);
2. модель отвечает **одним** JSON-объектом — либо
   ``{"tool": "<имя>", "arguments": {...}}``, либо ``{"final": "<текст>"}``;
3. реестр выполняет инструмент, результат уходит обратно в промт модели;
4. модель завершает работу финальным ответом.

**Почему JSON, а не свободный текст.** Модель обязана сказать, что именно вызвать:
иначе «агент позвал инструмент» остаётся фразой в промте, которую нельзя
проверить. Здесь выбор машиночитаем, поэтому результат можно вернуть в
конвейер и посчитать.

**Контракт с конвейером** — :class:`AgentResult`. Он умеет вернуться из
Celery-задачи как есть (``ok`` / ``output`` / ``steps`` / ``tool_calls``),
без разбора текста на стороне вызывающего.

Модуль не знает ни про сеть, ни про VK, ни про базу: инструменты приходят
снаружи реестром. Поэтому контур тестируется без сети и без денег.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from gateway_mcp.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)

#: Системный промт агента: протокол ответа + каталог инструментов.
SYSTEM_TEMPLATE = """\
Ты — агент САРАФАН. Тебе дали задание и список инструментов.

Отвечай РОВНО ОДНИМ JSON-объектом, без пояснений и без обрамления:
- чтобы вызвать инструмент: {"tool": "<имя инструмента>", "arguments": { ... }}
- чтобы закончить работу: {"final": "<итоговый ответ заданию>"}

Правила:
- Один JSON-объект на ответ. Ничего кроме него не выводи.
- Аргументы вызываемого инструмента — строго по его схеме input_schema.
- Если инструмента не хватает или он не даёт ответа — верни {"final": "..."}
  с честным объяснением, чего не хватило.
- Не выдумывай инструменты: в списке только перечисленные.

Инструменты:
{tools}
"""

#: Кусок JSON-объекта из ответа модели. Модели любят обрамлять JSON в
#: ```-блоки и добавлять текст вокруг — вырезаем первый сбалансированный объект.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class AgentStep:
    """Один шаг работы агента: действие модели и его исход."""

    index: int
    action: str  # "tool" | "final"
    tool: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    output: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class AgentResult:
    """Итог работы агента — то, что возвращается в конвейер."""

    ok: bool
    output: str = ""
    steps: List[AgentStep] = field(default_factory=list)
    tool_calls: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "tool_calls": self.tool_calls,
            "reason": self.reason,
            "steps": [step.to_dict() for step in self.steps],
        }


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Достать из ответа модели первый JSON-объект.

    Сначала пробуем разобрать текст как есть; если не вышло — ищем объект
    регуляркой (модель часто оборачивает JSON в ````-блок или добавляет
    вокруг пояснение). Возвращаем ``None``, если объекта нет.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _render_tool_result(name: str, payload: Dict[str, Any]) -> str:
    """Текст, который агент видит как результат вызова инструмента."""
    if payload.get("ok"):
        result = payload.get("result")
        if isinstance(result, str):
            body = result
        else:
            body = json.dumps(result, ensure_ascii=False, default=str)
        return f"Инструмент {name} вернул:\n{body}"
    error = str(payload.get("error") or "неизвестная ошибка")
    detail = payload.get("detail")
    tail = f" ({detail})" if detail else ""
    return f"Инструмент {name} не сработал: {error}{tail}"


class AgentRunner:
    """Гоняет агента: задание → модель → инструмент → модель → результат."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        model: Any,
        max_steps: int = 4,
        task_name: str = "",
    ) -> None:
        self.registry = registry
        self.model = model
        self.max_steps = max(1, int(max_steps))
        self.task_name = task_name

    def system_prompt(self) -> str:
        """Системный промт: протокол ответа + каталог инструментов."""
        catalog = json.dumps(self.registry.catalog(), ensure_ascii=False, indent=2)
        # Подстановка через replace, а не format: в шаблоне есть JSON-скобки
        # ({"tool": ...}), и format() принял бы их за поля подстановки.
        return SYSTEM_TEMPLATE.replace("{tools}", catalog)

    async def run(self, task: str, *, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        """Выполнить задание и вернуть результат.

        ``task`` — текст задания. ``context`` — необязательные данные
        (аргументы задачи, идентификатор региона и т.п.), которые уходят в
        промт модели вместе с заданием.
        """
        if not (task or "").strip():
            return AgentResult(ok=False, reason="empty_task")

        steps: List[AgentStep] = []
        transcript: List[str] = []
        tool_calls = 0

        for index in range(self.max_steps):
            user_prompt = self._build_user_prompt(task, context, transcript)
            answer = await self.model.complete(system=self.system_prompt(), user=user_prompt)
            if not isinstance(answer, dict) or not answer.get("ok"):
                reason = "model_failed"
                if isinstance(answer, dict):
                    reason = str(answer.get("reason") or reason)
                steps.append(AgentStep(index=index, action="model", ok=False, error=reason))
                return AgentResult(
                    ok=False,
                    output="",
                    steps=steps,
                    tool_calls=tool_calls,
                    reason=reason,
                )

            content = str(answer.get("content") or "")
            decision = extract_json_object(content)
            if decision is None:
                steps.append(
                    AgentStep(
                        index=index,
                        action="model",
                        ok=False,
                        output=content,
                        error="no_json_action",
                    )
                )
                return AgentResult(
                    ok=False,
                    output=content,
                    steps=steps,
                    tool_calls=tool_calls,
                    reason="no_json_action",
                )

            if "tool" in decision:
                tool_name = str(decision.get("tool") or "")
                arguments = decision.get("arguments") or {}
                if not isinstance(arguments, dict):
                    arguments = {}
                payload = await self.registry.call(tool_name, arguments)
                tool_calls += 1
                transcript.append(_render_tool_result(tool_name, payload))
                steps.append(
                    AgentStep(
                        index=index,
                        action="tool",
                        tool=tool_name,
                        arguments=arguments,
                        ok=bool(payload.get("ok")),
                        output="" if payload.get("ok") else str(payload.get("error") or ""),
                    )
                )
                if not payload.get("ok"):
                    logger.warning(
                        "агент %s: инструмент %s не сработал: %s",
                        self.task_name or "-",
                        tool_name,
                        payload.get("error"),
                    )
                continue

            if "final" in decision:
                final_text = str(decision.get("final") or "")
                steps.append(AgentStep(index=index, action="final", output=final_text))
                return AgentResult(
                    ok=True,
                    output=final_text,
                    steps=steps,
                    tool_calls=tool_calls,
                    reason="final",
                )

            steps.append(
                AgentStep(
                    index=index,
                    action="model",
                    ok=False,
                    output=content,
                    error="unknown_action",
                )
            )
            return AgentResult(
                ok=False,
                output=content,
                steps=steps,
                tool_calls=tool_calls,
                reason="unknown_action",
            )

        return AgentResult(
            ok=False,
            output="",
            steps=steps,
            tool_calls=tool_calls,
            reason="max_steps",
        )

    @staticmethod
    def _build_user_prompt(
        task: str,
        context: Optional[Dict[str, Any]],
        transcript: List[str],
    ) -> str:
        """Промт пользователя: задание, контекст и результаты прошлых вызовов."""
        blocks: List[str] = [f"ЗАДАНИЕ:\n{task}"]
        if context:
            blocks.append(
                "КОНТЕКСТ:\n" + json.dumps(context, ensure_ascii=False, indent=2, default=str)
            )
        if transcript:
            blocks.append("РЕЗУЛЬТАТЫ ВЫЗОВОВ:\n" + "\n\n".join(transcript))
        return "\n\n".join(blocks)
