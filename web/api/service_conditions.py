"""HTTP-дверь слоя условий услуг (``/api/service-conditions``).

Пилот единого профиля жителя (#386), шаг 1: карточка услуги отдаёт условия,
документы и владельца — **вместе со ссылкой на источник**.

Почему публично и без ключа. Ответ не содержит ничего приватного: условия
услуг, документы, владелец и ссылка на публичную страницу, где это написано.
Читать его должны серверы (пилот: Казанская спрашивает наш слой условий по
конкретному вопросу), у которых сессионной cookie нет и не будет. Узкая
поверхность — один точный путь в ``PUBLIC_EXACT`` (не префикс — см. комментарий
в ``middleware/auth_gate.py`` про почему публичные данные лежат там).

Читаемость важнее скрытности: человек, которому ответили «положено вот это»,
должен сам открыть источник и проверить. Условие без проверяемой ссылки —
это не условие, а обещание.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from modules.service_conditions import (
    CONTRACT_VERSION,
    SOURCE_LABEL,
    answer,
    build_service_cards,
    get_conditions,
    known_services,
)

router = APIRouter()


@router.get("/service-conditions")
async def service_conditions() -> dict:
    """Карточки услуг каталога с условиями, документами, владельцем и источником.

    Отдаёт **текущую** правду: карточки читаются из живого конфига каталога, а
    не из снимка, поэтому добавленное условие видно сразу после выката.
    """
    return {
        "version": CONTRACT_VERSION,
        "source": SOURCE_LABEL,
        "services": build_service_cards(),
    }


@router.get("/service-conditions/{service_title}")
async def service_conditions_for(service_title: str) -> dict:
    """Условия одной услуги — по названию из каталога.

    404, если такой услуги в каталоге нет: неизвестное название — это не
    «пустой список условий», а ошибка запроса, и молчаливый 200 с пустым
    списком выглядел бы как «условий нет» (то есть как выдумка — их не
    существует).
    """
    from config.services_catalog import SERVICES

    titles = {s["title"] for s in SERVICES}
    if service_title not in titles:
        raise HTTPException(status_code=404, detail="услуга не найдена в каталоге")
    return {
        "version": CONTRACT_VERSION,
        "source": SOURCE_LABEL,
        "service": service_title,
        "conditions": get_conditions(service_title),
    }


@router.get("/service-answer")
async def service_answer(
    service: str = Query(..., min_length=1, description="Название услуги из каталога"),
    question: str = Query("", description="Вопрос жителя"),
) -> dict:
    """Ответ по условиям услуги — в форме контракта ответа пилота.

    Каждое утверждение несёт ``source`` (вид источника и ссылку). Если по
    вопросу нет ни условия, ни факта — ``answer`` пуст, а ``missing`` и
    ``owner`` говорят, что спросить и у кого (правило 3 контракта: «не знаю» —
    валидный ответ, а не тишина).
    """
    from config.services_catalog import SERVICES

    titles = {s["title"] for s in SERVICES}
    if service not in titles:
        raise HTTPException(status_code=404, detail="услуга не найдена в каталоге")
    result = answer(service, question)
    result["services_with_conditions"] = known_services()
    return result
