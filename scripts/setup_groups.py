"""Автооформление ИНФО-сообществ: описание, аватар, обложка, закреп-визитка.

Этап 2 ребрендинга (план 2026-08-29). По каждому активному району смотрим
живой снимок сообщества (``groups.getById``) и решаем режим:

- **full** — сообщество «голое» (нет аватара ∧ нет обложки ∧ описание короче
  240 знаков): ставим описание (community-ключ), аватар (user VALSTAN, с
  чисткой системного поста), обложку (community-ключ), публикуем и закрепляем
  пост-визитку. Всё — из ``modules/promotion``: branding + copy + group_setup_vk.
- **spot** — у сообщества авторское оформление (11 старых): программно НЕ
  трогаем ничего. Город и статус, которые у них тоже бывают кривые, через API
  не правятся вообще (ловушка #219, ``docs/ops/group-setup-probe.md``) — скрипт
  лишь печатает список «что поправить руками/браузером».

Идемпотентность — через ``promo_group_setup`` (region_id + setup_version
уникальны): повторный прогон пропускает применённые записи, правка дизайна =
бамп ``branding.TEMPLATE_VERSION``. ``before`` пишется до правок — описание
откатывается командой ``--rollback <code>`` (аватар/обложку откатывать некуда:
их не было).

Бюджет user-вызовов VALSTAN: ~5 на full-регион (upload-server, save, wall.get,
wall.delete, wall.pin) при лимите ``get_valstan_call_budget()`` — скрипт сам
останавливается, досчитав бюджет, продолжение — следующим прогоном (порции).

Запуск (на проде, под env приложения):
    python scripts/setup_groups.py                      # dry-run, отчёт
    python scripts/setup_groups.py --audit              # только чтение: чего где нет
    python scripts/setup_groups.py --apply              # правки (гейт #025!)
    python scripts/setup_groups.py --apply --regions uni,falenki
    python scripts/setup_groups.py --repair --by-snapshot   # дозалить недостающее
    python scripts/setup_groups.py --rollback oparino   # вернуть описание

**Режим dry-run не отвечает на вопрос «у кого нет обложки или аватара».** Режим
``full`` — конъюнкция (нет аватара И нет обложки И описание короткое), поэтому
сообщество с обложкой и без аватара печатается как ``spot`` («авторское, не
трогаем»). Для вопроса «чего недостаёт» есть ``--audit``, и он смотрит в ВК, а
не в ``promo_group_setup``: журнал говорит, чем кончился прогон, а не что сейчас
в сообществе, и расходился с явью уже дважды (01.09).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("setup_groups")

# Порог «описание шаблонное»: у 25 голых групп это 200-230 знаков болванки
# из чек-листа создания; авторские описания старых — 500+.
TEMPLATE_DESC_MAX = 240

# Сколько user-вызовов VALSTAN стоит один full-регион (см. docstring).
USER_CALLS_PER_REGION = 5

# Все виды групп проекта: районы и области.
#
# Нужен там, где скрипт только ДОЗАЛИВАЕТ недостающее по живому снимку ВК
# (``--repair --by-snapshot``). Области — полноценные группы проекта, у них
# тоже бывают пустые аватар и обложка, и ``--audit`` их честно считает.
#
# Найдено 2026-11-20: аудит показывал «Чинить: 5», а ``--repair`` брал только
# районы — области ``kirov_obl`` и ``tatarstan_obl`` были в списке обещаний, но
# в ремонт не попадали никогда. Гейт зелёный, работа не сделана — ровно тот
# класс, который гейт и ловит.
ALL_KINDS = ("raion", "oblast")


def _genitive_from_zagolovki(zagolovki: Optional[dict]) -> Optional[str]:
    """«Новости Опаринского округа:» → «Опаринского округа»."""
    if not isinstance(zagolovki, dict):
        return None
    raw = (zagolovki.get("novost") or "").strip()
    if raw.lower().startswith("новости ") and raw.endswith(":"):
        return raw[len("новости ") : -1].strip()
    return None


async def load_targets(
    codes: Optional[List[str]], kinds: Tuple[str, ...] = ("raion",)
) -> List[dict]:
    """Активные регионы с главной группой + брендинг-данные из region_configs.

    ``kinds`` по умолчанию только районы — так исторически работали ``--apply`` и
    ``--repair``. Областные группы (``kind='oblast'``) для оформления и визитки
    не берутся: у них своё наполнение, а ``--apply`` ставит приветственный пост
    и закреп — это крупное сообщество, и лишний пост там не нужен.

    Но области — полноценные группы проекта: у них тоже есть аватар и обложка,
    и ``--audit`` их считает. Поэтому режимы, которые только **доливают
    недостающее** (``--repair --by-snapshot``), берут и области — см. ``ALL_KINDS``;
    режимы, которые что-то **пишут сверху** (``--apply``), остаются на районах.
    Описание и поисковый индекс нужны и областям, поэтому ``--audit`` и
    ``--refresh-desc`` зовут с обоими видами.
    """
    from sqlalchemy import text

    from database.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT r.id, r.code, r.name, r.neighbors, r.vk_group_id, "
                    "       r.config, r.kind, rc.zagolovki "
                    "FROM regions r "
                    "LEFT JOIN region_configs rc ON rc.region_code = r.code "
                    "WHERE r.is_active IS TRUE AND r.vk_group_id IS NOT NULL "
                    "  AND r.kind = ANY(:kinds) "
                    "ORDER BY r.code"
                ),
                {"kinds": list(kinds)},
            )
        ).fetchall()
    out = []
    for r in rows:
        if codes and r.code not in codes:
            continue
        out.append(
            {
                "region_id": r.id,
                "code": r.code,
                "name": r.name,
                "neighbors": r.neighbors,
                "vk_group_id": r.vk_group_id,
                "zagolovki": r.zagolovki,
                # Красивый адрес группы: его кэширует ночная таска
                # collect_member_snapshots в Region.config['screen_name'].
                # Нужен, чтобы публичные тексты ссылались на vk.com/<имя>,
                # а не на club<id> (закон о ссылках, AGENTS.md).
                "screen_name": (
                    (r.config or {}).get("screen_name") if isinstance(r.config, dict) else None
                ),
                "kind": r.kind,
                # Места для поискового индекса в описании. `localities_top` —
                # отранжированный список (см. --rank-localities); если ранга ещё
                # нет, берём сырой алфавитный, он хуже, но лучше пустого.
                "localities": (
                    (r.config or {}).get("localities_top") or (r.config or {}).get("localities")
                    if isinstance(r.config, dict)
                    else None
                )
                or [],
            }
        )
    return out


async def load_tokens() -> Tuple[Optional[str], Dict[int, str]]:
    from modules.vk_token_router import load_vk_routing

    return await load_vk_routing()


async def claim_setup(region_id: int, version: int) -> Optional[int]:
    """Идемпотентный insert в promo_group_setup; None = уже применено."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup

    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(PromoGroupSetup)
            .values(region_id=region_id, setup_version=version, status="dry_run")
            .on_conflict_do_nothing(index_elements=["region_id", "setup_version"])
            .returning(PromoGroupSetup.id)
        )
        row = (await session.execute(stmt)).scalar()
        if row is not None:
            await session.commit()
            return int(row)
        existing = (
            await session.execute(
                select(PromoGroupSetup.id, PromoGroupSetup.status).where(
                    PromoGroupSetup.region_id == region_id,
                    PromoGroupSetup.setup_version == version,
                )
            )
        ).first()
        await session.commit()
        if existing and existing.status == "applied":
            return None  # уже сделано этой версией шаблона
        return int(existing.id) if existing else None


