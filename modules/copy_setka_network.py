"""
Сетевой «хаб» SETKA: группа copy_by_setka → главные стены регионов.

Правила (текст поста на стене источника — главное поле text):
- Если в text есть слово «репост» (без учёта регистра) — на региональные стены
  уходит VK wall.repost прикреплённого поста (copy_history[0] или attachment type=wall).
- Иначе — копия содержимого: при repost-цепочке (copy_history) берётся исходный пост
  целиком (текст + вложения); иначе — сам пост. Публикация wall.post по регионам.
- Ключевые слова маршрутизации (подстрока, без учёта регистра) сужают рассылку
  до поддерева области: «киров» — сама `kirov_obl` + её районы, «казань» —
  `tatarstan_obl` + её районы. Нет ключевых слов или есть оба сразу — вся сеть.
  Слово-маршрут ортогонально «репост»/«копия»: «репост Казань» — это репост
  оригинала, но только по Татарстану.

За один запуск обрабатывается не больше одного нового поста; wall.get — последние 10;
история дублей (lip) — не больше 10 идентификаторов.
"""

from __future__ import annotations

import asyncio
import copy as copy_lib
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

WALL_FETCH_COUNT = 10
LIP_HISTORY_MAX = 10
# После стольких тиков с недобором (VK Captcha на хвосте) — сдаёмся и помечаем
# пост разосланным, чтобы не застрять навсегда и не блокировать новые посты.
PENDING_MAX_TRIES = 4


def _text_has_repost_keyword(text: str) -> bool:
    return "репост" in (text or "").lower()


# Слово-маршрут → код области. Подстрока тем же приёмом, что «репост»:
# «киров» ловит и «Киров», и «Кировская область», и «Кирово-Чепецк» — всё это
# Кировская область и есть. «Казань» — город, но в сетке это имя Татарстана:
# так сказал владелец, так и записано.
SCOPE_KEYWORDS = {
    "киров": "kirov_obl",
    "казань": "tatarstan_obl",
}


def resolve_scope_codes(text: str, regions: List[Any]) -> Optional[Set[str]]:
    """Коды регионов для рассылки по тексту хаб-поста. ``None`` = вся сеть.

    Ровно одно слово-маршрут → область + все её потомки по ``parent_region_id``
    (сама область входит: у неё своя стена). Ноль или оба сразу → ``None``:
    оба поддерева вместе и есть вся сеть, а перечислять их кодами значит
    потерять будущую третью область. Корня нет в списке регионов → пустое
    множество (вызывающий закроет пост с варнингом, а не заклинит хаб).
    """
    body = (text or "").lower()
    matched = {oblast for word, oblast in SCOPE_KEYWORDS.items() if word in body}
    if len(matched) != 1:
        return None
    (oblast_code,) = sorted(matched)
    root = None
    children: Dict[Any, List[Any]] = {}
    for reg in regions:
        if reg.code == oblast_code:
            root = reg
        pid = getattr(reg, "parent_region_id", None)
        if pid is not None:
            children.setdefault(pid, []).append(reg)
    if root is None:
        return set()
    scope = {root.code}
    root_id = getattr(root, "id", None)
    if root_id is None:
        return scope
    stack = [root_id]
    while stack:
        for child in children.pop(stack.pop(), ()):
            if child.code not in scope:
                scope.add(child.code)
                child_id = getattr(child, "id", None)
                if child_id is not None:
                    stack.append(child_id)
    return scope


def _mark_post_done(wt: Any, lip: str) -> None:
    """Добавить lip в список полностью разосланных постов (с дедупом и cap)."""
    prev = list(wt.lip or [])
    if lip not in prev:
        prev.append(lip)
    wt.lip = prev[-LIP_HISTORY_MAX:]


