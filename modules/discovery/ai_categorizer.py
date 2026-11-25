"""DeepSeek-based categoriser for VK community discovery candidates.

Input: snapshot of a VK group (name, description, members_count, recent posts).
Output: `{success, category, confidence, reasoning, is_info_page}` —
serialisable straight into ``community_candidates`` columns.

Designed defensively: any failure (no API key, network/429, malformed JSON,
empty answer) returns ``{success: False, error}`` and never raises. The caller
stores ``ai_category=None`` and lets the moderator pick the category by hand in
the UI.

**Движок — MiniMax** (переключено 23.11 по заказу владельца). Раньше здесь был
DeepSeek: еженедельный recheck платил 2 701 вызов и 2,29 млн prompt-токенов за
один прогон — а классификация и переписывание уже живут на бесплатном free-тире
MiniMax. Дипсик остался **платной страховкой**: если бесплатный движок не
ответил, категоризация всё равно выполняется (задача не должна молча перестать
проверять сеть), и это видно в отчёте прогона — по числу вызовов на каждом
движке.

**Исключение — отказ по ёмкости бокса** (``DISCOVERY_PAID_FALLBACK=auto``,
дефолт с 25.11): если MiniMax не ответил из-за того, что **бокс не дал место**
под CLI (``insufficient_memory`` / ``cli_slot_busy`` / ``oom_killed`` /
``timeout``), платная страховка **не срабатывает**. «Бокс не дал место» — это не
поломка движка; платить за него нечего, а сообщество и без вердикта остаётся
«active» (fail-safe в ``modules/discovery/health_check.py``) и получит вердикт в
следующем срезе. Замер 25.11: 122 платных вызова в сутки именно на этом пути.
Ручки: ``=1`` — прежнее поведение (платим за любой отказ), ``=0`` — не платим
никогда; общая ``CLASSIFIER_PAID_FALLBACK=0`` сильнее обеих.

⚠️ **Цена переключения — время, а не деньги.** Вызов через CLI MiniMax занимает
~10 с против ~1,7 с у Дипсика: еженедельный прогон всех регионов разом
растягивается с 32 минут до нескольких часов и начинает забирать воркер у
утренней волны (ADR-0020, P233). Поэтому recheck разложен по ночам
(``DISCOVERY_RECHECK_SLICES``): каждый регион всё так же проверяется раз в
неделю, но сеть не проверяется вся за одну ночь.

Разбор ответа остался терпимым к обёрткам (```json, текст вокруг): MiniMax Code
не умеет строгий JSON-схемой, поэтому JSON ловится из текста. Терпимость к
СОДЕРЖИМОМУ по-прежнему нулевая — категория вне списка схлопывается в
``other``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional

#: Импорт на уровне модуля — не только для красоты: тесты подменяют именно
#: ``ai_categorizer.chat`` (точка подмены одна и общая для обоих движков).
from modules.deepseek_client import chat
from modules.minimax_capacity import CAPACITY_REASONS

logger = logging.getLogger(__name__)

#: Движок категоризации: ``minimax`` (бесплатный free-тир, по умолчанию) или
#: ``deepseek`` (платный). Переопределяется переменной окружения.
ENV_ENGINE = "DISCOVERY_ENGINE"
DEFAULT_ENGINE = "minimax"
#: Платный фолбэк на Дипсик, если бесплатный движок не ответил.
#:
#: Режимы: ``auto`` (дефолт) — на отказ **по ёмкости бокса** платная страховка
#: не срабатывает; ``always`` (``1``) — прежнее поведение, платим за любой
#: отказ; ``never`` (``0``) — не платим никогда.
ENV_PAID_FALLBACK = "DISCOVERY_PAID_FALLBACK"
DEFAULT_PAID_FALLBACK = "auto"

_TEMPERATURE = 0.2  # категоризация — задача с одним «правильным» ответом
_MAX_TOKENS = 300

# Категории, которые модель должна выбирать. Совпадают с beat-schedule и
# Community.category. ``other`` — escape hatch для всего, что не подходит ни
# к одной теме (модератор решит, что с этим делать).
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "admin",
    "novost",
    "reklama",
    "sosed",
    "kultura",
    "sport",
    "detsad",
    # Расширенная областная повестка (community-mode oblast)
    "proisshestviya",
    "molodezh",
    "nauka",
    "promyshlennost",
    "selhoz",
    "zdorovie",
    "zhkh",
    "priroda",
    "other",
)


#: Роль и порядок изложения — одинаковые для обоих движков.
_ROLE = (
    "Ты — модератор сети региональных пабликов ВКонтакте. Тебе показывают "
    "VK-сообщество, его кратко надо классифицировать по тематике. "
)

#: Инструкция: список категорий и расшифровки.
_CATEGORY_DEFS = (
    "Выбери одну категорию из этого списка (строго ровно одно слово):\n"
    "  {cats}\n\n"
    "Где:\n"
    "  admin   — официальные органы (администрация района, депутаты, госорганы);\n"
    "  novost  — новостной паблик района/города;\n"
    "  reklama — объявления, барахолка, доска, куплю/продам;\n"
    "  sosed   — соседи, ДТП, происшествия, флуд жителей;\n"
    "  kultura — культура, афиша, дом культуры, библиотеки;\n"
    "  sport   — спорт, фитнес, секции, команды;\n"
    "  detsad  — детский сад, школа, родительские чаты;\n"
    "  other   — ничего из перечисленного не подходит.\n\n"
)

#: Что ещё спросить и в каком виде ответить.
_JSON_SCHEMA = (
    "Также определи: похоже ли это сообщество на ГЛАВНУЮ ИНФО-страницу "
    "района (универсальный паблик, центр коммуникации, обычно тысячи "
    "подписчиков, постит и новости, и объявления). Если да — is_info_page=true.\n\n"
    "Верни ТОЛЬКО валидный JSON-объект, БЕЗ markdown-ограждений, "
    "БЕЗ префикса 'json':\n"
    '{"category": "<one of the list>", "confidence": <0-100 integer>, '
    '"is_info_page": <true|false>, "reasoning": "<одна короткая фраза>"}'
)


def _region_part(region_name: Optional[str]) -> str:
    return (
        f"Регион (район), к которому хотим привязать сообщество: «{region_name}». "
        if region_name
        else ""
    )


def _build_instructions() -> str:
    """Инструкция без данных: список категорий, расшифровки, форма ответа."""
    return _CATEGORY_DEFS.format(cats=", ".join(ALLOWED_CATEGORIES)) + _JSON_SCHEMA


def _build_card(
    *,
    name: str,
    description: Optional[str],
    members_count: Optional[int],
    recent_posts: Iterable[str],
) -> str:
    """Карточка сообщества — данные, без инструкций.

    Отдельной частью нужна для MiniMax: CLI получает системный промт файлом и
    карточку файлом (см. ``_call_minimax``), а Дипсик — один общий промт.
    """
    members_part = f" (~{members_count} подписчиков)" if members_count else ""
    posts_block = "\n".join(
        f"- {p.strip()[:250]}" for p in list(recent_posts)[:10] if (p or "").strip()
    )
    if not posts_block:
        posts_block = "(нет доступных постов для анализа)"
    return (
        f"Сообщество: «{(name or '').strip()}»{members_part}.\n"
        f"Описание: «{(description or '').strip()[:600] or '—'}».\n"
        f"Последние посты:\n{posts_block}\n\n"
    )


def _build_system_prompt(*, region_name: Optional[str] = None) -> str:
    """Системный промт MiniMax: роль + инструкция (без карточки)."""
    return _ROLE + _region_part(region_name) + _build_instructions()


def _build_prompt(
    *,
    name: str,
    description: Optional[str],
    members_count: Optional[int],
    recent_posts: Iterable[str],
    region_name: Optional[str] = None,
) -> str:
    """Полный промт для Дипсика: роль, карточка, инструкция (как раньше)."""
    return (
        _ROLE
        + _region_part(region_name)
        + _build_card(
            name=name,
            description=description,
            members_count=members_count,
            recent_posts=recent_posts,
        )
        + _build_instructions()
    )


def _strip_json_fences(text: str) -> str:
    """Если модель всё-таки обернула JSON в ```json ... ``` — снимем обёртку."""
    t = (text or "").strip()
    if t.startswith("```"):
        # remove leading ``` or ```json
        t = re.sub(r"^```(?:json)?\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t


def _parse_response(content: str) -> Optional[Dict[str, Any]]:
    """Robust JSON parsing — иногда LLM возвращает с лишним текстом до/после.

    Если сразу не парсится, пытаемся вытащить первое ``{ ... }``.
    """
    stripped = _strip_json_fences(content)
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _normalise(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp/sanitize модельный ответ. Возвращает то, что пойдёт в БД."""
    cat = str(parsed.get("category") or "").strip().lower()
    if cat not in ALLOWED_CATEGORIES:
        cat = "other"
    try:
        confidence = int(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    is_info = bool(parsed.get("is_info_page"))
    reasoning = str(parsed.get("reasoning") or "").strip()[:400] or None
    return {
        "category": cat,
        "confidence": confidence,
        "is_info_page": is_info,
        "reasoning": reasoning,
    }


def _engine_name() -> str:
    """Какой движок звать: ``minimax`` (по умолчанию) или ``deepseek``."""
    name = (os.getenv(ENV_ENGINE) or DEFAULT_ENGINE).strip().lower()
    return name or DEFAULT_ENGINE


def _paid_fallback_mode() -> str:
    """Режим платной страховки подбора: ``auto`` | ``always`` | ``never``.

    - ``auto`` (дефолт) — платная страховка **не срабатывает** на отказы по
      ёмкости бокса (:data:`modules.minimax_capacity.CAPACITY_REASONS`): «бокс не
      дал место под CLI» — это не поломка движка, и платить за него нечего.
      Сообщество остаётся «active», вердикт придёт в следующем срезе;
    - ``always`` (``1``) — прежнее поведение: любой отказ MiniMax закрывается
      платным Дипсиком;
    - ``never`` (``0``) — не платим никогда.
    """
    raw = (os.getenv(ENV_PAID_FALLBACK) or DEFAULT_PAID_FALLBACK).strip().lower()
    if raw in ("0", "false", "no", "off", "never"):
        return "never"
    if raw in ("1", "true", "yes", "on", "always"):
        return "always"
    return "auto"


def _paid_fallback_enabled() -> bool:
    """Разрешён ли платный фолбэк на Дипсик, если бесплатный движок не ответил."""
    return _paid_fallback_mode() != "never"


def _global_paid_fallback_enabled() -> bool:
    """Общая ручка проекта ``CLASSIFIER_PAID_FALLBACK`` — сильнее ручки потока.

    «Платить нельзя вообще» значит «вообще»: ручка потока (``DISCOVERY_PAID_FALLBACK``)
    может запретить платить, но не может разрешить платить, когда счёт закрыт.
    """
    from config.opencode import paid_fallback_enabled

    return paid_fallback_enabled()


def _call_minimax(prompt: str, *, system: str = "", card: str = "") -> Dict[str, Any]:
    """Вызов MiniMax Code (free-тир). Контракт тот же, что у Дипсика.

    ⚠️ **Системный промт обязателен.** CLI MiniMax отказывается работать с
    пустым файлом промта («--system-prompt-file ... is empty») — и это выглядит
    как поломка движка, а не как ошибка вызывающего. 24.11 это стоило 385
    отказов и отката всего ночного среза discovery на платный Дипсик: путь
    «discovery на MiniMax» не отработал ни разу с переключения 23.11.

    Файлы для CLI: системный промт — инструкция, вложение — карточка
    сообщества (её же ждёт задание ``TASK_TEXT_DISCOVERY``).
    """
    from modules.minimax_engine import chat as _minimax_chat

    # ``label`` задаёт и метку в журнале расхода, и текст задания для CLI
    # (у discovery он свой — см. ``TASK_BY_LABEL`` в minimax_engine).
    return _minimax_chat(
        system=system,
        user=card or prompt,
        label="discovery",
    )


def _call_deepseek(prompt: str) -> Dict[str, Any]:
    """Вызов платного Дипсика (страховка и режим ``DISCOVERY_ENGINE=deepseek``)."""
    return chat(
        user=prompt,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
        json_object=True,
        label="discovery",
    )


def _call_engine(engine: str, prompt: str, **kwargs: Any) -> Dict[str, Any]:
    if engine == "minimax":
        return _call_minimax(prompt, **kwargs)
    if engine == "deepseek":
        return _call_deepseek(prompt)
    raise RuntimeError("неизвестный движок категоризации: %s" % engine)


async def categorize_candidate(
    *,
    name: str,
    description: Optional[str] = None,
    members_count: Optional[int] = None,
    recent_posts: Optional[List[str]] = None,
    region_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Categorise via the configured engine. Always returns a dict — never raises.

    Success: ``{success: True, category, confidence, is_info_page, reasoning,
              model, engine}``.
    Failure: ``{success: False, error, engine}``.
    """
    if not (name or "").strip():
        return {"success": False, "error": "name is empty"}

    prompt = _build_prompt(
        name=name,
        description=description,
        members_count=members_count,
        recent_posts=recent_posts or [],
        region_name=region_name,
    )
    # MiniMax получает инструкцию и карточку по отдельности: системный промт
    # файлом, карточка — вложением (см. ``_call_minimax``).
    system_prompt = _build_system_prompt(region_name=region_name)
    card = _build_card(
        name=name,
        description=description,
        members_count=members_count,
        recent_posts=recent_posts or [],
    )

    engine = _engine_name()
    # Движки синхронные (urllib / CLI) — уводим в поток, чтобы не блокировать
    # event loop.
    try:
        result = await asyncio.to_thread(
            _call_engine, engine, prompt, system=system_prompt, card=card
        )
    except RuntimeError as exc:
        return {"success": False, "error": str(exc), "engine": engine}

    if not result.get("ok") and engine != "deepseek":
        reason = str(result.get("reason") or "unknown")
        detail = str(result.get("detail") or "")[:160]
        mode = _paid_fallback_mode()
        if not _global_paid_fallback_enabled():
            logger.warning(
                "discovery: %s не ответил (%s%s) — платная страховка выключена "
                "(CLASSIFIER_PAID_FALLBACK), вердикта нет",
                engine,
                reason,
                (" %s" % detail) if detail else "",
            )
            return {"success": False, "error": reason, "engine": engine}
        if mode == "never":
            logger.warning(
                "discovery: %s не ответил (%s%s) — этот поток не платит, вердикта нет",
                engine,
                reason,
                (" %s" % detail) if detail else "",
            )
            return {"success": False, "error": reason, "engine": engine}
        if mode == "auto" and reason in CAPACITY_REASONS:
            logger.warning(
                "discovery: %s не ответил (%s%s) — отказ по ёмкости бокса, "
                "платная страховка пропущена, вердикта нет",
                engine,
                reason,
                (" %s" % detail) if detail else "",
            )
            return {"success": False, "error": reason, "engine": engine}
        logger.warning(
            "discovery: %s не ответил (%s%s) — закрываем платным Дипсиком",
            engine,
            reason,
            (" %s" % detail) if detail else "",
        )
        result = await asyncio.to_thread(_call_engine, "deepseek", prompt)
        engine = "deepseek"

    if not result.get("ok"):
        reason = str(result.get("reason") or "unknown")
        detail = result.get("detail")
        logger.warning(
            "%s categorize failed: %s%s", engine, reason, f" ({detail})" if detail else ""
        )
        error = "DEEPSEEK_API_KEY is not configured" if reason == "no_api_key" else reason
        return {"success": False, "error": error, "engine": engine}

    content = str(result.get("content") or "")
    parsed = _parse_response(content)
    if parsed is None:
        return {
            "success": False,
            "error": "AI response is not valid JSON",
            "raw": content[:300],
            "engine": engine,
        }

    out = _normalise(parsed)
    out["success"] = True
    out["engine"] = engine
    # Имя модели — фактическое из ответа клиента, а не константа модуля: оно
    # ложится в БД рядом с вердиктом, и «какой моделью размечено» должно быть
    # правдой, даже если DEEPSEEK_MODEL переопределён через env.
    out["model"] = str(result.get("model") or "")
    out["tokens_prompt"], out["tokens_completion"] = usage_tokens(result)
    return out


def usage_tokens(result: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """``(prompt_tokens, completion_tokens)`` из ответа клиента.

    Провайдер вправе не вернуть счётчики, и тогда честное значение — ``None``,
    а не ``0``: «нечем померить» не должно выглядеть как «вызов стоил ноль».
    Иначе сумма расхода по прогону тихо занижается, и решение «включать ли
    авто-подбор» принимается по неверной цене (P001).

    Имена полей у движков разные: Дипсик — ``prompt_tokens``/``completion_tokens``,
    MiniMax Code — ``inputTokens``/``outputTokens``. Здесь берётся любое из
    известных начертаний: «движок сменился» не должно обнулять учёт расхода.

    Отдельной функцией потому, что ``usage`` кладёт в результат клиент, а
    разбирают его два независимых потребителя (отбор кандидатов и recheck).
    """
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return (
        _usage_int(usage, "prompt_tokens", "inputTokens"),
        _usage_int(usage, "completion_tokens", "outputTokens"),
    )


def _usage_int(usage: Dict[str, Any], *names: str) -> Optional[int]:
    """Первое из известных начертаний поля, которое есть и число."""
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def sum_ai_tokens(results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Сумма токенов по набору результатов + признак полноты замера.

    ``tokens_measured`` отвечает на вопрос, который сумма сама не скажет: «это
    весь расход или нижняя оценка». Частичный замер (провайдер не вернул
    ``usage`` хотя бы по части вызовов) — это не ноль и не полная сумма, а
    третье состояние; без флага его приходится угадывать по отчёту.

    Провал вызова тоже попадает в ``tokens_unmeasured_calls``: неизвестно, был
    ли он оплачен провайдером (сеть оборвалась после отправки — не наш расход,
    но и не наш ноль).
    """
    prompt = 0
    completion = 0
    missing = 0
    for item in results:
        p = item.get("tokens_prompt")
        c = item.get("tokens_completion")
        if isinstance(p, (int, float)) and isinstance(c, (int, float)):
            prompt += int(p)
            completion += int(c)
        else:
            missing += 1
    return {
        "tokens_prompt": prompt,
        "tokens_completion": completion,
        "tokens_measured": missing == 0,
        "tokens_unmeasured_calls": missing,
    }
