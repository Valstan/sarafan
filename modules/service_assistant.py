"""Ассистент единого профиля жителя (пилот #386, шаг 3).

**Что это.** Слой условий услуг (`modules/service_conditions.py`, шаг 1) отвечает
на вопрос «что есть». Ассистент отвечает на вопрос **«что мне положено и что
для этого нужно»** — и отвечает по двум источникам: условия услуги (её
владелец опубликовал их дословно) и факт о самом жителе (его назвал он сам).

**Три правила, из которых всё собрано** (план пилота, «Контракт ответа»):

1. **Нет источника — нет утверждения.** Каждое утверждение ответа несёт
   ``source`` (вид источника и ссылку). Условие — это ссылка на публичную
   страницу владельца услуги; факт — это ссылка на сервис-владелец и ключ
   факта. Утверждения без источника в ответ не попадают **никогда**.
2. **Самопровозглашённый факт помечается «по вашим данным»** и не даёт
   обещаний прав: факт жителя — это его заявление, а не подтверждение.
3. **Нет данных ⇒ «не знаю, вот кто отвечает» + заявка**, а не тишина и не
   выдумка. Пустой ``answer`` — законный ответ, а не поломка.

**Про факты — главное.** Факты **не копируются**. Здесь нет ни таблицы
фактов, ни кэша, ни зеркала профиля: ассистент делает **один запрос к
сервису-владельцу по конкретному вопросу** (`modules/profile_facts.py`) и
использует ответ только в этом ответе. Два сервиса не хранят две копии
одного факта — иначе «спросили один раз» рассыплется на второй копии.

**Почему движок не нужен, чтобы ответить.** Подбор услуги и условия здесь
детерминированный: вопрос сопоставляется с текстом условия и его полем
``if`` («Хочу вступить в кружок, студию или секцию»). Это сделано
**намеренно**: план пилота требует «цитата обязательна», а приёмка требует
«выдумка = провал». Приблизительный движковый матчинг здесь дал бы ответ на
чуждой вопрос — ровно тот класс отказа, который ловит приёмка. Если матчинг
неоднозначен или слаб, ассистент отвечает «не знаю» и перечисляет, что
спросить, — вместо того чтобы угадать.

**Про ПДн.** Значение факта попадает в ответ жителю (это его собственный
факт, и он сам его назвал), но **не в журнал**: в лог уходят ключ факта и
статус.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from modules.profile_facts import (
    STATUS_NO_SUBJECT,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    STATUS_UNCONFIGURED,
    facts_enabled,
    fetch_fact,
)
from modules.service_conditions import (
    CONDITIONS,
    CONTRACT_VERSION,
    SERVICES_WITHOUT_CONDITIONS,
    SOURCE_LABEL,
)

logger = logging.getLogger(__name__)

#: Владелец, когда условия нет ни у одной услуги: спросить есть кого.
DEFAULT_OWNER = "Кабинет САРАФАН — +7 922 900-59-10"

#: Слова-стопы: слишком частые, чтобы что-то различать. Без них «как записаться
#: в кружок» и «как записаться в кружок или студию» сошлись бы по трём словам
#: из трёх — а различать надо именно содержательные.
STOP_WORDS = frozenset(
    {
        "и",
        "в",
        "во",
        "не",
        "что",
        "он",
        "на",
        "я",
        "с",
        "со",
        "как",
        "а",
        "то",
        "все",
        "она",
        "так",
        "его",
        "но",
        "да",
        "ты",
        "к",
        "у",
        "же",
        "вы",
        "за",
        "бы",
        "по",
        "только",
        "ее",
        "мне",
        "было",
        "вот",
        "от",
        "меня",
        "еще",
        "нет",
        "о",
        "из",
        "ему",
        "теперь",
        "когда",
        "даже",
        "ну",
        "ли",
        "если",
        "уже",
        "или",
        "ни",
        "быть",
        "был",
        "него",
        "до",
        "вас",
        "нибудь",
        "опять",
        "уж",
        "вам",
        "ведь",
        "там",
        "потом",
        "себя",
        "ничего",
        "ей",
        "может",
        "они",
        "тут",
        "где",
        "есть",
        "надо",
        "ней",
        "для",
        "мы",
        "тебя",
        "их",
        "чем",
        "была",
        "сам",
        "чтоб",
        "без",
        "будто",
        "чего",
        "раз",
        "тоже",
        "себе",
        "под",
        "будет",
        "тогда",
        "кто",
        "этот",
        "того",
        "потому",
        "этого",
        "какой",
        "совсем",
        "ним",
        "здесь",
        "этом",
        "один",
        "почти",
        "мой",
        "тем",
        "чтобы",
        "нее",
        "сейчас",
        "были",
        "куда",
        "зачем",
        "всех",
        "никогда",
        "можно",
        "при",
        "наконец",
        "два",
        "об",
        "другой",
        "хоть",
        "после",
        "над",
        "больше",
        "тот",
        "через",
        "эти",
        "нас",
        "про",
        "всего",
        "них",
        "какая",
        "много",
        "разве",
        "три",
        "эту",
        "моя",
        "впрочем",
        "хорошо",
        "свою",
        "этой",
        "перед",
        "иногда",
        "лучше",
        "чуть",
        "том",
        "нельзя",
        "такой",
        "им",
        "более",
        "всегда",
        "конечно",
        "всю",
        "между",
        "еще",
        "который",
    }
)

#: Минимум осмысленных слов в вопросе. Одно слово («скидка») не различает
#: условия — а ответ на чужой вопрос хуже, чем «не знаю».
MIN_QUERY_TOKENS = 2

#: Доля слов вопроса, которую должен покрыть текст условия. Порог высокий
#: намеренно: лучше «не знаю» + заявка, чем правдоподобная выдумка.
MIN_MATCH_SCORE = 0.6

#: Насколько лучший кандидат должен быть лучше второго. Ближе — значит
#: неоднозначно, и ассистент перечисляет оба, а не выбирает.
AMBIGUITY_MARGIN = 0.15

#: Словарь фактов ассистента. План пилота прямо требует его у нас: «Новый факт =
#: новая строка в реестре + миграция словаря у ассистента». Словарь Казанской
#: (``web/src/lib/facts.ts``) — источник истины по ЗНАЧЕНИЯМ, наш — по тому, как
#: мы их называем в ответе жителю.
#:
#: ``intents`` — по каким словам вопрос жителя признан вопросом **об этом
#: факте**. Список узкий намеренно: факт должен быть ОТВЕТОМ на вопрос, а не
#: приложением к нему. На вопрос «положена ли мне субсидия» факт «льготная
#: категория» ответом не является — там нужен владелец услуги, и ассистент
#: честно отвечает «не знаю, вот кто отвечает».
FACT_SPECS: Dict[str, Dict[str, Any]] = {
    "benefit_category": {
        "label": "Льготная категория",
        "question": "Есть ли у вас льготная категория?",
        "intents": (
            "льготная категория",
            "какая у меня льгота",
            "есть ли у меня льгота",
            "я льготник",
        ),
        "values": {
            "none": "Нет",
            "veteran": "Ветеран",
            "disability": "Инвалидность",
            "large_family": "Многодетная семья",
            "low_income": "Малоимущий / нуждающийся",
        },
    },
    "children_count": {
        "label": "Детей до 18 лет",
        "question": "Сколько у вас детей до 18 лет?",
        "intents": (
            "сколько у меня детей",
            "сколько у вас детей",
            "детей до 18",
            "детей до 18 лет",
        ),
        "values": {},
    },
}

#: Как факт выглядит в ответе: вид источника и ссылка. Ссылка — ключ, а не
#: значение: «kazanskaya/benefit_category» говорит, ЧТО спросили, и не
#: раздаёт содержимое профиля.
FACT_SOURCE_KIND = "profile_fact"
FACT_SOURCE_PREFIX = "kazanskaya"


@dataclass(frozen=True)
class Candidate:
    """Условие, которое может ответить на вопрос."""

    service: str
    question: str
    if_: str
    then: str
    documents: Tuple[str, ...]
    owner: str
    source_ref: str
    score: float = 0.0


def _tokens(text: str) -> List[str]:
    """Осмысленные слова текста: без стопов и коротких обрывков."""
    out: List[str] = []
    for raw in (text or "").lower().replace("ё", "е").split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) < 3 or word in STOP_WORDS:
            continue
        out.append(word)
    return out


def _coverage(query_tokens: List[str], text: str) -> float:
    """Доля слов вопроса, покрытая текстом.

    Именно покрытие вопроса, а не Жаккар: вопрос жителя короче условия
    («хочу вступить в кружок» против «как записаться в кружок или студию»), и
    мерить нужно «все ли мои слова нашлись», а не «какая доля объединения».
    """
    if not query_tokens:
        return 0.0
    haystack = set(_tokens(text))
    if not haystack:
        return 0.0
    hit = sum(1 for t in query_tokens if t in haystack)
    return hit / len(query_tokens)


def score_candidates(question: str) -> List[Candidate]:
    """Кандидаты-условия с оценкой совпадения, отсортированные по убыванию.

    Оценка — максимум покрытия вопроса текстом вопроса условия, текстом поля
    ``if`` и названием услуги. Точное совпадение (нормализованное) даёт 1.0
    вне зависимости от покрытия.
    """
    from modules.service_conditions import _norm

    query = (question or "").strip()
    norm_query = _norm(query)
    query_tokens = _tokens(query)

    scored: List[Candidate] = []
    for service, items in CONDITIONS.items():
        for item in items:
            if norm_query and norm_query == _norm(item["question"]):
                score = 1.0
            else:
                score = max(
                    _coverage(query_tokens, item["question"]),
                    _coverage(query_tokens, item.get("if") or ""),
                    _coverage(query_tokens, service),
                )
            scored.append(
                Candidate(
                    service=service,
                    question=item["question"],
                    if_=item.get("if") or "",
                    then=item["then"],
                    documents=tuple(item.get("documents") or ()),
                    owner=item.get("owner") or "",
                    source_ref=item["source_ref"],
                    score=score,
                )
            )
    scored.sort(key=lambda c: (-c.score, c.service, c.question))
    return scored


def pick_candidates(
    question: str,
    *,
    service: Optional[str] = None,
) -> Tuple[List[Candidate], bool]:
    """Отобрать кандидатов по вопросу.

    Возвращает ``(кандидаты, неоднозначно)``. Пустой список — вопрос не про
    наш каталог. ``неоднозначно=True`` — лучший и второй кандидат слишком
    близки, и выбирать нельзя: ассистент честно перечислит оба.
    """
    query_tokens = _tokens(question)
    if len(query_tokens) < MIN_QUERY_TOKENS:
        return [], False

    scored = score_candidates(question)
    if service:
        scored = [c for c in scored if c.service == service]
    scored = [c for c in scored if c.score >= MIN_MATCH_SCORE]
    if not scored:
        return [], False

    best = scored[0]
    if len(scored) > 1 and (best.score - scored[1].score) < AMBIGUITY_MARGIN:
        # Неоднозначно: отдаём оба кандидата, выбирать — не наше дело.
        return [best, scored[1]], True
    return [best], False


def fact_keys_for(question: str) -> List[str]:
    """Ключи фактов, нужные для ответа на вопрос.

    Пусто — вопрос не про наш факт, и за фактом мы НЕ идём. Это сознательное
    ограничение: лишний запрос факта — это лишняя ПДн и лишнее
    «переспросили». И — главное — факт, который не является ответом на
    вопрос, в ответ не попадает.
    """
    low = (question or "").lower().replace("ё", "е")
    keys: List[str] = []
    for key, spec in FACT_SPECS.items():
        if any(phrase in low for phrase in spec["intents"]):
            keys.append(key)
    return keys


def _statement_from_condition(candidate: Candidate) -> Dict[str, Any]:
    """Утверждение из условия услуги — с обязательной цитатой на источник."""
    return {
        "statement": candidate.then,
        "source": {
            "kind": "service_condition",
            "ref": candidate.source_ref,
            "verified": True,
        },
        "note": None,
        "documents": list(candidate.documents),
        "owner": candidate.owner,
    }


def _statement_from_fact(key: str, value: str, verified: bool, source: str) -> Dict[str, Any]:
    """Утверждение из факта жителя.

    Самопровозглашённый факт (source=self, verified_at=null) помечается «по
    вашим данным» и прав не обещает — это правило 2 контракта ответа.
    """
    spec = FACT_SPECS.get(key) or {}
    labels = spec.get("values") or {}
    # Значение из чужого словаря, которого нет в нашем, — показываем как есть
    # (оно всё равно с источником), но говорим в журнале: словарь отстал.
    label = labels.get(value)
    if label is None and value:
        logger.warning(
            "service assistant: значение факта %s отсутствует в нашем словаре — "
            "нужна миграция словаря ассистента",
            key,
        )
        label = value
    return {
        "statement": f"{spec.get('label', key)}: {label or value}",
        "source": {
            "kind": FACT_SOURCE_KIND,
            "ref": f"{FACT_SOURCE_PREFIX}/{key}",
            "verified": bool(verified),
        },
        "note": None if verified else "по вашим данным",
        "documents": [],
        "owner": "",
    }


def ask(
    question: str,
    *,
    esa_sub: Optional[str] = None,
    service: Optional[str] = None,
    request_id: Optional[str] = None,
    fact_fetcher: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Ответить на вопрос жителя по условиям услуг и его фактам.

    Форма ответа — контракт плана пилота (см. ``modules/service_conditions``).

    ``fact_fetcher`` подменяет запрос к сервису-владельцу (тесты и сценарий
    «сервис недоступен»). По умолчанию — реальный клиент из
    ``modules.profile_facts``.
    """
    fetcher = fact_fetcher or fetch_fact

    candidates, ambiguous = pick_candidates(question, service=service)
    answer_items: List[Dict[str, Any]] = []
    missing: List[str] = []
    owner = ""

    if candidates and not ambiguous:
        chosen = candidates[0]
        answer_items.append(_statement_from_condition(chosen))
        owner = chosen.owner
        missing.extend(c.question for c in candidates[1:])
    elif candidates and ambiguous:
        # Два кандидата почти равны — утверждать нельзя. Перечисляем, что
        # спросить, и говорим, к кому идти.
        missing.extend(c.question for c in candidates)
        owner = candidates[0].owner

    # ── Факты: только по конкретному вопросу, только один раз за ответ ──
    fact_keys = fact_keys_for(question)
    if fact_keys:
        if not esa_sub:
            # Спрашивать некого: без жителя факт не имеет смысла. Это не
            # «не знаю» — это «не спросили, кого спрашивать».
            missing.append("уточните, о ком вопрос — нужен житель, чьи данные смотреть")
        elif not facts_enabled():
            logger.info("service assistant: запрос факта выключен флагом, ключи: %s", fact_keys)
            missing.append("спросить сервис-владельца о ваших данных")
        else:
            for key in fact_keys:
                outcome = fetcher(sub=esa_sub, key=key, question=question)
                # В лог — ключ и статус, НЕ значение.
                logger.info(
                    "service assistant: факт %s → статус %s",
                    key,
                    getattr(outcome, "status", "unknown"),
                )
                if getattr(outcome, "status", "") == STATUS_OK and outcome.value:
                    answer_items.append(
                        _statement_from_fact(
                            key,
                            outcome.value,
                            getattr(outcome, "verified", False),
                            getattr(outcome, "source", ""),
                        )
                    )
                else:
                    missing.append(_fact_question(key))

    # Правило 3: нет данных — «не знаю, вот кто отвечает» + заявка.
    if not answer_items:
        owner = owner or DEFAULT_OWNER
        if not missing:
            missing = [(question or "").strip() or "уточните вопрос"]
    elif not owner:
        owner = DEFAULT_OWNER

    return {
        "version": CONTRACT_VERSION,
        "source": SOURCE_LABEL,
        "answer": answer_items,
        "missing": missing,
        "owner": owner,
        "request_id": request_id,
    }


def _fact_question(key: str) -> str:
    """Вопрос жителю, который ассистент не смог закрыть сам.

    Формулировки взяты из словаря фактов (``FACT_SPECS``), который повторяет
    словарь Казанской: спрашиваем жителя РОВНО ОДИН РАЗ, иначе он перестанет
    отвечать.
    """
    spec = FACT_SPECS.get(key) or {}
    return str(spec.get("question") or "Уточните детали, о которых идёт речь")


def service_notes() -> Dict[str, str]:
    """Почему у услуги нет условий (для ассистента и для витрины)."""
    return dict(SERVICES_WITHOUT_CONDITIONS)


__all__ = (
    "AMBIGUITY_MARGIN",
    "Candidate",
    "DEFAULT_OWNER",
    "FACT_SPECS",
    "MIN_MATCH_SCORE",
    "MIN_QUERY_TOKENS",
    "STATUS_NOT_FOUND",
    "STATUS_NO_SUBJECT",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "STATUS_UNCONFIGURED",
    "ask",
    "fact_keys_for",
    "pick_candidates",
    "score_candidates",
    "service_notes",
)