async def finish_setup(
    setup_id: int,
    *,
    status: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    applied_fields: Optional[list] = None,
    pinned_post_url: Optional[str] = None,
    vk_error_code: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    from sqlalchemy import update

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(PromoGroupSetup)
            .where(PromoGroupSetup.id == setup_id)
            .values(
                status=status,
                before=before,
                after=after,
                applied_fields=applied_fields,
                pinned_post_url=pinned_post_url,
                vk_error_code=vk_error_code,
                error=error,
            )
        )
        await session.commit()


def build_texts(target: dict) -> dict:
    """Все тексты и картинки региона: описание, визитка, аватар, обложка."""
    from config.promo import get_network_list_url
    from modules.promotion.branding import default_tagline, render_avatar, render_cover
    from modules.promotion.copy import render_group_description, render_welcome_post
    from modules.region_links import base_title, parse_neighbors

    title = base_title(target["name"], None)
    genitive = _genitive_from_zagolovki(target["zagolovki"]) or f"района ({title})"
    site_url = get_network_list_url()

    neighbors = []
    for code in parse_neighbors(target.get("neighbors"))[:5]:
        info = target.get("_neighbor_index", {}).get(code)
        if info:
            neighbors.append(info)

    # У области в поисковом индексе перечисляются районы, у района — сёла.
    # Сущность разная, роль одна: длинный хвост запросов «<место> новости».
    from modules.promotion.copy import _PLACES_LABEL_DISTRICTS, _PLACES_LABEL_LOCALITIES

    places_label = (
        _PLACES_LABEL_DISTRICTS if target.get("kind") == "oblast" else _PLACES_LABEL_LOCALITIES
    )

    return {
        "title": title,
        "description": render_group_description(
            district_name=genitive,
            center_city=title,
            site_url=site_url,
            localities=target.get("localities") or (),
            places_label=places_label,
        ),
        "welcome": render_welcome_post(
            district_name=genitive, neighbors=neighbors, site_url=site_url
        ),
        "avatar": render_avatar(target["code"], title),
        "cover": render_cover(target["code"], title, default_tagline(genitive.capitalize())),
        "genitive": genitive,
    }


def neighbor_index(targets: List[dict]) -> Dict[str, dict]:
    """Ссылки на соседей для визитки — красивым адресом, если он известен.

    Раньше здесь стояло ``community_url(id, None)``: второй аргумент был **всегда**
    ``None``, поэтому закреплённая визитка во всех 41 сообществе ссылалась на
    соседей как на ``vk.com/club241197723``. Красивый адрес при этом лежал
    закэшированным в ``Region.config['screen_name']`` — то есть закон «наружу
    красивая ссылка, внутрь числовой id» (``AGENTS.md``) нарушался ровно в том
    месте, которое читают люди.

    Фолбэк на ``club<id>`` остаётся: он рабочий всегда и не зависит от того,
    добежала ли ночная таска.
    """
    from modules.region_links import base_title, community_url

    out = {}
    for t in targets:
        url = community_url(t["vk_group_id"], t.get("screen_name"))
        if url:
            out[t["code"]] = {"name": f"{base_title(t['name'], None)} ИНФО", "url": url}
    return out


async def rollback(code: str) -> int:
    """Вернуть описание региона из последнего ``before`` в promo_group_setup."""
    import vk_api
    from sqlalchemy import select, text

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup
    from modules.promotion.group_setup_vk import edit_description
    from modules.vk_token_router import load_community_tokens

    async with AsyncSessionLocal() as session:
        region = (
            await session.execute(
                text("SELECT id, vk_group_id FROM regions WHERE code = :c"), {"c": code}
            )
        ).first()
        if not region:
            logger.error("Регион %s не найден", code)
            return 2
        row = (
            (
                await session.execute(
                    select(PromoGroupSetup)
                    .where(PromoGroupSetup.region_id == region.id)
                    .order_by(PromoGroupSetup.setup_version.desc())
                )
            )
            .scalars()
            .first()
        )
        if not row or not row.before:
            logger.error("Нет сохранённого before для %s — откатывать нечего", code)
            return 2
        community_tokens = await load_community_tokens(session)

    token = community_tokens.get(abs(int(region.vk_group_id)))
    if not token:
        logger.error("Нет community-ключа для %s", code)
        return 2
    api = vk_api.VkApi(token=token).get_api()
    result = edit_description(api, region.vk_group_id, row.before.get("description") or "")
    logger.info("Откат описания %s: %s", code, "ok" if result.ok else result.detail)
    await finish_setup(row.id, status="rolled_back", before=row.before, after=row.after)
    return 0 if result.ok else 1


