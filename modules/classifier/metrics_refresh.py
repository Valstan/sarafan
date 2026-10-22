"""Обновление метрик собранных постов — данные под рейтинг (звено 5, шаг 1).

Метрики в момент сбора почти нулевые: пост собирается через минуты после
публикации, и лайков у него ещё нет. Поэтому рейтинг строится не на том, что
видел сбор, а на том, что доросло за окно отсева.

**Границы прохода — правила владельца, не оптимизация:**

* **не трогаем посты старше окна** (:mod:`modules.classifier.audit_window`) —
  они всё равно отсеются по старости, и тратить на них вызовы ВК незачем;
* **не трогаем уже опубликованное нами** (``work_tables.lip``) — их рейтинг
  ни на что не влияет, пост из мешка уже ушёл;
* **берём обе стороны аудита, ``kept`` и ``dropped``.** Без метрик на
  отсеянных нельзя проверить находку D-024 (ИИ считает публикуемыми 43% того,
  что выкинули алгоритмы), а именно на неё опирается будущее снятие фильтров.

Объём посчитан на проде 2026-08-19: окно 72 часа = 7774 строки по 29 регионам,
то есть 78 батчей за круг. Замер 22.10: окно выросло до **~15 000 строк**,
8 кругов в сутки, круг 290–368 с и ~14 500 записанных строк в каждом. Batches
идут через общий с парсером per-token тормоз (см.
``vk_monitor/post_metrics.py``), поэтому круг занимает десятки секунд — время,
на которое сессия БД специально отпускается (см. :func:`refresh_metrics`).

**Запись — пачками.** Раньше на каждую строку шёл свой ``UPDATE``: 14 500
кругов до Postgres внутри одной транзакции, и это была половина времени круга.
Теперь одна пачка = один ``WITH v(...) AS (VALUES ...) UPDATE ... FROM v``
(:func:`_apply_batch`), а ``RETURNING lip`` даёт честный счёт изменённых строк
там, где ``rowcount`` у CTE-формы в SQLite приходит ``-1``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from config.classifier import get_metrics_fetch_workers
from modules.classifier.audit_window import AUDIT_WINDOW_HOURS, in_window, window_cutoff
from modules.vk_monitor.post_metrics import Ref

logger = logging.getLogger(__name__)

_WALL_RE = re.compile(r"wall(-?\d+)_(\d+)\s*$")

# Поля метрик, которые таска пишет в аудит. Порядок не важен, важен состав:
# всё, чего ВК не прислал, не должно затирать уже измеренное.
_METRIC_FIELDS = ("views", "likes", "comments", "reposts")

# Сколько строк аудита уходит одним ``UPDATE ... FROM (VALUES ...)``. 500 — это
# 3 500 биндов на запрос: далеко от потолка Postgres (65 535) и SQLite (32 766),
# но и не «по одной строке». Замер 22.10: круг переписывал ~14 500 строк
# отдельными запросами и шёл 290–368 с.
_BATCH_ROWS = 500


def ref_from_post_url(url: Optional[str]) -> Optional[Ref]:
    """``https://vk.com/wall-100_7`` → ``(-100, 7)``.

    ``lip`` для этого не годится: он хранится как ``{abs(owner_id)}_{id}`` и
    знак владельца теряет, а ``wall.getById`` без знака отдаст чужой пост или
    ничего. В ``post_url`` знак сохранён — берём оттуда.
    """
    if not url:
        return None
    m = _WALL_RE.search(str(url))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def drop_already_published(
    candidates: Sequence[Tuple[Ref, str]],
    published_lips: Set[str],
) -> List[Tuple[Ref, str]]:
    """Выкинуть посты, которые мы уже опубликовали. Чистая функция."""
    if not published_lips:
        return list(candidates)
    return [(ref, lip) for ref, lip in candidates if lip not in published_lips]


async def load_published_lips(session, candidate_lips: Optional[Set[str]] = None) -> Set[str]:
    """Опубликованные нами lip'ы из ``work_tables.lip`` (JSON-списки).

    В эту колонку пишут четыре разных модуля (``cascaded_bulletin``,
    ``copy_setka_network``, ``krugozor_broadcast``, ``telegram_gonba_mirror``),
    и схема JSON ничего не гарантирует. Не-список здесь бросал бы
    ``TypeError``, который внешний ``try/except`` Celery-таски превращает в
    ``ok: False`` на КАЖДОМ круге раз в 3 часа — метрики перестали бы
    обновляться совсем, а заметно это было бы только в логе. Поэтому битую
    строку пропускаем с предупреждением, а круг доезжает на остальных.

    ``candidate_lips`` — те lip'ы, про которые вопрос вообще задан. Без него
    функция поднимала в память ВСЮ колонку: замер на проде 2026-09-22 —
    **129 764 lip'а** в 643 строках, и всё это множество строилось каждые три
    часа ради пересечения с ~7,8 тыс. кандидатов окна. На боксе с 1536 МБ и
    без swap (P163/P164) разница заметная, а достаётся даром: ответ функции от
    сужения не меняется — ``drop_already_published`` всё равно смотрит только
    на кандидатов.
    """
    from sqlalchemy import select

    from database.models_extended import WorkTable

    # Спрашивать не про что — и запрос не нужен.
    if candidate_lips is not None and not candidate_lips:
        return set()

    if candidate_lips is not None and _is_postgres(session):
        try:
            return await _published_lips_pg(session, candidate_lips)
        except Exception:
            # Откат, а не падение — и это не перестраховка «на всякий случай».
            # Ошибка здесь прилетела бы внутрь ``try/except`` Celery-таски и
            # стала бы ``ok: False`` каждые 3 часа: метрики перестали бы
            # обновляться совсем, рейтинг застыл бы на старых числах, и видно
            # это было бы только в логе — ровно тот отказ, против которого
            # писана эта функция. Портируемая ветка медленнее, но верна всегда.
            logger.warning(
                "load_published_lips: быстрый путь PostgreSQL не сработал, "
                "иду полным проходом по work_tables",
                exc_info=True,
            )
            # Неудачный запрос оставляет транзакцию PostgreSQL в aborted, и
            # следующий SELECT упал бы уже по этой причине. Читающая
            # транзакция, откатывать нечего.
            await session.rollback()

    out = set()
    bad = 0
    result = await session.stream(select(WorkTable.lip))
    async for (lips,) in result:
        if lips is None:
            continue
        if not isinstance(lips, (list, tuple)):
            bad += 1
            continue
        for lip in lips:
            lip_str = str(lip)
            if candidate_lips is None or lip_str in candidate_lips:
                out.add(lip_str)
    if bad:
        logger.warning(
            "load_published_lips: %d строк work_tables.lip не список — пропущены; "
            "их публикации не будут исключены из обновления метрик",
            bad,
        )
    return out


async def _published_lips_pg(session, candidate_lips: Set[str]) -> Set[str]:
    """Быстрый путь: разворот JSON-массива и пересечение — на стороне БД.

    ``::json`` стоит осознанно. Модель объявляет колонку как ``JSON``, но
    ``json_array_elements_text`` принимает ровно ``json``, а неявного
    приведения между ``json`` и ``jsonb`` в PostgreSQL нет: окажись колонка
    на боксе ``jsonb`` — запрос падал бы каждые три часа. Явный каст верен для
    обоих типов и для ``json`` бесплатен.

    ``json_typeof`` держит то же обещание, что ``isinstance`` в портируемой
    ветке: строка-не-массив пропускается, а не роняет круг.
    """
    from sqlalchemy import text as sa_text

    rows = (
        await session.execute(
            sa_text(
                "SELECT DISTINCT e.lip FROM work_tables wt "
                "CROSS JOIN LATERAL json_array_elements_text(wt.lip::json) AS e(lip) "
                "WHERE wt.lip IS NOT NULL AND json_typeof(wt.lip::json) = 'array' "
                "AND e.lip = ANY(:cands)"
            ),
            {"cands": list(candidate_lips)},
        )
    ).all()
    out = {str(lip) for (lip,) in rows}

    # Сигнал о битых строках на этом пути не теряем: его цена — один счётчик
    # по той же небольшой таблице, а молчащая порча данных дороже.
    bad_rows = (
        await session.execute(
            sa_text(
                "SELECT count(*) FROM work_tables "
                "WHERE lip IS NOT NULL AND json_typeof(lip::json) <> 'array'"
            )
        )
    ).scalar()
    if bad_rows:
        logger.warning(
            "load_published_lips: %d строк work_tables.lip не список — пропущены; "
            "их публикации не будут исключены из обновления метрик",
            int(bad_rows),
        )
    return out


def _is_postgres(session) -> bool:
    """PostgreSQL ли под сессией. Не знаем — считаем, что нет.

    Ветка с ``LATERAL`` — оптимизация, а не требование: тесты ходят по SQLite,
    и ошибиться в определении диалекта здесь должно быть безопасно.
    """
    try:
        return getattr(getattr(session.bind, "dialect", None), "name", "") == "postgresql"
    except Exception:  # pragma: no cover - сессия без bind
        return False


async def select_refresh_candidates(
    session,
    *,
    hours: int = AUDIT_WINDOW_HOURS,
) -> Tuple[List[Tuple[Ref, str]], int]:
    """Посты аудита в окне ``hours`` → ``(кандидаты, сколько url не разобрано)``.

    Окно — общее с витриной (:mod:`modules.classifier.audit_window`): то, что
    мы меряем, и то, что показываем, обязано совпадать, иначе на панели
    появятся строки, метрики которых никто не обновляет.

    Число неразобранных ``post_url`` возвращается, а не теряется молча:
    приёмочная проверка «доля ``views IS NOT NULL`` около 90%» без него
    показывала бы необъяснимое расхождение.
    """
    from sqlalchemy import select

    from database.models_extended import CollectedPostAudit

    cutoff = window_cutoff(hours)
    stmt = (
        select(CollectedPostAudit.post_url, CollectedPostAudit.lip)
        .where(in_window(cutoff))
        .order_by(CollectedPostAudit.collected_at.desc())
    )

    out: List[Tuple[Ref, str]] = []
    unparsable = 0
    for url, lip in (await session.execute(stmt)).all():
        ref = ref_from_post_url(url)
        if ref is None:
            unparsable += 1
            continue
        out.append((ref, lip))
    if unparsable:
        logger.warning(
            "select_refresh_candidates: %d строк аудита с неразбираемым post_url — "
            "метрики по ним не обновятся",
            unparsable,
        )
    return out, unparsable


def _batch_statement(rows: List[Dict[str, Any]], now: datetime, *, casts: bool):
    """Собрать один ``WITH v(...) AS (VALUES ...) UPDATE ... FROM v`` на пачку.

    Почему текст, а не Core-конструктор. ``values()`` в ``UPDATE ... FROM``
    SQLAlchemy не связывает: параметры теряются (``[parameters: [{}]]``), а
    алиас со списком колонок (``AS v(lip, views, ...)``) SQLite не понимает
    вовсе. CTE понимают оба диалекта.

    ``casts`` — только для PostgreSQL. Там ``VALUES`` с ``NULL`` в колонке
    получает тип ``text``, и ``COALESCE`` падает на несовпадении типов; явный
    ``CAST`` это снимает. В SQLite ``CAST(... AS TIMESTAMP)`` наоборот испортил
    бы дату (числовая близость типа съедает строку), поэтому без кастов.
    """
    from sqlalchemy import Boolean, DateTime, Integer, String, bindparam
    from sqlalchemy import text as sa_text

    def cast(name: str, sql_type: str) -> str:
        return f"CAST(:{name} AS {sql_type})" if casts else f":{name}"

    tuples = []
    params = []
    for i, row in enumerate(rows):
        values_sql = ", ".join(
            [
                cast(f"lip_{i}", "TEXT"),
                cast(f"views_{i}", "INTEGER"),
                cast(f"likes_{i}", "INTEGER"),
                cast(f"comments_{i}", "INTEGER"),
                cast(f"reposts_{i}", "INTEGER"),
                cast(f"pub_{i}", "TIMESTAMP"),
                cast(f"has_{i}", "BOOLEAN"),
            ]
        )
        tuples.append(f"({values_sql})")
        params.extend(
            [
                bindparam(f"lip_{i}", row["lip"], type_=String),
                bindparam(f"views_{i}", row["views"], type_=Integer),
                bindparam(f"likes_{i}", row["likes"], type_=Integer),
                bindparam(f"comments_{i}", row["comments"], type_=Integer),
                bindparam(f"reposts_{i}", row["reposts"], type_=Integer),
                bindparam(f"pub_{i}", row["published_at"], type_=DateTime),
                bindparam(f"has_{i}", row["has_metrics"], type_=Boolean),
            ]
        )
    params.append(bindparam("stamp_now", now, type_=DateTime))

    sql = (
        "WITH v(lip, views, likes, comments, reposts, published_at, has_metrics) AS "
        "(VALUES " + ", ".join(tuples) + ") "
        "UPDATE collected_post_audit SET "
        "views = COALESCE(v.views, collected_post_audit.views), "
        "likes = COALESCE(v.likes, collected_post_audit.likes), "
        "comments = COALESCE(v.comments, collected_post_audit.comments), "
        "reposts = COALESCE(v.reposts, collected_post_audit.reposts), "
        "published_at = COALESCE(collected_post_audit.published_at, v.published_at), "
        "metrics_updated_at = CASE WHEN v.has_metrics THEN "
        + cast("stamp_now", "TIMESTAMP")
        + " ELSE collected_post_audit.metrics_updated_at END "
        "FROM v "
        "WHERE collected_post_audit.lip = v.lip "
        "AND (v.has_metrics OR "
        "(collected_post_audit.published_at IS NULL AND v.published_at IS NOT NULL)) "
        # RETURNING, а не rowcount: у CTE-формы UPDATE в SQLite ``rowcount``
        # приходит ``-1``, и «обновлено 0» включило бы ложный гейт
        # ``no_metrics_fetched``. Возвращённые lip'ы — это ровно изменённые
        # строки, на обоих диалектах.
        "RETURNING collected_post_audit.lip"
    )
    return sa_text(sql).bindparams(*params)


async def _apply_batch(session, rows: List[Dict[str, Any]], now: datetime) -> int:
    """Один UPDATE на пачку строк: ``WITH ... UPDATE ... FROM v``.

    Зачем пачкой. Замер 22.10: круг обновлял **~14 500 строк** отдельными
    ``UPDATE``, и таска шла 290–368 с, стоя в единственном слоте воркера
    (48 мин в сутки). Строк столько не потому, что они меняются, — столько их
    в окне 72 ч, и каждая переписывалась своим запросом. Пачка снимает
    круги до Postgres: вместо 14 500 запросов — 29.

    ``COALESCE`` в ``SET`` держит главное обещание :func:`apply_metrics`:
    поле, которого ВК не прислал (``NULL`` в пачке), не затирает измеренное,
    а ``published_at`` заполняется только когда его ещё нет.

    ``WHERE`` отсекает строки, которые всё равно не изменились бы: строка без
    единой метрики и с уже проставленной датой — это не «обновление», и
    считать её изменённой значило бы врать в гейт ``no_metrics_fetched``.
    """
    res = await session.execute(_batch_statement(rows, now, casts=_is_postgres(session)))
    return len(res.scalars().all())


async def apply_metrics(
    session, metrics_by_ref: Dict[Ref, Dict[str, Any]], lip_by_ref: Dict[Ref, str]
) -> int:
    """Записать метрики в аудит. Возвращает число РЕАЛЬНО изменённых строк.

    Считаем реально изменённые строки, а не количество ответов ВК: гейт
    ``no_metrics_fetched`` в :func:`refresh_metrics` должен значить «в БД
    ничего не поменялось», иначе он снова разрешит тихий круг вхолостую.
    Счёт берётся из ``RETURNING lip``, а не из ``rowcount``: пачка уходит одним
    ``UPDATE``, но у CTE-формы в SQLite ``rowcount`` приходит ``-1``, и ноль
    вместо правды включил бы гейт на здоровом круге.

    ``published_at`` перезаписывается только когда его ещё нет: дата поста не
    меняется, а ответ ВК может её и не принести.

    **Отсутствующее не затирает измеренное.** Если ВК не прислал поле (сменился
    токен в карусели, community-token не видит просмотры), прежнее значение
    остаётся, и ``metrics_updated_at`` не штампуется на пустом месте: иначе
    строка становилась бы одновременно «свежей» и «не меренной», а витрина
    показывала бы прочерк на посте, который вчера был измерен.
    """
    now = datetime.utcnow()
    rows: List[Dict[str, Any]] = []
    for ref, m in metrics_by_ref.items():
        lip = lip_by_ref.get(ref)
        if not lip:
            continue
        measured = {f: m[f] for f in _METRIC_FIELDS if m.get(f) is not None}
        published_at = m.get("published_at")
        if not measured and not published_at:
            continue
        rows.append(
            {
                "lip": lip,
                "views": measured.get("views"),
                "likes": measured.get("likes"),
                "comments": measured.get("comments"),
                "reposts": measured.get("reposts"),
                "published_at": published_at,
                "has_metrics": bool(measured),
            }
        )

    updated = 0
    for start in range(0, len(rows), _BATCH_ROWS):
        updated += await _apply_batch(session, rows[start : start + _BATCH_ROWS], now)
    # Запись шла текстом, мимо ORM: объекты, уже поднятые в этой сессии, об
    # этом не знают и продолжали бы отдавать старые числа (в таске их нет —
    # там на руках только кортежи (ref, lip), но в тестах и в ручных прогонах
    # они есть). expire_all не ходит в БД, он только помечает объекты
    # протухшими.
    session.expire_all()
    await session.commit()
    return updated


async def refresh_metrics(session, *, hours: int = AUDIT_WINDOW_HOURS) -> Dict[str, Any]:
    """Один круг обновления метрик.

    Сама функция исключения не ловит — их ловит обвязка вокруг ``run_coro``
    в Celery-таске ``tasks.celery_app.refresh_post_metrics``. Здесь только
    явные неуспехи (нет токена, ВК не отдал ни одной метрики) — они
    возвращаются как результат, а не бросаются.
    """
    import vk_api

    from modules.vk_monitor.post_metrics import fetch_metrics_for_token
    from modules.vk_token_router import get_healthy_read_token

    candidates, unparsable = await select_refresh_candidates(session, hours=hours)
    # Спрашиваем только про кандидатов окна: ответ тот же, память — по числу
    # кандидатов, а не по всей истории публикаций.
    published = await load_published_lips(session, {lip for _, lip in candidates})
    live = drop_already_published(candidates, published)
    skipped = len(candidates) - len(live)
    if not live:
        return {
            "ok": True,
            "checked": 0,
            "updated": 0,
            "skipped_published": skipped,
            "unparsable_urls": unparsable,
        }

    token = await get_healthy_read_token()
    if not token:
        # Молчать нельзя: без токена метрики не обновятся ни разу, а рейтинг
        # тихо застынет на старых числах.
        logger.warning("refresh_metrics: живого READ-токена нет, круг пропущен")
        return {
            "ok": False,
            "error": "no_read_token",
            "checked": len(live),
            "updated": 0,
            "skipped_published": skipped,
            "unparsable_urls": unparsable,
        }

    # Отпускаем соединение с БД перед походом в ВК. Круг из 151 батча идёт
    # через per-token тормоз (0.4 с на вызов) — замер 22.10: 265 с в один
    # поток, 87 с в три. Держать всё это время открытую транзакцию значит
    # держать Postgres в idle-in-transaction: висит соединение из пула, а с
    # ним горизонт autovacuum. Ничего из выбранного не протухнет: дальше в
    # руках только кортежи (ref, lip), ORM-объектов мы не держим.
    await session.commit()

    api = vk_api.VkApi(token=token).get_api()
    lip_by_ref = {ref: lip for ref, lip in live}
    metrics = fetch_metrics_for_token(
        api,
        [ref for ref, _ in live],
        token=token,
        workers=get_metrics_fetch_workers(),
    )

    # Транзакция под запись открывается заново — следующим обращением к сессии.
    updated = await apply_metrics(session, metrics, lip_by_ref)
    if updated == 0:
        # Токен был, но в БД ничего не поменялось (бан токена посреди прохода,
        # сетевой сбой на всех батчах разом, ответы без единой метрики) —
        # fetch_metrics_for_token глотает отказы по-батчево и молча вернёт
        # пустой словарь. «Проверено много, обновлено ноль» — это отказ, а не
        # успех: без этой ветки он повторил бы инцидент 2026-08-19, где таска
        # трое суток рапортовала успех, ничего не сделав.
        logger.warning(
            "refresh_metrics: %d постов проверено, ни одна строка не изменилась — "
            "метрики от ВК не пришли ни на один батч",
            len(live),
        )
        return {
            "ok": False,
            "error": "no_metrics_fetched",
            "checked": len(live),
            "updated": 0,
            "skipped_published": skipped,
            "unparsable_urls": unparsable,
        }
    return {
        "ok": True,
        "checked": len(live),
        "updated": updated,
        "skipped_published": skipped,
        "unparsable_urls": unparsable,
    }
