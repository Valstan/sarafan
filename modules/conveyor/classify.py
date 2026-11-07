"""LLM-стадия конвейера: рубрика сайта + заголовок + лёгкая редактура (D-015).

Один вызов DeepSeek на пост — между отбором (``source``) и доставкой (``delivery``).
Модель решает три вещи разом, потому что все три требуют одного и того же чтения
текста, а платить за три чтения незачем:

  1. **берём или нет** — материал не для этого сайта → ``reject`` с причиной;
  2. **рубрика** — slug из списка приёмника;
  3. **заголовок и лёгкая правка читабельности**.

**Фактов не добавлять** — правило зашито в системный промпт и продублировано
проверкой post-factum (``_looks_inflated``): текст из ВК можно чистить, нельзя
дописывать. Модель, которой сказали «сделай красиво», охотно сочиняет
подробности, и на районном портале это будет выглядеть как выдумка от имени
местной администрации.

Ответ модели проходит те же ворота, что и всё исходящее: сначала разбирается
здесь, затем инвариант мусора в ``delivery.check_invariant``. Ни одна ветка не
обязана доверять предыдущей.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config.deepseek import (
    get_api_key,
    get_base_url,
    get_max_tokens,
    get_model,
    get_timeout,
    thinking_disabled,
)
from modules.deepseek_client import call_api, finish_reason

logger = logging.getLogger(__name__)

# Во сколько раз ответ может быть длиннее исходника, прежде чем мы заподозрим
# дописывание. Лёгкая редактура текст обычно укорачивает; рост в полтора раза —
# это уже не «чистка», а сочинение. Порог с запасом: короткие посты (100–200
# символов) от добавленного заголовка растут в долях, и наказывать их не за что.
_MAX_GROWTH_RATIO = 1.5
_GROWTH_CHECK_MIN_CHARS = 400

SYSTEM_PROMPT = """Ты редактор новостной ленты районного сайта.

Тебе дают текст поста из ВКонтакте. Твоя работа — решить, годится ли он для сайта,
и если да — подготовить к публикации.

ЖЁСТКИЕ ЗАПРЕТЫ:
- НЕ добавляй фактов, которых нет в исходном тексте: ни дат, ни имён, ни цифр,
  ни названий, ни выводов. Не знаешь — не пиши.
- НЕ придумывай подробности «для связности». Лучше короче, чем выдумано.
- НЕ меняй смысл и не смягчай/усиливай оценки автора.

ЧТО МОЖНО: убрать эмодзи-мусор и призывы подписаться, разбить на абзацы,
исправить опечатки и пунктуацию, убрать обрывки вроде «читайте далее».

