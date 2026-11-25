"""Адаптеры движков под единый контракт агента.

Агенту нужен один способ спросить модель. Боевых движков в проекте два:
``modules/deepseek_client`` (платный API) и ``modules/minimax_engine`` (пул
аккаунтов MiniMax Code). Оба уже отдают **один и тот же** словарь
``{"ok": True, "content": ...}`` либо ``{"ok": False, "reason": ...}`` —
поэтому адаптер тут тонкий: он приводит вызов к ``await complete(...)``.

Третий адаптер — :class:`ScriptedModel`: детерминированная модель с
заготовленными ответами. Она нужна двум вещам:

* тестам контура «задание → инструмент → результат» — без сети и без денег;
* замеру ёмкости локальной машины (сколько задач одновременно и сколько в
  час) — там, где платный API исказил бы замер чистой латентностью сети.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence

# Контракт модели: один метод, один словарь.
# ``content`` — текст ответа модели; агент сам разбирает, что это за действие.


class ModelError(Exception):
    """Движок не ответил пригодным для агента ответом."""


class DeepSeekModel:
    """Адаптер боевого платного движка (``modules.deepseek_client``)."""

    name = "deepseek"

    def __init__(self, *, label: str = "agent", max_tokens: Optional[int] = None) -> None:
        self.label = label
        self.max_tokens = max_tokens

    async def complete(self, *, system: str, user: str) -> Dict[str, Any]:
        from modules import deepseek_client

        result = await asyncio.to_thread(
            deepseek_client.chat,
            user=user,
            system=system,
            label=self.label,
            max_tokens=self.max_tokens,
        )
        return result


class MinimaxModel:
    """Адаптер контура MiniMax Code (``modules.minimax_engine``)."""

    name = "minimax"

    def __init__(self, *, timeout: Optional[float] = None, task: str = "") -> None:
        self.timeout = timeout
        self.task = task

    async def complete(self, *, system: str, user: str) -> Dict[str, Any]:
        from modules import minimax_engine

        result = await asyncio.to_thread(
            minimax_engine.chat,
            user=user,
            system=system,
            timeout=self.timeout,
            task=self.task,
        )
        return result


class ScriptedModel:
    """Модель с заготовленными ответами — для тестов и замера ёмкости.

    ``responses`` — список ответов, которые выдаются по очереди; последний
    повторяется, когда список исчерпан. Ответ-строка уходит модели как есть,
    поэтому в неё можно положить JSON действия агента.
    """

    name = "scripted"

    def __init__(self, responses: Sequence[str], *, delay: float = 0.0) -> None:
        self._responses: List[str] = list(responses)
        self._delay = delay
        self._index = 0
        self.calls: List[Dict[str, str]] = []

    async def complete(self, *, system: str, user: str) -> Dict[str, Any]:
        self.calls.append({"system": system, "user": user})
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._responses:
            return {"ok": False, "reason": "no_scripted_responses"}
        index = min(self._index, len(self._responses) - 1)
        self._index += 1
        return {"ok": True, "content": self._responses[index]}


class FailingModel:
    """Модель, которая всегда отказывает — для проверки ветки отказа."""

    name = "failing"

    def __init__(self, reason: str = "engine_failed") -> None:
        self.reason = reason

    async def complete(self, *, system: str, user: str) -> Dict[str, Any]:
        return {"ok": False, "reason": self.reason}


def build_model(kind: str, **kwargs: Any) -> Any:
    """Собрать модель по имени вида (``deepseek`` / ``minimax`` / ``scripted``)."""
    normalized = (kind or "").strip().lower()
    if normalized == "deepseek":
        return DeepSeekModel(**kwargs)
    if normalized == "minimax":
        return MinimaxModel(**kwargs)
    if normalized == "scripted":
        return ScriptedModel(**kwargs)
    raise ValueError(f"неизвестный вид движка: {kind}")
