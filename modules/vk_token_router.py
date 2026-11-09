"""VK Token Router / Policy.

Центральный решатель: «какой VK-токен использовать для этой операции и в каком
порядке пробовать кандидатов, если первый упал».

Контекст и история. До 2026-05-27 здесь была простая функция ``pick_token`` —
«если есть community-токен для группы, используем его, иначе fallback на
переданный user-token». Реализованы:

* Семантика операций (:class:`TokenOp`) — READ / COMMUNITY_WRITE / USER_WRITE.
* Env-конфиг ролей (``VK_PUBLISH_TOKEN_NAMES``, ``VK_NEVER_PUBLISH_TOKEN_NAMES``
  в ``config.runtime``).
* Динамическое состояние токена в БД (``vk_tokens.disabled_until``,
  ``last_error_code``, ``consecutive_errors``) — миграция 014.
* Автоматический cooldown по VK error codes 5 (invalid_token) / 17
  (validation_required) / 29 (rate_limit_per_token) — :meth:`TokenPolicy.report_error`.
* Telegram-alert при auto-disable — через :mod:`modules.notifications.telegram_notifier`.

Жизненный цикл вызова:

>>> async with AsyncSessionLocal() as s:
...     policy = TokenPolicy(s)
...     for cand in await policy.pick(TokenOp.READ):
...         try:
...             result = vk_call(cand.token, ...)
...             await policy.report_success(cand.name)
...             break
...         except ApiError as e:
...             await policy.report_error(cand.name, e.code)
...             if e.code in (5, 17, 29):
...                 continue
...             raise

Старая функция ``pick_token`` сохранена (она используется legacy-кодом
``BaseVKChecker._api_for`` и ``VKPublisher._client_for_group`` через
``community_tokens={cid: token}``). Когда весь код переедет на ``TokenPolicy``
— её можно будет удалить.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import VKToken

logger = logging.getLogger(__name__)


class TokenOp(str, enum.Enum):
    """Семантические категории операций VK API.

    Влияет на то, какой набор кандидатов вернёт :meth:`TokenPolicy.pick`.

    READ — любой read-only вызов: ``wall.get``, ``groups.search``,
    ``groups.getById``, ``database.getCities``, ``users.get``. Подходят все
    active user-токены (включая Vita).

    COMMUNITY_WRITE — публикации/действия от имени сообщества: ``wall.post``,
    ``photos.getWallUploadServer``/``photos.save``, ``wall.createComment``,
    ``messages.send``, ``likes.add``. Сначала пытаемся community-токен
    целевой группы, потом — user-tokens из ``VK_PUBLISH_TOKEN_NAMES`` минус
    ``VK_NEVER_PUBLISH_TOKEN_NAMES``.

    USER_WRITE — операции, для которых VK API в принципе не принимает
    community-токен: ``wall.repost`` (copy_setka хаб). Только user-tokens из
    whitelist, исключая deny-list. Если все недоступны — операция fail.

    COMMUNITY_DM — личное сообщение **от имени сообщества** (``messages.send``
    с ``group_id``): личность отправителя — сообщество, поэтому каскад
    community-токен → user-токены допустим (user-токен админа шлёт как
    группа). Отличие от COMMUNITY_WRITE — фильтр по способности ``messages``
    из снапшота (Этап 3, 2026-09-05).

    USER_DM — сообщение **от конкретного аккаунта** (``account``): ровно этот
    user-токен или ничего. Никакого каскада и никакого резерва: подмена
    личности отправителя недопустима.
    """

    READ = "read"
    COMMUNITY_WRITE = "community_write"
    USER_WRITE = "user_write"
    COMMUNITY_DM = "community_dm"
    USER_DM = "user_dm"


# Какая проба снапшота способностей (token_capabilities) отвечает за операцию.
# Только ЛС: у READ проба «wall.get(чужое)» бьёт в одну конкретную донорскую
# стену, и err15 там значит «стена закрыта», а не «нет права» (критик Этапа 3) —
# такой фильтр мог бы на месяц оставить парсер без токенов.
#
# Семантика по источнику кандидата:
# - community-токен: deny-list — исключаем ТОЛЬКО по явному отказу (err7/err15),
#   неизвестное (в снапшоте всего 2 COMM из ~19) оставляем;
# - user-токен в DM-операции: allow-list — нужен явный «ok» (у user-токенов
#   scope ``messages`` нет, замер 2026-09-05; без снапшота хвост не подмешиваем).
CAPABILITY_PROBE: Dict[str, str] = {
    "community_dm": "messages.getConversations",
    "user_dm": "messages.getConversations",
}
_CAPABILITY_DENY = frozenset({"err7", "err15"})
_CAPABILITIES_TTL = 600.0  # секунд: снапшот меняется раз в месяц, Redis дёргаем раз в 10 мин
# ``at=None`` — ещё не читали. Не 0.0: ``time.monotonic()`` на свежей машине
# (CI) меньше TTL, и нулевая метка выглядела бы как «свежий кеш» (падение CI #646).
_capabilities_cache: Dict[str, object] = {"at": None, "matrix": None}


def _capability_filter(
    candidates: List["TokenCandidate"],
    op: "TokenOp",
    matrix: Optional[Dict[str, Dict[str, str]]],
    *,
    user_allow_list: bool = True,
) -> List["TokenCandidate"]:
    """Фильтр по способностям: community — deny-list, user в DM — allow-list.

    ``user_allow_list=False`` — для USER_DM: аккаунт назван явно, и без снапшота
    его не отсекаем (deny-list, как у community); в COMMUNITY_DM user-хвост
    подмешивается автоматически и потому требует явного «ok».
    """
    probe = CAPABILITY_PROBE.get(getattr(op, "value", str(op)))
    if not probe:
        return candidates
    out: List[TokenCandidate] = []
    for c in candidates:
        row = (matrix or {}).get(c.name) or (matrix or {}).get(c.name.upper()) or {}
        value = row.get(probe)
        if c.source == "user" and user_allow_list:
            if value != "ok":
                logger.info("token router: %s не подмешан для %s — %s=%s", c.name, op, probe, value)
                continue
        elif value in _CAPABILITY_DENY:
            logger.info("token router: %s пропущен для %s — %s=%s", c.name, op, probe, value)
            continue
        out.append(c)
    return out


def _capabilities_matrix_safe() -> Optional[Dict[str, Dict[str, str]]]:
    """Матрица снапшота или ``None`` (нет Redis/замера/выключено env). Синхронно."""
    from config.runtime import _getenv

    if (_getenv("VK_CAPABILITY_FILTER", "1") or "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        from modules.vk_monitor.token_capabilities import load_snapshot

        snap = load_snapshot()
        matrix = (snap or {}).get("matrix")
        return matrix if isinstance(matrix, dict) else None
    except Exception:  # pragma: no cover - снапшот не имеет права ломать выбор
        return None


async def _capabilities_matrix_cached() -> Optional[Dict[str, Dict[str, str]]]:
    """Снапшот с кешем на процесс (TTL) и чтением Redis в потоке — не в event loop."""
    import asyncio
    import time as _time

    now = _time.monotonic()
    at = _capabilities_cache["at"]
    if at is not None and now - float(at) < _CAPABILITIES_TTL:  # type: ignore[arg-type]
        return _capabilities_cache["matrix"]  # type: ignore[return-value]
    matrix = await asyncio.to_thread(_capabilities_matrix_safe)
    _capabilities_cache["at"] = now
    _capabilities_cache["matrix"] = matrix
    return matrix


@dataclass(frozen=True)
class TokenCandidate:
    """Один кандидат для выполнения операции.

    Возвращается :meth:`TokenPolicy.pick` в упорядоченном виде. Caller
    пробует кандидатов в этом порядке, пока один не выполнит операцию или
    список не кончится.

    Attributes:
        name: имя токена (``VALSTAN``, ``VITA``, или ``COMM_<id>`` для
            community). Совпадает с ``vk_tokens.name`` в БД, либо
            ``ENV:<name>`` для токенов из env без записи в БД.
        token: сам access_token.
        source: ``'community'`` (привязан к group_id) или ``'user'``.
        community_id: только для ``source='community'`` — abs(group_id).
    """

    name: str
    token: str
    source: str  # 'community' | 'user'
    community_id: Optional[int] = None


def _register_name_safe(token: str, name: str) -> None:
    """Объявить учёту расхода, какому имени соответствует строка токена.

    ``VKClient`` знает только строку токена, поэтому имя для счётчиков
    (:mod:`modules.vk_monitor.token_usage`) объявляется здесь — там, где имя и
    значение видны одновременно. Best-effort: если модуль учёта недоступен,
    маршрутизация продолжает работать как раньше.
    """
    try:
        from modules.vk_monitor.token_usage import register_token_name

        register_token_name(token, name)
    except Exception:  # pragma: no cover — учёт не имеет права ломать выбор
        logger.debug("register_token_name failed for %s", name)


def _candidate(
    name: str,
    token: str,
    source: str,
    community_id: Optional[int] = None,
) -> TokenCandidate:
    """Собрать кандидата и заодно объявить его имя учёту расхода."""
    _register_name_safe(token, name)
    return TokenCandidate(name=name, token=token, source=source, community_id=community_id)


def _calls_today_safe() -> Dict[str, int]:
    """``{имя: запросов сегодня}``; пустой словарь, если учёт недоступен."""
    try:
        from modules.vk_monitor.token_usage import get_calls_today

        return get_calls_today()
    except Exception:  # pragma: no cover — балансировка деградирует до last_used
        logger.debug("get_calls_today failed — balancing falls back to last_used")
        return {}


# VK error codes, по которым TokenPolicy автоматически кладёт токен в cooldown.
# Каждому соответствует длительность блокировки (часы).
#
#   5  — invalid_token / user_authorisation_failed. Чаще всего значит, что
#        access_token аннулирован VK'ом (бан аккаунта, смена пароля, IP-pin).
#        Длительный cooldown — 24ч, чтобы не долбить заведомо мёртвый токен.
#   17 — validation_required. VK требует капчу/код от пользователя. Без
#        участия человека не решается, поэтому тоже 24ч.
#   29 — rate_limit_per_token. Токен превысил суточный лимит запросов.
#        Час cooldown'а — стандартный VK-таймаут для этой ошибки.
_AUTO_DISABLE_CODES_HOURS: Dict[int, float] = {
    5: 24.0,
    17: 24.0,
    29: 1.0,
}


async def load_community_tokens(session: AsyncSession) -> Dict[int, str]:
    """Вернуть legacy-карту ``{abs(group_id): первый active token}``.

    Полный пул загружает :meth:`TokenPolicy._load_communities`. Эта функция
    сохранена для старых читателей, которым нужен ровно один токен на группу;
    выбор детерминированный: legacy ``COMM_<id>`` идёт раньше именованных
    резервов ``COMM_<id>_<ACCOUNT>``.
    """
    q = await session.execute(
        select(VKToken).where(
            VKToken.community_id.isnot(None),
            VKToken.is_active.is_(True),
        )
    )
    rows = sorted(q.scalars(), key=lambda t: (int(t.community_id), t.name.count("_"), t.name))
    out: Dict[int, str] = {}
    for t in rows:
        out.setdefault(int(t.community_id), t.token)
        # Публикация ходит этой legacy-картой (VKPublisher._client_for_group),
        # минуя pick() — без регистрации весь расход wall.post уезжал бы в
        # «UNKNOWN:<отпечаток>» вместо имени сообщества (замер 2026-07-25).
        _register_name_safe(t.token, t.name.upper())
    return out


async def load_vk_routing() -> "tuple[Optional[str], Dict[int, str]]":
    """Вернуть ``(user_token, {abs(group_id): community_token})`` для community-write.

    Единая точка маршрутизации токенов для notifications + ad_cabinet (антидрейф —
    см. предупреждение в шапке ``web/api/notifications._load_vk_routing``).
    Открывает собственную сессию. ``user_token`` — первый живой user-кандидат из
    ``TokenPolicy.pick(COMMUNITY_WRITE)`` (whitelist минус deny-list); ``None``
    если живых нет.
    """
    from database.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        policy = TokenPolicy(session)
        candidates = await policy.pick(TokenOp.COMMUNITY_WRITE)
        user_token = next((c.token for c in candidates if c.source == "user"), None)
        if not user_token:
            return None, {}
        community_tokens = await load_community_tokens(session)
    return user_token, community_tokens


async def load_community_routing() -> Dict[int, str]:
    """``{abs(group_id): community_token}`` **независимо** от живости user-токенов.

    ``load_vk_routing`` возвращает ``(None, {})``, когда нет годного user-токена
    публикации, и этим молча выключал бот САРАФАНа, доставку Радара и
    автоприветствие — им user-токен не нужен вовсе (Этап 3, 2026-09-05).
    """
    from database.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        return await load_community_tokens(session)


async def load_photo_upload_token(session: Optional[AsyncSession] = None) -> Optional[str]:
    """Токен аккаунта-владельца для заливки фото на стену сообщества.

    Заливает **владелец группы** — тот, от кого выпущены наши community-ключи.
    Разкладку сняла живая проба 09.11 (P214, ``docs/ops/photo-upload-path.md``):

    * community-токен залить фото в стену не может вообще — ``[27] method is
      unavailable with group auth``;
    * фото, залитое администратором, community-токен в записи **не принимает**:
      пост уходит без вложения, молча;
    * фото аккаунта-владельца принимается и становится собственностью группы.

    Значит, «user-токен админа» и «user-токен владельца» — не одно и то же:
    публиковать может МАМА, а заливать фото — только владелец.
    """
    from config.runtime import get_photo_upload_token_names

    if session is None:
        from database.connection import AsyncSessionLocal

        async with AsyncSessionLocal() as own_session:
            return await load_photo_upload_token(own_session)

    policy = TokenPolicy(session)
    for name in get_photo_upload_token_names():
        candidate = await policy.token_for_account(name)
        if candidate is not None:
            return candidate.token
    return None


def pick_token(
    community_tokens: Dict[int, str],
    group_id: int,
    user_token_fallback: str,
) -> tuple[str, bool]:
    """LEGACY: используется ``BaseVKChecker._api_for`` и старыми кейсами.

    Новый код должен брать :class:`TokenPolicy`. Сохранено для совместимости —
    логика «есть community → берём его, иначе fallback».
    """
    cid = abs(int(group_id))
    tok = community_tokens.get(cid)
    if tok:
        return tok, True
    return user_token_fallback, False


class TokenPolicy:
    """Stateful policy: выбор токенов + учёт ошибок.

    Создаётся per-Celery-task / per-request (session-scoped). Внутри —
    минимальный кеш активных токенов: один SELECT в начале pick(), повторное
    pick() в той же сессии переиспользует кеш.

    Telegram-alert при auto-disable — мягкий: если ``telegram_notifier``
    недоступен, исключение глотается и логируется (alert — best-effort).
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._active_cache: Optional[Dict[str, VKToken]] = None
        self._community_cache: Optional[Dict[int, List[VKToken]]] = None

    # ------------------------------------------------------------------
    # Запросы состояния
    # ------------------------------------------------------------------

    async def _load_active(self) -> Dict[str, VKToken]:
        """Кешированный список active user-токенов (community_id IS NULL).

        «Active» = ``is_active=TRUE`` И (``disabled_until IS NULL`` или
        ``disabled_until < NOW()``). Имя возвращается в верхнем регистре —
        совпадает с тем, как имена хранятся в env (``VK_TOKEN_<NAME>``).
        """
        if self._active_cache is not None:
            return self._active_cache
        now = datetime.utcnow()
        q = await self._session.execute(
            select(VKToken).where(
                VKToken.community_id.is_(None),
                VKToken.is_active.is_(True),
            )
        )
        out: Dict[str, VKToken] = {}
        for t in q.scalars():
            if t.disabled_until is not None and t.disabled_until > now:
                continue
            out[t.name.upper()] = t
        self._active_cache = out
        return out

    async def _load_communities(self) -> Dict[int, List[VKToken]]:
        """Active community-токены, сгруппированные в каскад по сообществу.

        ``COMM_<id>`` — существующий основной токен (исторически VALSTAN),
        именованные резервы вроде ``COMM_<id>_MAMA`` идут следом. Остальные
        имена сортируются детерминированно.
        """
        if self._community_cache is not None:
            return self._community_cache
        now = datetime.utcnow()
        q = await self._session.execute(
            select(VKToken).where(
                VKToken.community_id.isnot(None),
                VKToken.is_active.is_(True),
            )
        )
        out: Dict[int, List[VKToken]] = {}
        for t in q.scalars():
            if t.disabled_until is not None and t.disabled_until > now:
                continue
            out.setdefault(int(t.community_id), []).append(t)
        for cid, rows in out.items():
            legacy_name = f"COMM_{cid}"
            rows.sort(
                key=lambda t: (
                    0 if t.name.upper() == legacy_name else 1,
                    0 if t.name.upper().endswith("_MAMA") else 1,
                    t.name.upper(),
                )
            )
        self._community_cache = out
        return out

    def _invalidate_cache(self) -> None:
        self._active_cache = None
        self._community_cache = None

    async def _user_tokens(self) -> Dict[str, str]:
        """Все доступные **user**-токены: БД (единый источник) + env как дополнение.

        Единый источник токенов — БД (``/tokens`` UI, решение владельца
        2026-07-12): значения берём из активных БД-строк. env ``VK_TOKENS``
        остаётся bootstrap/аварийным дополнением — добавляет только имена,
        которых в БД нет вовсе (или у которых в БД пустой token). Имя,
        выключенное в БД, env НЕ воскрешает.
        """
        from config.runtime import VK_TOKENS

        active_db = await self._load_active()
        tokens: Dict[str, str] = {name: row.token for name, row in active_db.items() if row.token}
        for name, tok in (VK_TOKENS or {}).items():
            upper = name.upper()
            if not tok or upper in tokens:
                continue
            if await self._token_exists_but_disabled(upper):
                continue
            tokens[upper] = tok
        return tokens

    async def token_for_account(self, name: str) -> Optional["TokenCandidate"]:
        """Токен **конкретного** user-аккаунта по имени — без каскада и без фильтров.

        Каскад :meth:`pick` отвечает на вопрос «кто публикует», а здесь вопрос
        другой: «кто именно должен выполнить операцию». Разные аккаунты
        неравнозначны: заливку фото в стену сообщества делает аккаунт-владелец
        группы (P214, замер 09.11) — администратор ту же загрузку делает, но
        community-токен такое фото в записи потом не принимает.

        Hard deny-list (``VK_NEVER_PUBLISH_TOKEN_NAMES``) действует и здесь:
        аккаунту, которому запрещено публиковать, нельзя и заливать фото.
        """
        from config.runtime import get_never_publish_token_names

        upper = (name or "").upper()
        if not upper:
            return None
        if upper in get_never_publish_token_names():
            return None
        tokens = await self._user_tokens()
        tok = tokens.get(upper)
        if not tok:
            return None
        return _candidate(upper, tok, "user")

    # ------------------------------------------------------------------
    # Главный метод: pick
    # ------------------------------------------------------------------

    async def pick(
        self,
        op: TokenOp,
        group_id: Optional[int] = None,
        account: Optional[str] = None,
    ) -> List[TokenCandidate]:
        """Упорядоченный список кандидатов для операции.

        Args:
            op: семантика операции — см. :class:`TokenOp`.
            group_id: для ``COMMUNITY_WRITE``/``COMMUNITY_DM`` — целевая группа
                (любой знак, abs берётся внутри). Игнорируется для READ / USER_WRITE.
            account: для ``USER_DM`` — имя user-токена (``VALSTAN``, ``MAMA``),
                от имени которого идёт сообщение. Без него USER_DM пуст.

        Returns:
            Список :class:`TokenCandidate` в порядке предпочтения. Пустой
            список — нет ни одного подходящего токена; caller обязан вернуть
            понятную ошибку («сейчас публиковать нечем»).
        """
        from config.runtime import (
            get_never_publish_token_names,
            get_publish_token_names,
            get_reserve_publish_token_names,
        )

        never_publish = get_never_publish_token_names()
        active_db = await self._load_active()

        user_tokens = await self._user_tokens()

        # UI-override (миграция 023): user-токены с ``role='publish'`` в БД
        # добавляются к env-whitelist'у АДДИТИВНО — роль только РАСШИРЯЕТ набор
        # публикаторов. Hard deny-list ниже по-прежнему имеет приоритет.
        db_publish = sorted(
            name
            for name, row in active_db.items()
            if (getattr(row, "role", None) or "").lower() == "publish"
        )

        if op == TokenOp.READ:
            # READ: любой active user-токен — читать умеют только они
            # (community-токен на wall.get отвечает error 27, замер
            # 2026-07-25, см. docs/VK_TOKEN_ROADMAP.md).
            #
            # Порядок — балансировка нагрузки (заказ владельца 2026-07-25):
            # первым идёт токен, который сегодня сделал МЕНЬШЕ запросов
            # (``token_usage``), при равенстве — давно не использованный
            # (last_used ASC, NULL = никогда → в голову).
            #
            # Почему одного last_used мало: READ-токен выбирается один на
            # волну парсинга, а волны разные по весу — район с 60 донорами и
            # район с 8 донорами стоят одинакового штампа, но отличаются по
            # расходу почти на порядок. Счётчик запросов выравнивает именно
            # расход, а не число выборов.
            calls_today = _calls_today_safe()

            def _balance_key(name: str):
                row = active_db.get(name)
                lu = getattr(row, "last_used", None) if row is not None else None
                return (calls_today.get(name, 0), lu is not None, lu or datetime.min)

            out: List[TokenCandidate] = []
            for name in sorted(user_tokens.keys(), key=_balance_key):
                out.append(_candidate(name, user_tokens[name], "user"))
            return out

        if op == TokenOp.USER_DM:
            # Личность отправителя фиксирована: ровно этот аккаунт или ничего.
            # Hard deny-list действует и здесь (VITA не пишет никому).
            name = (account or "").upper()
            tok = user_tokens.get(name) if name else None
            if not tok or name in never_publish:
                return []
            return _capability_filter(
                [_candidate(name, tok, "user")],
                op,
                await self._capabilities(),
                user_allow_list=False,
            )

        # Каскад публикации (решение владельца 2026-07-12):
        # community-токен группы → основной whitelist (VALSTAN) → резерв
        # (VITA, строго последним). Порядок внутри эшелона — порядок env-CSV;
        # db role='publish' добираются после env-имён (детерминированно).
        def _user_write_names() -> List[str]:
            primary = list(get_publish_token_names()) + [
                n for n in db_publish if n not in get_publish_token_names()
            ]
            if not primary:
                # Нет явного whitelist'а — исторический fallback: все active
                # user-токены (кроме резервных — те всё равно добавятся ниже).
                reserve_set = {n for n in get_reserve_publish_token_names()}
                primary = [n for n in user_tokens.keys() if n not in reserve_set]
            ordered = primary + [n for n in get_reserve_publish_token_names() if n not in primary]
            seen: set = set()
            result: List[str] = []
            for n in ordered:
                if n in never_publish or n in seen:
                    continue
                seen.add(n)
                result.append(n)
            return result

        if op == TokenOp.USER_WRITE:
            out = []
            for name in _user_write_names():
                tok = user_tokens.get(name)
                if tok:
                    out.append(_candidate(name, tok, "user"))
            return out

        # COMMUNITY_WRITE / COMMUNITY_DM: community-token (если group_id передан)
        # первым, потом user-tokens каскадом whitelist → reserve. Для DM сверху —
        # фильтр по способности ``messages`` (снапшот), для публикаций — нет:
        # каскад публикации проверен боем, а замер photos/wall у части токенов
        # заведомо старее прав.
        out = []
        if group_id is not None:
            cid = abs(int(group_id))
            comms = await self._load_communities()
            community_rows = comms.get(cid, [])
            for ct in community_rows:
                out.append(_candidate(ct.name, ct.token, "community", cid))
        for name in _user_write_names():
            tok = user_tokens.get(name)
            if tok:
                out.append(_candidate(name, tok, "user"))
        if op == TokenOp.COMMUNITY_DM:
            return _capability_filter(out, op, await self._capabilities())
        return out

    async def _capabilities(self) -> Optional[Dict[str, Dict[str, str]]]:
        """Снапшот способностей (процессный кеш, Redis в потоке, best-effort)."""
        return await _capabilities_matrix_cached()

    async def _token_exists_but_disabled(self, name: str) -> bool:
        """True, если в БД есть запись с этим name и она сейчас в disabled."""
        q = await self._session.execute(
            select(VKToken).where(VKToken.name == name, VKToken.community_id.is_(None))
        )
        row = q.scalar_one_or_none()
        if row is None:
            return False
        if not row.is_active:
            return True
        if row.disabled_until is not None and row.disabled_until > datetime.utcnow():
            return True
        return False

    # ------------------------------------------------------------------
    # Учёт результатов
    # ------------------------------------------------------------------

    async def report_error(self, name: str, vk_error_code: int) -> None:
        """Зафиксировать VK error для токена; auto-disable по 5/17/29.

        Поднимает ``consecutive_errors`` всегда. Записывает
        ``disabled_until=now()+hours`` если ``vk_error_code`` в
        :data:`_AUTO_DISABLE_CODES_HOURS`. Шлёт Telegram-alert при
        auto-disable.

        Идемпотентна: если токена нет в БД (только в env) — записывается
        новая строка с минимальными полями. Это удобно для первого запуска,
        когда vk_tokens таблица не synced с env.
        """
        upper = name.upper()
        hours = _AUTO_DISABLE_CODES_HOURS.get(int(vk_error_code))
        now = datetime.utcnow()
        disabled_until = now + timedelta(hours=hours) if hours else None

        # Ищем существующую запись
        q = await self._session.execute(select(VKToken).where(VKToken.name == upper))
        row = q.scalar_one_or_none()

        if row is None:
            # Создавать запись не будем — БД не обязана содержать всю env.
            # Просто логируем; в следующий pick этот токен останется
            # доступным (потому что _token_exists_but_disabled вернёт False).
            logger.warning(
                "TokenPolicy.report_error: no DB row for %s, vk_code=%s — skipping persistence",
                upper,
                vk_error_code,
            )
            self._invalidate_cache()
            return

        row.last_error_code = int(vk_error_code)
        row.last_error_at = now
        row.consecutive_errors = int(row.consecutive_errors or 0) + 1
        if disabled_until is not None:
            row.disabled_until = disabled_until
            row.error_message = f"auto-disable: VK error {vk_error_code} at {now.isoformat()}"
        await self._session.commit()
        self._invalidate_cache()

        if disabled_until is not None:
            logger.warning(
                "TokenPolicy: token %s auto-disabled until %s (VK error %s)",
                upper,
                disabled_until.isoformat(),
                vk_error_code,
            )
            await _send_telegram_alert_safe(
                f"🛑 VK-токен {upper} автоматически отключён до {disabled_until.isoformat()} "
                f"(VK error {vk_error_code}). Проверьте здоровье токена."
            )

    async def report_success(self, name: str) -> None:
        """Сбросить ``consecutive_errors`` после удачного вызова.

        Не трогает ``disabled_until`` — ручной enable делается отдельно через
        :meth:`enable` или ``POST /api/tokens/{name}/enable``.
        """
        upper = name.upper()
        await self._session.execute(
            update(VKToken)
            .where(VKToken.name == upper)
            .values(consecutive_errors=0, last_used=datetime.utcnow())
        )
        await self._session.commit()
        self._invalidate_cache()

    # ------------------------------------------------------------------
    # Manual control (UI / SQL)
    # ------------------------------------------------------------------

    async def disable(self, name: str, hours: float, reason: str = "manual") -> bool:
        """Manual disable on ``hours`` hours. Возвращает True если записано.

        Если токена нет в БД — создаётся новая запись (минимальная) с
        полями ``name`` и ``token`` из env. Это нужно, чтобы dashboard'у
        пользователя было что увидеть.
        """
        upper = name.upper()
        from config.runtime import VK_TOKENS

        until = datetime.utcnow() + timedelta(hours=float(hours))
        q = await self._session.execute(
            select(VKToken).where(VKToken.name == upper, VKToken.community_id.is_(None))
        )
        row = q.scalar_one_or_none()
        if row is None:
            env_tok = VK_TOKENS.get(upper)
            if not env_tok:
                logger.error("TokenPolicy.disable: %s not in env and not in DB", upper)
                return False
            row = VKToken(
                name=upper,
                token=env_tok,
                is_active=True,
                disabled_until=until,
                error_message=f"manual disable: {reason}",
            )
            self._session.add(row)
        else:
            row.disabled_until = until
            row.error_message = f"manual disable: {reason}"
        await self._session.commit()
        self._invalidate_cache()
        logger.info("TokenPolicy: %s disabled until %s (reason: %s)", upper, until, reason)
        return True

    async def enable(self, name: str) -> bool:
        """Сбросить ``disabled_until`` и счётчик ошибок."""
        upper = name.upper()
        res = await self._session.execute(
            update(VKToken)
            .where(VKToken.name == upper, VKToken.community_id.is_(None))
            .values(
                disabled_until=None,
                consecutive_errors=0,
                error_message=None,
            )
        )
        await self._session.commit()
        self._invalidate_cache()
        return res.rowcount > 0


