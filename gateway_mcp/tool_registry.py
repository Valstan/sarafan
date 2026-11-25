"""Реестр инструментов агента: одна схема — один вызов.

Слой нужен для каркаса «модель выдаёт задание — инструмент выполняет»
([ADR-0024](../docs/adr/0024-agентnyy-sloy-zadanie-instrument.md)). До него у
инструмента было две жизни: FastMCP-обёртка в ``gateway_mcp/server.py`` и
вызов руками в коде потребителя. Схема и сам вызов жили в разных местах, и
«описать инструмент» означало переписать текст, а «вызвать» — продублировать
разбор аргументов.

Здесь ровно три сущности:

* :class:`ToolSpec` — имя, описание, JSON Schema аргументов, обработчик;
* :class:`ToolRegistry` — реестр спецификаций плюс вызов с валидацией;
* :func:`schema_from_model` — JSON Schema из pydantic-модели параметров.

Модуль **не зависит** от пакета ``mcp`` — как и ``client.py``. Поэтому реестр
работает и тестируется в основном venv проекта, а MCP-сервер
(``server.py``) строит свои инструменты поверх того же реестра: описание
инструмента пишется один раз.

Договорённость о вызове: обработчик получает **словарь** аргументов
(уже проверенных моделью параметров, если она задана) и возвращает любое
JSON-сериализуемое значение. Ошибка инструмента — исключение; реестр
превращает его в ``{"ok": False, "error": ...}``, а не роняет агента.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError

ToolHandler = Callable[[Dict[str, Any]], Awaitable[Any]]

# Общие аннотации read-only-инструментов (шлюз только читает, идемпотентно,
# ходит во внешний сервис). Совпадают с прежними аннотациями в server.py.
READ_ANNOTATIONS: Dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def schema_from_model(model: Optional[Type[BaseModel]]) -> Dict[str, Any]:
    """JSON Schema аргументов инструмента из pydantic-модели.

    Пустая схема (``{"type": "object", "properties": {}}``) — если модели нет:
    инструмент без аргументов.
    """
    if model is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    schema = model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


@dataclass(frozen=True)
class ToolSpec:
    """Описание одного инструмента: что он делает и как его вызвать."""

    name: str
    title: str
    description: str
    input_schema: Dict[str, Any]
    handler: ToolHandler
    annotations: Dict[str, Any] = field(default_factory=lambda: dict(READ_ANNOTATIONS))
    params_model: Optional[Type[BaseModel]] = None

    def catalog_entry(self) -> Dict[str, Any]:
        """Запись для каталога инструментов (в репозитории и в промте модели)."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": dict(self.annotations),
        }


class ToolRegistry:
    """Набор инструментов агента: регистрация, каталог, вызов с валидацией."""

    def __init__(self, specs: Optional[List[ToolSpec]] = None) -> None:
        self._specs: Dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Добавить инструмент. Повторное имя — ошибка (тихо перетирать нельзя)."""
        if spec.name in self._specs:
            raise ValueError(f"инструмент уже зарегистрирован: {spec.name}")
        self._specs[spec.name] = spec
        return spec

    def add(
        self,
        name: str,
        *,
        title: str,
        description: str,
        handler: ToolHandler,
        params_model: Optional[Type[BaseModel]] = None,
        input_schema: Optional[Dict[str, Any]] = None,
        annotations: Optional[Dict[str, Any]] = None,
    ) -> ToolSpec:
        """Собрать :class:`ToolSpec` и зарегистрировать его."""
        spec = ToolSpec(
            name=name,
            title=title,
            description=description,
            input_schema=input_schema or schema_from_model(params_model),
            handler=handler,
            annotations=dict(annotations) if annotations else dict(READ_ANNOTATIONS),
            params_model=params_model,
        )
        return self.register(spec)

    def get(self, name: str) -> Optional[ToolSpec]:
        """Инструмент по имени или ``None``."""
        return self._specs.get(name)

    def names(self) -> List[str]:
        """Имена инструментов в порядке регистрации."""
        return list(self._specs)

    def catalog(self) -> List[Dict[str, Any]]:
        """Полный каталог: имя, описание, схема, аннотации."""
        return [spec.catalog_entry() for spec in self._specs.values()]

    async def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Вызвать инструмент по имени.

        Возвращает ``{"ok": True, "result": ...}`` либо
        ``{"ok": False, "error": "..."}``. Никогда не бросает: падение одного
        инструмента не должно ронять агента — агент получает текст ошибки и
        решает сам (попробовать другой инструмент или ответить).
        """
        spec = self._specs.get(name)
        if spec is None:
            return {
                "ok": False,
                "error": f"неизвестный инструмент: {name}",
                "available": self.names(),
            }

        args = dict(arguments or {})
        if spec.params_model is not None:
            try:
                validated = spec.params_model.model_validate(args)
            except ValidationError as exc:
                return {
                    "ok": False,
                    "error": "аргументы не прошли проверку схемы",
                    "detail": _format_validation_error(exc),
                }
            args = validated.model_dump()

        try:
            result = await spec.handler(args)
        except Exception as exc:  # noqa: BLE001 — падение инструмента не роняет агента
            return {
                "ok": False,
                "error": f"инструмент {name} упал: {type(exc).__name__}: {exc}",
            }
        return {"ok": True, "result": result}


def _format_validation_error(exc: ValidationError) -> str:
    """Короткий разбор ошибок pydantic — модель должна понять, что поправить."""
    parts: List[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ()))
        parts.append(f"{loc or '<корень>'}: {err.get('msg', 'неверное значение')}")
    return "; ".join(parts)
