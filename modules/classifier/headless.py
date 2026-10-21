"""Headless-классификация постов на DeepSeek — замена облачной рутине (D-024).

**Зачем.** Вердикты HITL-классификатора до сих пор производит облачная
рутина-агент: она ходит в ``/api/classifier/pending``, читает постулаты, выносит
суждения и POST'ит их обратно. Мандат brain 2026-08-10 (решение владельца D-024)
требует перевести рутинные нейро-вызовы на прямой DeepSeek API — тем же
паттерном, что контент-конвейер: ключ из комнаты КАРМАНа, вызов из Celery по
cron'у, без облака и без включённого компьютера владельца.

**Что здесь на кону — не «накопление Корпуса».** На проде
``CLASSIFIER_ENFORCE_ENABLED=1``, и ``enforce.fetch_blocked_lips`` читает эти
вердикты напрямую: посты с действием ``delete``/``hold`` не попадают в сводку.
За трое суток (окно enforce) это ~2000 постов из ~4200. То есть модуль стоит на
пути публикации, а не сбоку от неё, и его отказ обязан быть тихим и fail-open —
нет вердикта, пост публикуется по обычным фильтрам.

**Постулаты идут системным сообщением, посты — пользовательским.** Постулаты
одинаковы для всех чанков прогона (``render_effective_postulates`` отдаёт базу
байт-в-байт, когда выученных правил нет — там это сделано ровно ради
prompt-cache). Стабильный префикс кэшируется у провайдера; переносить его в
пользовательскую часть значит платить за него на каждом чанке заново.

**Чанки режутся внутри региона.** Батч ``fetch_pending`` собран round-robin'ом
по регионам (чтобы никто не голодал), но склейка (``merge_with``) — суждение о
том, что два поста об одном событии; посты соседних районов склеивать нельзя, а
посты одного района нужно видеть вместе. Поэтому перед нарезкой батч
перегруппировывается по региону: чанк = один регион.

**Посты без текста пропускаются.** Их ~5% потока. Облачная рутина скачивала
вложения и смотрела на них; текстовая модель этого не может, а выдумывать по
одному заголовку — значит писать вердикт, который затем ЗАБЛОКИРУЕТ пост в
публикации. Пропуск оставляет пост без вердикта, то есть публикующимся по
обычным фильтрам — прежнее поведение, а не новое. Фоновая таска отсекает их
ещё в ``fetch_pending(text_only=True)``, до лимита батча: пост без вердикта
остаётся «свежим» всё окно и иначе занимал бы место на каждом прогоне.
Фильтр здесь остаётся для волнового пути (``prepublish``).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config.opencode import engine_enabled
from modules.classifier.schema import ClassifierVerdict, parse_verdict_loose
from modules.deepseek_client import chat as _deepseek_chat

logger = logging.getLogger(__name__)


def chat(**kwargs: Any) -> Dict[str, Any]:
    """Вызов модели текущим движком. ``CLASSIFIER_ENGINE=opencode`` → харнесс.

    Переключатель стоит здесь, а не в ``classify_posts``/тасках: один рубеж на
    все пути классификации (фоновая таска, prepublish внутри волны, кнопка в
    UI), а DeepSeek остаётся дефолтом — откат пилота без деплоя кода.

    Здесь же стоит предохранитель: под лимитом free-тира вызов возвращает отказ
    сразу, не трогая сеть (``modules.engine_guard``). Рубеж один по той же
    причине — второй экземпляр решения разошёлся бы с первым молча.
    """
    from modules import engine_guard

    if not engine_guard.allow():
        return {"ok": False, "reason": "breaker_open"}

    if engine_enabled():
        from modules.opencode_engine import chat as _opencode_chat

        result = _opencode_chat(**kwargs)
    else:
        result = _deepseek_chat(**kwargs)

    engine_guard.note_result(bool(result.get("ok")), str(result.get("reason") or ""))
    return result


# Сколько постов уходит в один вызов. Больше — дешевле (постулаты не
# переплачиваются), но длиннее ответ и выше риск, что модель начнёт экономить на
# последних постах чанка. 10 — компромисс, вынесен в аргумент ради замера.
DEFAULT_CHUNK_SIZE = 10
# Потолок ответа. На один вердикт уходит ~120 токенов (тема, действие, короткое
# обоснование), 10 вердиктов ≈ 1200; берём с запасом на длинные reasoning.
# 240 вместо 220 с 2026-09-22: вердикт подрос на два поля (``urgent``,
# ``importance``). Потолок урезает ОТВЕТ, и упереться в него значит получить
# обрыв JSON на середине пачки — то есть потерять вердикты по уже оплаченным
# постам, а не сэкономить.
_MAX_TOKENS_PER_POST = 240
# Текст поста в промпте. Длиннее — деньги без пользы: тема и действие видны по
# первым абзацам, а хвост длинных постов это обычно контакты и хэштеги.
_POST_TEXT_CAP = 2000

TASK_PROMPT = """Ты — редактор районной новостной ленты ВКонтакте. Тебе дают пачку
собранных постов. По КАЖДОМУ вынеси вердикт по правилам ниже.