async def _send_telegram_alert_safe(text: str) -> None:
    """Telegram-алёрт владельцу об автоотключении токена (best-effort).

    Раньше импортировал несуществующую функцию и молчал (разведка Этапа 3,
    2026-09-05) — теперь тот же канал, что у пингов кабинета.
    """
    import asyncio

    try:
        from modules.ad_cabinet import owner_ping

        await asyncio.to_thread(owner_ping.notify_owner, text)
    except Exception:  # pragma: no cover - алёрт не роняет маршрутизацию
        logger.warning("token router: telegram alert failed", exc_info=True)


# ----------------------------------------------------------------------
# Sync-friendly helper для legacy кода (parsing_tasks.py, copy_setka и пр.)
# ----------------------------------------------------------------------


async def get_active_parse_tokens(session: AsyncSession) -> Dict[str, str]:
    """Вернуть ``{name: token}`` user-токенов, годных для READ прямо сейчас.

    Используется legacy-местами, которые создают
    ``VKTokenRotatorAsync(list_of_tokens)`` и не хотят интегрироваться в
    :class:`TokenPolicy` целиком. Возвращаемое значение можно скармливать
    в ``VKTokenRotatorAsync([...values()])``.

    Источник значений — **БД** (``vk_tokens``), а не env. Единый источник
    истины: токены добавляются/меняются через ``/tokens`` UI, парсинг и
    публикация читают одну и ту же запись (раньше парсинг брал значение из
    env ``VK_TOKENS``, публикация — из БД, что приводило к рассинхрону при
    ротации токена — инцидент VALSTAN 2026-05-28).

    Фильтр (user-токены, ``community_id IS NULL``):
    - ``is_active = TRUE`` и непустой ``token``;
    - не на cooldown: ``disabled_until IS NULL`` или ``< now()``;
    - ``validation_status != 'invalid'`` — явно протухший токен в парсинг не
      берём, иначе словим VK error 5 и авто-disable на ровном месте.
      ``unknown`` / ``valid`` — годятся (свежедобавленный токен ещё «unknown»).
    """
    now = datetime.utcnow()
    q = await session.execute(select(VKToken).where(VKToken.community_id.is_(None)))
    out: Dict[str, str] = {}
    for t in q.scalars():
        if not t.is_active or not t.token:
            continue
        if t.disabled_until is not None and t.disabled_until > now:
            continue
        if t.validation_status == "invalid":
            continue
        name = t.name.upper()
        out[name] = t.token
        # Legacy-места крутят токены сами (VKTokenRotatorAsync), минуя pick() —
        # имя для учёта расхода объявляем здесь, иначе их запросы уедут в
        # «UNKNOWN:<fp>» и отчёт покажет расход мимо роутера.
        _register_name_safe(t.token, name)
    return out


