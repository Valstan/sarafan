"""Профили агентов: отдельная задача — отдельная очередь — отдельный слот.

**Проблема, которую это решает.** На проде волна и периодика делят один
воркер и его слоты (ADR-0020, ADR-0022): длинная задача волны съедает слот,
и мелкая периодика ждёт в очереди, протухая по ``expires``. Владелец хочет
на локальной машине наоборот — «под каждую задачу свой агент».

**Как это ложится на механику.** Разделение задач идёт **по очереди Celery**:
у каждого профиля своя очередь, у каждой очереди — свой процесс воркера
(``--queues=<имя>``). Один воркер — один слот, и его блокирует только то,
что лежит в его очереди.

**Пул аккаунтов.** Каждый воркер сам берёт аккаунт из пула
(``modules/karman_minimax`` — аренда у Кармана, ``modules/minimax_engine`` —
собственный пул с ротацией). Отдельный процесс воркера означает отдельную
аренду: один агент, съевший лимит аккаунта, не валит остальных.

**Локальные модели.** Профиль — это ещё и выбор движка (``engine``). Если
владелец решит крутить локальные модели вместо внешнего API, меняется поле
движка у профиля, а не код конвейера: остальное (очередь, инструменты,
лимит шагов) остаётся прежним.

Маршрутизация задачи в очередь делается **явно** при вызове
(``apply_async(queue=...)``), а не через ``task_routes``: так переезд не
зависит от правки боевой конфигурации очередей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Очереди агентов. Имена не совпадают с боевыми (`celery`, `wave`, `digest`):
# локальный стенд поднимается рядом с боевой раскладкой, и одинаковые имена
# означали бы, что локальный агент может забрать боевую задачу.
QUEUE_AGENT = "agent"
QUEUE_AGENT_WAVE = "agent_wave"
QUEUE_AGENT_DISCOVERY = "agent_discovery"


@dataclass(frozen=True)
class AgentProfile:
    """Описание агента: чем он занимается и на чьём движке работает."""

    name: str
    queue: str
    engine: str
    max_steps: int = 4
    description: str = ""
    tools: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "queue": self.queue,
            "engine": self.engine,
            "max_steps": self.max_steps,
            "description": self.description,
            "tools": list(self.tools),
        }


#: Профили по умолчанию. Профиль выбирается именем в задании.
AGENT_PROFILES: Dict[str, AgentProfile] = {
    "generic": AgentProfile(
        name="generic",
        queue=QUEUE_AGENT,
        engine="deepseek",
        max_steps=4,
        description="Универсальный агент: задание из очереди, инструменты по каталогу.",
    ),
    "wave": AgentProfile(
        name="wave",
        queue=QUEUE_AGENT_WAVE,
        engine="minimax",
        max_steps=6,
        description=(
            "Агент волны: длинные задания (веер регионов, публикационные слоты). "
            "Движок — MiniMax Code CLI, лимит шагов выше."
        ),
    ),
    "discovery": AgentProfile(
        name="discovery",
        queue=QUEUE_AGENT_DISCOVERY,
        engine="minimax",
        max_steps=6,
        description=(
            "Агент подбора сообществ: карточки на входе, вердикт по каждой. "
            "Партия вердиктов — один вызов движка на N карточек (ADR-0015)."
        ),
    ),
}


def get_profile(name: str) -> AgentProfile:
    """Профиль по имени. Неизвестное имя — понятная ошибка, а не тихий дефолт."""
    key = (name or "").strip().lower() or "generic"
    profile = AGENT_PROFILES.get(key)
    if profile is None:
        known = ", ".join(sorted(AGENT_PROFILES))
        raise KeyError(f"неизвестный профиль агента: {name}. Известные: {known}")
    return profile


def profiles_catalog() -> List[Dict[str, Any]]:
    """Каталог профилей — для промта модели и для документации."""
    return [profile.as_dict() for profile in AGENT_PROFILES.values()]


def build_runner(
    profile: AgentProfile,
    *,
    registry: Any,
    model: Optional[Any] = None,
) -> Any:
    """Собрать :class:`~gateway_mcp.agent.AgentRunner` под профиль.

    Модель по умолчанию — по профилю (движок профиля). Явно переданная
    модель всегда выигрывает: так замер ёмкости и тесты не зависят от
    боевого движка.
    """
    from gateway_mcp.agent import AgentRunner
    from gateway_mcp.model_adapters import build_model

    chosen = model if model is not None else build_model(profile.engine)
    return AgentRunner(
        registry=registry,
        model=chosen,
        max_steps=profile.max_steps,
        task_name=profile.name,
    )


__all__ = [
    "AGENT_PROFILES",
    "QUEUE_AGENT",
    "QUEUE_AGENT_DISCOVERY",
    "QUEUE_AGENT_WAVE",
    "AgentProfile",
    "build_runner",
    "get_profile",
    "profiles_catalog",
]