def process_region(
    target: dict,
    *,
    apply: bool,
    user_api,
    community_api,
    out_dir: Optional[str],
) -> Tuple[str, str, Optional[dict]]:
    """Один регион: снимок → режим → (dry-run печать | правки). Синхронно.

    Возвращает (mode, summary, report) — report непустой при apply.
    """
    from modules.promotion.group_setup_vk import (
        edit_description,
        get_current,
        pin_post,
        post_welcome,
        upload_avatar,
        upload_cover,
    )

    gid = target["vk_group_id"]
    snap = get_current(community_api or user_api, gid)
    if not snap.ok:
        return "error", f"снимок не взялся: {snap.detail}", None
    cur = snap.payload or {}

    bare = (
        not cur.get("has_photo")
        and not cur.get("has_cover")
        and len(cur.get("description") or "") <= TEMPLATE_DESC_MAX
    )
    mode = "full" if bare else "spot"
    texts = build_texts(target)

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{target['code']}_avatar.jpg"), "wb") as fh:
            fh.write(texts["avatar"])
        with open(os.path.join(out_dir, f"{target['code']}_cover.jpg"), "wb") as fh:
            fh.write(texts["cover"])

    if mode == "spot":
        gaps = []
        if not (cur.get("status") or "").strip():
            gaps.append("статус пуст")
        if (cur.get("city") or "") in ("", "Калинино"):
            gaps.append(f"город «{cur.get('city')}» — руками")
        return mode, "; ".join(gaps) or "всё на месте", None

    if not apply:
        return (
            mode,
            f"desc {len(cur.get('description') or '')}→{len(texts['description'])} зн., "
            f"аватар+обложка+визитка; город «{cur.get('city')}» — руками",
            None,
        )

    if community_api is None:
        return "error", "нет community-ключа — full-режим невозможен", None

    report: dict = {"before": cur, "applied": [], "errors": []}
    steps = [
        ("description", lambda: edit_description(community_api, gid, texts["description"])),
        ("avatar", lambda: upload_avatar(user_api, gid, texts["avatar"])),
        ("cover", lambda: upload_cover(community_api, gid, texts["cover"])),
    ]
    for field, action in steps:
        res = action()
        if res.ok:
            report["applied"].append(field)
        else:
            report["errors"].append(f"{field}: [{res.vk_error_code}] {res.detail}")
        time.sleep(interval())

    posted = post_welcome(community_api, gid, texts["welcome"])
    if posted.ok:
        post_id = posted.payload["post_id"]
        report["applied"].append("welcome_post")
        report["post_url"] = f"https://vk.ru/wall-{abs(int(gid))}_{post_id}"
        time.sleep(interval())
        pinned = pin_post(user_api, gid, post_id)
        if pinned.ok:
            report["applied"].append("pin")
        else:
            report["errors"].append(f"pin: [{pinned.vk_error_code}] {pinned.detail}")
    else:
        report["errors"].append(f"welcome: [{posted.vk_error_code}] {posted.detail}")

    summary = f"применено: {', '.join(report['applied']) or 'ничего'}"
    if report["errors"]:
        summary += f"; ошибки: {'; '.join(report['errors'])}"
    return mode, summary, report


def interval() -> float:
    from config.promo import get_post_interval_seconds

    return get_post_interval_seconds()


def repair_region(
    target: dict,
    *,
    user_api,
    community_api,
    retries: int = 2,
) -> Tuple[str, int, bool]:
    """Дозалить только недостающие аватар/обложку по живому снимку.

    Нужен после перемежающегося ``error 129 Invalid photo`` первой порции:
    повторный full-прогон сделал бы дубль визитки, а repair смотрит на снимок
    (нет фото → аватар, нет обложки → обложка) и больше ничего не трогает.
    Каждый шаг ретраится: 129 у ВК транзиторный (у одного региона проходил
    аватар и падала обложка, у соседнего — наоборот).

    Возвращает ``(summary, потраченные user-вызовы, ok)``.

    ``ok`` считается по **недостающему**, а не по тексту отчёта: ретрай для
    того и написан, чтобы падение попытки не было падением работы. Успех, до
    которого дошли со второго-третьего захода, обязан записаться в журнал
    успехом — иначе `--repair` докладывает провалом ровно то, ради чего
    существует. Так и вышло 01.09: у `oparino` и `sanchursk` аватары встали,
    а строки остались `error`, потому что вызывающий искал слово «ошибки» в
    сводке. Тот же класс, что и гейт, называвший не тот счётчик (PR #588).
    """
    from modules.promotion.group_setup_vk import get_current, upload_avatar, upload_cover

    gid = target["vk_group_id"]
    snap = get_current(community_api or user_api, gid)
    if not snap.ok:
        # Снимка нет — судить не о чем; «не смогли посмотреть» это не «всё
        # починили», поэтому запись остаётся error и регион вернётся сюда.
        return f"снимок не взялся: {snap.detail}", 0, False
    cur = snap.payload or {}
    texts = build_texts(target)
    missing = set()
    if not cur.get("has_photo"):
        missing.add("avatar")
    if not cur.get("has_cover"):
        missing.add("cover")
    done, errors = [], []
    user_calls = 0

    if "avatar" in missing:
        for attempt in range(retries + 1):
            res = upload_avatar(user_api, gid, texts["avatar"])
            user_calls += 4
            if res.ok:
                done.append("avatar")
                break
            errors.append(f"avatar#{attempt}: [{res.vk_error_code}] {res.detail}")
            time.sleep(interval())
        time.sleep(interval())

    if "cover" in missing:
        if community_api is None:
            # Молчаливый пропуск выглядел бы как «чинить было нечего»: обложки
            # нет, и она не появится, — это незакрытая цель, а не no-op.
            errors.append("cover: нет community-ключа")
        else:
            for attempt in range(retries + 1):
                res = upload_cover(community_api, gid, texts["cover"])
                if res.ok:
                    done.append("cover")
                    break
                errors.append(f"cover#{attempt}: [{res.vk_error_code}] {res.detail}")
                time.sleep(interval())

    # Мерило — что осталось недостающим, а не сколько попыток по дороге упало.
    ok = not (missing - set(done))
    if not missing:
        return "всё на месте", user_calls, True
    summary = f"дозалито: {', '.join(done) or 'ничего'}"
    if errors:
        summary += f"; ошибки: {'; '.join(errors[-2:])}"
    return summary, user_calls, ok


