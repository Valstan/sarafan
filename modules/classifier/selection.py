"""Отбор в сводку ПО МЕТКАМ нейросети + политика деградации (звено 5, D-024).

**Смена смысла вердикта.** До сих пор вердикт работал вычитанием:
``enforce.fetch_blocked_lips`` убирал ``delete``/``hold`` из кандидатов, а сам
отбор вели алгоритмические фильтры. Метка ``publish`` не значила «взять» — она
значила «не мешать». Здесь наоборот: в сводку идут ТОЛЬКО посты с ``publish``,
а редакционные фильтры отключаются как второй, слепой редактор.

**Зачем меняем.** Измерено 2026-08-12: из 2313 постов, отброшенных алгоритмами,
ИИ-фильтр считает публикуемыми 995 (43%) — фильтр ``advertisement`` резал
«Продам помидоры» и «Продаю свежий мёд», ровно то, что утверждённые правила 16,
22 и 26 велят публиковать. Два редактора с противоположными инструкциями, и
слепой побеждал, потому что стоял первым. Обратная сторона важнее: алгоритмы
пропускают спам, за который блокируют аккаунт владельца (бан до 18.08.2026).

**Политика деградации — решение владельца, а не общий приём.** Если вердиктов
на регион нет (DeepSeek лёг, ключ кончился, сайт провайдера упал):

1. **Первая волна — пропускается.** Ничего не публикуем и ждём.
2. **Вторая волна подряд — публикуем алгоритмами**, но ТОЛЬКО её саму.
   Пропущенная волна не досылается: два поста подряд в ленту — хуже, чем один
   пропуск. Догонять упущенное нельзя.
3. Первая же успешная волна сбрасывает счётчик.

**Молчание нейросети — событие, о котором сообщают.** Не «тихо переживём»:
владелец должен узнать, что фильтр встал, и разобраться (кончились токены, лёг
провайдер, отозван ключ). Поэтому пропуск волны шлёт Telegram-алёрт с cooldown.

Состояние волн живёт в Redis по ключу ``(регион, тема)``: волны идут по
расписанию тем, и «вторая волна подряд» считается внутри своей темы — у
``novost`` и ``afisha`` разные календари, смешивать их счётчики значит
пропускать публикации там, где всё в порядке.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

KEY_PREFIX = "setka:classifier_selection"
# Сколько живёт отметка «волна пропущена». Больше суток держать бессмысленно:
# если фильтр молчал сутки, это уже не «пропустим волну», а инцидент.
_SKIP_TTL_SECONDS = 26 * 3600
# Напоминание об простое — общесистемное, раз в час (заказ владельца 2026-09-28:
# десятки регионов слали каждый своё, телефон пищал непрерывно). Первый
# заметивший регион шлёт, остальные час молчат. Детали по волнам — в логе
# (`classifier selection: фильтр молчит`), а не в телефоне.
ALERT_COOLDOWN_SECONDS = 3600

MODE_VERDICTS = "verdicts"  # отбор по меткам — норма
MODE_SKIP_WAVE = "skip-wave"  # вердиктов нет, первая волна — молчим
MODE_FALLBACK = "algorithmic-fallback"  # вердиктов нет вторую волну — алгоритмы


async def fetch_publish_map(session, region_code: str) -> Dict[str, Optional[str]]:
    """lip → канон-тема для постов, которые нейро-вердикт РАЗРЕШАЕТ в сводку.

    Зеркало ``enforce.fetch_blocked_lips`` с той же семантикой правки оператора
    (``verdict_type='action'``, ``outcome='correct'``) — правка человека главнее
    решения ИИ в обе стороны. Окно свежести то же, что у источника
    классификатора: посты старше него в сводку и так не идут.

    Fail-closed НЕ делаем: ошибка чтения → пустой набор, а вызывающий по пустому
    набору уйдёт в политику деградации (пропуск волны + алёрт). Тихо
    опубликовать всё подряд при сломанной БД — ровно тот сценарий, из-за
    которого аккаунт улетает в бан.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    try:
        from config.classifier import get_source_days
        from database.models_extended import ClassificationCorrection, ContentClassification
        from modules.classifier.service import _theme_canon_map, canonicalize_theme

        cutoff = datetime.utcnow() - timedelta(days=get_source_days())
        rows = (
            await session.execute(
                select(
                    ContentClassification.id,
                    ContentClassification.lip,
                    ContentClassification.verdict,
                ).where(
                    ContentClassification.created_at >= cutoff,
                    ContentClassification.region_code == region_code,
                )
            )
        ).all()
        if not rows:
            return set()

        corr_rows = (
            await session.execute(
                select(
                    ClassificationCorrection.classification_id,
                    ClassificationCorrection.verdict_type,
                    ClassificationCorrection.operator_value,
                ).where(
                    ClassificationCorrection.classification_id.in_([r[0] for r in rows]),
                    ClassificationCorrection.verdict_type.in_(("action", "theme")),
                    ClassificationCorrection.outcome == "correct",
                )
            )
        ).all()
        operator_action = {cid: val for cid, vtype, val in corr_rows if vtype == "action"}
        operator_theme = {cid: val for cid, vtype, val in corr_rows if vtype == "theme"}

        canon = await _theme_canon_map(session)
        allowed: Dict[str, Optional[str]] = {}
        for cid, lip, verdict in rows:
            action = operator_action.get(cid)
            if action is None:
                action = ((verdict or {}).get("action") or "").strip().lower()
            else:
                action = str(action or "").strip().lower()
            if action != "publish":
                continue
            # Тема — тем же правилом, что и действие: правка человека главнее
            # решения ИИ. Раньше здесь читалось только `action`, поэтому тема
            # вердикта на отбор не влияла вовсе — и запрещённая рубрика ехала в
            # ленту, если у поста стоял publish.
            raw_theme = operator_theme.get(cid)
            if raw_theme is None:
                raw_theme = (verdict or {}).get("theme")
            allowed[lip] = canonicalize_theme(raw_theme, canon)
        return allowed
    except Exception as e:  # noqa: BLE001 — наблюдаемость не роняет публикацию
        logger.warning("classifier selection: чтение вердиктов не удалось: %s", e)
        return {}


