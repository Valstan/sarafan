"""Журнал публикаций: что и когда реально вышло на стену (миграция 091).

Появился под квоты тем (заказ владельца 2026-08-30). Владелец задаёт долю темы
в ленте — «новости 50%, детсады 5%» — а доля считается ОТ ЧЕГО-ТО. Ни один
существующий журнал на вопрос «какие посты и когда мы опубликовали» не отвечает:
``work_tables.lip`` это курсор дедупа без времени и с потолком 1000,
``bulletin_curation_runs`` и ``collected_post_audit`` гейтованы и покрывают один
район, ``parsing_stats`` знает только счётчик кандидатов ДО отбора.

**Контракт безопасности — копия ``modules/curation/recorder.py``:**
  * **Изолированная сессия.** Своя ``AsyncSessionLocal()``, не трогает транзакцию
    публикации — падение журнала не откатит уже отправленное в ВК.
  * **Never raises.** Любое исключение глушится в WARNING.

**Но БЕЗ env-гейта, в отличие от recorder'а.** Гейтованный журнал считает по
дырявым данным: доля темы, посчитанная по одному району из двадцати девяти,
описывает не сеть, а этот район. Квота — механизм принятия решений, её вход
обязан быть полным.

Пишем только УСПЕШНЫЕ публикации. ``work_tables.lip`` двигается и на неуспехе
(курсор дедупа сознательно не ретраит), но журнал отвечает на другой вопрос —
«что стоит в ленте», — и провалившаяся отправка в нём завышала бы расход квоты.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


async def _resolve_verdict_themes(session, lips: Sequence[str]) -> Dict[str, Optional[str]]:
    """lip → канон-тема вердикта (или ``None``, если вердикта нет).

    Тему берём здесь, а не прокидываем через волну. У отбора карта lip → тема на
    руках, но она умирает вместе с постами, не влезшими в тройку
    ``max_posts_per_bulletin``, — а журналу нужны темы именно опубликованных.
    """
    from sqlalchemy import select

    from database.models_extended import ContentClassification
    from modules.classifier.service import _theme_canon_map, canonicalize_theme

    if not lips:
        return {}
    rows = (
        await session.execute(
            select(ContentClassification.lip, ContentClassification.verdict).where(
                ContentClassification.lip.in_(list(lips))
            )
        )
    ).all()
    if not rows:
        return {}
    canon = await _theme_canon_map(session)
    return {lip: canonicalize_theme((verdict or {}).get("theme"), canon) for lip, verdict in rows}


async def record_publication(
    *,
    region_code: str,
    wave_theme: str,
    kind: str,
    posts_included: Sequence[str],
    publish_result: Optional[Dict[str, Any]] = None,
    source_region_code: Optional[str] = None,
) -> None:
    """Записать опубликованные посты в журнал. Best-effort, никогда не бросает.

    Вызывается тем же циклом ``for kind, d, pub in results``, что и
    ``record_curation_run``, — то есть одна врезка покрывает районную волну,
    каскад, соседский канал, траурную сводку и хедлайнер.
    """
    try:
        lips = [str(lip) for lip in (posts_included or []) if str(lip or "").strip()]
        if not lips:
            return

        pub = publish_result or {}
        # Неуспешная отправка в журнал не идёт: см. шапку модуля. Отсутствие
        # ключа success трактуем как успех — так же, как это делает подсчёт
        # ``posts_published`` в волне, чтобы два счётчика не разъехались.
        if "success" in pub and not pub.get("success"):
            return

        # P215: журнал отвечает на вопрос «что стоит в ленте», а «wall.post
        # вернул post_id» — не ответ на него. Пост, ушедший user-токеном в
        # группу, где аккаунт не админ, лежит в очереди «Предложенные записи»
        # и подписчикам не виден — но id у него настоящий, и без проверки он
        # попадал в журнал как опубликованный (503 строки по трём группам,
        # инцидент 03.11). Издатель теперь отдаёт ``on_wall``: True — пост на
        # стене, False — может быть в предложенных. False ⇒ не засчитываем.
        # Метки нет (старый вызов) — ведём себя как раньше.
        if pub.get("on_wall") is False:
            logger.info(
                "publication journal: публикация не на стене (via=%s) — в журнал не идёт",
                pub.get("via") or "?",
            )
            return

        from database.connection import AsyncSessionLocal
        from database.models_extended import PublishedPost

        async with AsyncSessionLocal() as session:
            themes = await _resolve_verdict_themes(session, lips)
            for lip in lips:
                session.add(
                    PublishedPost(
                        lip=lip,
                        region_code=region_code,
                        wave_theme=wave_theme,
                        verdict_theme=themes.get(lip),
                        kind=kind or "regular",
                        source_region_code=source_region_code,
                        published_url=pub.get("url"),
                    )
                )
            await session.commit()
        logger.info(
            "publication journal: записано %d постов (регион=%s волна=%s вид=%s)",
            len(lips),
            region_code,
            wave_theme,
            kind,
        )
    except Exception:  # pragma: no cover — журнал НИКОГДА не валит публикацию
        logger.warning("record_publication failed (journal, ignored)", exc_info=True)


async def fetch_published_counts(
    session,
    region_code: Optional[str] = None,
    *,
    window_hours: int,
) -> Dict[str, int]:
    """Сколько постов каждой темы опубликовано за скользящее окно.

    ``region_code=None`` — по всей сети: доли задаются сетевыми, и страница долей
    показывает факт в том же разрезе, в каком владелец выставляет план. Волне же
    нужен свой район, иначе двадцать девять параллельных волн считали бы чужую
    ленту своей.

    Окно скользящее, а не календарные сутки, сознательно: календарный день даёт
    обрыв в полночь — утренняя волна стартует с пустым счётчиком и не ограничена
    ничем, а вечерние душатся. Скользящее держит знаменатель ровным с первой волны.

    Посты без темы вердикта в счёт не идут: доля темы считается среди тех, у кого
    тема есть. Ошибка чтения → пустой словарь, и квота на этой волне не применится
    (fail-open, см. ``modules.classifier.quota``).
    """
    from sqlalchemy import func, select

    from database.models_extended import PublishedPost

    try:
        cutoff = datetime.utcnow() - timedelta(hours=max(1, int(window_hours)))
        stmt = (
            select(PublishedPost.verdict_theme, func.count())
            .where(
                PublishedPost.published_at >= cutoff,
                PublishedPost.verdict_theme.is_not(None),
            )
            .group_by(PublishedPost.verdict_theme)
        )
        if region_code is not None:
            stmt = stmt.where(PublishedPost.region_code == region_code)
        rows = (await session.execute(stmt)).all()
        return {str(theme): int(count) for theme, count in rows if theme}
    except Exception as e:  # noqa: BLE001 — счётчик не важнее волны
        logger.warning("publication journal: чтение счётчиков не удалось: %s", e)
        return {}


async def prune_published_posts(session, *, keep_days: int = 400) -> int:
    """Удалить записи журнала старше ``keep_days``. Возвращает число удалённых.

    ~1200 строк в сутки — не горит, но ретеншен заводим сразу: журнал без него
    единственный в проекте растёт вечно, а окно квоты всё равно сутки.
    """
    from sqlalchemy import delete, select

    from database.models_extended import PublishedPost

    cutoff = datetime.utcnow() - timedelta(days=max(1, int(keep_days)))
    doomed: List[int] = list(
        (await session.execute(select(PublishedPost.id).where(PublishedPost.published_at < cutoff)))
        .scalars()
        .all()
    )
    if not doomed:
        return 0
    await session.execute(delete(PublishedPost).where(PublishedPost.id.in_(doomed)))
    await session.commit()
    return len(doomed)
