"""Отбор постов для сайта-получателя — вход конвейера (D-015).

Источник — **пересечение двух журналов**, а не один из них (решение владельца
2026-08-09, обоснование в ``config/content_conveyor``):

- ``bulletin_curation_runs`` с непустым ``published_post_id`` даёт признак
  «пост вошёл в опубликованную сводку района», то есть прошёл отбор, за который
  уже отвечает человек; сам текст там — снапшот кандидата;
- ``collected_post_audit`` (ADR-0004) даёт по тому же ``lip`` чистый текст поста
  и сводку вложений с прямыми ссылками на фото — то, что сайт переложит к себе.

Брать текст из аудита, а не из кандидата сводки, важно не из аккуратности:
кандидат — снимок для ленты классификатора, а аудит хранит ``media`` (миграция
060). Без него доставка знала бы, что вложения есть, но не где они лежат.

Уже обработанное отсеивается по ``conveyor_deliveries`` (site, lip): повторный
прогон не должен платить за LLM второй раз. Дедуп на стороне сайта (по
``vkPostId``) эту нашу трату не покрывает — он защищает его коллекцию, не наш счёт.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select

from database.models_extended import BulletinCurationRun, CollectedPostAudit, ConveyorDelivery
from modules.deduplication.fingerprints import jaccard_similarity, text_token_set

logger = logging.getLogger(__name__)

# --- Дедуп «одна новость от разных пабликов» (recommend brain 2026-09-14) -----
#
# Школа, ДК и районная газета постят одно событие — портал режет такое по
# нормализованному заголовку за 7 дней и отвечает ``duplicateOf``. У нас это
# решается раньше и дешевле: до LLM-вызова и до того, как приёмник пойдёт качать
# те же фотографии второй раз.
#
# **Порог подобран замером, а не на глаз.** 315 доставок портала за 30 дней,
# все пары внутри окна: сигнал ``max(Jaccard по тексту, Jaccard по лиду)``
# ловит 18 пар при 0.5, и ближайшая пара-НЕ-дубль лежит на 0.32 («Отключение
# воды» против «Отключение электроэнергии»). То есть между порогом и шумом
# полторы десятых запаса.
#
# Почему два сигнала, а не один. Полный текст ловит дословную перепечатку, но
# проваливает пересказ с разным хвостом: одинаковые заголовки «Педагоги
# Малмыжского района участвуют в областных туристских соревнованиях» дают по
# полному тексту всего 0.48, а по лиду — 0.62. Лид, в свою очередь, слеп к
# перепечатке с переписанным началом. Максимум из двух ловит оба класса.
#
# Почему порог осознанно строгий. Цены ошибок несимметричны: наш пропуск
# подхватит дедуп портала по заголовку (новость всё равно не задвоится), а наш
# ложный срез — это местная новость, которой на сайте не будет никогда, потому
# что строка журнала закроет её от следующих прогонов. Поэтому берём запас.
DUP_WINDOW_DAYS = 7
DUP_THRESHOLD = 0.5
DUP_FULL_CHARS = 1200
DUP_LEAD_CHARS = 300


def _published_lips(
    runs: Sequence[BulletinCurationRun], skip_themes: Sequence[str]
) -> Dict[str, str]:
    """map lip → тема сводки, в которую пост вошёл (новейшая запись выигрывает).

    Тема берётся у сводки, а не у поста: она и есть решение сборщика о том, чем
    этот пост был в ленте района, и по ней работает дешёвый префильтр ``skip_themes``.
    """
    skip = {t.strip().lower() for t in (skip_themes or ()) if str(t).strip()}
    out: Dict[str, str] = {}
    for run in runs:
        theme = (run.theme or "").strip().lower()
        if theme in skip:
            continue
        cands = run.candidates or []
        if not isinstance(cands, (list, tuple)):
            continue
        for c in cands:
            if not isinstance(c, dict):
                continue
            lip = str(c.get("lip") or "").strip()
            if not lip or lip in out:
                continue
            out[lip] = theme
    return out


def _norm_keywords(keywords: Sequence[str]) -> List[str]:
    """Слова ловли в нижнем регистре, пустые выброшены."""
    return [k.strip().lower() for k in (keywords or ()) if str(k).strip()]


def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    """Есть ли в тексте хоть одно слово ловли. Пустой список слов → ``False``.

    Подстрокой, а не по границам слов, и это осознанно: «ярмарк» обязан ловить
    «ярмарка/ярмарке/ярмарочный», а морфологии у нас здесь нет и заводить её
    ради четырёх слов незачем. Плата за подстроку — ложные срабатывания на
    длинных словах; для тематического сайта это дешевле пропуска, потому что
    дальше стоит LLM с правилами сайта, которая лишнее отклонит.
    """
    body = (text or "").lower()
    if not body:
        return False
    return any(k in body for k in _norm_keywords(keywords))


def passes_source_filter(
    lip: str,
    text: str,
    *,
    owner_ids: Sequence[int],
    keywords: Sequence[str],
) -> bool:
    """Годится ли пост тематическому сайту: свой паблик **ИЛИ** профильное слово.

    Ни того, ни другого не задано → берём всё (поведение сайта без сужений).
    Задано только одно → работает только оно.
    """
    wanted_owners = {str(abs(int(x))) for x in (owner_ids or ()) if str(x).strip()}
    words = _norm_keywords(keywords)
    if not wanted_owners and not words:
        return True
    if wanted_owners and lip.partition("_")[0] in wanted_owners:
        return True
    return bool(words) and matches_keywords(text, words)


def _only_owners(lip_theme: Dict[str, str], owner_ids: Sequence[int]) -> Dict[str, str]:
    """Оставить посты только названных пабликов. Пустой список — без ограничения.

    Тематический сайт (Казанская — ярмарка и карнавал) берёт не весь район, а
    один-два источника: Дом культуры и оргкомитет. Сужаем ДО LLM — это второй
    дешёвый префильтр рядом с ``skip_themes``, а не редакционное решение: что из
    постов ДК про ярмарку, решает модель по правилам сайта.

    ``lip`` хранит owner по модулю (так пишет сбор), поэтому сравниваем с
    ``abs(owner_id)`` — конфиг может быть записан и со знаком ВК, и без.
    """
    wanted = {str(abs(int(x))) for x in owner_ids if str(x).strip()}
    if not wanted:
        return lip_theme
    return {lip: theme for lip, theme in lip_theme.items() if lip.partition("_")[0] in wanted}


# --- Повтор классификации после временного сбоя (завал 402, октябрь 2026) ----
#
# Строка ``failed`` БЕЗ вердикта означает «классификация не состоялась» (402,
# сеть, падение), а не решение. Хоронить её навсегда — терять волну целиком:
# двухнедельный простой движка превратил редкий край в основной поток (110
# постов портала за 7 суток). Такие строки возвращаются в отбор, пока не
# исчерпан лимит попыток и выдержана пауза между ними.
#
# Рядом лежит вторая половина той же дыры: ``selected`` без движения дольше
# суток — сирота убитого посреди прогона воркера (OOM P164), а не «в работе».
# Её тоже подбираем, иначе волна, не перехваченная парсингом, пропадает молча.
#
# Решения не трогаем: ``rejected``/``held`` и ``failed`` С вердиктом принадлежат
# человеку и повтору доставки (``retry_failed``) — их пересмотр молча следующим
# прогоном снова стоил бы вызова за вызовом.
CLASSIFY_RETRY_MAX = 5
# Пауза перед N-й попыткой (N = число уже сделанных): 1ч, 6ч, сутки, трое, неделя.
CLASSIFY_RETRY_BACKOFF = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
)
# Возраст сироты ``selected``, после которого строка считается брошенной.
SELECTED_ORPHAN_AFTER = timedelta(days=1)


async def _requeueable_lips(session, *, site: str) -> set:
    """lip'ы, которым положен повторный заход в отбор: несбывшаяся классификация
    (``failed`` без вердикта) в пределах лимита попыток и после паузы, плюс
    брошенные ``selected``-сироты. Пустое множество — нечего подбирать."""
    site_key = (site or "").strip().lower()
    if not site_key:
        return set()
    now = datetime.utcnow()
    rows = (
        await session.execute(
            select(
                ConveyorDelivery.lip,
                ConveyorDelivery.status,
                ConveyorDelivery.attempts,
                ConveyorDelivery.updated_at,
                ConveyorDelivery.verdict,
            ).where(ConveyorDelivery.site == site_key)
        )
    ).all()
    out = set()
    for lip, status, attempts, updated, verdict in rows:
        n = attempts or 0
        if n >= CLASSIFY_RETRY_MAX:
            continue
        if status == "failed" and verdict is None:
            wait = CLASSIFY_RETRY_BACKOFF[min(n, len(CLASSIFY_RETRY_BACKOFF) - 1)]
            if updated is not None and updated <= now - wait:
                out.add(lip)
        elif status == "selected":
            if updated is not None and updated <= now - SELECTED_ORPHAN_AFTER:
                out.add(lip)
    return out


async def _delivered_lips(session, *, site: str) -> set:
    """lip'ы, у которых для этого сайта уже есть строка журнала — в любом статусе.

    Отсекаем и ``rejected``/``held``: пост, однажды признанный неподходящим,
    не должен возвращаться в следующий прогон и снова стоить вызова. Пересмотр
    такого решения — операторское действие над строкой журнала, а не побочный
    эффект расписания.
    """
    rows = (
        await session.execute(select(ConveyorDelivery.lip).where(ConveyorDelivery.site == site))
    ).all()
    return {r[0] for r in rows}


async def fetch_pending_for_site(
    session,
    site: Dict[str, Any],
    *,
    days: int = 3,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Посты, готовые к классификации для этого сайта — новейшие первыми.

    Возвращает список ``{lip, text, url, media, theme, region_code}``. Пост без
    текста пропускается: отдавать LLM пустую строку незачем, а на сайт такой
    материал всё равно не пойдёт.
    """
    site_key = str(site.get("key") or "").strip().lower()
    region = str(site.get("source_region") or "").strip()
    if not site_key or not region:
        return []

    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    runs = (
        (
            await session.execute(
                select(BulletinCurationRun)
                .where(BulletinCurationRun.region_code == region)
                .where(BulletinCurationRun.published_post_id.isnot(None))
                .where(BulletinCurationRun.created_at >= cutoff)
                .order_by(BulletinCurationRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    owner_ids = site.get("source_owner_ids") or ()
    keywords = site.get("source_keywords") or ()
    lip_theme = _published_lips(runs, site.get("skip_themes") or ())
    # Сузить по пабликам ДО выборки текстов можно только когда ловли по словам
    # нет: со словами кандидатом становится весь район, и решает уже текст,
    # которого на этом шаге ещё нет.
    if owner_ids and not keywords:
        lip_theme = _only_owners(lip_theme, owner_ids)
    if not lip_theme:
        return []

    done = await _delivered_lips(session, site=site_key)
    # Несбывшаяся классификация и брошенный отбор — не «обработано»: такие строки
    # возвращаются в игру (лимит попыток + пауза — внутри _requeueable_lips).
    done -= await _requeueable_lips(session, site=site_key)
    wanted = [lip for lip in lip_theme if lip not in done]
    if not wanted:
        return []

    rows = (
        (
            await session.execute(
                select(CollectedPostAudit)
                .where(CollectedPostAudit.lip.in_(wanted))
                .order_by(CollectedPostAudit.collected_at.desc())
            )
        )
        .scalars()
        .all()
    )

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for r in rows:
        if r.lip in seen:
            continue
        seen.add(r.lip)
        text = (r.post_text or "").strip()
        if not text:
            continue
        if keywords and not passes_source_filter(
            r.lip, text, owner_ids=owner_ids, keywords=keywords
        ):
            continue
        out.append(
            {
                "lip": r.lip,
                "text": text,
                "url": r.post_url or "",
                "media": r.media or [],
                "theme": lip_theme.get(r.lip, ""),
                "region_code": r.region_code,
                # Дата поста В ВК (миграция 080). Уезжает в ``date`` приёмника —
                # по ней сортируется лента портала (D-091). ``None`` у постов
                # из бэклога до миграции 080; на свежих сборах заполнено.
                "published_at": r.published_at,
            }
        )
        if len(out) >= max(1, limit):
            break
    return out


def dup_signature(text: str) -> Tuple[frozenset, frozenset]:
    """Подпись текста для near-dup: ``(слова тела, слова лида)``.

    Обе половины — множества слов от ``text_token_set`` (тот же разбор, что у
    дедупа сводок: свой второй токенайзер разошёлся бы с первым молча).
    """
    body = (text or "").strip()
    return text_token_set(body[:DUP_FULL_CHARS]), text_token_set(body[:DUP_LEAD_CHARS])


def dup_similarity(a: Tuple[frozenset, frozenset], b: Tuple[frozenset, frozenset]) -> float:
    """Схожесть двух подписей — максимум из «по телу» и «по лиду». См. порог выше."""
    return max(jaccard_similarity(a[0], b[0]), jaccard_similarity(a[1], b[1]))


def split_near_duplicates(
    posts: Sequence[Dict[str, Any]],
    *,
    recent: Sequence[Tuple[str, Tuple[frozenset, frozenset]]] = (),
    threshold: float = DUP_THRESHOLD,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделить партию на ``(к работе, дубли)``. У дубля проставлен ``dup_of``.

    Два прохода сравнения, и оба нужны:

    * против уже доставленного за окно (``recent``) — новость, которая вчера
      уехала от газеты, сегодня приезжает от школы;
    * внутри самой партии — один прогон часто забирает обе копии разом.

    **Внутри партии выигрывает более длинный текст**, а не первый по порядку.
    Порядок здесь — ``collected_at desc``, то есть про качество он не говорит
    ничего; длина же говорит: полный пересказ события даёт модели больше, чем
    короткая заметка о том же. Сравнение по длине делает выбор воспроизводимым —
    иначе результат зависел бы от того, чей парсер отработал первым.
    """
    kept: List[Dict[str, Any]] = []
    kept_sigs: List[Tuple[frozenset, frozenset]] = []
    dropped: List[Dict[str, Any]] = []

    for post in posts:
        sig = dup_signature(str(post.get("text") or ""))

        twin = next(
            (lip for lip, prev in recent if dup_similarity(sig, prev) >= threshold),
            None,
        )
        if twin:
            dropped.append({**post, "dup_of": twin})
            continue

        hit = next(
            (i for i, prev in enumerate(kept_sigs) if dup_similarity(sig, prev) >= threshold),
            None,
        )
        if hit is None:
            kept.append(post)
            kept_sigs.append(sig)
            continue

        rival = kept[hit]
        if len(str(post.get("text") or "")) > len(str(rival.get("text") or "")):
            kept[hit], kept_sigs[hit] = post, sig
            dropped.append({**rival, "dup_of": str(post.get("lip") or "")})
        else:
            dropped.append({**post, "dup_of": str(rival.get("lip") or "")})

    return kept, dropped


async def fetch_recent_signatures(
    session,
    *,
    site: str,
    days: int = DUP_WINDOW_DAYS,
) -> List[Tuple[str, Tuple[frozenset, frozenset]]]:
    """Подписи постов, уже уехавших на сайт за окно — для дедупа между прогонами.

    Берём только ``delivered``: ``rejected`` модель уже отвергла, и новая копия
    той же новости заслуживает такого же вердикта, а не наследования чужого.
    Окно считаем по дате поста в ВК, как её считает и приёмник.
    """
    site_key = (site or "").strip().lower()
    if not site_key:
        return []
    cutoff = datetime.utcnow() - timedelta(days=max(1, days))
    rows = (
        await session.execute(
            select(CollectedPostAudit.lip, CollectedPostAudit.post_text)
            .join(ConveyorDelivery, ConveyorDelivery.lip == CollectedPostAudit.lip)
            .where(ConveyorDelivery.site == site_key)
            .where(ConveyorDelivery.status == "delivered")
            .where(CollectedPostAudit.collected_at >= cutoff)
        )
    ).all()
    return [(lip, dup_signature(text or "")) for lip, text in rows if (text or "").strip()]


async def record_selection(
    session,
    *,
    site: str,
    lips: Sequence[str],
) -> int:
    """Завести строки журнала со статусом ``selected``. Возвращает число созданных.

    Идемпотентно: существующие ``(site, lip)`` пропускаются. Коммит — на
    вызывающем, чтобы отбор и первый шаг обработки ложились одной транзакцией.
    """
    site_key = (site or "").strip().lower()
    # ``x is not None`` до ``str(x)``: иначе None превращается в строку "None",
    # проходит проверку на непустоту и заводит строку журнала-призрак.
    fresh = [str(x).strip() for x in (lips or []) if x is not None and str(x).strip()]
    if not site_key or not fresh:
        return 0
    existing = {
        r[0]
        for r in (
            await session.execute(
                select(ConveyorDelivery.lip)
                .where(ConveyorDelivery.site == site_key)
                .where(ConveyorDelivery.lip.in_(fresh))
            )
        ).all()
    }
    added = 0
    for lip in fresh:
        if lip in existing:
            continue
        existing.add(lip)
        session.add(ConveyorDelivery(site=site_key, lip=lip, status="selected"))
        added += 1
    return added


async def fetch_audit_snapshots(session, *, lips: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Снапшоты постов из аудита по списку ``lip`` — map lip → пост.

    Нужны при досылке: вердикт лежит в журнале, а текст и медиа — в аудите, и
    собирать тело запроса заново дешевле, чем хранить его копию в журнале.
    """
    wanted = [str(x).strip() for x in (lips or []) if x is not None and str(x).strip()]
    if not wanted:
        return {}
    rows = (
        (
            await session.execute(
                select(CollectedPostAudit).where(CollectedPostAudit.lip.in_(wanted))
            )
        )
        .scalars()
        .all()
    )
    return {
        r.lip: {
            "lip": r.lip,
            "text": (r.post_text or "").strip(),
            "url": r.post_url or "",
            "media": r.media or [],
            "region_code": r.region_code,
            "published_at": r.published_at,
        }
        for r in rows
    }


async def update_delivery(
    session,
    *,
    site: str,
    lip: str,
    status: str,
    reason: Optional[str] = None,
    verdict: Optional[Dict[str, Any]] = None,
    attempts: Optional[int] = None,
    attempts_inc: bool = False,
    http_status: Optional[int] = None,
    remote_id: Optional[str] = None,
    clear_reason: bool = False,
) -> bool:
    """Обновить строку журнала. ``False`` — строки нет (отбор её не заводил).

    Пишем только переданные поля: прогон доставки не должен затирать вердикт,
    добытый предыдущим шагом, лишь потому, что не знает о нём.

    ``clear_reason`` — стереть причину явно. Нужен при удачной досылке: иначе в
    строке останется ``network`` от прошлого сбоя, и запись «доставлено, причина:
    сеть недоступна» будет врать тому, кто придёт разбираться.
    """
    row = (
        await session.execute(
            select(ConveyorDelivery)
            .where(ConveyorDelivery.site == (site or "").strip().lower())
            .where(ConveyorDelivery.lip == (lip or "").strip())
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.status = status
    if clear_reason:
        row.reason = None
    elif reason is not None:
        row.reason = reason[:64]
    if verdict is not None:
        row.verdict = verdict
    if attempts_inc:
        row.attempts = (row.attempts or 0) + 1
    elif attempts is not None:
        row.attempts = attempts
    if http_status is not None:
        row.http_status = http_status
    if remote_id is not None:
        row.remote_id = remote_id[:100]
    return True


async def site_status_counts(session, *, site: str) -> Dict[str, int]:
    """Сводка журнала по статусам для сайта — основа метрики и разбора «почему нет на сайте»."""
    from sqlalchemy import func

    rows = (
        await session.execute(
            select(ConveyorDelivery.status, func.count())
            .where(ConveyorDelivery.site == (site or "").strip().lower())
            .group_by(ConveyorDelivery.status)
        )
    ).all()
    return {str(s): int(n) for s, n in rows}


def summarize_for_prompt(post: Dict[str, Any], *, max_chars: int = 1500) -> Optional[str]:
    """Компактное представление поста для LLM-вызова. ``None`` — подавать нечего.

    Обрезка по символам, а не по токенам, намеренно: точный счёт токенов требует
    токенизатора модели, а нам нужен предохранитель от аномально длинного поста,
    и грубой границы для него достаточно.
    """
    text = (post.get("text") or "").strip()
    if not text:
        return None
    media = post.get("media") or []
    kinds = sorted({str(m.get("type")) for m in media if isinstance(m, dict) and m.get("type")})
    head = f"[тема сводки: {post.get('theme') or '—'}; вложения: {', '.join(kinds) or 'нет'}]"
    return f"{head}\n{text[:max_chars]}"