async def fetch_urgent_lips(session, region_code: str) -> Set[str]:
    """lip'ы, помеченные вердиктом как практически срочные для жителей.

    Отключения света и воды, перекрытия, аварии на сетях, официальные
    предупреждения. Решение владельца 2026-09-22: такой пост идёт в ленту
    вперёд рейтинга, потому что свежему сообщению об аварии просто неоткуда
    взять просмотры, а нужно оно людям именно сейчас.

    Окно то же, что у ``fetch_publish_map`` — окно свежести классификатора.
    Правки оператора здесь НЕ читаются: тип коррекции для срочности ещё не
    заведён (``verdict_type`` знает только action/theme/merge), и молча
    подставлять вместо него чужое поле значило бы врать о происхождении
    решения. Хвост записан в PENDING.

    Fail-open: любая ошибка → пустое множество, то есть порядок публикации
    прежний, чисто по рейтингу. Срочность — ускоритель, и ломаться она обязана
    в сторону обычного поведения, а не пропуска волны.
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    try:
        from config.classifier import get_source_days
        from database.models_extended import ContentClassification

        cutoff = datetime.utcnow() - timedelta(days=get_source_days())
        rows = (
            await session.execute(
                select(ContentClassification.lip, ContentClassification.verdict).where(
                    ContentClassification.created_at >= cutoff,
                    ContentClassification.region_code == region_code,
                )
            )
        ).all()
        return {lip for lip, verdict in rows if bool((verdict or {}).get("urgent"))}
    except Exception as e:  # noqa: BLE001 — срочность не имеет права ронять волну
        logger.warning("classifier selection: чтение срочности не удалось: %s", e)
        return set()


async def fetch_publish_lips(session, region_code: str) -> Set[str]:
    """Только lip'ы, разрешённые вердиктом. Тонкая обёртка над ``fetch_publish_map``.

    Осталась отдельной функцией, потому что у неё есть свой потребитель
    (``modules.classifier.rating.top_by_rating``), которому тема не нужна, и
    десяток тестов, подменяющих её монкейпатчем.
    """
    return set(await fetch_publish_map(session, region_code))


def _skip_key(region_code: str, theme: str) -> str:
    return f"{KEY_PREFIX}:skipped:{region_code}:{theme}"


def _alert_key() -> str:
    # Ключ общесистемный, без региона: фильтр встаёт сразу для всей сети, и
    # отдельное письмо от каждого региона — гарантированный способ научить
    # владельца не читать эти алёрты (потоп 22–28.09 при простое DeepSeek).
    return f"{KEY_PREFIX}:alert_cooldown"


def decide_mode(
    *,
    publish_lips: Set[str],
    region_code: str,
    theme: str,
    redis_client: Any = None,
) -> Tuple[str, bool]:
    """Выбрать режим волны. Возвращает ``(режим, слать_ли_алёрт)``.

    Вердикты есть → ``verdicts`` и сброс отметки пропуска.
    Вердиктов нет, отметки нет → ``skip-wave``, ставим отметку, алёрт.
    Вердиктов нет, отметка есть → ``algorithmic-fallback``, отметку снимаем
    (следующий отказ снова начнёт с пропуска волны — политика повторяется, а не
    залипает в режиме алгоритмов навсегда).

    Без Redis отметку хранить негде: тогда единственное безопасное поведение —
    пропустить волну и сообщить. Молча уйти в алгоритмы значит выпустить в ленту
    то, за что банят аккаунт.
    """
    if publish_lips:
        if redis_client is not None:
            try:
                redis_client.delete(_skip_key(region_code, theme))
            except Exception:  # noqa: BLE001
                pass
        return MODE_VERDICTS, False

    if redis_client is None:
        logger.warning(
            "classifier selection: вердиктов нет и Redis недоступен (%s/%s) — пропускаем волну",
            region_code,
            theme,
        )
        return MODE_SKIP_WAVE, True

    try:
        already_skipped = bool(redis_client.get(_skip_key(region_code, theme)))
    except Exception:  # noqa: BLE001
        already_skipped = False

    if already_skipped:
        try:
            redis_client.delete(_skip_key(region_code, theme))
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "classifier selection: вердиктов нет вторую волну (%s/%s) — публикуем алгоритмами, "
            "пропущенную волну НЕ досылаем",
            region_code,
            theme,
        )
        return MODE_FALLBACK, True

    try:
        redis_client.setex(_skip_key(region_code, theme), _SKIP_TTL_SECONDS, 1)
    except Exception:  # noqa: BLE001
        pass
    return MODE_SKIP_WAVE, True


def format_alert(*, mode: str, region_code: str, theme: str) -> str:
    """Текст Telegram-алёрта о молчании ИИ-фильтра.

    Текст общесистемный: фильтр встаёт сразу для всей сети, поэтому называть
    регион и тему как суть проблемы — врать (проблема не в этом регионе).
    Регион-триггер остаётся в сигнатуре и в логе вызывающего, в телефон едет
    только состояние контура.
    """
    if mode == MODE_SKIP_WAVE:
        head = "⚠️ <b>ИИ-фильтр молчит — волны пропускаются</b>"
        tail = (
            "Волны <b>не публикуются</b>. Если на следующих волнах вердиктов снова не "
            "будет, сводки выйдут на алгоритмических фильтрах — а они пропускают спам."
        )
    else:
        head = "🚨 <b>ИИ-фильтр молчит — публикуем алгоритмами</b>"
        tail = (
            "Сводки выходят на алгоритмических фильтрах. Пропущенные волны НЕ "
            "досылаются. Алгоритмы пропускают спам — разберись с фильтром."
        )
    return "\n".join(
        [
            head,
            "",
            "Что проверить: кончились ли токены DeepSeek, жив ли их API, "
            "не отозван ли ключ в комнате КАРМАНа.",
            "",
            "Детали по волнам — в логе: "
            "<code>grep 'classifier selection: фильтр молчит'</code>.",
            "",
            tail,
        ]
    )


def maybe_alert(
    *,
    mode: str,
    region_code: str,
    theme: str,
    telegram_token: Optional[str],
    chat_id: Optional[str],
    redis_client: Any = None,
) -> str:
    """Послать алёрт о молчании фильтра (общесистемный cooldown).

    Cooldown ОДИН на всю сеть, а не по региону и не по паре регион+тема: если
    фильтр встал, он встал для всех тем и регионов сразу, и отдельное письмо от
    каждого — гарантированный способ научить владельца не читать эти алёрты
    (заказ 2026-09-28: напоминание раз в час, детали — в логе).
    """
    if not telegram_token or not chat_id:
        return "skipped:no-telegram-config"
    if redis_client is not None:
        try:
            if redis_client.get(_alert_key()):
                return "skipped:cooldown"
        except Exception:  # noqa: BLE001
            pass
    try:
        from modules import telegram_http as tg_http

        resp = tg_http.post(
            f"https://api.telegram.org/bot{telegram_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": format_alert(mode=mode, region_code=region_code, theme=theme),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code != 200:
            return f"error:http-{resp.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"error:{type(e).__name__}"

    if redis_client is not None:
        try:
            redis_client.setex(_alert_key(), ALERT_COOLDOWN_SECONDS, 1)
        except Exception:  # noqa: BLE001
            pass
    return "alert-sent"


MODE_DISABLED = "disabled"  # гейт выключен — волна идёт как раньше


def selection_enabled() -> bool:
    """Отбирать ли в сводку по меткам (env, дефолт — выкл; зеркало prepublish)."""
    import os

    return os.getenv("CLASSIFIER_SELECTION_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def _fetch_theme_shares(session) -> Dict[str, Optional[float]]:
    """Доли тем из словаря: ``{тема: процент}``. Служебные темы пропускаем.

    «Мусор» не публикуется вовсе, «соседи» идут отдельным каналом со своим
    расписанием — потолок для них был бы ручкой, ни к чему не подключённой.
    """
    from sqlalchemy import select

    from database.models_extended import ClassifierTheme

    rows = (
        await session.execute(
            select(
                ClassifierTheme.name,
                ClassifierTheme.share_percent,
                ClassifierTheme.is_service,
            )
        )
    ).all()
    return {
        str(name): (None if share is None else float(share))
        for name, share, is_service in rows
        if not is_service
    }


# Состояние квоты для поля «квота=» строки отбора. До 2026-09-11 там печаталось
# «не применялась» в трёх разных случаях: гейт выключен; квота отработала и
# ничего не срезала; квота срезала пост, а правило непустой волны его вернуло
# (Малмыж, 11.09 12:21). По такой строке нельзя было отличить выключенную квоту
# от работающей — ровно то, от чего эта строка должна была защищать.
QUOTA_EMPTY = "empty"  # резать нечего: волна пуста ещё до квоты
QUOTA_NO_SESSION = "no_session"  # без сессии БД (юнит-тесты политики деградации)
QUOTA_OFF = "off"  # гейт выключен, и запретов (доля 0) нет
QUOTA_NO_SHARES = "no_shares"  # гейт включён, но ни одной доли не задано
QUOTA_BANS_ONLY = "bans_only"  # гейт выключен, действуют только запреты
QUOTA_APPLIED = "applied"  # потолки отработали — срезали или нет
QUOTA_RESCUED = "rescued"  # квота вычистила волну, правило непустой волны вернуло лучшее
QUOTA_ERROR = "error"  # отказ, fail-open

_QUOTA_LABELS = {
    QUOTA_EMPTY: "резать нечего",
    QUOTA_NO_SESSION: "без сессии БД",
    QUOTA_OFF: "выключена",
    QUOTA_NO_SHARES: "долей не задано",
    QUOTA_ERROR: "сбой, fail-open",
}


def quota_log_label(state: str, dropped: Dict[str, int], rescued: int = 0) -> str:
    """Текст поля «квота=» — по состоянию квоты, а не по пустоте словаря убранного.

    Словарь убранного считается ПОСЛЕ правила непустой волны (он обязан сходиться
    со сводкой), поэтому сам по себе не говорит, работала ли квота: волна из одного
    срезанного и возвращённого поста даёт пустой словарь.
    """
    if state == QUOTA_RESCUED:
        tail = f", убрано {dropped}" if dropped else ""
        return f"волна опустела, возвращено {rescued}{tail}"
    if state in (QUOTA_APPLIED, QUOTA_BANS_ONLY):
        prefix = "только запреты: " if state == QUOTA_BANS_ONLY else ""
        return prefix + (str(dropped) if dropped else "ничего не срезала")
    return _QUOTA_LABELS.get(state, state)


async def _apply_quota(
    session,
    selected,
    *,
    region_code: str,
    theme_of,
) -> Tuple[list, Dict[str, int], str, int]:
    """Применить потолки долей тем. Fail-open: любой отказ → вход без изменений.

    Возвращает ``(оставленные, убрано_по_темам, состояние, возвращено_спасением)``,
    где состояние — одна из констант ``QUOTA_*`` выше; нужно для строки лога.

    Запрет темы (доля 0) работает ВСЕГДА, а потолки — только под гейтом
    ``CLASSIFIER_THEME_QUOTA_ENABLED``. Разделение нарочное: гейт нужен, чтобы
    журнал публикаций сутки поработал вхолостую и знаменатель перестал быть
    пустым, но запрет владельца ждать сутки не должен.
    """
    if not selected:
        return selected, {}, QUOTA_EMPTY, 0
    # Без сессии читать нечего: так зовут отбор юнит-тесты политики деградации,
    # и ERROR-лог в этом случае был бы ложной тревогой ровно того класса, который
    # приучает не читать логи.
    if session is None:
        return selected, {}, QUOTA_NO_SESSION, 0
    try:
        from config.classifier import (
            get_theme_quota_min_posts,
            get_theme_quota_window_hours,
            theme_quota_enabled,
        )
        from modules.classifier.quota import HEADLINER_SLOTS, apply_theme_quota
        from modules.publication_journal import fetch_published_counts

        gate = theme_quota_enabled()
        shares = await _fetch_theme_shares(session)
        if not gate:
            shares = {t: v for t, v in shares.items() if v is not None and float(v) <= 0}
        shares = {t: v for t, v in shares.items() if v is not None}
        if not shares:
            return selected, {}, (QUOTA_NO_SHARES if gate else QUOTA_OFF), 0

        published: Dict[str, int] = {}
        slots = HEADLINER_SLOTS
        if gate:
            published = await fetch_published_counts(
                session, region_code, window_hours=get_theme_quota_window_hours()
            )
            # Мест в ленте, которые добавляет эта волна: сводка плюс хедлайнер.
            # Берём сетевой дефолт, а не эффективное значение региона: конфиг
            # региона сюда не приезжает, а расхождение стоит долей процента в
            # знаменателе — меньше, чем цена лишнего параметра в сигнатуре,
            # которую пришлось бы протащить через обе точки вызова.
            from modules.bulletin_pipeline_settings import DEFAULT_PIPELINE

            slots = int(DEFAULT_PIPELINE.get("max_posts_per_bulletin", 3)) + HEADLINER_SLOTS

        from config.classifier import get_rating_views_alpha
        from utils.post_utils import post_rating_of

        alpha = get_rating_views_alpha()
        rescued: list = []
        kept, dropped = apply_theme_quota(
            selected,
            theme_of=theme_of,
            rating_of=lambda p: post_rating_of(p, alpha=alpha),
            shares=shares,
            published=published,
            slots=slots,
            min_posts=get_theme_quota_min_posts(),
            on_rescue=rescued.append,
        )
        if rescued:
            return kept, dropped, QUOTA_RESCUED, rescued[0]
        return kept, dropped, (QUOTA_APPLIED if gate else QUOTA_BANS_ONLY), 0
    except Exception as e:  # noqa: BLE001 — квота не важнее волны
        logger.error("theme quota failed — fail-open: %s", e, exc_info=True)
        return selected, {}, QUOTA_ERROR, 0


async def apply_wave_selection(
    session,
    posts,
    *,
    region_code: str,
    theme: str,
) -> Tuple[list, str, int]:
    """Единственная точка входа отбора для волны публикации (звено 5, шаг 2).

    Возвращает ``(посты, режим, сколько_убрано)``. Порядок в волне жёсткий:
    вызывается ПОСЛЕ prepublish — тот доклассифицирует свежесобранное и
    записывает вердикты, которые здесь читает ``fetch_publish_lips``. Обратный
    порядок оставил бы свежие посты без вердиктов, и отбор «только publish»
    выбрасывал бы их все.

    Redis и Telegram-конфиг добываются здесь, а не пробрасываются через волну:
    отказ любого из них деградирует политику (см. ``decide_mode``/``maybe_alert``),
    но не меняет сигнатуру вызова. Любой НЕполитический отказ — fail-open с
    ERROR: усилитель, не точка отказа (тот же контракт, что у prepublish).
    """
    if not selection_enabled() or not posts:
        return list(posts), MODE_DISABLED, 0

    try:
        from modules.classifier.prepublish import post_lip

        publish_map = await fetch_publish_map(session, region_code)
        publish_lips = set(publish_map)

        redis_client = None
        try:
            from modules.notifications.storage import NotificationsStorage

            redis_client = NotificationsStorage().redis_client
        except Exception:  # noqa: BLE001 — без Redis политика деградирует сама
            logger.debug("classifier selection: redis unavailable", exc_info=True)

        mode, need_alert = decide_mode(
            publish_lips=publish_lips,
            region_code=region_code,
            theme=theme,
            redis_client=redis_client,
        )

        if need_alert:
            try:
                from config.runtime import TELEGRAM_ALERT_CHAT_ID, TELEGRAM_TOKENS

                # Та же эвристика, что _pick_telegram_bot_token в celery_app;
                # импортировать её оттуда нельзя — модуль не должен тянуть
                # celery-приложение с его import-side-effects.
                tokens = dict(TELEGRAM_TOKENS)
                bot_token = next(
                    (tokens[k] for k in ("VALSTANBOT", "ALERT", "AFONYA") if tokens.get(k)),
                    next(iter(tokens.values()), None),
                )
                status = maybe_alert(
                    mode=mode,
                    region_code=region_code,
                    theme=theme,
                    telegram_token=bot_token,
                    chat_id=TELEGRAM_ALERT_CHAT_ID,
                    redis_client=redis_client,
                )
                logger.warning(
                    "classifier selection: фильтр молчит (%s/%s), режим=%s, алёрт=%s",
                    region_code,
                    theme,
                    mode,
                    status,
                )
            except Exception:  # noqa: BLE001 — алёрт не важнее волны
                logger.error("classifier selection: алёрт не отправлен", exc_info=True)

        if mode == MODE_SKIP_WAVE:
            return [], mode, len(posts)
        if mode == MODE_FALLBACK:
            return list(posts), mode, 0

        selected = [p for p in posts if post_lip(p) in publish_lips]
        removed = len(posts) - len(selected)

        # Квота тем (заказ владельца 2026-08-30). Врезана здесь, а не выше или
        # ниже по волне, по трём причинам: сигнатура apply_wave_selection не
        # меняется, поэтому обе точки вызова — районная волна и каскад — получают
        # квоту без правок; только здесь на руках одновременно посты и их темы
        # (ниже, в BulletinBuilder, темы уже нет и сессии БД тоже); и это уже
        # слой редакционных решений, а не алгоритмических фильтров.
        selected, quota_dropped, quota_state, quota_rescued = await _apply_quota(
            session,
            selected,
            region_code=region_code,
            theme_of=lambda p: publish_map.get(post_lip(p)),
        )
        removed += sum(quota_dropped.values())
        # Сколько из убранных движок не судил вовсе. Пост без текста headless
        # пропускает намеренно (`has_text`, headless.py:85), вердикта у него не
        # появляется — а отбор берёт ТОЛЬКО publish, и «нет вердикта» молча
        # значит «не публиковать». В вычитающем мире такой пост публиковался.
        # Без этого числа `отобрано=0` неразличимо: то ли нейросеть отбраковала
        # всё, то ли ей нечего было судить. Замер 2026-08-21: 56% пустых волн —
        # второе. Берём ИМЕННО `has_text` движка, а не свою копию условия:
        # разошедшиеся определения — самый незаметный способ мерить не то.
        from modules.classifier.headless import has_text

        no_text = sum(1 for p in posts if post_lip(p) not in publish_lips and not has_text(p))
        logger.info(
            "classifier selection: регион=%s тема=%s кандидатов=%d отобрано=%d"
            " убрано=%d безтекста=%d квота=%s",
            region_code,
            theme,
            len(posts),
            len(selected),
            removed,
            no_text,
            # Пишем в ту же строку, что и режим отбора, и именно СОСТОЯНИЕ квоты:
            # по одному словарю убранного не отличить «выключена» от «ничего не
            # срезала» и от «срезала, но правило непустой волны вернуло пост».
            quota_log_label(quota_state, quota_dropped, quota_rescued),
        )
        return selected, mode, removed
    except Exception as e:  # noqa: BLE001 — усилитель, не точка отказа
        logger.error("classifier selection failed — fail-open: %s", e, exc_info=True)
        return list(posts), "error", 0
