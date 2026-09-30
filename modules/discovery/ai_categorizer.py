"""DeepSeek-based categoriser for VK community discovery candidates.

Input: snapshot of a VK group (name, description, members_count, recent posts).
Output: `{success, category, confidence, reasoning, is_info_page}` —
serialisable straight into ``community_candidates`` columns.

Designed defensively: any failure (no API key, network/429, malformed JSON,
empty answer) returns ``{success: False, error}`` and never raises. The caller
stores ``ai_category=None`` and lets the moderator pick the category by hand in
the UI.

**Движок — DeepSeek** (мандат brain 2026-08-10, решение владельца D-024). Был
Groq через его SDK; ключ Groq вдобавок оказался скомпрометирован и держался
рабочим только ради двух потребителей, из которых этот — один. Перевод снимает
и мандат, и техдолг разом: после него ключ отзывается, а не ротируется.

Разбор ответа остался терпимым к обёрткам (```json, текст вокруг), хотя
DeepSeek зовётся в режиме строгого JSON: режим гарантирует синтаксис, а не то,
что модель не добавит пояснение. Терпимость к СОДЕРЖИМОМУ по-прежнему нулевая —
категория вне списка схлопывается в ``other``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

from modules.deepseek_client import chat

logger = logging.getLogger(__name__)

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


def _build_prompt(
    *,
    name: str,
    description: Optional[str],
    members_count: Optional[int],
    recent_posts: Iterable[str],
    region_name: Optional[str] = None,
) -> str:
    members_part = f" (~{members_count} подписчиков)" if members_count else ""
    region_part = (
        f"Регион (район), к которому хотим привязать сообщество: «{region_name}». "
        if region_name
        else ""
    )
    posts_block = "\n".join(
        f"- {p.strip()[:250]}" for p in list(recent_posts)[:10] if (p or "").strip()
    )
    if not posts_block:
        posts_block = "(нет доступных постов для анализа)"
    cats_list = ", ".join(ALLOWED_CATEGORIES)
    return (
        "Ты — модератор сети региональных пабликов ВКонтакте. Тебе показывают "
        "VK-сообщество, его кратко надо классифицировать по тематике. "
        f"{region_part}"
        f"Сообщество: «{(name or '').strip()}»{members_part}.\n"
        f"Описание: «{(description or '').strip()[:600] or '—'}».\n"
        f"Последние посты:\n{posts_block}\n\n"
        "Выбери одну категорию из этого списка (строго ровно одно слово):\n"
        f"  {cats_list}\n\n"
        "Где:\n"
        "  admin   — официальные органы (администрация района, депутаты, госорганы);\n"
        "  novost  — новостной паблик района/города;\n"
        "  reklama — объявления, барахолка, доска, куплю/продам;\n"
        "  sosed   — соседи, ДТП, происшествия, флуд жителей;\n"
        "  kultura — культура, афиша, дом культуры, библиотеки;\n"
        "  sport   — спорт, фитнес, секции, команды;\n"
        "  detsad  — детский сад, школа, родительские чаты;\n"
        "  other   — ничего из перечисленного не подходит.\n\n"
        "Также определи: похоже ли это сообщество на ГЛАВНУЮ ИНФО-страницу "
        "района (универсальный паблик, центр коммуникации, обычно тысячи "
        "подписчиков, постит и новости, и объявления). Если да — is_info_page=true.\n\n"
        "Верни ТОЛЬКО валидный JSON-объект, БЕЗ markdown-ограждений, "
        "БЕЗ префикса 'json':\n"
        '{"category": "<one of the list>", "confidence": <0-100 integer>, '
        '"is_info_page": <true|false>, "reasoning": "<одна короткая фраза>"}'
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


async def categorize_candidate(
    *,
    name: str,
    description: Optional[str] = None,
    members_count: Optional[int] = None,
    recent_posts: Optional[List[str]] = None,
    region_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Run DeepSeek categorisation. Always returns a dict — never raises.

    Success: ``{success: True, category, confidence, is_info_page, reasoning,
              model}``.
    Failure: ``{success: False, error}``.
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

    # Клиент синхронный (urllib) — уводим в поток, чтобы не блокировать event loop.
    result = await asyncio.to_thread(
        chat,
        user=prompt,
        temperature=_TEMPERATURE,
        max_tokens=_MAX_TOKENS,
        json_object=True,
        label="discovery",
    )
    if not result.get("ok"):
        reason = str(result.get("reason") or "unknown")
        detail = result.get("detail")
        logger.warning("DeepSeek categorize failed: %s%s", reason, f" ({detail})" if detail else "")
        error = "DEEPSEEK_API_KEY is not configured" if reason == "no_api_key" else reason
        return {"success": False, "error": error}

    content = str(result.get("content") or "")
    parsed = _parse_response(content)
    if parsed is None:
        return {
            "success": False,
            "error": "AI response is not valid JSON",
            "raw": content[:300],
        }

    out = _normalise(parsed)
    out["success"] = True
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

    Отдельной функцией потому, что ``usage`` кладёт в результат клиент, а
    разбирают его два независимых потребителя (отбор кандидатов и recheck).
    """
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return _usage_int(usage, "prompt_tokens"), _usage_int(usage, "completion_tokens")


def _usage_int(usage: Dict[str, Any], field: str) -> Optional[int]:
    value = usage.get(field)
    return int(value) if isinstance(value, (int, float)) else None


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
