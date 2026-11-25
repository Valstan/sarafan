"""Замер ёмкости локальной машины для агентов.

**Что именно измеряется.** Сколько заданий агента локальная машина
**одновременно** держит и сколько **в час** проводит. Измеряется не модель,
а контур: планировщик asyncio, разбор ответа, вызов инструмента, сборка
результата.

**Почему модель заменена заглушкой.** Боевой движок (DeepSeek API или
MiniMax Code CLI) отвечает по сети, и его латентность мешала бы ровно то,
что здесь измеряется: ёмкость машины. Замер с настоящей моделью отвечает на
другой вопрос («сколько аккаунтов и какой движок»), и он делается отдельно —
см. `docs/AGENT_RUNTIME.md`.

Заглушка умеет **имитировать задержку движка** (`--latency`): без неё ответ
мгновенный, и «задач в час» показывали бы скорость пустого цикла. С
задержкой, равной типичной латентности вызова, число становится сравнимым
с боевым.

**Что честно, а что нет.** Замер честен для контура и **не** говорит ни
слова о том, сколько заданий в час выдержит боевая связка «движок + пул
аккаунтов + лимиты VK». Это отдельная величина, и она измеряется на боевом
контуре.

Запуск:
    python -m gateway_mcp.bench --concurrency 1,2,4,8 --tasks 24 --latency 0.05
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from gateway_mcp.agent import AgentRunner
from gateway_mcp.model_adapters import ScriptedModel
from gateway_mcp.tool_registry import ToolRegistry

#: Ответы заглушки: сначала вызов инструмента, потом финальный ответ.
SCRIPT = (
    '{"tool": "bench_echo", "arguments": {"value": "замер"}}',
    '{"final": "замер завершён"}',
)

#: Тот же сценарий для настоящих инструментов по локальной базе.
SCRIPT_LOCAL = (
    '{"tool": "sarafan_region_stats", "arguments": {"region_code": "mi"}}',
    '{"final": "замер завершён"}',
)


def script_for(registry: ToolRegistry) -> tuple[str, ...]:
    """Сценарий заглушки под набор инструментов."""
    if "bench_echo" in registry.names():
        return SCRIPT
    return SCRIPT_LOCAL


async def _echo(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Инструмент-заглушка: возвращает аргументы обратно (имитация работы)."""
    return {"echo": arguments.get("value")}


def build_bench_registry(real_tools: bool = False) -> ToolRegistry:
    """Реестр для замера.

    По умолчанию — инструмент-заглушка: замеряется контур. ``real_tools=True``
    подключает настоящие инструменты по локальной базе Сарафана — тогда в
    замере есть и стоимость обращения к PostgreSQL.
    """
    if real_tools:
        from gateway_mcp.registry import build_registry

        return build_registry(include_gateway=False, include_local=True)

    registry = ToolRegistry()
    registry.add(
        "bench_echo",
        title="Замер: эхо",
        description="Инструмент-заглушка для замера ёмкости контура агента.",
        handler=_echo,
    )
    return registry


@dataclass
class LevelResult:
    """Результат одного уровня конкурентности."""

    concurrency: int
    tasks: int
    seconds: float
    ok: int
    failed: int
    tool_calls: int

    @property
    def per_hour(self) -> float:
        if self.seconds <= 0:
            return 0.0
        return round(self.tasks / self.seconds * 3600, 1)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "concurrency": self.concurrency,
            "tasks": self.tasks,
            "seconds": round(self.seconds, 3),
            "tasks_per_hour": self.per_hour,
            "ok": self.ok,
            "failed": self.failed,
            "tool_calls": self.tool_calls,
        }