По каждому посту реши пять вещей:
1. **theme** — тема СТРОГО из канонического словаря тем в правилах ниже.
2. **action** — `publish` (годится в сводку), `delete` (мусор, не наше, реклама,
   перепечатка), `hold` (не уверен / нужен человек).
3. **merge_with** — если пост об ОДНОМ И ТОМ ЖЕ событии, что другой пост этой же
   пачки, перечисли его lip. Нет такого — пустой список.
4. **urgent** — `true` ТОЛЬКО для практической срочной информации жителям,
   которую поздно читать завтра: отключения электричества, воды, тепла, газа;
   перекрытия дорог и остановка транспорта; аварии и ремонт коммуникаций;
   официальные предупреждения об опасном явлении (МЧС, штормовое, паводок);
   срочный розыск пропавшего человека. Во ВСЕХ остальных случаях `false`.
   Криминальная хроника, ДТП, суды, военные сводки, смерти и соболезнования
   срочными НЕ считаются — даже когда выглядят громко.
5. **importance** — 0..100, насколько новость важна жителям района. Просмотры и
   лайки в карточке поста — подсказка, а не приговор: свежий пост их набрать
   ещё не успел, а перепечатка старой сенсации могла набрать много.

ЖЁСТКИЕ ПРАВИЛА:
- Не выдумывай фактов и не достраивай смысл: решай по тому, что написано.
- Не уверен между publish и delete — ставь `hold`. Ошибочный `delete` молча
  убирает новость из ленты района, и никто этого не заметит.
- `confidence` — честная оценка 0..100, а не вежливые 90 везде.
- `reasoning` — одна короткая фраза, до 200 символов.
- `urgent: true` двигает пост в ленте вперёд всех остальных, поэтому ставится
  редко. Сомневаешься — `false`.

Ответ — СТРОГО JSON, без markdown-обёртки:
{"verdicts": [{"lip": "...", "theme": "...", "action": "publish|delete|hold",
"merge_with": [], "split": false, "confidence": 0-100, "urgent": false,
"importance": 0-100, "reasoning": "..."}]}

Ровно один вердикт на каждый поданный пост, lip копируй дословно.

