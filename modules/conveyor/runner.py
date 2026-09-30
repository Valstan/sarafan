"""Прогон конвейера по одному сайту: отбор → классификация → доставка (D-015).

Три звена в **одном прогоне**, а не в трёх отложенных очередях — и это не
удобство, а требование: ссылки на медиа в аудите живут ровно столько, сколько
живут у ВК-CDN (G56/G63). Отложив доставку на «когда дойдут руки», конвейер
исправно отправлял бы новости с битыми картинками, и заметили бы это читатели.

Каждый пост проходит свой путь независимо: отказ на одном не мешает остальным.
Всё, что случилось, попадает в ``conveyor_deliveries`` — включая отказы, потому
что «почему этой новости нет на сайте» должно иметь ответ строкой в журнале.

**Публикация (D-091, 2026-09-14).** Shadow-фаза кончилась: портал попросил, а
владелец решил, что публикует конвейер, а не человек. Ожидание «автопубликация
появится на стороне сайта, когда наберётся agree-rate» не сбылось буквально —
agree-rate набрался (портал оценил разбивку и заголовки как «заметно лучше
ручного импорта»), но публиковать оказалось некому: за 32 дня из 379 черновиков
вышло 0. Право выражено ключом, а не флагом — сайт без выданного ключа
(Казанская) по-прежнему получает только черновики.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from config.content_conveyor import (
    get_batch_max,
    get_ingest_key,
    get_publish_key,
    get_source_days,
    wants_publish,
)
from modules.conveyor import classify as classify_mod
from modules.conveyor import delivery as delivery_mod
from modules.conveyor import source as source_mod

logger = logging.getLogger(__name__)

# Пауза между постами: DeepSeek ограничивает частоту, а конвейер не в горячем
# пути — торопиться некуда. Секунды, не миллисекунды: 20 постов × 1 с — это
# 20 секунд на прогон, что для трёх запусков в сутки несущественно.
DEFAULT_PACE_SECONDS = 1.0

# Ручной режим (вердикты глазами, движок DeepSeek лежит). Причины, которые
# закрывают пост навсегда, — решение человека, а не модели, и журнал обязан
# это различать: строка «manual_reject» читается как «оператор отказал»,
# «llm_reject» — «модель отказала». Смешав их, разбор «почему этой новости нет
# на сайте» через месяц будет врать о том, кто принял решение.
_DECISION_PREFIXES = ("llm_reject", "manual_reject")


def _manual_reason(refusal: Optional[str]) -> str:
    """Переименовать код отказа модели в код отказа человека.

    Гейты остаются теми же (``parse_verdict``), меняется только подпись: в
    журнале должно быть видно, что решение принял оператор, а не DeepSeek.
    """
    code = str(refusal or "unknown")
    if code.startswith("llm_reject"):
        return "manual_reject" + code[len("llm_reject") :]
    if code.startswith("llm_"):
        return "manual_" + code[len("llm_") :]
    return f"manual_{code}"


async def run_site(
    session,
    site: Dict[str, Any],
    *,
    rules: str = "",
    days: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    pace: float = DEFAULT_PACE_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
    verdicts: Optional[Mapping[str, Dict[str, Any]]] = None,
    collect_results: bool = False,
    dry_full: bool = False,
) -> Dict[str, Any]:
    """Один прогон по сайту. Возвращает сводку: сколько отобрано и чем кончилось.

    ``dry_run`` — пройти весь путь **без** классификации и доставки: отбор
    выполняется, журнал не пишется, наружу ничего не уходит. Нужен, чтобы
    посмотреть на живых данных, что именно конвейер собирается отправить, до
    того как он это отправит. ``dry_full`` добавляет в превью сам текст поста —
    этого хватает, чтобы прочитать кандидатов и написать по ним вердикты.

    ``verdicts`` — **ручной режим**: карта ``lip → вердикт`` в том же формате,
    что отдаёт модель (``action``/``section``/``title``/``text``). Классификация
    тогда не вызывается вовсе, а гейты вердикта — те же самые
    (``classify.parse_verdict``), включая сторож на дописанные факты. Пост без
    вердикта не уходит никуда и **не падает в LLM**: молчаливый уход в движок,
    который лежит, выглядел бы как «отбор пуст», а это другое совсем.

    ``collect_results`` кладёт в сводку построчный результат — он нужен ручному
    прогону для сверки с журналом, а автоматическому (beat) не нужен и потому
    по умолчанию не собирается.
    """
    site_key = str(site.get("key") or "").strip().lower()
    manual_mode = verdicts is not None
    stats: Dict[str, Any] = {
        "site": site_key,
        "selected": 0,
        "deduped": 0,
        "delivered": 0,
        "rejected": 0,
        "held": 0,
        "failed": 0,
        "skipped": 0,
        "without_verdict": [],
        "unknown_sections": [],
        "tokens": 0,
        "publish": bool(wants_publish(site)),
        "dry_run": bool(dry_run),
        "manual": bool(manual_mode),
    }

    posts = await source_mod.fetch_pending_for_site(
        session,
        site,
        days=days if days is not None else get_source_days(),
        limit=limit if limit is not None else get_batch_max(),
    )
    if not posts:
        stats["selected"] = 0
        return stats

    # Дубли снимаем ДО LLM и до доставки — в этом вся экономия (recommend brain
    # 2026-09-14). Одна новость от школы, ДК и газеты иначе стоила бы трёх
    # вызовов модели и трёх скачиваний одних и тех же фотографий приёмником.
    recent = await source_mod.fetch_recent_signatures(session, site=site_key)
    posts, dups = source_mod.split_near_duplicates(posts, recent=recent)
    stats["selected"] = len(posts)
    stats["deduped"] = len(dups)

    if dry_run:
        stats["preview"] = [
            {
                "lip": p["lip"],
                "theme": p["theme"],
                "chars": len(p["text"]),
                "media": len(p["media"]),
                **(
                    {
                        "text": p.get("text") or "",
                        "url": p.get("url") or "",
                        "published_at": p.get("published_at"),
                        "items": p.get("media") or [],
                    }
                    if dry_full
                    else {}
                ),
            }
            for p in posts
        ]
        stats["preview_duplicates"] = [
            {"lip": p["lip"], "dup_of": p.get("dup_of", "")} for p in dups
        ]
        return stats
    if not posts and not dups:
        return stats

    key = get_ingest_key(site)
    publish_key = get_publish_key(site)
    sections = site.get("sections") or ()
    # Журнал заводим **только на то, что реально пойдёт в обработку**. В ручном
    # режиме это неочевидно и дорого: отбор исключает любой lip, у которого есть
    # строка журнала (в любом статусе), — значит кандидат без вердикта, попавший
    # в record_selection, тихо выпадает из будущих прогонов навсегда. Модельный
    # путь этой ловушки не знает (там вердикт есть у каждого), а ручный знает
    # всегда: часть кандидатов в любой порции остаётся без решения.
    to_process = [p for p in posts if str(p.get("lip") or "") in verdicts] if manual_mode else posts
    await source_mod.record_selection(
        session, site=site_key, lips=[p["lip"] for p in to_process + dups]
    )
    # Дубль закрываем строкой журнала сразу: без неё следующий прогон подберёт
    # его как новый пост и заплатит за ту же новость второй раз. Причина несёт
    # lip победителя — «почему этой новости нет на сайте» обязано иметь ответ.
    for dup in dups:
        await source_mod.update_delivery(
            session,
            site=site_key,
            lip=str(dup.get("lip") or ""),
            status="rejected",
            reason=f"dup:{dup.get('dup_of') or '?'}",
        )
    await session.commit()
    if dups:
        logger.info(
            "конвейер %s: снято дублей до LLM — %d (%s)",
            site_key,
            len(dups),
            ", ".join(f"{d.get('lip')}→{d.get('dup_of')}" for d in dups[:5]),
        )

    results: List[Dict[str, Any]] = []
    for i, post in enumerate(posts):
        lip = str(post.get("lip") or "")
        manual = None
        if manual_mode:
            manual = verdicts.get(lip)
            if manual is None:
                # Пост без вердикта — не сбой и не отказ. Ни строки в журнале
                # (сказать нечего), ни ухода в LLM (движок может лежать, и тогда
                # тишина выглядела бы как «отбор пуст»).
                stats["skipped"] += 1
                stats["without_verdict"].append(lip)
                continue
        if i and pace and sleep is not None:
            sleep(pace)
        outcome = await _process_one(
            session,
            site,
            post,
            key=key,
            sections=sections,
            rules=rules,
            publish_key=publish_key,
            manual_verdict=manual,
        )
        results.append(outcome)
        stats[outcome["bucket"]] = stats.get(outcome["bucket"], 0) + 1
        stats["tokens"] += outcome.get("tokens") or 0
        await session.commit()

    if collect_results:
        stats["results"] = [
            {
                "lip": r.get("lip"),
                "bucket": r.get("bucket"),
                "http_status": r.get("http_status"),
                "remote_id": r.get("remote_id"),
                "reason": r.get("reason"),
            }
            for r in results
        ]

    stats["unknown_sections"] = classify_mod.collect_unknown_sections(
        [r["classify"] for r in results if r.get("classify")]
    )
    if stats["unknown_sections"]:
        # Не глушим и не подгоняем под черновой список рубрик приёмника —
        # директива просит вернуть, какая нарезка просится по факту потока.
        logger.info(
            "конвейер %s: рубрики вне списка сайта — %s",
            site_key,
            ", ".join(stats["unknown_sections"]),
        )
    return stats


async def retry_failed(
    session,
    site: Dict[str, Any],
    *,
    limit: int = 50,
    sleep: Optional[Callable[[float], None]] = None,
    pace: float = DEFAULT_PACE_SECONDS,
) -> Dict[str, Any]:
    """Дослать посты, упавшие на доставке, по уже сохранённым вердиктам.

    Классификация не повторяется: вердикт лежит в журнале с прошлого прогона, и
    платить за него второй раз незачем. Именно ради этого ``_process_one``
    сохраняет вердикт даже когда доставка провалилась.

    Берём только ``failed`` — ``rejected`` и ``held`` это решения, а не сбои,
    и досылать их нельзя.
    """
    from sqlalchemy import select

    from database.models_extended import ConveyorDelivery

    site_key = str(site.get("key") or "").strip().lower()
    key = get_ingest_key(site)
    publish_key = get_publish_key(site)
    stats = {"site": site_key, "retried": 0, "delivered": 0, "held": 0, "failed": 0}

    rows = (
        (
            await session.execute(
                select(ConveyorDelivery)
                .where(ConveyorDelivery.site == site_key)
                .where(ConveyorDelivery.status == "failed")
                .where(ConveyorDelivery.verdict.isnot(None))
                .order_by(ConveyorDelivery.created_at)
                .limit(max(1, limit))
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return stats

    posts = await source_mod.fetch_audit_snapshots(session, lips=[r.lip for r in rows])
    for i, row in enumerate(rows):
        post = posts.get(row.lip)
        if not post:
            continue
        if i and pace and sleep is not None:
            sleep(pace)
        stats["retried"] += 1
        body = delivery_mod.build_payload(
            post,
            row.verdict or {},
            date_iso=delivery_mod.iso_date(post.get("published_at")),
            publish=bool(publish_key),
        )
        bad = delivery_mod.check_invariant(body)
        if bad:
            await source_mod.update_delivery(
                session, site=site_key, lip=row.lip, status="held", reason=bad
            )
            stats["held"] += 1
            await session.commit()
            continue
        res = delivery_mod.deliver(site, key, body, sleep=time.sleep, publish_key=publish_key)
        await source_mod.update_delivery(
            session,
            site=site_key,
            lip=row.lip,
            status="delivered" if res.get("ok") else "failed",
            reason=None if res.get("ok") else str(res.get("reason") or "delivery_failed"),
            clear_reason=bool(res.get("ok")),
            attempts=res.get("attempts"),
            http_status=res.get("status"),
            remote_id=res.get("remote_id"),
        )
        stats["delivered" if res.get("ok") else "failed"] += 1
        await session.commit()
    return stats


async def _process_one(
    session,
    site: Dict[str, Any],
    post: Dict[str, Any],
    *,
    key: str,
    sections,
    rules: str,
    publish_key: str = "",
    manual_verdict: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Путь одного поста. Никогда не бросает — падение на одном не рушит прогон.

    ``manual_verdict`` — вердикт человека вместо модели (движок DeepSeek лежит).
    Разбор тот же (``classify.parse_verdict``), поэтому «нет заголовка», «текст
    раздулся» и прочие гейты работают и здесь: ручной путь не должен стать
    дырой, через которую на сайт уедет то, что модель бы не пропустила.
    """
    site_key = str(site.get("key") or "").strip().lower()
    lip = str(post.get("lip") or "")
    if manual_verdict is not None:
        verdict, refusal = classify_mod.parse_verdict(manual_verdict, post, sections=sections or ())
        verdict_res = (
            {"ok": True, "verdict": verdict, "usage": {"total_tokens": 0}, "source": "manual"}
            if verdict is not None
            else {"ok": False, "reason": _manual_reason(refusal), "source": "manual"}
        )
    else:
        try:
            # api_key НЕ передаём: у classify свой ключ (DeepSeek), а `key` здесь —
            # ключ доставки на сайт. Это разные секреты разных сторон, и смешать их
            # означало бы отправить ключ сайта в чужой API.
            verdict_res = classify_mod.classify(post, sections=sections, rules=rules)
        except Exception as e:  # pragma: no cover — защитный контур
            logger.warning("конвейер %s: классификация упала на %s: %s", site_key, lip, e)
            verdict_res = {"ok": False, "reason": "classify_crashed"}

    tokens = ((verdict_res.get("usage") or {}) or {}).get("total_tokens") or 0
    if not verdict_res.get("ok"):
        reason = str(verdict_res.get("reason") or "llm_failed")
        # Отказ модели — это решение («не для сайта»), а сбой связи — нет.
        # Первое закрывает пост навсегда, второе оставляет строку в failed,
        # чтобы следующий прогон её не подобрал молча как новую.
        bucket = "rejected" if reason.startswith(_DECISION_PREFIXES) else "failed"
        await source_mod.update_delivery(
            session, site=site_key, lip=lip, status=bucket, reason=reason
        )
        return {
            "lip": lip,
            "bucket": bucket,
            "tokens": tokens,
            "reason": reason,
            "classify": verdict_res,
        }

    verdict = verdict_res["verdict"]
    body = delivery_mod.build_payload(
        post,
        verdict,
        date_iso=delivery_mod.iso_date(post.get("published_at")),
        publish=bool(publish_key),
    )
    bad = delivery_mod.check_invariant(body)
    if bad:
        # Задержан, а не выброшен: строка в журнале со статусом held — это то,
        # что человек сможет посмотреть и решить сам (pool #133).
        await source_mod.update_delivery(
            session, site=site_key, lip=lip, status="held", reason=bad, verdict=verdict
        )
        return {
            "lip": lip,
            "bucket": "held",
            "tokens": tokens,
            "reason": bad,
            "classify": verdict_res,
        }

    res = delivery_mod.deliver(site, key, body, sleep=time.sleep, publish_key=publish_key)
    if res.get("ok"):
        await source_mod.update_delivery(
            session,
            site=site_key,
            lip=lip,
            status="delivered",
            verdict=verdict,
            attempts=res.get("attempts"),
            http_status=res.get("status"),
            remote_id=res.get("remote_id"),
        )
        return {
            "lip": lip,
            "bucket": "delivered",
            "tokens": tokens,
            "http_status": res.get("status"),
            "remote_id": res.get("remote_id"),
            "classify": verdict_res,
        }

    await source_mod.update_delivery(
        session,
        site=site_key,
        lip=lip,
        status="failed",
        reason=str(res.get("reason") or "delivery_failed"),
        verdict=verdict,
        attempts=res.get("attempts"),
        http_status=res.get("status"),
    )
    return {
        "lip": lip,
        "bucket": "failed",
        "tokens": tokens,
        "reason": str(res.get("reason") or "delivery_failed"),
        "http_status": res.get("status"),
        "classify": verdict_res,
    }