async def mark_repaired(region_id: int, version: int, summary: str, ok: bool) -> None:
    """Обновить существующую error-запись после repair.

    Сводка пишется в ``after`` всегда, даже при успехе: колонка ``error`` при
    ``ok`` обязана обнулиться (иначе строка `applied` тащит текст ошибки), но
    след «взялось с третьей попытки» терять не за чем — по нему видно, как
    часто у ВК шалит 129.
    """
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup

    async with AsyncSessionLocal() as session:
        row = (
            (
                await session.execute(
                    select(PromoGroupSetup).where(
                        PromoGroupSetup.region_id == region_id,
                        PromoGroupSetup.setup_version == version,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return
        row.status = "applied" if ok else "error"
        row.error = None if ok else f"repair: {summary}"[:500]
        after = dict(row.after or {})
        after["repair"] = summary[:500]
        row.after = after
        await session.commit()


async def _regions_we_dressed() -> set:
    """Регионы, которые оформлял ЭТОТ скрипт (журнал ``promo_group_setup``).

    Вынесено из ``run_force_cover`` отдельной функцией, чтобы у режима вообще
    появился шов для теста: без него первая же строка прогона идёт в БД, и
    проверить хоть что-нибудь (темп, отбор) можно только на живой базе.

    Берутся статусы ``applied`` И ``error`` — обоснование в докстринге вызывающего.
    """
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup

    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(PromoGroupSetup.region_id).where(
                PromoGroupSetup.status.in_(("applied", "error"))
            )
        )
    return set(rows.scalars().all())


async def run_force_cover(codes: Optional[List[str]]) -> int:
    """``--force-cover``: перезалить обложку там, где её ставили МЫ.

    Зачем отдельный режим. Обычный прогон решает ``full``/``spot`` по тому,
    «голое» ли сообщество (нет аватара ∧ нет обложки ∧ короткое описание). Это
    правильная защита для одиннадцати старых сообществ с авторским оформлением —
    но она же не даёт переставить **нашу собственную** обложку, если та уехала:
    сообщество с кривой обложкой выглядит оформленным и попадает в ``spot``.

    Ровно это случилось 31.08: у десяти сообществ порции 1 обложка была залита с
    кропом 1590×400 из холста 2560×644, ВК отрезал 38% ширины, и заголовок
    обрывался на 71%. Бамп ``TEMPLATE_VERSION`` тут не помогает — версия решает,
    писать ли запись в журнал, а режим считается раньше и по живому снимку.

    **Чужое оформление не трогается никогда:** цели берутся из
    ``promo_group_setup``, то есть только те сообщества, которые оформлял этот
    скрипт. Сообщество без такой записи не попадёт сюда, даже если назвать его
    в ``--regions``.

    Берутся статусы ``applied`` И ``error``. Запись ``error`` — тоже наша: в
    full-режим попадает только «голое» сообщество (нет аватара ∧ нет обложки ∧
    короткое описание), поэтому за ней не может стоять авторское оформление, а
    у ВК регулярно падает часть заливок перемежающейся ошибкой 129. Ограничение
    одним ``applied`` оставляло бы такие сообщества без обложки навсегда:
    ``--repair`` их чинит, но тратит user-бюджет VALSTAN, который к ночи обычно
    исчерпан, — а обложка ставится community-ключом и не стоит ничего.
    Пропускаются только ``dry_run`` — там ничего и не применялось.

    Расход user-бюджета VALSTAN — **ноль**: обложка ставится community-ключом.
    """
    import vk_api

    from modules.promotion.group_setup_vk import upload_cover

    ours = await _regions_we_dressed()
    if not ours:
        logger.info("Нет сообществ, оформленных этим скриптом — переливать нечего")
        return 0

    targets = [t for t in await load_targets(codes) if t["region_id"] in ours]
    if not targets:
        logger.info("Под фильтр не попало ни одного нашего сообщества")
        return 0

    # Обложка собирается тем же build_texts, что и в штатном прогоне: перелитая
    # обязана быть побайтно той же, иначе «починка» подменит оформление.
    all_targets = await load_targets(None)
    nb_index = neighbor_index(all_targets)
    for t in targets:
        t["_neighbor_index"] = nb_index

    _, community_tokens = await load_tokens()

    logger.info("FORCE-COVER: сообществ %d (user-бюджет не расходуется)", len(targets))
    ok_count = 0
    wrote_any = False
    for target in targets:
        gid = abs(int(target["vk_group_id"]))
        comm_token = community_tokens.get(gid)
        if not comm_token:
            logger.info("  %-14s ⛔ нет community-ключа", target["code"])
            continue
        community_api = vk_api.VkApi(token=comm_token).get_api()
        cover = build_texts(target)["cover"]
        # Тот же темп, что у описаний: этот режим тоже проходит по всей сети
        # подряд (31.08 — десять перезаливок), и квота у аккаунта общая.
        if wrote_any:
            await asyncio.sleep(interval())
        wrote_any = True
        res = await asyncio.to_thread(upload_cover, community_api, gid, cover)
        if res.ok:
            ok_count += 1
            logger.info("  %-14s 🖼 обложка перезалита (%d байт)", target["code"], len(cover))
        else:
            logger.info("  %-14s ⛔ %s", target["code"], res.detail)
    logger.info("\nГотово: перезалито %d из %d", ok_count, len(targets))
    return 0


async def _journal_row_id(region_id: int, version: int) -> Optional[int]:
    """Строка журнала этой версии шаблона; создаётся, если её нет."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup

    async with AsyncSessionLocal() as session:
        await session.execute(
            pg_insert(PromoGroupSetup)
            .values(region_id=region_id, setup_version=version, status="dry_run")
            .on_conflict_do_nothing(index_elements=["region_id", "setup_version"])
        )
        await session.commit()
        row = (
            await session.execute(
                select(PromoGroupSetup.id, PromoGroupSetup.before).where(
                    PromoGroupSetup.region_id == region_id,
                    PromoGroupSetup.setup_version == version,
                )
            )
        ).first()
    return row


# Буквы и цифры, после которых имя места читается серединой другого слова
# («научили» — не «Учили»). Татарские буквы — для районов Татарстана.
_WORD_CHARS = "A-Za-zА-Яа-яЁё0-9ӘәӨөҮүҖҗҢңҺһ"
# Спецсимволы регулярок Postgres (ARE): в имени места они значат сами себя.
_ARE_SPECIALS = frozenset("\\.^$*+?()[]{}|")


def mention_pattern(place: str) -> str:
    """Регулярка Postgres «в тексте упомянуто это место» — для ранжирования.

    Слева граница слова, справа свобода: «Шемордан» засчитывается и в
    «Шемордане», и в «Шеморданом» (падежи), а «Учили» внутри «научили» — нет.
    Регистр учитывается: имя села пишут с заглавной, а тот же набор букв со
    строчной почти всегда слово языка («нас учили»). Спецсимволы экранируются,
    чтобы точка или скобка в названии не стали частью регулярки.

    Совпадения этой формулы — подмножество прежнего ``ILIKE '%имя%'``: она только
    отбрасывает и ничего не добавляет. Цена — упоминания капсом («АРСК») не
    засчитываются; для ранга это приемлемо.
    """
    escaped = "".join("\\" + ch if ch in _ARE_SPECIALS else ch for ch in place)
    return f"(^|[^{_WORD_CHARS}])" + escaped


async def run_rank_places(codes: Optional[List[str]], apply: bool) -> int:
    """--rank-localities: отранжировать места для поискового индекса описания.

    Заказ владельца 06.09: в хвост писать «те, которые самые густонаселённые».
    **Данных о населении у нас нет** — локалитеты хранятся простым списком имён,
    и выдумывать их порядок нельзя. Зато есть 115 тысяч собранных постов с
    текстами, и частота упоминания села в ленте района — не хуже населения, а
    для поискового индекса **лучше**: она отражает не сколько людей прописано, а
    о чём люди пишут, то есть что они и ищут.

    Проверка на Туже: алфавит давал «Азансола, Артеково, Ашеево, Безденежье»,
    ранг даёт «Тужа (208 упоминаний), Ныр (42), Шешурга, Греково, Васькино».

    У области мест другого рода — районы; их ранг берётся по числу подписчиков
    районной группы (``region_member_snapshots``), потому что упоминаний района
    в ленте самой области почти не бывает.

    Подсчёт упоминаний — :func:`mention_pattern` (начало слова и регистр). До
    11.09 здесь стоял ``ILIKE '%имя%'``, то есть подстрока, и он мерил язык, а не
    место: у Арска в восьмёрку описания попало «Учили» — из «научили» и «нас
    учили». Пока ранг только сортировал список, это было терпимо; с 06.09 он
    пишется в публичное описание, которое читают люди и индексирует поиск ВК.
    Числа по-прежнему нельзя выдавать за перепись: «Пачи» засчитывается и в
    «Большие Пачи».
    """
    from sqlalchemy import text

    from database.connection import AsyncSessionLocal

    targets = await load_targets(codes, kinds=("raion", "oblast"))
    if not targets:
        logger.error("Нет целей")
        return 1

    logger.info(
        "RANK-PLACES: регионов %d | режим: %s | только БД, ВК не трогаем",
        len(targets),
        "запись" if apply else "dry-run",
    )

    written = 0
    async with AsyncSessionLocal() as session:
        for target in targets:
            code = target["code"]
            if target.get("kind") == "oblast":
                rows = (
                    await session.execute(
                        text(
                            "SELECT r.name, s.members_count "
                            "FROM regions r JOIN LATERAL ("
                            "  SELECT members_count FROM region_member_snapshots "
                            "  WHERE region_id = r.id ORDER BY snapshot_date DESC LIMIT 1"
                            ") s ON TRUE "
                            "WHERE r.kind='raion' AND r.is_active IS TRUE "
                            "  AND r.parent_region_id = :pid "
                            "ORDER BY s.members_count DESC LIMIT 12"
                        ),
                        {"pid": target["region_id"]},
                    )
                ).fetchall()
                from modules.region_links import base_title

                ranked = [base_title(r.name, None) for r in rows]
            else:
                region_row = (
                    await session.execute(
                        text("SELECT config FROM regions WHERE id = :rid"),
                        {"rid": target["region_id"]},
                    )
                ).first()
                cfg_now = region_row.config if region_row else None
                places = [str(p) for p in ((cfg_now or {}).get("localities") or [])]
                rows = []
                if places:
                    # Шаблон собирается в Python (mention_pattern) и едет парой к
                    # имени: экранирование проверено юнит-тестом, а не живёт
                    # строкой внутри SQL.
                    rows = (
                        await session.execute(
                            text(
                                "SELECT p.place, count(*) AS hits "
                                "FROM unnest(CAST(:places AS text[]), CAST(:patterns AS text[])) "
                                "  AS p(place, pattern) "
                                "JOIN collected_post_audit a "
                                "  ON a.region_code = :code AND a.post_text ~ p.pattern "
                                "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 12"
                            ),
                            {
                                "places": places,
                                "patterns": [mention_pattern(p) for p in places],
                                "code": code,
                            },
                        )
                    ).fetchall()
                ranked = [r.place for r in rows]

            if not ranked:
                logger.info("  %-14s — нечего ранжировать", code)
                continue
            logger.info("  %-14s %s", code, ", ".join(ranked[:8]))
            if not apply:
                continue

            region = (
                await session.execute(
                    text("SELECT config FROM regions WHERE id = :rid"),
                    {"rid": target["region_id"]},
                )
            ).first()
            cfg = dict(region.config) if isinstance(region.config, dict) else {}
            cfg["localities_top"] = ranked
            await session.execute(
                text("UPDATE regions SET config = CAST(:cfg AS json) WHERE id = :rid"),
                {"cfg": json.dumps(cfg, ensure_ascii=False), "rid": target["region_id"]},
            )
            written += 1
        if apply:
            await session.commit()

    logger.info("")
    logger.info("Готово: записано рангов %d", written)
    return 0


async def run_refresh_desc(codes: Optional[List[str]], apply: bool) -> int:
    """--refresh-desc: привести описания к шаблону v3, сохранив оригинал.

    Заказ владельца 06.09: «описания у групп надо подправить, если оно устаревшее
    или маленькое, в том числе и у старых сообществ». Обычный ``--apply`` их не
    берёт: старые сообщества идут в режиме ``spot`` («авторское оформление — не
    трогаем»), и это правило про **аватар с обложкой**, которые у них авторские и
    ценные. Описание владелец решил унифицировать отдельно.

    Идёт **community-ключом**, то есть ноль user-вызовов VALSTAN.

    Откат обеспечен и это не формальность: мы затираем авторские тексты на
    500–1900 знаков шаблоном на ~250. Оригинал кладётся в ``before.description``
    и достаётся командой ``--rollback <code>``. **Существующий ``before`` не
    перезаписывается** — если в нём уже лежит снимок, сделанный до наших правок,
    он и есть настоящий оригинал; затерев его вторым прогоном, мы бы подменили
    точку отката шаблоном и откатывать стало бы не к чему.

    Идемпотентность — по содержимому: если в ВК уже ровно тот текст, который мы
    собираемся записать, регион пропускается и запись не идёт.

    **Пауза между записями обязательна** (найдено 06.09 на живом прогоне): сорок
    ``groups.edit`` подряд исчерпывают квоту аккаунта, и дальше молча перестаёт
    срабатывать не только скрипт, но и ручная правка в браузере — на часы. Пауза
    берётся из того же ``interval()``, что у ``process_region``/``repair_region``:
    единственный рычаг темпа на весь скрипт, а не своя константа. До 07.09 паузу
    держал агент снаружи, разбивая прогон на порции руками — правило жило в голове,
    а не в коде, и следующий прогон терял его вместе со сменой сессии.
    """
    import vk_api

    from modules.promotion.branding import TEMPLATE_VERSION
    from modules.promotion.group_setup_vk import edit_description, get_current

    # Описание нужно и областным группам — «все группы проекта» это 41 + 2.
    targets = await load_targets(codes, kinds=("raion", "oblast"))
    if not targets:
        logger.error("Нет целей")
        return 1

    _, community_tokens = await load_tokens()
    logger.info(
        "REFRESH-DESC: сообществ %d | режим: %s | community-ключ, user-бюджет не расходуется",
        len(targets),
        "запись" if apply else "dry-run",
    )

    changed = skipped = failed = 0
    wrote_any = False
    for target in targets:
        code = target["code"]
        gid = abs(int(target["vk_group_id"]))
        token = community_tokens.get(gid)
        if not token:
            logger.info("  %-14s ⛔ нет community-ключа", code)
            failed += 1
            continue
        api = vk_api.VkApi(token=token).get_api()

        snap = await asyncio.to_thread(get_current, api, target["vk_group_id"])
        if not snap.ok:
            logger.info("  %-14s ⛔ снимок не взялся: %s", code, snap.detail)
            failed += 1
            continue
        old = (snap.payload or {}).get("description") or ""
        new = build_texts(target)["description"]

        if old.strip() == new.strip():
            logger.info("  %-14s ✓ уже шаблонное (%d знаков)", code, len(old))
            skipped += 1
            continue

        logger.info("  %-14s %d → %d знаков", code, len(old), len(new))
        if not apply:
            continue

        # Пауза стоит ПЕРЕД записью и только между записями. Так у прогона нет
        # висящего хвоста после последней группы, а пропущенные (уже шаблонные)
        # сообщества не оплачиваются ожиданием: квоту тратит запись, а не обход.
        # На срезе 07.09 это 34 пропуска из 43 — почти три минуты пустого сна.
        if wrote_any:
            await asyncio.sleep(interval())
        wrote_any = True

        res = await asyncio.to_thread(edit_description, api, target["vk_group_id"], new)
        if not res.ok:
            logger.info("  %-14s ⛔ запись не прошла: %s", code, res.detail)
            failed += 1
            continue

        row = await _journal_row_id(target["region_id"], TEMPLATE_VERSION)
        if row is not None:
            before = row.before if isinstance(row.before, dict) else None
            if not (before or {}).get("description"):
                # Первый заход: оригинал ещё не сохранён — кладём живой текст.
                before = dict(before or {})
                before["description"] = old
            await finish_setup(
                row.id,
                status="applied",
                before=before,
                after={"description": new},
                applied_fields=["description"],
            )
        changed += 1
        logger.info("  %-14s ✍ описание обновлено", code)

    logger.info("")
    logger.info(
        "Готово: изменено %d, пропущено (уже шаблонные) %d, не вышло %d",
        changed,
        skipped,
        failed,
    )
    return 0 if failed == 0 else 1


async def run_audit(codes: Optional[List[str]]) -> int:
    """--audit: чего фактически недостаёт каждому сообществу. Только чтение.

    Зачем отдельный режим. Обычный dry-run печатает **режим** (``full``/``spot``),
    а режим — конъюнкция: ``full`` = нет аватара И нет обложки И описание короткое.
    Сообщество, у которого стоит обложка, но нет аватара, попадает в ``spot`` — и
    отчёт говорит «авторское оформление, не трогаем», хотя недостающее поле есть.
    То есть на вопрос «у кого нет обложки ИЛИ аватара» dry-run не отвечает в
    принципе: его условие не покрывает область вопроса (#284).

    Второй источник, ``promo_group_setup.status``, отвечает на другой вопрос —
    «чем закончился прогон», а не «что сейчас в ВК». Он уже расходился с явью
    дважды (01.09: у ``nagorsk`` аватар стоял при строке ``error``; у ``orichi``
    строки не было вовсе). Поэтому здесь снимок берётся из ВК, а журнал
    печатается рядом — расхождение видно глазами, а не выясняется постфактум.

    User-бюджет не расходуется, пока у сообщества есть community-ключ:
    ``groups.getById`` идёт им. Регионы без ключа считаются отдельно и помечены —
    у них снимок стоит один user-вызов.
    """
    import vk_api

    from modules.promotion.group_setup_vk import get_current, is_admin_only_wall

    targets = await load_targets(codes, kinds=("raion", "oblast"))
    if not targets:
        logger.error("Нет целей")
        return 1

    journal = await load_journal_status()
    user_token, community_tokens = await load_tokens()
    user_api = vk_api.VkApi(token=user_token).get_api() if user_token else None

    logger.info("AUDIT: сообществ %d | снимок из ВК, журнал — рядом для сверки", len(targets))
    logger.info(
        "  %-14s %-7s %-8s %-6s %-9s %s",
        "код",
        "аватар",
        "обложка",
        "опис.",
        "предложка",
        "журнал",
    )

    missing_avatar: List[str] = []
    missing_cover: List[str] = []
    unseen: List[str] = []
    user_calls = 0

    for target in targets:
        code = target["code"]
        gid = abs(int(target["vk_group_id"]))
        comm_token = community_tokens.get(gid)
        api = vk_api.VkApi(token=comm_token).get_api() if comm_token else user_api
        if api is None:
            unseen.append(code)
            logger.info("  %-14s %-7s %-8s %-6s %-9s %s", code, "?", "?", "?", "?", "нет ключа")
            continue
        if not comm_token:
            user_calls += 1
        snap = await asyncio.to_thread(get_current, api, gid)
        if not snap.ok:
            # «Не смогли посмотреть» — это не «всё на месте»: пустой ответ и
            # отсутствие поля должны выглядеть по-разному, иначе аудит начнёт
            # молчать одинаково на «чисто» и на «не измерили».
            unseen.append(code)
            logger.info("  %-14s %-7s %-8s %-6s %-9s %s", code, "?", "?", "?", "?", snap.detail)
            continue
        cur = snap.payload or {}
        has_avatar = bool(cur.get("has_photo"))
        has_cover = bool(cur.get("has_cover"))
        # Стена — объективный факт снимка. Про ПРЕДЛОЖКУ снимок не знает ничего:
        # её включает настройка «Кто может предлагать посты», которой нет ни в
        # groups.getById, ни (с тех пор как ВК выпилил getSettings) вообще нигде
        # в API. Мерить её умеет только scripts/probe_suggestions.py — пробой
        # can_suggest токеном постороннего аккаунта.
        admin_only = is_admin_only_wall(cur)
        if not has_avatar:
            missing_avatar.append(code)
        if not has_cover:
            missing_cover.append(code)
        logger.info(
            "  %-14s %-7s %-8s %-6d %-9s %s",
            code,
            "✓" if has_avatar else "НЕТ",
            "✓" if has_cover else "НЕТ",
            len(cur.get("description") or ""),
            "?" if admin_only is None else ("админы" if admin_only else f"wall={cur.get('wall')}"),
            journal.get(target["region_id"], "—"),
        )

    logger.info("")
    logger.info("Недостаёт аватара: %d — %s", len(missing_avatar), ", ".join(missing_avatar) or "—")
    logger.info("Недостаёт обложки: %d — %s", len(missing_cover), ", ".join(missing_cover) or "—")
    logger.info(
        "Предложка (кто может предлагать посты) в снимок ВК не входит — "
        "мерить через scripts/probe_suggestions.py"
    )
    if unseen:
        # Отдельной строкой и всегда: неизмеренное не должно раствориться в «всё ок».
        logger.info("НЕ ИЗМЕРЕНО: %d — %s", len(unseen), ", ".join(unseen))
    todo = sorted(set(missing_avatar) | set(missing_cover))
    logger.info(
        "Чинить: %d — %s (потрачено user-вызовов на снимки: %d)",
        len(todo),
        ", ".join(todo) or "—",
        user_calls,
    )
    return 0


async def load_journal_status() -> Dict[int, str]:
    """``region_id → status`` в журнале текущей версии шаблона (для сверки)."""
    from sqlalchemy import select

    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup
    from modules.promotion.branding import TEMPLATE_VERSION

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(PromoGroupSetup.region_id, PromoGroupSetup.status).where(
                    PromoGroupSetup.setup_version == TEMPLATE_VERSION
                )
            )
        ).all()
    return {region_id: status for region_id, status in rows}


async def select_repair_targets_by_snapshot(codes: Optional[List[str]]) -> List[dict]:
    """Кого чинить — по живому снимку ВК, а не по строке журнала.

    Журнал отвечает на «чем закончился прогон», а чинить надо то, чего **сейчас**
    нет в ВК. Пока выбор шёл по ``status='error'``, сообщество с уцелевшей строкой
    ``ok`` и пропавшим аватаром в ``--repair`` не попадало вообще: гейт стоял, был
    зелёный и не покрывал область (#284).

    Снимок берётся community-ключом, поэтому отбор почти не стоит user-бюджета;
    сам ремонт всё равно решает по своему снимку внутри ``repair_region``.

    Области берутся наравне с районами (``ALL_KINDS``): ремонт только доливает
    недостающее и ничего не переписывает сверху, поэтому область тут безопасна —
    в отличие от ``--apply``, который ставит пост и закреп (см. ``load_targets``).
    """
    import vk_api

    from modules.promotion.group_setup_vk import get_current

    targets = await load_targets(codes, kinds=ALL_KINDS)
    user_token, community_tokens = await load_tokens()
    user_api = vk_api.VkApi(token=user_token).get_api() if user_token else None

    out: List[dict] = []
    for target in targets:
        gid = abs(int(target["vk_group_id"]))
        comm_token = community_tokens.get(gid)
        api = vk_api.VkApi(token=comm_token).get_api() if comm_token else user_api
        if api is None:
            continue
        snap = await asyncio.to_thread(get_current, api, gid)
        if not snap.ok:
            # Снимок не взялся — регион берём в работу: repair_region попробует
            # снова и честно вернёт «снимок не взялся», а не тихо пропустит.
            out.append(target)
            continue
        cur = snap.payload or {}
        if not cur.get("has_photo") or not cur.get("has_cover"):
            out.append(target)
    return out


async def run_repair(codes: Optional[List[str]], by_snapshot: bool = False) -> int:
    """--repair: пройти регионы с записью status='error' и дозалить поля.

    С ``--by-snapshot`` цели выбираются по живому состоянию ВК, а не по журналу.
    """
    import vk_api
    from sqlalchemy import select

    from config.promo import get_valstan_call_budget
    from database.connection import AsyncSessionLocal
    from database.models import PromoGroupSetup
    from modules.promotion.branding import TEMPLATE_VERSION

    if by_snapshot:
        targets = await select_repair_targets_by_snapshot(codes)
        if not targets:
            logger.info("По живому снимку недостающих аватаров и обложек нет — чинить нечего")
            return 0
    else:
        async with AsyncSessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(PromoGroupSetup.region_id).where(
                            PromoGroupSetup.setup_version == TEMPLATE_VERSION,
                            PromoGroupSetup.status == "error",
                        )
                    )
                )
                .scalars()
                .all()
            )
        broken_ids = set(rows)
        if not broken_ids:
            logger.info("Записей со статусом error нет — чинить нечего")
            return 0

        targets = [
            t for t in await load_targets(codes, kinds=ALL_KINDS) if t["region_id"] in broken_ids
        ]
    all_targets = await load_targets(None)
    nb_index = neighbor_index(all_targets)
    for t in targets:
        t["_neighbor_index"] = nb_index

    user_token, community_tokens = await load_tokens()
    user_api = vk_api.VkApi(token=user_token).get_api() if user_token else None
    if user_api is None:
        logger.error("Нет user-токена")
        return 2

    budget = get_valstan_call_budget()
    spent = 0
    logger.info("REPAIR: регионов с ошибками %d, бюджет %d user-вызовов", len(targets), budget)
    for target in targets:
        if spent >= budget:
            logger.info("  %-14s ⏸ бюджет исчерпан", target["code"])
            continue
        gid = abs(int(target["vk_group_id"]))
        comm_token = community_tokens.get(gid)
        community_api = vk_api.VkApi(token=comm_token).get_api() if comm_token else None
        summary, calls, ok = await asyncio.to_thread(
            repair_region, target, user_api=user_api, community_api=community_api
        )
        spent += calls
        await mark_repaired(target["region_id"], TEMPLATE_VERSION, summary, ok)
        logger.info("  %-14s %s %s", target["code"], "🔧" if ok else "⛔", summary)
    return 0


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="выполнить правки (гейт #025)")
    parser.add_argument("--regions", default=None, help="коды через запятую")
    parser.add_argument("--rollback", default=None, metavar="CODE", help="откат описания")
    parser.add_argument(
        "--repair", action="store_true", help="дозалить упавшие аватары/обложки (status=error)"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="только чтение: у кого фактически нет аватара/обложки (снимок ВК + журнал рядом)",
    )
    parser.add_argument(
        "--by-snapshot",
        action="store_true",
        help="с --repair: выбирать цели по живому снимку ВК, а не по строке журнала",
    )
    parser.add_argument(
        "--refresh-desc",
        action="store_true",
        help="привести описания к шаблону v3 (в т.ч. у авторских spot-групп); с --apply — запись",
    )
    parser.add_argument(
        "--rank-localities",
        action="store_true",
        help="отранжировать места для поискового индекса (район — по упоминаниям, область — "
        "по подписчикам районов); с --apply пишет config.localities_top",
    )
    parser.add_argument("--out", default=None, help="куда сложить превью картинок")
    parser.add_argument(
        "--force-cover",
        action="store_true",
        help="перезалить обложку там, где её ставили мы (авторские не трогает)",
    )
    args = parser.parse_args()

    from modules.secrets_bootstrap import bootstrap_secrets

    bootstrap_secrets()

    if args.rollback:
        return await rollback(args.rollback)
    if args.force_cover:
        codes = [c.strip() for c in args.regions.split(",")] if args.regions else None
        return await run_force_cover(codes)
    if args.audit:
        codes = [c.strip() for c in args.regions.split(",")] if args.regions else None
        return await run_audit(codes)
    if args.rank_localities:
        codes = [c.strip() for c in args.regions.split(",")] if args.regions else None
        return await run_rank_places(codes, apply=args.apply)
    if args.refresh_desc:
        codes = [c.strip() for c in args.regions.split(",")] if args.regions else None
        return await run_refresh_desc(codes, apply=args.apply)
    if args.repair:
        codes = [c.strip() for c in args.regions.split(",")] if args.regions else None
        return await run_repair(codes, by_snapshot=args.by_snapshot)

    import vk_api

    from config.promo import get_valstan_call_budget
    from modules.promotion.branding import TEMPLATE_VERSION

    codes = [c.strip() for c in args.regions.split(",")] if args.regions else None
    targets = await load_targets(codes)
    if not targets:
        logger.error("Нет целей")
        return 2

    all_targets = await load_targets(None)
    nb_index = neighbor_index(all_targets)
    for t in targets:
        t["_neighbor_index"] = nb_index

    user_token, community_tokens = await load_tokens()
    user_api = vk_api.VkApi(token=user_token).get_api() if user_token else None
    if args.apply and user_api is None:
        logger.error("Нет user-токена — аватары и закреп невозможны")
        return 2

    budget = get_valstan_call_budget()
    budget_regions = max(1, budget // USER_CALLS_PER_REGION)
    done_full = 0

    logger.info(
        "Регионов: %d | режим: %s | шаблон v%d | бюджет VALSTAN: %d вызовов (~%d full-регионов)",
        len(targets),
        "APPLY" if args.apply else "dry-run",
        TEMPLATE_VERSION,
        budget,
        budget_regions,
    )

    for target in targets:
        gid = abs(int(target["vk_group_id"]))
        comm_token = community_tokens.get(gid)
        community_api = vk_api.VkApi(token=comm_token).get_api() if comm_token else None

        setup_id = None
        if args.apply:
            setup_id = await claim_setup(target["region_id"], TEMPLATE_VERSION)
            if setup_id is None:
                logger.info(
                    "  %-14s ✅ уже применено (v%d) — пропуск", target["code"], TEMPLATE_VERSION
                )
                continue
            if done_full >= budget_regions:
                logger.info("  %-14s ⏸ бюджет VALSTAN исчерпан — продолжить завтра", target["code"])
                continue

        mode, summary, report = await asyncio.to_thread(
            process_region,
            target,
            apply=args.apply,
            user_api=user_api,
            community_api=community_api,
            out_dir=args.out,
        )
        icon = {"full": "🛠", "spot": "👁", "error": "⛔"}.get(mode, "·")
        logger.info("  %-14s %s %-5s %s", target["code"], icon, mode, summary)

        if args.apply and setup_id is not None and report is not None:
            ok = not report["errors"]
            await finish_setup(
                setup_id,
                status="applied" if ok else "error",
                before=report.get("before"),
                after={"applied": report["applied"]},
                applied_fields=report["applied"],
                pinned_post_url=report.get("post_url"),
                error="; ".join(report["errors"]) or None,
            )
            if mode == "full":
                done_full += 1
        elif args.apply and setup_id is not None:
            # spot/error: запись не нужна — снимаем claim, чтобы не висел dry_run
            await finish_setup(setup_id, status="dry_run", error=f"{mode}: {summary}")

    logger.info("\nГотово. Город и статус правятся только в веб-интерфейсе (см. отчёт spot).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