Ответ — СТРОГО JSON, без markdown-обёртки:
{"action": "accept"|"reject", "reason": "кратко, если reject",
 "section": "slug рубрики", "title": "заголовок до 90 символов",
 "text": "подготовленный текст"}"""


def build_user_prompt(
    post: Dict[str, Any],
    *,
    sections: Sequence[str],
    rules: str = "",
    max_chars: int = 4000,
) -> str:
    """Пользовательская часть промпта: правила сайта, рубрики, сам пост.

    Порядок частей — не оформление, а деньги (R29, мандат brain 2026-08-30).
    Провайдер кэширует **общий префикс** запросов, и всё, что одинаково для
    сайта — рубрики и его правила, — обязано стоять ДО всего, что меняется от
    поста к посту: тема сводки, вложения, текст. Раньше «Тема сводки района»
    шла второй строкой, и префикс обрывался на ней: правила сайта (самая длинная
    часть) оплачивались заново на каждом посте с другой темой. Замер «до» на
    живом потоке 01.09: в кэше стабильно 2048 токенов при 82.5% попаданий.
    """
    slugs = ", ".join(str(s) for s in sections) or "(рубрики не заданы)"
    media = post.get("media") or []
    kinds = sorted({str(m.get("type")) for m in media if isinstance(m, dict) and m.get("type")})
    # Стабильная для сайта часть — первой.
    parts = [f"Рубрики сайта (выбери одну): {slugs}"]
    if rules.strip():
        parts.append(f"\nПравила этого сайта:\n{rules.strip()}")
    # Переменная часть — только после неё.
    parts.append(
        "\n".join(
            [
                f"\nТема сводки района: {post.get('theme') or '—'}",
                f"Вложения поста: {', '.join(kinds) or 'нет'}",
            ]
        )
    )
    parts.append(f"\nТекст поста:\n{str(post.get('text') or '')[:max_chars]}")
    return "\n".join(parts)


def stable_prefix(prompt: str) -> str:
    """Часть пользовательского промпта, общая для всех постов одного сайта.

    Нужна тесту-сторожу: перестановка строк в ``build_user_prompt`` выглядит
    безобидно, ответы модели от неё не меняются, ошибок в логах нет — меняется
    только счёт за токены. Единственный наблюдаемый сигнал без такого теста —
    упавшая доля кэша через сутки на проде.
    """
    return prompt.split("\nТема сводки района:", 1)[0]


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    """Достать JSON из ответа модели, даже если он обёрнут в ```json.

    Терпимость здесь уместна: обёртка — частая привычка моделей, и ронять из-за
    неё уже оплаченный вызов расточительно. Терпимость к СОДЕРЖИМОМУ — нет, его
    проверяет ``parse_verdict``.
    """
    raw = (content or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _looks_inflated(original: str, edited: str) -> bool:
    """Правка выросла настолько, что похожа на дописывание?

    Грубая проверка по длине — не детектор выдумки, а сторож на самый заметный
    её случай. Тонкую проверку делает человек в ленте: shadow-фаза для того и есть.
    """
    src = (original or "").strip()
    out = (edited or "").strip()
    if len(src) < _GROWTH_CHECK_MIN_CHARS:
        return False
    return len(out) > len(src) * _MAX_GROWTH_RATIO


def parse_verdict(
    raw: Dict[str, Any],
    post: Dict[str, Any],
    *,
    sections: Sequence[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Разобрать ответ модели. Возвращает ``(вердикт, причина отказа)``.

    Неизвестный slug рубрики **не отказ**: приёмник его тоже не считает отказом
    (создаёт пост без рубрики + warning), а директива запрещает молча подгонять
    поток под список. Мы такой slug пропускаем дальше и копим для обратной связи.
    """
    action = str(raw.get("action") or "").strip().lower()
    if action == "reject":
        return None, f"llm_reject:{str(raw.get('reason') or 'без причины')[:48]}"
    if action != "accept":
        return None, "llm_bad_action"

    title = str(raw.get("title") or "").strip()
    text = str(raw.get("text") or "").strip()
    if not title:
        return None, "llm_no_title"
    if not text:
        return None, "llm_no_text"
    if _looks_inflated(str(post.get("text") or ""), text):
        return None, "llm_text_inflated"

    section = str(raw.get("section") or "").strip().lower()
    known = {str(s).strip().lower() for s in (sections or ())}
    verdict: Dict[str, Any] = {
        "action": "accept",
        "section": section,
        "title": title[:200],
        "text": text,
        "section_known": (section in known) if known else True,
    }
    return verdict, None


# HTTP-вызов переехал в общий ``modules.deepseek_client`` (D-024): у DeepSeek
# появился второй и третий потребитель, и копия вызова в каждом разошлась бы
# молча. Имя ``_call_api`` сохранено — на него смотрят тесты этого модуля, и
# ломать их ради переименования нечестно: поведение не изменилось.
_call_api = call_api


def _is_balance_error(response: Dict[str, Any]) -> bool:
    """Ответ DeepSeek «Insufficient Balance» — на счёте нет денег."""
    blob = json.dumps(
        response.get("error") if response.get("error") is not None else response, ensure_ascii=False
    )
    low = blob.lower()
    return "insufficient balance" in low or "insufficient_balance" in low


def _call_opencode(body: Dict[str, Any]):
    """Живой free-движок в форме ответа прямого DeepSeek.

    Контракт ``opencode_engine.chat`` зеркалит deepseek_client, поэтому подмена
    не трогает разбор ответа ниже: тот же JSON, тот же разбор, те же причины.

    ⚠️ Контур OpenCode+ВПН выведен из эксплуатации 06.11 (ADR-0014), поэтому
    на текущем проде этот запасной путь не сработает: сервера на боксе нет.
    Он остаётся как часть рецепта отката — если контур вернётся, подмена
    заработает снова.
    """
    from modules.opencode_engine import chat as _opencode_chat

    system = ""
    user = ""
    for message in body.get("messages") or []:
        role = str((message or {}).get("role") or "")
        if role == "system":
            system = str((message or {}).get("content") or "")
        elif role == "user":
            user = str((message or {}).get("content") or "")
    result = _opencode_chat(user=user, system=system, label="conveyor")
    if not result.get("ok"):
        return 0, {
            "error": {
                "message": str(result.get("reason") or "opencode_failed"),
                "detail": result.get("detail"),
            }
        }
    return 200, {
        "choices": [{"message": {"content": str(result.get("content") or "")}}],
        "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
    }


def classify(
    post: Dict[str, Any],
    *,
    sections: Sequence[str],
    rules: str = "",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Классифицировать один пост. Возвращает ``{ok, verdict?, reason?, usage?}``.

    Никогда не бросает: конвейер — фоновая рутина, и падение на одном посте не
    должно ронять прогон. Любой отказ возвращается кодом причины, который ляжет
    в ``conveyor_deliveries.reason`` и станет ответом на вопрос «почему этой
    новости нет на сайте».
    """
    key = api_key if api_key is not None else get_api_key()
    if not key:
        return {"ok": False, "reason": "no_api_key"}
    if not str(post.get("text") or "").strip():
        return {"ok": False, "reason": "empty_text"}

    body = {
        "model": get_model(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(post, sections=sections, rules=rules)},
        ],
        # Низкая температура: нам нужна предсказуемая рубрикация, а не
        # разнообразие формулировок.
        "temperature": 0.2,
        "max_tokens": get_max_tokens(),
        "response_format": {"type": "json_object"},
    }
    if thinking_disabled():
        # Тот же выключатель, что и в `deepseek_client.chat`: без него явное
        # имя модели включает «раздумья», и они съедают бюджет ответа целиком —
        # конвейер получает обрезанный JSON и теряет классификацию поста
        # (замер 07.11: 277 обрезанных вызовов за сутки).
        body["thinking"] = {"type": "disabled"}

    status, response = _call_api(
        body,
        api_key=key,
        base_url=get_base_url(),
        timeout=get_timeout(),
        label="conveyor",
    )
    if status == 402 or _is_balance_error(response):
        # Прямой DeepSeek без денег на счёте («Insufficient Balance», 402) — идём на
        # живой free-движок: он отдаёт тот же контракт chat(). Без этого конвейер
        # молчит (замер 05.11: 150 постов без вердикта, сайты голодные с 17.10).
        fb_status, fb_response = _call_opencode(body)
        if 200 <= fb_status < 300:
            status, response = fb_status, fb_response
        else:
            logger.warning(
                "конвейер: DeepSeek без баланса, free-движок не ответил (%s) для %s",
                fb_status,
                post.get("lip"),
            )
    if status == 0:
        return {"ok": False, "reason": "network", "detail": response.get("error")}
    if not (200 <= status < 300):
        return {"ok": False, "reason": f"http_{status}", "detail": response.get("error")}

    usage = response.get("usage") if isinstance(response.get("usage"), dict) else None
    if finish_reason(response) == "length":
        # Упёрлись в max_tokens: JSON оборван. Отдельная причина, а не
        # llm_unparseable — лечится бюджетом вывода, а не промптом (G334).
        logger.warning("DeepSeek упёрся в max_tokens для %s", post.get("lip"))
        return {"ok": False, "reason": "truncated", "usage": usage}

    choices = response.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        content = str((choices[0].get("message") or {}).get("content") or "")
    raw = _extract_json(content)
    if raw is None:
        logger.warning("DeepSeek вернул неразбираемый ответ для %s", post.get("lip"))
        return {"ok": False, "reason": "llm_unparseable"}

    verdict, refusal = parse_verdict(raw, post, sections=sections)
    if verdict is None:
        return {"ok": False, "reason": refusal, "usage": usage}
    return {"ok": True, "verdict": verdict, "usage": usage}


def collect_unknown_sections(results: Sequence[Dict[str, Any]]) -> List[str]:
    """Рубрики, которые модель выдала мимо списка сайта — материал для обратной связи.

    Директива прямо просит вернуть, «какая нарезка просится по факту потока»,
    вместо тихой подгонки классификатора под черновой список из семи slug'ов.
    """
    out: List[str] = []
    for r in results or ():
        v = (r or {}).get("verdict") or {}
        if v and not v.get("section_known"):
            s = str(v.get("section") or "").strip().lower()
            if s and s not in out:
                out.append(s)
    return out
