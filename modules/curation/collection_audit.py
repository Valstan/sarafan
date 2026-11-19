"""Fail-safe shadow-рекордер аудита сбора (ADR-0004, вариант B).

Классификатор видит ОБЕ стороны сбора: ``kept`` (пост прошёл детерминированный
фильтр — кандидат в публикацию) и ``dropped`` + причина (выброшен). Захват — на
границе сбора (``advanced_parser``), НИКОГДА не ломает публикацию:

  * **Изолированная сессия.** Своя ``AsyncSessionLocal()`` — падение не откатит сбор.
  * **Never raises.** Любое исключение глушится в WARNING (как ``recorder.py``).
  * **Gated.** OFF по умолчанию (``COLLECTION_AUDIT_SHADOW_ENABLED``) + allowlist
    регионов (``COLLECTION_AUDIT_REGION_CODES``) — обкатка на одном районе.

Причина отсева **пере-выводится** теми же чистыми функциями, что и
``_filter_post`` (``is_advertisement`` / ``check_blacklist`` / no-attachments), в
том же порядке — чтобы не трогать логику фильтра (нулевой риск для публикации).
Coupling с ``_filter_post`` отмечен в ADR-0004: при изменении фильтров синхронить.

Механические дропы (возраст / дедуп / black_id / blocked_lips) и region_words
(MVP-лимит) → ``reason=None`` → НЕ пишем: это не «пере-фильтрация», а корректный
шум. **Следствие, за которое уже заплачено:** «в таблице нет дропов» НЕ значит
«постов не отбрасывали» — самый частый слой отсева здесь невидим по замыслу.

**Таблица непригодна для сравнения регионов и тем по ОБЪЁМУ сбора.** ``lip``
уникален ГЛОБАЛЬНО (``CollectedPostAudit.lip``, ``unique=True``), а дедуп в
``record_collection_audit`` фильтрует по ``lip`` без предиката на ``region_code``
и ``theme``: пост записывается один раз в жизни — на ту пару (регион, тема), чья
волна увидела его первой во всей сети. У района, чьи стены читают волны восьми
тем в сутки, счётчик по одной теме систематически обнуляется в пользу более
ранней волны. Объём сбора мерить по ``parsing_stats.total_posts_scanned``
(P171, разбор 2026-09-22: сравнение районов по этой таблице дало неверный
диагноз — «6 против медианы 90» оказалось артефактом дедупа).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from config.classifier import advertisement_filter_enabled
from config.runtime import collection_audit_shadow_enabled, get_collection_audit_region_codes
from modules.filters.ads_filter import is_marked_advertisement
from utils.post_utils import clear_copy_history, lip_of_post, vk_post_datetime
from utils.text_utils import check_blacklist, is_advertisement, is_hard_spam, is_neighbor_bulletin
from utils.vk_attachments import summarize_media

logger = logging.getLogger(__name__)

_MAX_AUDIT_TEXT = 3000
# Темы, которым медиа НЕ обязательно (как в _filter_post шаг 8).
_NO_ATTACH_EXEMPT_THEMES = ("novost", "reklama", "oblast")


def should_audit(region_code: str) -> bool:
    """Дёшево решить, аудируем ли регион (флаг + allowlist) — без сборки payload."""
    if not collection_audit_shadow_enabled():
        return False
    allow = get_collection_audit_region_codes()
    if allow is not None and (region_code or "").strip().lower() not in allow:
        return False
    return True


def _has_media(post: Dict[str, Any]) -> bool:
    atts = post.get("attachments")
    return bool(atts) if isinstance(atts, (list, dict)) else False


def _post_lip(post: Dict[str, Any]) -> Optional[str]:
    try:
        owner_id = int(post.get("owner_id", post.get("from_id", 0)) or 0)
        post_id = int(post.get("id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not owner_id or not post_id:
        return None
    return lip_of_post(owner_id, post_id)


def _derive_drop_reason(post: Dict[str, Any], theme: str, region_config: Any) -> Optional[str]:
    """Пере-вывод причины content-дропа (порядок как в ``_filter_post``).

    None = механический дроп (возраст/дедуп/black_id) или region_words (MVP) →
    не пишем. Возвращаемые причины совпадают со счётчиками ``posts_filtered_*``.
    """
    text = (post.get("text") or "").strip()
    # Шаг 5a: жёсткий спам/скам — режется для ВСЕХ тем, включая reklama (как в
    # _filter_post шаг 5a). Проверяем первым — совпадает с порядком фильтра.
    if is_hard_spam(text):
        return "hard_spam"
    # Шаг 5b: своя же соседская сводка — конечный продукт, обратно в оборот не
    # берём никогда (матрёшка «сводка в сводке», Малмыж 2026-07-27). Это
    # content-дроп, а не механический, поэтому по ADR-0004 он обязан быть виден
    # классификатору — до 2026-09-22 шаг существовал в фильтре
    # (advanced_parser, счётчик posts_filtered_neighbor_bulletin) и отсутствовал
    # здесь, то есть соседские сводки выбрасывались без следа (P171).
    if is_neighbor_bulletin(text):
        return "neighbor_bulletin"
    # Шаг 5c: чужая РАЗМЕЧЕННАЯ реклама — тоже для всех тем, включая reklama.
    # Порядок и определение те же, что в _filter_post (coupling ADR-0004).
    if is_marked_advertisement(post):
        return "marked_ad"
    # Шаг 5: реклама (для темы reklama фильтр не срабатывает — как в _filter_post).
    # Флаг тот же, что у фильтра (CLASSIFIER_ADVERTISEMENT_FILTER_ENABLED):
    # аудит обязан пере-выводить ПРИЧИНУ настоящую, а не «как было до флага».
    if (
        advertisement_filter_enabled()
        and theme != "reklama"
        and is_advertisement(text, skip_for_reklama=False, theme=theme)
    ):
        return "advertisement"
    # Шаг 6: чёрный список слов региона.
    blacklist = getattr(region_config, "delete_msg_blacklist", None)
    if blacklist and check_blacklist(text, blacklist):
        return "blacklist_text"
    # Шаг 8: обязательные вложения для не-novost/reklama/oblast тем.
    if theme not in _NO_ATTACH_EXEMPT_THEMES and not _has_media(post):
        return "no_attachments"
    return None


def _snapshot(
    post: Dict[str, Any],
    *,
    lip: str,
    region_code: str,
    theme: str,
    decision: str,
    reason: Optional[str],
) -> Dict[str, Any]:
    owner_id = int(post.get("owner_id", post.get("from_id", 0)) or 0)
    post_id = int(post.get("id", 0) or 0)
    text = (post.get("text") or "").strip()
    try:
        media = summarize_media(post)
    except Exception:  # pragma: no cover — сводка медиа не должна ронять аудит
        media = []
    return {
        "lip": lip,
        "region_code": region_code,
        "theme": theme,
        "post_text": (text[:_MAX_AUDIT_TEXT] or None),
        "post_url": f"https://vk.com/wall{owner_id}_{post_id}",
        "published_at": vk_post_datetime(post.get("date")),
        "has_media": _has_media(post),
        "media": media or None,
        "decision": decision,
        "drop_reason": reason,
    }


def build_audit_records(
    *,
    region_code: str,
    theme: str,
    region_config: Any,
    collected: Sequence[Dict[str, Any]],
    kept: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Собрать записи аудита: все kept + content-дропы (без механических/дублей).

    Чистая функция (без БД) — тестируется отдельно. ``collected`` — сырой батч
    (до unwrap), ``kept`` — прошедшие ``_filter_post`` (уже unwrap). Матчинг по lip.
    """
    kept_lips = {lip for lip in (_post_lip(p) for p in kept) if lip}
    records: List[Dict[str, Any]] = []
    seen: set = set()
    for raw in collected:
        try:
            post = clear_copy_history(raw)  # unwrap репоста → lip как у _filter_post
        except Exception:  # pragma: no cover — битый пост не должен ронять аудит
            continue
        lip = _post_lip(post)
        if not lip or lip in seen:
            continue
        seen.add(lip)
        if lip in kept_lips:
            records.append(
                _snapshot(
                    post,
                    lip=lip,
                    region_code=region_code,
                    theme=theme,
                    decision="kept",
                    reason=None,
                )
            )
        else:
            reason = _derive_drop_reason(post, theme, region_config)
            if reason is None:
                continue  # механический дроп — не «пере-фильтрация», пропускаем
            records.append(
                _snapshot(
                    post,
                    lip=lip,
                    region_code=region_code,
                    theme=theme,
                    decision="dropped",
                    reason=reason,
                )
            )
    return records