async def get_publish_candidates_for_group(
    session: AsyncSession,
    group_id: int,
) -> List[TokenCandidate]:
    """Convenience-обёртка над ``TokenPolicy.pick(COMMUNITY_WRITE, group_id)``.

    Не делает report_error / report_success — caller ответственен за это.
    """
    return await TokenPolicy(session).pick(TokenOp.COMMUNITY_WRITE, group_id=group_id)


# ----------------------------------------------------------------------
# Healthy READ token: probe + self-healing cooldown (инцидент 2026-07-12)
# ----------------------------------------------------------------------


def _probe_token_sync(token: str) -> Optional[int]:
    """Живость токена одним лёгким ``users.get`` (stdlib, без vk_api).

    Returns:
        ``None`` — токен здоров; ``int`` — VK error_code (5 = мёртв);
        ``-1`` — сеть/прочий сбой (токен НЕ виноват, disable не делаем).
    """
    import json
    import urllib.parse
    import urllib.request

    url = "https://api.vk.com/method/users.get?" + urllib.parse.urlencode(
        {"access_token": token, "v": "5.199"}
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.load(resp)
    except Exception:
        return -1
    err = data.get("error")
    if err:
        try:
            return int(err.get("error_code"))
        except (TypeError, ValueError):
            return -1
    return None


async def pick_healthy_read_token(session: AsyncSession) -> Optional[TokenCandidate]:
    """Первый READ-кандидат, живость которого подтверждена probe'ом.

    Лечит инцидент 2026-07-12: мёртвый-но-включённый токен (VK error 5)
    вставал первым в ротацию чтения и заклинивал парсинг на 4 дня — путь
    чтения брал ``next(iter(...))`` без перебора и без auto-disable.
    Теперь: probe ``users.get`` перед выдачей; мёртвый кандидат (5/17/29)
    уходит в cooldown через ``report_error`` (+Telegram-alert) и берётся
    следующий. Сетевые сбои probe'а (-1) токен не дисквалифицируют.

    Цена — один лишний VK-вызов на выбор токена; выключенные probe'ом токены
    не переопрашиваются до конца cooldown'а (их отсеивает ``_load_active``).
    """
    import asyncio

    policy = TokenPolicy(session)
    for cand in await policy.pick(TokenOp.READ):
        code = await asyncio.to_thread(_probe_token_sync, cand.token)
        if code is None:
            # Штамп last_used → карусель: следующий pick(READ) поставит этот
            # токен в хвост, нагрузка чтения распределяется равномерно.
            try:
                await policy.report_success(cand.name)
            except Exception:  # pragma: no cover — ротация не должна ронять чтение
                logger.exception("pick_healthy_read_token: report_success failed")
            return cand
        if code in _AUTO_DISABLE_CODES_HOURS:
            logger.warning(
                "pick_healthy_read_token: %s мёртв (VK error %s) — cooldown, пробуем следующий",
                cand.name,
                code,
            )
            await policy.report_error(cand.name, code)
        else:
            logger.warning(
                "pick_healthy_read_token: probe %s не прошёл (code %s) — пропуск без disable",
                cand.name,
                code,
            )
    return None


async def get_healthy_read_token() -> Optional[str]:
    """Convenience: строка первого живого READ-токена (собственная сессия).

    Для мест без готовой AsyncSession (radar-адаптеры, notification-чекеры,
    web-эндпоинты). ``None`` — живых READ-токенов нет; caller обязан вернуть
    понятную ошибку. Замена хардкоду ``VK_TOKENS.get("VALSTAN")`` (env),
    который в инциденте 2026-07-12 держал мёртвый токен до рестарта.
    """
    from database.connection import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        cand = await pick_healthy_read_token(session)
        return cand.token if cand else None


def get_active_parse_tokens_sync() -> Dict[str, str]:
    """Sync-friendly обёртка над ``get_active_parse_tokens``.

    Подразумевает наличие активного event-loop'а (Celery task через
    ``utils.celery_asyncio.run_coro`` — обычный кейс на проде).

    Основной источник — БД (см. ``get_active_parse_tokens``). env ``VK_TOKENS``
    остаётся только аварийным fallback'ом на случай недоступной БД в горячем
    пути парсинга — чтобы случайная DB-ошибка не обнулила токены. Если env
    позже почистят (токены живут в БД) — fallback вернёт пусто, парсинг
    залогирует отсутствие токенов; это допустимая деградация при DB-down.
    """
    from config.runtime import VK_TOKENS

    try:
        from database.connection import AsyncSessionLocal
        from utils.celery_asyncio import run_coro

        async def _impl() -> Dict[str, str]:
            async with AsyncSessionLocal() as s:
                return await get_active_parse_tokens(s)

        return run_coro(_impl())
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("get_active_parse_tokens_sync fallback to env-only: %s", e)
        return {k: v for k, v in (VK_TOKENS or {}).items() if v}