async def run_level(
    registry: ToolRegistry,
    *,
    concurrency: int,
    tasks: int,
    latency: float,
) -> LevelResult:
    """Прогнать ``tasks`` заданий при заданной конкурентности."""
    semaphore = asyncio.Semaphore(concurrency)
    results: List[Any] = []
    script = script_for(registry)

    async def one(index: int) -> None:
        async with semaphore:
            model = ScriptedModel(script, delay=latency)
            runner = AgentRunner(registry=registry, model=model, task_name=f"bench-{index}")
            results.append(await runner.run(f"Задание №{index}: прогнать контур агента"))

    started = time.monotonic()
    await asyncio.gather(*(one(i) for i in range(tasks)))
    elapsed = time.monotonic() - started

    return LevelResult(
        concurrency=concurrency,
        tasks=tasks,
        seconds=elapsed,
        ok=sum(1 for r in results if r.ok),
        failed=sum(1 for r in results if not r.ok),
        tool_calls=sum(r.tool_calls for r in results),
    )


def machine_profile() -> Dict[str, Any]:
    """Профиль машины: ядра и память. Инфра-деталь — в репозиторий не едет."""
    info: Dict[str, Any] = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "processor": platform.processor() or "unknown",
    }
    try:
        import os

        info["cpu_count"] = os.cpu_count()
    except Exception:  # pragma: no cover — профиль не критичен
        pass
    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        info["ram_total_mb"] = round(memory.total / 1024 / 1024)
        info["ram_available_mb"] = round(memory.available / 1024 / 1024)
    except Exception:  # pragma: no cover — psutil может не быть в venv
        pass
    return info


async def main_async(args: argparse.Namespace) -> Dict[str, Any]:
    registry = build_bench_registry(real_tools=args.real_tools)
    levels: List[LevelResult] = []
    for concurrency in args.concurrency:
        levels.append(
            await run_level(
                registry,
                concurrency=concurrency,
                tasks=args.tasks,
                latency=args.latency,
            )
        )
    return {
        "machine": machine_profile(),
        "latency_seconds": args.latency,
        "tasks_per_level": args.tasks,
        "real_tools": bool(args.real_tools),
        "tools": registry.names(),
        "levels": [level.as_dict() for level in levels],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Замер ёмкости локальной машины для агентов")
    parser.add_argument(
        "--env-file",
        default="",
        help=(
            "env-файл локального стенда (например config\\local-stand.env). "
            "Нужен для инструментов по локальной базе."
        ),
    )
    parser.add_argument(
        "--concurrency",
        default="1,2,4,8",
        help="Уровни конкурентности через запятую (по умолчанию 1,2,4,8).",
    )
    parser.add_argument(
        "--tasks", type=int, default=24, help="Заданий на уровень (по умолчанию 24)."
    )
    parser.add_argument(
        "--latency",
        type=float,
        default=0.05,
        help="Имитация задержки ответа движка, секунд на вызов (по умолчанию 0.05).",
    )
    parser.add_argument("--json", action="store_true", help="Вывести только JSON.")
    parser.add_argument(
        "--real-tools",
        action="store_true",
        help="Замерять с настоящими инструментами по локальной базе (нужен DATABASE_URL).",
    )
    args = parser.parse_args()

    if args.env_file:
        # Тот же env-файл, что и у приложения: без него инструменты по
        # локальной базе не увидят DATABASE_URL (на проде переменные даёт
        # systemd, на Windows — сам процесс, см. infra/windows/local_env.py).
        import os

        os.environ["SETKA_ENV_FILES"] = args.env_file

    args.concurrency = [int(part) for part in str(args.concurrency).split(",") if part.strip()]
    report = asyncio.run(main_async(args))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    machine = report["machine"]
    print("Машина:", json.dumps(machine, ensure_ascii=False))
    print(
        f"Задержка движка (имитация): {report['latency_seconds']} с, "
        f"заданий на уровень: {report['tasks_per_level']}, инструменты: {report['tools']}"
    )
    print()
    print(
        f"{'конкурентность':>15} | {'заданий/час':>13} | {'секунд':>8} | {'ok':>4} | {'ошибок':>7}"
    )
    print("-" * 60)
    for level in report["levels"]:
        print(
            f"{level['concurrency']:>15} | {level['tasks_per_hour']:>13} | "
            f"{level['seconds']:>8} | {level['ok']:>4} | {level['failed']:>7}"
        )


if __name__ == "__main__":
    main()