async def record_collection_audit(
    *,
    region_code: str,
    theme: str,
    region_config: Any,
    collected: Sequence[Dict[str, Any]],
    kept: Sequence[Dict[str, Any]],
) -> None:
    """Запарковать аудит сбора (shadow). Best-effort: НИКОГДА не бросает.

    Идемпотентно по ``lip`` (first-seen wins). Вызывать ПОСЛЕ сбора/фильтрации —
    на падение аудита сбор/публикация не влияют.
    """
    try:
        if not should_audit(region_code):
            return
        records = build_audit_records(
            region_code=region_code,
            theme=theme,
            region_config=region_config,
            collected=collected or [],
            kept=kept or [],
        )
        added = 0
        if records:
            added = await _persist(records)
        # Печатаем ВСЕГДА и четырьмя числами. До 2026-09-22 строка стояла после
        # раннего `return` при пустых records, и журнал получался инвертирован:
        # «постов не было» не печаталось ВООБЩЕ, а `recorded 0` означало
        # противоположное — «посты пришли, прошли фильтр, но все их lip уже в
        # таблице». На этой инверсии разбор 22.09 построил неверный вывод о
        # пуле источников Подосиновца (P171). Теперь четыре случая различимы:
        #   collected=0                    — ВК не отдал ничего;
        #   collected=N records=0          — всё ушло в механические дропы;
        #   collected=N records=M added=0  — всё уже записано раньше (dup=M);
        #   added>0                        — записали новое.
        logger.info(
            "collection audit: collected=%d kept=%d records=%d added=%d dup=%d "
            "(region=%s theme=%s; records = kept + content-drops)",
            len(collected or []),
            len(kept or []),
            len(records),
            added,
            len(records) - added,
            region_code,
            theme,
        )
    except Exception:  # pragma: no cover — аудит НИКОГДА не валит сбор
        # region/theme в сообщении: без них упавший аудит неатрибутируем, и
        # «регион молчит» неотличимо от «аудит по региону падает».
        logger.warning(
            "record_collection_audit failed (shadow, ignored; region=%s theme=%s)",
            region_code,
            theme,
            exc_info=True,
        )


async def _persist(records: List[Dict[str, Any]]) -> int:
    """Записать новые снимки, вернуть сколько добавлено. Идемпотентно по ``lip``.

    Дедуп по ``lip`` БЕЗ предиката на регион и тему — так задумано (first-seen
    wins), но именно поэтому таблица не годится для сравнения объёмов; см.
    докстринг модуля.
    """
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models_extended import CollectedPostAudit

    lips = [r["lip"] for r in records]
    async with AsyncSessionLocal() as session:
        existing = {
            lip
            for (lip,) in (
                await session.execute(
                    select(CollectedPostAudit.lip).where(CollectedPostAudit.lip.in_(lips))
                )
            ).all()
        }
        added = 0
        for r in records:
            if r["lip"] in existing:
                continue
            existing.add(r["lip"])
            session.add(CollectedPostAudit(**r))
            added += 1
        if added:
            await session.commit()
    return added