async def _fetch_post_by_lip(token: str, lip: str) -> Optional[Dict[str, Any]]:
    """Дофетчить исходный пост по lip (когда он уехал из последних N постов)."""
    from modules.vk_monitor.vk_client import VKClient

    try:
        abs_oid_s, pid_s = lip.split("_")
        owner = -abs(int(abs_oid_s))
        pid = int(pid_s)
    except (ValueError, AttributeError):
        return None
    fetched = await asyncio.to_thread(VKClient(token).get_posts_by_ids, [(owner, pid)])
    return fetched[0] if fetched else None


def _resolve_repost_target(post: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Кого репостить: внутренний пост из copy_history или wall-вложение."""
    ch = post.get("copy_history") or []
    if ch:
        o = ch[0]
        try:
            return int(o["owner_id"]), int(o["id"])
        except (KeyError, TypeError, ValueError):
            pass
    for att in post.get("attachments") or []:
        if att.get("type") != "wall":
            continue
        w = att.get("wall") or {}
        oid = w.get("from_id", w.get("owner_id"))
        pid = w.get("id")
        if oid is not None and pid is not None:
            try:
                return int(oid), int(pid)
            except (TypeError, ValueError):
                continue
    return None


async def execute_copy_setka_network(
    session: AsyncSession,
    *,
    test_mode: bool = False,
) -> Dict[str, Any]:
    from config.runtime import (
        copy_setka_disabled,
        get_copy_setka_max_post_age_hours,
        get_copy_setka_post_interval_seconds,
        get_copy_setka_repost_message,
        get_copy_setka_source_owner_id,
        get_copy_setka_target_region_codes,
    )
    from database.models import Region
    from database.models_extended import WorkTable
    from modules.publisher.vk_publisher_extended import VKPublisher
    from modules.vk_monitor.vk_client import VKClient
    from utils.post_utils import clear_copy_history, lip_of_post
    from utils.vk_attachments import build_attachments_list, extract_vk_attachments

    if copy_setka_disabled():
        logger.info("COPY_SETKA_DISABLED — сетевой хаб пропущен")
        return {
            "success": False,
            "skipped": True,
            "error": "COPY_SETKA_DISABLED",
            "stats": _empty_stats(),
        }

    source_owner_id = get_copy_setka_source_owner_id()

    # Фильтр disabled-токенов через TokenPolicy (миграция 014):
    # если Valstan в cooldown — берётся следующий из env (обычно Vita).
    # ВАЖНО: НЕ fallback'ить на полный get_parse_tokens() — это вернёт
    # заблокированный VALSTAN и worker уйдёт в loop с error 5 от VK
    # (инцидент 2026-05-27).
    from modules.vk_token_router import get_active_parse_tokens

    parse_tokens = await get_active_parse_tokens(session)
    if not parse_tokens:
        return {
            "success": False,
            "error": "No active VK READ tokens (all in cooldown?)",
            "stats": _empty_stats(),
        }

    result = await session.execute(
        select(WorkTable).where(
            WorkTable.region_code == "copy",
            WorkTable.theme == "setka",
        )
    )
    wt = result.scalars().first()
    if not wt:
        wt = WorkTable(region_code="copy", theme="setka", lip=[], hash=[])
        session.add(wt)
        await session.commit()
        await session.refresh(wt)

    # Источник copy_by_setka — ЧАСТНАЯ группа (is_closed=2): wall.get отдаёт
    # VK error 15 («доступна только участникам сообщества») для токенов, чей
    # аккаунт в ней не состоит. get_active_parse_tokens идёт без ORDER BY →
    # порядок строк из Postgres недетерминирован (плавает после last_used-
    # апдейтов), и слепой «первый токен» периодически попадал на НЕ-участника
    # → get_wall_posts глотал error 15 и возвращал [] → «no posts on source
    # wall», и вся сеть переставала размножать посты (инцидент 2026-06-07,
    # после ротации токенов 2026-05-28). Поэтому перебираем все активные READ-
    # токены и берём первый, которым стена реально читается.
    posts: List[Dict[str, Any]] = []
    used_token_name: Optional[str] = None
    for tok_name, tok in parse_tokens.items():
        fetched = await asyncio.to_thread(
            VKClient(tok).get_wall_posts, source_owner_id, WALL_FETCH_COUNT, 0
        )
        if fetched:
            posts = fetched
            used_token_name = tok_name
            break
        logger.info(
            "copy-setka: стена источника %s недоступна/пуста токеном %s — пробую следующий",
            source_owner_id,
            tok_name,
        )

    if not posts:
        logger.warning(
            "copy-setka: ни один из %d активных READ-токенов не смог прочитать "
            "стену источника %s (частная группа? аккаунт-участник в cooldown?)",
            len(parse_tokens),
            source_owner_id,
        )
        return {
            "success": True,
            "message": "no posts on source wall",
            "posts_published": 0,
            "stats": _empty_stats(),
        }

    logger.info("copy-setka: стена источника прочитана токеном %s", used_token_name)

    max_age = int(get_copy_setka_max_post_age_hours() * 3600)
    now_ts = int(time.time())

    # Карта lip → пост (для поиска pending-поста среди свежих).
    posts_by_lip: Dict[str, Dict[str, Any]] = {}
    for p in posts:
        pid = p.get("id")
        if pid is None:
            continue
        oid = p.get("owner_id", source_owner_id)
        posts_by_lip[lip_of_post(int(oid), int(pid))] = p

    # Целевые регионы спросим позже — когда будет известен текст кандидата:
    # слово-маршрут («Киров»/«Казань») сужает рассылку до поддерева области.
    regions: List[Any] = []
    target_codes: Set[str] = set()

    # --- Выбор поста: сперва ДОБИРАЕМ недоставленный (pending) -----------------
    # Пост помечается «разослан» (wt.lip) только когда его получили ВСЕ целевые
    # регионы. Если на прошлом тике часть регионов отвалилась по VK Captcha
    # (бурст из 16+ репостов с одного аккаунта), хвост добирается на следующих
    # тиках. Прогресс храним в work_tables.hash псевдо-региона copy/setka (для
    # него фото-дедуп не нужен) как {"lip", "done": [codes], "tries"}. Без
    # миграции. PENDING_MAX_TRIES — backstop от вечного застревания.
    candidate: Optional[Dict[str, Any]] = None
    done_codes: Set[str] = set()
    pending_tries = 0

    pending = wt.hash if isinstance(wt.hash, dict) else None
    if pending and pending.get("lip"):
        plip = str(pending["lip"])
        pdone = {str(c) for c in (pending.get("done") or [])}
        ptries = int(pending.get("tries") or 0)
        cand = posts_by_lip.get(plip)
        if cand is None:
            cand = await _fetch_post_by_lip(parse_tokens[used_token_name], plip)
        if cand is None:
            logger.info(
                "copy-setka: pending-пост %s недоступен (удалён?) — снимаю pending",
                plip,
            )
            wt.hash = []
            await session.commit()
        else:
            candidate = cand
            done_codes = pdone
            pending_tries = ptries
            logger.info(
                "copy-setka: добор pending-поста %s — уже есть у %d рег. (попытка %d/%d)",
                plip,
                len(pdone),
                ptries + 1,
                PENDING_MAX_TRIES,
            )

    # Нет pending к добору → берём свежий новый пост.
    if candidate is None:
        known: Set[str] = set(wt.lip or [])
        posts_sorted = sorted(posts, key=lambda p: p.get("date", 0), reverse=True)
        for p in posts_sorted:
            oid = p.get("owner_id", source_owner_id)
            pid = p.get("id")
            if pid is None:
                continue
            lip = lip_of_post(int(oid), int(pid))
            if lip in known:
                continue
            post_date = int(p.get("date") or 0)
            if max_age > 0 and now_ts - post_date > max_age:
                continue
            candidate = p
            break

    if candidate is None:
        return {
            "success": True,
            "message": "no fresh post to propagate",
            "posts_published": 0,
            "stats": _empty_stats(),
        }

    src_oid = int(candidate.get("owner_id", source_owner_id))
    src_pid = int(candidate["id"])
    src_lip = lip_of_post(src_oid, src_pid)
    body_text = candidate.get("text") or ""

    # Целевые регионы (все активные, кроме псевдо-региона copy) — только
    # теперь, когда известен текст: слово-маршрут сужает рассылку до области.
    region_filter = get_copy_setka_target_region_codes()
    rq = select(Region).where(
        Region.is_active.is_(True),
        Region.vk_group_id.isnot(None),
        Region.code != "copy",
    )
    if region_filter:
        rq = rq.where(Region.code.in_(list(region_filter)))

    regions_result = await session.execute(rq)
    regions = list(regions_result.scalars().all())
    if not regions:
        return {
            "success": False,
            "error": "no target regions with vk_group_id",
            "stats": _empty_stats(),
        }

    scope_codes = resolve_scope_codes(body_text, regions)
    if scope_codes is not None:
        regions = [reg for reg in regions if reg.code in scope_codes]
        if not regions:
            logger.warning(
                "copy-setka: слово-маршрут matched %s, но регионов в скоупе нет "
                "(пост %s) — закрываю, чтобы не клинить хаб",
                sorted(scope_codes),
                src_lip,
            )
            _mark_post_done(wt, src_lip)
            wt.hash = []
            await session.commit()
            return {
                "success": False,
                "error": "scope matched no regions",
                "source_lip": src_lip,
                "scope": sorted(scope_codes),
                "stats": _empty_stats(),
            }
        logger.info(
            "copy-setka: скоуп %s — шлём в %d регионов: %s",
            sorted(scope_codes),
            len(regions),
            ", ".join(sorted(reg.code for reg in regions)),
        )
    target_codes = {reg.code for reg in regions}

    # Pending мог закрыться ещё до сужения (все получившие — внутри скоупа,
    # скоуп от текста не меняется между тиками): тогда просто закрываем.
    if done_codes and target_codes <= done_codes:
        logger.info("copy-setka: pending-пост %s уже доставлен всем — закрываю", src_lip)
        _mark_post_done(wt, src_lip)
        wt.hash = []
        await session.commit()
        return {
            "success": True,
            "message": "pending post already delivered to scope",
            "posts_published": 0,
            "source_lip": src_lip,
            "scope": sorted(scope_codes) if scope_codes is not None else "all",
            "stats": _empty_stats(),
        }

    use_api_repost = _text_has_repost_keyword(body_text)

    msg_suffix = get_copy_setka_repost_message()

    # Подгружаем community-токены и user-кандидатов для публикации через
    # TokenPolicy (миграция 014). Vita исключена deny-list'ом, Valstan в
    # cooldown — пропускается. wall.repost (USER_WRITE) при пустом списке
    # user-кандидатов вернёт «no publish token» и copy_setka запишет ошибку.
    publisher = await VKPublisher.create_with_policy(
        session,
        target_group_id=None,
        test_polygon_mode=test_mode,
    )

    repost_pair: Optional[Tuple[int, int]] = None
    copy_text: str = ""
    copy_attachments: List[str] = []

    if use_api_repost:
        repost_pair = _resolve_repost_target(candidate)
        if repost_pair is None:
            logger.warning(
                "copy-setka: в тексте есть «репост», "
                "но не найден вложенный пост (copy_history/wall)"
            )
            return {
                "success": False,
                "error": "repost keyword but no inner wall post to repost",
                "source_lip": src_lip,
                "stats": _empty_stats(),
            }
    else:
        raw = copy_lib.deepcopy(candidate)
        effective = clear_copy_history(raw)
        copy_text = effective.get("text") or ""
        att_dict = extract_vk_attachments(effective)
        copy_attachments = build_attachments_list(att_dict, max_items=10)

    # Шлём только тем регионам, кто ещё НЕ получил этот пост, с паузой между
    # репостами (анти-Captcha: бурст с одного аккаунта триггерит капчу).
    targets_remaining = [reg for reg in regions if reg.code not in done_codes]
    interval = get_copy_setka_post_interval_seconds()

    newly_done: Set[str] = set()
    errors: List[str] = []
    for idx, reg in enumerate(targets_remaining):
        if idx > 0 and interval > 0:
            await asyncio.sleep(interval)
        gid = int(reg.vk_group_id)
        try:
            if use_api_repost and repost_pair:
                ro, rp = repost_pair
                out = await publisher.publish_repost(
                    group_id=gid,
                    source_owner_id=ro,
                    source_post_id=rp,
                    message=msg_suffix,
                )
            else:
                out = await publisher.publish_bulletin(
                    group_id=gid,
                    text=copy_text,
                    attachments=copy_attachments,
                )
            if out.get("success"):
                newly_done.add(reg.code)
                logger.info("copy-setka: %s -> %s OK %s", src_lip, reg.code, out.get("url"))
            else:
                errors.append(f"{reg.code}: {out.get('error', 'unknown')}")
        except Exception as e:
            logger.exception("copy-setka: failed for %s", reg.code)
            errors.append(f"{reg.code}: {e}")

    all_done = done_codes | newly_done
    complete = target_codes <= all_done
    missing = sorted(target_codes - all_done)

    if complete:
        _mark_post_done(wt, src_lip)
        wt.hash = []
        logger.info("copy-setka: пост %s доставлен всем %d регионам", src_lip, len(target_codes))
    else:
        tries = pending_tries + 1
        if tries >= PENDING_MAX_TRIES:
            logger.warning(
                "copy-setka: пост %s не добран за %d попыток — сдаюсь; " "пропущены регионы: %s",
                src_lip,
                tries,
                ", ".join(missing),
            )
            _mark_post_done(wt, src_lip)
            wt.hash = []
        else:
            wt.hash = {"lip": src_lip, "done": sorted(all_done), "tries": tries}
            logger.info(
                "copy-setka: пост %s разослан частично (%d/%d), добор на след. тике; "
                "осталось: %s",
                src_lip,
                len(all_done),
                len(target_codes),
                ", ".join(missing),
            )
    await session.commit()

    return {
        "success": complete or len(newly_done) > 0,
        "posts_published": len(newly_done),
        "source_lip": src_lip,
        "mode": "wall.repost" if use_api_repost else "wall.post copy",
        "scope": sorted(scope_codes) if scope_codes is not None else "all",
        "targets": len(targets_remaining),
        "complete": complete,
        "done_total": len(all_done),
        "missing": missing,
        "errors": errors[:20],
        "stats": {
            "total_groups_checked": len(targets_remaining),
            "total_posts_scanned": len(posts),
            "posts_final_count": 1 if complete else 0,
            "posts_filtered_old": 0,
            "posts_filtered_duplicate_lip": 0,
            "posts_filtered_duplicate_text": 0,
            "posts_filtered_duplicate_foto": 0,
            "posts_filtered_black_id": 0,
            "posts_filtered_no_region_words": 0,
            "posts_filtered_advertisement": 0,
            "posts_filtered_no_attachments": 0,
        },
    }


def _empty_stats() -> Dict[str, int]:
    return {
        "total_groups_checked": 0,
        "total_posts_scanned": 0,
        "posts_filtered_old": 0,
        "posts_filtered_duplicate_lip": 0,
        "posts_filtered_duplicate_text": 0,
        "posts_filtered_duplicate_foto": 0,
        "posts_filtered_black_id": 0,
        "posts_filtered_no_region_words": 0,
        "posts_filtered_advertisement": 0,
        "posts_filtered_no_attachments": 0,
        "posts_final_count": 0,
    }
