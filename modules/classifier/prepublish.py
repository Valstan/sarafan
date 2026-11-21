"""Нейро-фильтр ВНУТРИ волны: вердикт выносится ДО первой публикации.

**Дыра, которую это закрывает** (измерено 2026-08-19 по заказу владельца).
Волна публикации сама парсит посты из ВК, тут же пишет их в аудит сбора и тут
же публикует отобранное. Классификация же шла отдельной таской раз в 3 часа по
уже накопленному аудиту. Значит в момент первой публикации вердикта по посту
не было **по построению**, а не по невезению:

    за 14 дней из 20 366 постов, прошедших детерминированный фильтр,
    вердикт получили 52.7% — и во ВСЕХ 10 743 случаях он появился ПОЗЖЕ сбора.
    Раньше сбора — ноль.

``enforce.fetch_blocked_lips`` ловил такие посты только на следующих волнах, и
ловил много (15.08 — 4140 постов против 1015 у детерминированного hard-spam),
но первая волна оставалась незакрытой. А отсев спама — главная задача движка:
за ретрансляцию чужой рекламы ВК банит аккаунт админа паблика (инцидент Уржум
2026-07-08, G151).

**Что делает модуль.** Получает кандидатов, которых волна собирается
опубликовать, доклассифицирует тех, у кого вердикта ещё нет, и возвращает
lip'ы под блокировку. Батч маленький (это уже отфильтрованные кандидаты одной
темы одного района, обычно единицы-десятки), поэтому задержка волны — секунды,
а не минуты.

**Fail-open, и это осознанно.** Отказ движка (нет ключа, сеть, лимит) →
пустой набор, волна публикует по обычным фильтрам. Fail-closed остановил бы
сводки всех районов на любой сбой DeepSeek — а инцидент 2026-08-19 показал, что
сбой может длиться сутками. Цена молчаливого fail-open была ровно та, из-за
которой этот модуль и пишется, поэтому отказ теперь: (1) логируется ERROR,
(2) виден сторожу ``modules.classifier.heartbeat`` — свежих вердиктов не
появляется, и через 8 часов приходит алёрт.

**Гейт ``CLASSIFIER_PREPUBLISH_ENABLED``, по умолчанию выключен.** Модуль
встаёт на путь публикации, а не сбоку от него; включать осознанно и после
наблюдения за расходом токенов.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Set

from config.opencode import get_publish_timeout_for_engine

# ``post_lip`` переехал в utils (нужен и сборщику сводки — порядок срочного),
# но импортируется сюда ради прежнего пути `modules.classifier.prepublish.post_lip`:
# им пользуются волна и десяток тестов.
from utils.post_utils import post_lip  # noqa: F401  (ре-экспорт)

logger = logging.getLogger(__name__)

# ─── Состояние движка в волне ───────────────────────────────────────────
#
# «Движок молчит» и «движка не звали» — разные вещи, и путать их дорого.
# Замер 21.11 (сеть, 241 волна за день): 90 волн прошли мимо модели —
# «меньше порога 5, модель не зовём», сознательная экономия токенов; ещё 52
# волны увидели «ни одного вердикта» и попали под политику деградации, которая
# объявила движок молчащим. Итог: 31 волна пропущена целиком, публикаций ноль,
# алёрт «ИИ-фильтр молчит» в Telegram. Движок при этом был ЖИВ: фоновый
# классификатор в 15:13 отдал 71 вердикт без единой ошибки.
#
# Молчание движка — это когда его ЗВАЛИ, а он не ответил. Не позвали — не
# молчание, и волна не должна за это пропадать. Отсюда четыре состояния, а не
# два: отбор читает их и знает, кого винить.

ENGINE_CONSULTED = "consulted"
"""Движка звали, вердикты получили."""

ENGINE_NOT_CONSULTED_FLOOR = "not-consulted-floor"
"""Не звали: кандидатов меньше порога (экономия токенов). Не сбой."""

ENGINE_NOT_CONSULTED_GATE = "not-consulted-gate"
"""Не звали: гейт ``CLASSIFIER_PREPUBLISH_ENABLED`` выключен. Не сбой."""

ENGINE_FAILED = "failed"
"""Звали, а движок не ответил. Вот это — молчание."""


@dataclass(frozen=True)
class PrepublishResult:
    """Итог нейро-гейта волны: что заблокировано и что было с движком.

    ``blocked`` — lip'ы под блокировку (движок осудил: delete/hold).
    ``engine_state`` — одно из ``ENGINE_*`` выше. Отбор по меткам читает его,
    чтобы отличить «движок молчит» (политика деградации) от «движка не звали»
    (обычные фильтры, волна идёт).
    """

    blocked: Set[str]
    engine_state: str


def prepublish_enabled() -> bool:
    """Классифицировать ли кандидатов прямо в волне (env, дефолт — выкл)."""
    return os.getenv("CLASSIFIER_PREPUBLISH_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _post_url(post: Dict[str, Any]) -> str:
    """Ссылка на пост. В URL знак owner_id нужен, в отличие от lip."""
    owner = post.get("owner_id")
    post_id = post.get("id")
    if owner is None or post_id is None:
        return ""
    return f"https://vk.com/wall{owner}_{post_id}"


def to_classifier_items(
    posts: Sequence[Dict[str, Any]], *, region_code: str
) -> List[Dict[str, Any]]:
    """Кандидаты волны → форма, которую понимает ``headless.classify_posts``.

    Регион проставляется один и тот же: волна идёт по одному району, а модель
    судит гео-относительными правилами («чужой район → delete») и обязана знать,
    от чьего лица смотрит.

    Счётчики и дата едут вместе с текстом (решение владельца 2026-09-22): модель
    решает ``urgent`` и ``importance``, и без них она судила бы о важности, не
    видя ни отклика людей, ни того, свежий перед ней пост или вчерашний.
    """
    from utils.post_utils import post_counts

    items: List[Dict[str, Any]] = []
    for post in posts:
        lip = post_lip(post)
        if not lip:
            continue
        item = {
            "lip": lip,
            "region_code": region_code,
            "text": str(post.get("text") or "").strip(),
            "url": _post_url(post),
            "media": [],
            "date": post.get("date"),
        }
        item.update(post_counts(post))
        items.append(item)
    return items


async def prepublish_candidates(
    session,
    posts: Sequence[Dict[str, Any]],
    *,
    region_code: str,
) -> PrepublishResult:
    """Доклассифицировать кандидатов волны и вернуть lip'ы под блокировку.

    Возвращает ``PrepublishResult``: заблокированные lip'ы + состояние движка.
    Состояние — не украшение: отбор по меткам читает его, чтобы отличить
    «движок молчит» (политика деградации: пропустить волну) от «движка не
    звали» (обычные фильтры, волна идёт). Без этого различия волна
    пропадала из-за сознательной экономии токенов, а не из-за сбоя (см.
    :data:`ENGINE_CONSULTED` и комментарий выше).

    Никакого отказа не бросаем наружу: fail-open, волна идёт по обычным
    фильтрам (см. docstring модуля).
    """
    if not prepublish_enabled() or not posts or not region_code:
        return PrepublishResult(blocked=set(), engine_state=ENGINE_NOT_CONSULTED_GATE)

    try:
        from config.classifier import (
            classifier_disabled,
            get_chunk_size_for_engine,
            get_headless_regions_per_chunk,
            get_prepublish_min_candidates,
        )

        if classifier_disabled():
            return PrepublishResult(blocked=set(), engine_state=ENGINE_NOT_CONSULTED_GATE)

        from modules.classifier import enforce, headless, rules, service
        from modules.secrets_bootstrap import ensure_secret

        items = to_classifier_items(posts, region_code=region_code)
        if not items:
            return PrepublishResult(blocked=set(), engine_state=ENGINE_NOT_CONSULTED_GATE)

        # Уже размеченные не переклассифицируем: вердикт по ним применит
        # ``enforce`` тем же набором правил, а повторный вызов — деньги на ветер.
        known = await enforce.fetch_blocked_lips(session, region_code)
        already = await _lips_with_verdict(session, [i["lip"] for i in items], region_code)
        fresh = [i for i in items if i["lip"] not in already]
        if not fresh:
            # Логируем и этот исход. Иначе гейт, отработавший «всё уже размечено»,
            # неотличим в логе от гейта, который не отработал вовсе — а мы только
            # что чинили ровно такую немоту в самом классификаторе.
            logger.info(
                "classifier prepublish: регион=%s кандидатов=%d новых=0"
                " (все уже размечены), блокирует=%d",
                region_code,
                len(items),
                len(known & {i["lip"] for i in items}),
            )
            # Движок по этим постам УЖЕ высказался (вердикты есть) — это не молчание.
            return PrepublishResult(blocked=known, engine_state=ENGINE_CONSULTED)

        # Порог «звать модель только на пачку». Стоимость вызова задают
        # постулаты, а не посты: один кандидат и десять стоят почти одинаково,
        # при этом в выпуск попадает не больше трёх постов. Замер 25.10: 946
        # вызовов из 1319 были ради одного кандидата. Ниже порога — обычные
        # фильтры (та же fail-open политика, что при отказе движка).
        floor = get_prepublish_min_candidates()
        if len(fresh) < floor:
            logger.info(
                "classifier prepublish: регион=%s кандидатов=%d новых=%d — меньше"
                " порога %d, модель не зовём (fail-open, блокирует=%d)",
                region_code,
                len(items),
                len(fresh),
                floor,
                len(known & {i["lip"] for i in items}),
            )
            # Ключевая строка всего фикса: модель НЕ звали, и это не её молчание.
            # Отбор обязан знать это и не вычитать волну из-под политики деградации.
            return PrepublishResult(blocked=known, engine_state=ENGINE_NOT_CONSULTED_FLOOR)

        # Ключ дотягиваем, но его наличие НЕ решаемо здесь: нет ключа — модель
        # просто не ответит, и это уже разберётся по её собственному отказу.
        # Проверять ``ensure_secret`` заранее нельзя: в тестах и в сухом прогоне
        # модель подставлена заглушкой, и ранняя проверка убила бы вызов.
        ensure_secret("DEEPSEEK_API_KEY")

        try:
            postulates = await rules.render_effective_postulates(session)
        except Exception as e:  # noqa: BLE001 — до движения движка не дошли
            # Постулаты читаются из БД: сбой тут ничего не говорит о движке.
            # Приписывать ему молчание нельзя — волна идёт по обычным фильтрам.
            logger.warning(
                "classifier prepublish: постулаты не прочитаны (%s) — модель не зовём",
                e,
                exc_info=True,
            )
            return PrepublishResult(blocked=known, engine_state=ENGINE_NOT_CONSULTED_GATE)

        run = headless.classify_posts(
            fresh,
            postulates=postulates,
            chunk_size=get_chunk_size_for_engine(),
            regions_per_chunk=get_headless_regions_per_chunk(),
            # Волна стоит в единственном слоте воркера: не ответил за короткий
            # потолок — идём по обычным фильтрам (fail-open, как при отказе чанка).
            # Потолок — по текущему движку: у MiniMax вызов это запуск
            # агента-CLI (десятки секунд), общие 40 с его обрывали бы.
            timeout=get_publish_timeout_for_engine(),
        )
        if run["failures"]:
            logger.error(
                "classifier prepublish: %s/%s чанков отказали (%s) — волна %s идёт"
                " по обычным фильтрам",
                len(run["failures"]),
                run["chunks"],
                run["failures"][0],
                region_code,
            )
        if not run["verdicts"]:
            # Пост без текста движок пропускает намеренно: текстовая модель судила
            # бы по одному заголовку, а вердикт затем ЗАБЛОКИРОВАЛ бы публикацию.
            # Это штатный исход, и кричать о нём нельзя: предупреждение, которое
            # срабатывает на норме, обучает не смотреть на предупреждения — тот
            # же способ ослепнуть, которым сегодня уже потеряли трое суток.
            all_without_text = run.get("skipped_no_text", 0) >= len(fresh)
            log = logger.info if all_without_text else logger.warning
            log(
                "classifier prepublish: регион=%s кандидатов=%d новых=%d вердиктов=0 (%s)",
                region_code,
                len(items),
                len(fresh),
                (
                    "все без текста — так и задумано"
                    if all_without_text
                    else "движок молчит, волна идёт по обычным фильтрам"
                ),
            )
            # Ответили, но судить было нечего — молчанием это не считаем.
            # Ответили и промолчали (чанки отказали) — считаем: это и есть
            # случай, на который задумана политика деградации.
            return PrepublishResult(
                blocked=known,
                engine_state=ENGINE_CONSULTED if all_without_text else ENGINE_FAILED,
            )

        await service.record_verdicts(
            session, run["verdicts"], source="headless", region_codes_fallback=[region_code]
        )
        await session.commit()

        blocked = {
            str(v.lip) for v in run["verdicts"] if v.normalized_action() in ("delete", "hold")
        }
        logger.info(
            "classifier prepublish: регион=%s кандидатов=%d новых=%d заблокировано=%d токенов=%d",
            region_code,
            len(items),
            len(fresh),
            len(blocked),
            run["tokens"],
        )
        return PrepublishResult(blocked=known | blocked, engine_state=ENGINE_CONSULTED)
    except Exception as e:  # noqa: BLE001 — усилитель, не точка отказа
        logger.error("classifier prepublish failed — fail-open: %s", e, exc_info=True)
        # Сбой гейта — не доказательство молчания движка: к моменту сбоя модель
        # могла и не зваться. Волна идёт по обычным фильтрам.
        return PrepublishResult(blocked=set(), engine_state=ENGINE_NOT_CONSULTED_GATE)


async def _lips_with_verdict(session, lips: Sequence[str], region_code: str) -> Set[str]:
    """lip'ы, по которым вердикт в этом регионе уже есть."""
    if not lips:
        return set()
    from sqlalchemy import select

    from database.models_extended import ContentClassification

    rows = (
        await session.execute(
            select(ContentClassification.lip).where(
                ContentClassification.lip.in_(list(lips)),
                ContentClassification.region_code == region_code,
            )
        )
    ).all()
    return {str(lip) for (lip,) in rows}


__all__ = [
    "ENGINE_CONSULTED",
    "ENGINE_FAILED",
    "ENGINE_NOT_CONSULTED_FLOOR",
    "ENGINE_NOT_CONSULTED_GATE",
    "PrepublishResult",
    "post_lip",
    "prepublish_candidates",
    "prepublish_enabled",
    "to_classifier_items",
]