=== ПРАВИЛА КЛАССИФИКАЦИИ (постулаты) ===
"""


def has_text(post: Dict[str, Any]) -> bool:
    """Есть ли у поста текст, по которому вообще можно судить."""
    return bool(str(post.get("text") or post.get("post_text") or "").strip())


def _post_text(post: Dict[str, Any]) -> str:
    return str(post.get("text") or post.get("post_text") or "").strip()


def build_system_prompt(postulates: str) -> str:
    """Задача + эффективные постулаты. Одинаков для всех чанков прогона."""
    return TASK_PROMPT + (postulates or "").strip()


def _metrics_line(post: Dict[str, Any]) -> str:
    """Счётчики поста строкой. «—» там, где ВК ничего не прислал.

    Прочерк вместо нуля намеренно: «не измерено» и «ноль просмотров» — разные
    сведения, и подсовывать модели ноль вместо неизвестности значит врать ей
    про популярность свежего поста.
    """
    from utils.post_utils import post_counts

    c = post_counts(post)

    def _v(key: str) -> str:
        return "—" if c[key] is None else str(c[key])

    return (
        f"метрики: просмотры {_v('views')}, лайки {_v('likes')}, "
        f"репосты {_v('reposts')}, комментарии {_v('comments')}"
    )


def _age_line(post: Dict[str, Any]) -> str:
    """Возраст поста в часах на момент разбора. «—», если даты нет."""
    raw = post.get("date")
    if raw is None:
        published = post.get("published_at")
        if isinstance(published, datetime):
            hours = (datetime.utcnow() - published).total_seconds() / 3600.0
            return f"возраст: {max(0.0, hours):.1f} ч"
        return "возраст: —"
    try:
        hours = (time.time() - float(raw)) / 3600.0
    except (TypeError, ValueError):
        return "возраст: —"
    return f"возраст: {max(0.0, hours):.1f} ч"


def build_user_prompt(posts: Sequence[Dict[str, Any]]) -> str:
    """Пачка постов одного региона. lip — ключ, по которому вернётся вердикт.

    Метрики и возраст поехали в промпт 2026-09-22 (решение владельца): раньше
    модель судила только по тексту, а рейтинг считался после неё и отдельно,
    так что два сигнала о посте нигде не встречались. Цена — около двадцати
    токенов на пост.
    """
    parts: List[str] = []
    for i, p in enumerate(posts, 1):
        media = p.get("media") or []
        kinds = sorted({str(m.get("type")) for m in media if isinstance(m, dict) and m.get("type")})
        parts.append(
            "\n".join(
                [
                    f"### Пост {i}",
                    f"lip: {p.get('lip')}",
                    f"регион: {p.get('region_code') or '—'}",
                    f"тема сводки (подсказка сбора): {p.get('theme') or '—'}",
                    f"вложения: {', '.join(kinds) or 'нет'}",
                    _metrics_line(p),
                    _age_line(p),
                    f"текст: {_post_text(p)[:_POST_TEXT_CAP]}",
                ]
            )
        )
    return "\n\n".join(parts)


def group_by_region(posts: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Перегруппировать батч по регионам, сохраняя порядок внутри региона."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for p in posts:
        out.setdefault(str(p.get("region_code") or ""), []).append(p)
    return out


def chunk_posts(
    posts: Sequence[Dict[str, Any]], size: int = DEFAULT_CHUNK_SIZE
) -> List[List[Dict[str, Any]]]:
    """Нарезать батч на чанки, НЕ смешивая регионы (см. docstring модуля)."""
    size = max(1, int(size))
    chunks: List[List[Dict[str, Any]]] = []
    for _region, group in sorted(group_by_region(posts).items()):
        for i in range(0, len(group), size):
            chunks.append(group[i : i + size])
    return chunks


def _extract_json(content: str) -> Optional[Dict[str, Any]]:
    """Достать JSON, даже если модель обернула его в ```json (частая привычка)."""
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_chunk_response(
    payload: Dict[str, Any], posts: Sequence[Dict[str, Any]]
) -> Tuple[List[ClassifierVerdict], List[str]]:
    """Разобрать ответ модели в вердикты. Возвращает ``(вердикты, причины отказов)``.

    Снапшот (text/url/region_code) подставляем СВОИ, а не эхо модели: он у нас
    уже есть, и просить модель переписывать текст поста — это платить за него
    второй раз и получать шанс на искажение.

    Вердикт с незнакомым ``lip`` отбрасывается: модель либо перепутала ключ,
    либо сочинила пост. Записать такой вердикт значит заблокировать чужую
    публикацию по чужому решению.
    """
    by_lip = {str(p.get("lip")): p for p in posts}
    verdicts: List[ClassifierVerdict] = []
    problems: List[str] = []
    seen: set = set()

    raw_list = payload.get("verdicts")
    if not isinstance(raw_list, list):
        return [], ["ответ без списка verdicts"]

    for raw in raw_list:
        v = parse_verdict_loose(raw)
        if v is None:
            problems.append("нечинимый вердикт (нет lip или theme)")
            continue
        post = by_lip.get(v.lip)
        if post is None:
            problems.append(f"незнакомый lip {v.lip!r}")
            continue
        if v.lip in seen:
            problems.append(f"дубль вердикта на {v.lip!r}")
            continue
        seen.add(v.lip)
        # Действие в поле темы тема обнуляет молча (schema.normalized_theme).
        # В сводке прогона это должно быть видно: молчание тут читалось бы как
        # «модель тематизировала пачку», хотя тематизации не случилось.
        if not v.normalized_theme():
            problems.append(f"действие в поле темы у {v.lip!r} — тема обнулена")
        # Склейка допустима только внутри поданной пачки — иначе модель
        # ссылается на пост, которого не видела.
        v.merge_with = [m for m in v.merge_with if m in by_lip and m != v.lip]
        v.text = _post_text(post)[:10000]
        v.url = str(post.get("url") or post.get("post_url") or "")[:300]
        v.region_code = str(post.get("region_code") or "")[:50]
        verdicts.append(v)

    missing = [lip for lip in by_lip if lip not in seen]
    if missing:
        problems.append(f"без вердикта осталось {len(missing)} пост(ов)")
    return verdicts, problems


def classify_chunk(
    posts: Sequence[Dict[str, Any]],
    *,
    postulates: str,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Один вызов модели на чанк. Никогда не бросает."""
    if not posts:
        return {"ok": True, "verdicts": [], "problems": [], "usage": None}

    result = chat(
        system=build_system_prompt(postulates),
        user=build_user_prompt(posts),
        temperature=0.2,
        max_tokens=_MAX_TOKENS_PER_POST * len(posts),
        json_object=True,
        model=model,
        timeout=timeout,
        label="headless",
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "reason": result.get("reason"),
            "detail": result.get("detail"),
            "verdicts": [],
            "problems": [],
            # Обрезанный по max_tokens вызов оплачен — его расход в учёт.
            "usage": result.get("usage"),
        }

    payload = _extract_json(str(result.get("content") or ""))
    if payload is None:
        return {
            "ok": False,
            "reason": "llm_unparseable",
            "verdicts": [],
            "problems": [],
            "usage": result.get("usage"),
        }

    verdicts, problems = parse_chunk_response(payload, posts)
    for v in verdicts:
        v.model = str(result.get("model") or "")[:100] or None
    return {
        "ok": True,
        "verdicts": verdicts,
        "problems": problems,
        "usage": result.get("usage"),
        "model": result.get("model"),
    }


def classify_posts(
    posts: Sequence[Dict[str, Any]],
    *,
    postulates: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Классифицировать батч. Возвращает вердикты + счётчики прогона.

    Отказ одного чанка не отменяет остальные: посты независимы, а прогон стоит
    денег. Причины отказов копятся в ``failures`` и уходят в лог целиком.

    ``timeout`` — потолок одного чанка. Путь публикации передаёт короткий
    (``config.opencode.get_publish_timeout``): волна не должна стоять в слоте
    воркера, пока free-тир думает. ``None`` — общий потолок движка.
    """
    with_text = [p for p in posts if has_text(p)]
    skipped_no_text = len(posts) - len(with_text)

    all_verdicts: List[ClassifierVerdict] = []
    problems: List[str] = []
    failures: List[str] = []
    tokens = 0
    chunks = chunk_posts(with_text, chunk_size)

    def _drain() -> None:
        nonlocal tokens
        for chunk in chunks:
            res = classify_chunk(chunk, postulates=postulates, model=model, timeout=timeout)
            usage = res.get("usage") or {}
            tokens += int(usage.get("total_tokens") or 0)
            if not res.get("ok"):
                failures.append(str(res.get("reason")))
                continue
            all_verdicts.extend(res["verdicts"])
            problems.extend(res["problems"])

    if engine_enabled():
        # Serve живёт ровно на батч (oneshot): воркер/таска его не носит
        # постоянно — боксу 1.5 ГБ (аудит 14.10). Порт уже занят живым
        # serve — переиспользуем; свободен — ребёнок, гасится в finally.
        from modules.opencode_engine import harness_server

        with harness_server():
            _drain()
    else:
        _drain()

    return {
        "verdicts": all_verdicts,
        "chunks": len(chunks),
        "posts_in": len(posts),
        "skipped_no_text": skipped_no_text,
        "failures": failures,
        "problems": problems,
        "tokens": tokens,
    }


def summarize(run: Dict[str, Any]) -> str:
    """Однострочная сводка прогона для лога."""
    return "posts=%s chunks=%s verdicts=%s skipped_no_text=%s failures=%s tokens=%s" % (
        run.get("posts_in"),
        run.get("chunks"),
        len(run.get("verdicts") or []),
        run.get("skipped_no_text"),
        len(run.get("failures") or []),
        run.get("tokens"),
    )


def actions_histogram(verdicts: Iterable[ClassifierVerdict]) -> Dict[str, int]:
    """Распределение действий — то, чем headless сверяется с рутиной."""
    out: Dict[str, int] = {}
    for v in verdicts:
        out[v.normalized_action()] = out.get(v.normalized_action(), 0) + 1
    return out
