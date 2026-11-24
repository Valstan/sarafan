"""
VK API Client for SETKA project
Handles all interactions with VK API
"""

import asyncio
import logging
import threading
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

import vk_api

from modules.vk_monitor.rate_limiter import RateLimiter, build_rate_limiter
from modules.vk_monitor.token_usage import record_call

logger = logging.getLogger(__name__)

# VK API error codes that indicate expected, recoverable conditions
# (closed wall, banned/deleted user, private community).
# https://dev.vk.com/reference/errors
# ⚠️ Этот frozenset выбирает УРОВЕНЬ ЛОГА для read-методов, а не политику
# проекта по кодам. Техдолг «коды 9/14/214/219 не обрабатывает никто» указывал
# сюда — и указывал неверно: эти коды приходят на wall.post и логируются как
# ERROR, то есть уже громко. Добавить их сюда значило бы понизить их до
# WARNING — ровно наоборот тому, что нужно.
# Политика записи живёт в modules/publisher (VKPublishError, _WALL_SCOPED_CODES).
_VK_EXPECTED_ERROR_CODES = frozenset({15, 18, 203, 212, 220})


def _log_vk_api_error(prefix: str, error: vk_api.exceptions.ApiError) -> None:
    """Log VK ApiError at WARNING for expected/operational codes, ERROR otherwise."""
    level = logging.WARNING if error.code in _VK_EXPECTED_ERROR_CODES else logging.ERROR
    logger.log(level, "%s: %s", prefix, error)


def enforce_token_rate_limit(token: str, method: str = "") -> None:
    """Притормозить и учесть один вызов VK API под ``token``.

    Общая точка для кода, который ходит в ВК **мимо** :class:`VKClient` —
    сейчас это батч-фетчер метрик ``vk_monitor/post_metrics.py`` (ему на вход
    дают готовый ``vk_api``-объект, потому что политика выбора токена у
    кабинета и у обновления метрик района разная).

    Своего sleep такой код заводить не должен: тормоз обязан быть ОДИН на
    токен. Обновление метрик берёт тот же живой READ-токен, что и боевой
    парсер, и залп из 78 вызовов подряд поставил бы cooldown/captcha именно на
    него — вспомогательная работа деградировала бы сбор постов. Тот же
    лимитер (и тот же Redis-backend при ``VK_RATE_LIMIT_BACKEND=redis``)
    держит общий счёт по всем процессам Celery.

    Учёт (``token_usage.record_call``) здесь же и по той же причине: иначе
    ~620 вызовов в сутки не попадут в отчёт по токенам и расход будет
    выглядеть меньше реального.
    """
    VKClient._get_rate_limiter().wait(token)
    record_call(token, method)


class VKClient:
    """VK API Client with token rotation and rate limiting.

    **Global per-token rate limit (added 2026-05-22, refactored 2026-05-26).**

    `vk_api` library уже sleep'ит при rate-limit (HTTP 6 / TooManyRequests),
    но это per-VkApi-session. Если в одном процессе живут несколько `VKClient`
    с одним и тем же токеном (например при одновременном parse'е нескольких
    регионов через разные task'и), они НЕ видят счётчик друг друга и могут
    разом отправить burst > 3 req/sec — VK ставит cooldown / captcha на токен.

    Решение: общий :class:`RateLimiter` (см. ``rate_limiter.py``). Два backend'а:

    - ``threading`` (default) — per-process через ``threading.Lock``.
    - ``redis`` — cross-process через Redis Lua-script; нужен для multi-worker
      Celery (`celery -c N` с prefork).

    Backend выбирается через env ``VK_RATE_LIMIT_BACKEND`` (default
    ``threading``). RedisRateLimiter с graceful fallback на threading при
    недоступном Redis — приоритет «не повесить», а не «строгий контроль».
    """

    # ~2.5 req/sec — чуть ниже VK-документированного лимита 3 req/sec,
    # запас на jitter в сети.
    GLOBAL_PARSE_INTERVAL_SECONDS: ClassVar[float] = 0.4

    # Lazy-singleton rate-limiter, общий для всех инстансов VKClient.
    # Сбрасывается в тестах через fixture (set to None → пересоздаётся).
    _rate_limiter: ClassVar[Optional[RateLimiter]] = None
    _rate_limiter_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, token: str):
        """Initialize VK client with token"""
        from modules.vk_monitor.wall_cache import WallCacheStats

        self.token = token
        self.session = None
        self.vk = None
        # Счётчики кэша стен — чтобы волна могла сказать в логе, сработал ли он.
        # Без этой строки «кэш включён» и «кэш помогает» были бы неразличимы.
        self.wall_cache_stats = WallCacheStats()
        self._init_session()

    @classmethod
    def _get_rate_limiter(cls) -> RateLimiter:
        if cls._rate_limiter is None:
            with cls._rate_limiter_lock:
                if cls._rate_limiter is None:  # double-checked
                    cls._rate_limiter = build_rate_limiter(cls.GLOBAL_PARSE_INTERVAL_SECONDS)
        return cls._rate_limiter

    def _enforce_rate_limit(self, method: str = "") -> None:
        """Block (sleep) until `GLOBAL_PARSE_INTERVAL_SECONDS` since the last
        VK API call under the same token. Делегирует в shared RateLimiter.

        Called from `api_call` and from sync wall/groups/getById wrappers.
        Async wrappers (`get_posts`, `get_groups`, `get_messages`) — тоже
        прокидываются через `asyncio.to_thread(self._enforce_rate_limit)`,
        чтобы не блокировать event loop.

        Здесь же считается расход запросов по токенам
        (:mod:`modules.vk_monitor.token_usage`): это единственная точка, через
        которую проходит каждый вызов клиента, поэтому счётчик получается
        полным без обвешивания каждого метода. ``method`` — имя VK-метода для
        разбивки в отчёте; учёт best-effort и никогда не роняет вызов.

        Тело делегировано в модульную :func:`enforce_token_rate_limit`, чтобы
        код, ходящий в ВК мимо клиента (батч-фетчер метрик), тормозил и
        считался ТЕМ ЖЕ механизмом, а не своей копией.
        """
        enforce_token_rate_limit(self.token, method)

    def _init_session(self):
        """Initialize VK session"""
        try:
            self.session = vk_api.VkApi(token=self.token)
            self.vk = self.session.get_api()
            logger.info("VK session initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize VK session: {e}")
            raise

    def get_wall_posts(
        self,
        owner_id: int,
        count: int = 10,
        offset: int = 0,
        *,
        cache_ttl: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get posts from VK community wall

        Args:
            owner_id: VK group ID (negative for communities)
            count: Number of posts to fetch (max 100)
            offset: Offset for pagination
            cache_ttl: срок жизни записи в кэше стен, секунды. ``None`` — общий
                дефолт (``WALL_CACHE_TTL_SECONDS``), ``0`` — явный обход кэша
                для вызывающего, которому нужны свежие счётчики любой ценой.

        Returns:
            List of posts

        Кэш проверяется ДО тормоза rate-limit: попадание не должно ни ждать
        своей очереди к токену, ни тратить его квоту — иначе экономия вызовов
        превратилась бы в экономию, которая всё равно стоит времени.

        ``offset > 0`` кэш обходит: пагинация читает разные куски стены, а
        ключ здесь один на владельца.
        """
        from modules.vk_monitor.wall_cache import get_cached_wall, store_wall

        use_cache = offset == 0 and (cache_ttl is None or cache_ttl > 0)
        if use_cache:
            cached = get_cached_wall(owner_id, min(count, 100))
            if cached is not None:
                self.wall_cache_stats.hits += 1
                logger.debug("wall cache hit: %s (%d постов)", owner_id, len(cached))
                return cached
            self.wall_cache_stats.misses += 1
        else:
            self.wall_cache_stats.bypass += 1

        try:
            self._enforce_rate_limit("wall.get")
            response = self.vk.wall.get(owner_id=owner_id, count=min(count, 100), offset=offset)

            posts = response.get("items", [])
            logger.info(f"Fetched {len(posts)} posts from {owner_id}")
            if use_cache:
                store_wall(owner_id, min(count, 100), posts, ttl=cache_ttl)
            return posts

        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error(f"VK API error for {owner_id}", e)
            return []
        except Exception as e:
            logger.error(f"Unexpected error fetching posts from {owner_id}: {e}")
            return []

    # Сколько стен упаковываем в один ``execute``. Предел ВК — 25 вызовов API
    # внутри одного VKScript; берём 20 с запасом: у скрипта есть и собственные
    # ограничения (время выполнения, размер ответа), а 20 групп по 20 постов с
    # вложениями — уже сотни килобайт JSON.
    EXECUTE_BATCH_SIZE: ClassVar[int] = 20

    def get_wall_posts_batch(
        self, owner_ids: List[int], count: int = 20
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Стены НЕСКОЛЬКИХ сообществ за один запрос (VK ``execute``).

        Возвращает ``{owner_id: [посты]}``. Сообщество, которое ВК не отдал
        (удалено, закрыто, забанено), приходит с пустым списком — как и при
        поштучном чтении, где ``get_wall_posts`` глотает ApiError и отдаёт ``[]``.

        **Зачем.** Измерено 2026-08-20: весь расход трёх user-токенов — это
        ``wall.get`` по донорским стенам, один вызов на группу на волну (5282
        сканирования против 5361 вызова за те же часы). ``execute`` исполняет до
        25 вызовов API на стороне ВК за один запрос, то есть волна из 13-14 групп
        укладывается в один вызов вместо четырнадцати.

        **Порядок ответа не гарантирован по смыслу, поэтому не используется.**
        VKScript возвращает массив в порядке вызовов, но сопоставлять по индексу
        опасно: при частичном отказе ВК кладёт в элемент ``false``, и сдвиг на
        один превратил бы посты одного района в посты соседнего. Поэтому
        owner_id берётся из самих постов, а индекс служит только для тех групп,
        которые не отдали ни одного поста.
        """
        from modules.vk_monitor.wall_cache import get_cached_wall, store_wall

        out: Dict[int, List[Dict[str, Any]]] = {oid: [] for oid in owner_ids}
        if not owner_ids:
            return out

        per_call = min(int(count), 100)

        # Сначала кэш, в ВК — только за промахами. Смысл именно в этом: волна
        # соседней темы того же района читает те же донорские стены, и без
        # отсева попаданий ``execute`` снова сходил бы за всеми.
        pending: List[int] = []
        for oid in owner_ids:
            cached = get_cached_wall(oid, per_call)
            if cached is not None:
                out[int(oid)] = cached
                self.wall_cache_stats.hits += 1
            else:
                self.wall_cache_stats.misses += 1
                pending.append(oid)
        if not pending:
            logger.info(
                "execute batch: %d групп целиком из кэша, запросов к ВК 0",
                len(owner_ids),
            )
            return out
        owner_ids = pending

        for i in range(0, len(owner_ids), self.EXECUTE_BATCH_SIZE):
            chunk = owner_ids[i : i + self.EXECUTE_BATCH_SIZE]
            ids_literal = ",".join(str(int(o)) for o in chunk)
            # var-объявления и while — VKScript не знает for-of и шаблонных строк.
            code = (
                f"var ids=[{ids_literal}];var i=0;var r=[];"
                f'while(i<ids.length){{r.push(API.wall.get({{"owner_id":ids[i],'
                f'"count":{per_call},"extended":0}}));i=i+1;}}return r;'
            )
            try:
                self._enforce_rate_limit("execute")
                resp = self.session.method("execute", {"code": code})
            except vk_api.exceptions.ApiError as e:
                # Отказ ВСЕГО батча — не тихий ноль: вызывающий обязан узнать и
                # перечитать группы поштучно, иначе волна выйдет с пустой лентой
                # и это будет выглядеть как «сегодня новостей нет».
                _log_vk_api_error(f"VK execute batch error ({len(chunk)} групп)", e)
                raise
            except Exception as e:
                logger.error(f"Unexpected execute batch error: {e}")
                raise

            items = resp if isinstance(resp, list) else []
            for pos, entry in enumerate(items):
                if not isinstance(entry, dict):
                    # false — ВК отказал по этой конкретной группе (закрыта,
                    # удалена, бан). Оставляем пустой список: поведение то же,
                    # что у поштучного чтения.
                    continue
                posts = entry.get("items") or []
                if not posts:
                    continue
                owner = posts[0].get("owner_id")
                if owner is None and pos < len(chunk):
                    owner = chunk[pos]
                if owner is None:
                    continue
                out.setdefault(int(owner), []).extend(posts)
                store_wall(int(owner), per_call, posts)

        total = sum(len(v) for v in out.values())
        logger.info(
            "execute batch: %d групп за %d запрос(ов), постов %d (%s)",
            len(owner_ids),
            (len(owner_ids) + self.EXECUTE_BATCH_SIZE - 1) // self.EXECUTE_BATCH_SIZE,
            total,
            self.wall_cache_stats.summary(),
        )
        return out

    def get_posts_by_ids(self, refs: List[tuple]) -> List[Dict[str, Any]]:
        """
        Пакетная загрузка постов wall.getById (до 100 за запрос).

        Args:
            refs: список (owner_id, post_id)

        Returns:
            Список постов в порядке ответа API (может быть короче refs при ошибках)
        """
        if not refs:
            return []
        out: List[Dict[str, Any]] = []
        batch_size = 100
        for i in range(0, len(refs), batch_size):
            chunk = refs[i : i + batch_size]
            posts_str = ",".join(f"{oid}_{pid}" for oid, pid in chunk)
            try:
                self._enforce_rate_limit("wall.getById")
                resp = self.vk.wall.getById(posts=posts_str)
                if resp:
                    out.extend(resp)
            except vk_api.exceptions.ApiError as e:
                _log_vk_api_error("VK wall.getById batch error", e)
            except Exception as e:
                logger.error(f"Unexpected getById batch error: {e}")
        return out

    def get_post_by_id(self, owner_id: int, post_id: int) -> Optional[Dict[str, Any]]:
        """
        Get specific post by ID

        Args:
            owner_id: VK group ID
            post_id: Post ID

        Returns:
            Post data or None
        """
        try:
            self._enforce_rate_limit("wall.getById")
            posts_str = f"{owner_id}_{post_id}"
            response = self.vk.wall.getById(posts=[posts_str])

            if response:
                return response[0]
            return None

        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error(f"VK API error getting post {owner_id}_{post_id}", e)
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting post {owner_id}_{post_id}: {e}")
            return None

    def get_group_info(self, group_id: int) -> Optional[Dict[str, Any]]:
        """
        Get information about VK group

        Args:
            group_id: VK group ID (positive or negative)

        Returns:
            Group info or None
        """
        try:
            # Convert to positive ID if needed
            group_id = abs(group_id)

            self._enforce_rate_limit("groups.getById")
            response = self.vk.groups.getById(group_id=group_id)

            if response:
                return response[0]
            return None

        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error(f"VK API error getting group info {group_id}", e)
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting group info {group_id}: {e}")
            return None

    def search_groups(
        self,
        query: str,
        city_id: Optional[int] = None,
        count: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Search VK groups via ``groups.search``.

        Используется модулем discovery для авто-регистрации сообществ.
        ``city_id`` — численный VK city_id (через ``database.getCities``);
        если задан, поиск ограничивается этим городом. ``count`` ≤ 1000.

        Returns:
            Список dict-ов ``{id, name, screen_name, members_count, photo_200, ...}``
            из ``response.items``. На ошибке VK API — пустой список.
        """
        if not (query or "").strip():
            return []
        try:
            self._enforce_rate_limit("groups.search")
            kwargs: Dict[str, Any] = {
                "q": query,
                "count": min(int(count), 1000),
                "offset": int(offset),
                "country_id": 1,  # Russia
            }
            if city_id:
                kwargs["city_id"] = int(city_id)
            response = self.vk.groups.search(**kwargs)
            items = (response or {}).get("items", [])
            logger.info(
                "groups.search(q=%r, city_id=%s) → %d items",
                query,
                city_id,
                len(items),
            )
            return items
        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error(f"VK groups.search error (q={query!r})", e)
            return []
        except Exception as e:
            logger.error(f"Unexpected groups.search error (q={query!r}): {e}")
            return []

    def get_groups_by_ids(
        self,
        group_ids: List[int],
        fields: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Batch ``groups.getById`` — до 500 id за вызов.

        ``group_ids`` — список положительных id (для отрицательных делается abs).
        ``fields`` — comma-separated extra fields VK API (например,
        ``"description,members_count,activity,status"``).

        Returns:
            Объединённый список ``items`` со всех страниц. Ошибки одного chunk'а
            не валят весь вызов (chunk пропускается, остальные продолжаются).
        """
        if not group_ids:
            return []
        out: List[Dict[str, Any]] = []
        batch_size = 500
        ids = [abs(int(g)) for g in group_ids]
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            try:
                self._enforce_rate_limit("groups.getById")
                kwargs: Dict[str, Any] = {"group_ids": chunk}
                if fields:
                    kwargs["fields"] = fields
                resp = self.vk.groups.getById(**kwargs)
                if resp:
                    out.extend(resp)
            except vk_api.exceptions.ApiError as e:
                _log_vk_api_error(f"VK groups.getById batch error (size={len(chunk)})", e)
            except Exception as e:
                logger.error(f"Unexpected groups.getById batch error (size={len(chunk)}): {e}")
        return out

    def get_groups_by_refs(
        self,
        refs: List[str],
        fields: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Batch ``groups.getById`` по строковым refs (screen_name / ``club123``).

        В отличие от :meth:`get_groups_by_ids` (только числовые id, делает abs),
        принимает текстовые refs — например, screen_name'ы из блока «Ссылки»
        главной ИНФО-страницы района. VK молча отбрасывает пользовательские
        домены (getById вернёт только сообщества), так что лишние refs безопасны.

        Args:
            refs: список screen_name / ``club<id>`` строк (до 500 за вызов).
            fields: comma-separated extra fields VK API.

        Returns:
            Объединённый список ``items``. Ошибка chunk'а не валит весь вызов.
        """
        clean = [str(r).strip() for r in (refs or []) if str(r or "").strip()]
        if not clean:
            return []
        out: List[Dict[str, Any]] = []
        batch_size = 500
        for i in range(0, len(clean), batch_size):
            chunk = clean[i : i + batch_size]
            try:
                self._enforce_rate_limit("groups.getById")
                kwargs: Dict[str, Any] = {"group_ids": ",".join(chunk)}
                if fields:
                    kwargs["fields"] = fields
                resp = self.vk.groups.getById(**kwargs)
                if resp:
                    out.extend(resp)
            except vk_api.exceptions.ApiError as e:
                _log_vk_api_error(f"VK groups.getById refs error (size={len(chunk)})", e)
            except Exception as e:
                logger.error(f"Unexpected groups.getById refs error (size={len(chunk)}): {e}")
        return out

    def get_group_member_ids(
        self,
        group_id: int,
        max_members: int = 300000,
    ) -> Tuple[List[int], bool]:
        """Все user-id участников ОТКРЫТОЙ группы через ``groups.getMembers``.

        Постранично по 1000 (max VK). ``group_id`` — положительный или
        отрицательный (берётся ``abs``). Возвращает ``(ids, complete)``:
          * ``ids``      — user-id участников (положительные, без deactivated);
          * ``complete`` — ``True`` если выкачали всех; ``False`` при ошибке VK
                            (закрытая группа / нет доступа, error 15) или обрыве
                            на ``max_members`` (страховка от гигантских групп).

        Закрытая группа → VK error 15 → ``([], False)``: для дедупа области
        такая группа просто не войдёт в объединение (считаем по доступным,
        ``group_count`` это отразит). Rate-limited (общий лимитер токена).
        """
        gid = abs(int(group_id))
        out: List[int] = []
        offset = 0
        page = 1000
        while offset < max_members:
            try:
                self._enforce_rate_limit("groups.getMembers")
                resp = self.vk.groups.getMembers(group_id=gid, offset=offset, count=page)
            except vk_api.exceptions.ApiError as e:
                _log_vk_api_error(f"VK groups.getMembers error (gid={gid}, offset={offset})", e)
                return out, False
            except Exception as e:
                logger.error(f"Unexpected groups.getMembers error (gid={gid}): {e}")
                return out, False
            items = (resp or {}).get("items") or []
            for x in items:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
            total = int((resp or {}).get("count") or 0)
            offset += page
            if not items or offset >= total:
                return out, True
        logger.warning(
            "groups.getMembers gid=%s hit max_members=%d guard — partial", gid, max_members
        )
        return out, False

    def resolve_city(
        self,
        query: str,
        country_id: int = 1,
        count: int = 20,
    ) -> List[Dict[str, Any]]:
        """Resolve city name → ``vk_city_id`` via ``database.getCities``.

        Используется wizard'ом нового региона: модератор вводит «Малмыж»,
        возвращается список ``[{id, title, region, area}, …]`` для dropdown.

        ``q`` для ``database.getCities`` ищет по подстроке (case-insensitive),
        VK сортирует по importance. ``count`` ≤ 100.

        Returns:
            Список dict с полями ``id`` (vk_city_id), ``title``, ``area``,
            ``region``. На ошибке — пустой список.
        """
        if not (query or "").strip():
            return []
        try:
            self._enforce_rate_limit("database.getCities")
            response = self.vk.database.getCities(
                country_id=int(country_id),
                q=query,
                count=min(int(count), 100),
            )
            items = (response or {}).get("items", [])
            logger.info(
                "database.getCities(q=%r) → %d cities",
                query,
                len(items),
            )
            return items
        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error(f"VK database.getCities error (q={query!r})", e)
            return []
        except Exception as e:
            logger.error(f"Unexpected database.getCities error (q={query!r}): {e}")
            return []

    def get_cities_by_ids(self, city_ids: Sequence[int]) -> Dict[int, Dict[str, Any]]:
        """``database.getCities`` по точным id → ``{city_id: {title, area, region}}``.

        Нужен discovery'у для area-guard'а: у группы VK отдаёт поле ``city``
        (id города), а ``database.getCities`` для города отдаёт его **область**
        (``area``). Это единственный способ отличить «паблик нашего района» от
        «паблик чужой области с тем же названием» — на глаз по имени нельзя.

        ``city_ids`` — до 100 id за вызов; лишние и неположительные отбрасываются.
        На ошибке VK API — пустой словарь (вызывающий решает, что делать).
        """
        ids: List[int] = []
        seen: set[int] = set()
        for raw in city_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid <= 0 or cid in seen:
                continue
            seen.add(cid)
            ids.append(cid)
        if not ids:
            return {}

        out: Dict[int, Dict[str, Any]] = {}
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            try:
                self._enforce_rate_limit("database.getCities")
                response = self.vk.database.getCities(
                    country_id=1,
                    city_ids=",".join(str(c) for c in chunk),
                )
                items = (response or {}).get("items", [])
            except vk_api.exceptions.ApiError as e:
                _log_vk_api_error("VK database.getCities error (city_ids)", e)
                items = []
            except Exception as e:
                logger.error("Unexpected database.getCities error (city_ids): %s", e)
                items = []
            for it in items or []:
                try:
                    cid = int(it.get("id"))
                except (TypeError, ValueError):
                    continue
                out[cid] = {
                    "title": (it.get("title") or "").strip(),
                    "area": (it.get("area") or "").strip(),
                    "region": (it.get("region") or "").strip(),
                }
        return out

    def parse_attachments(self, post: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse post attachments (photos, videos, links, etc.)

        Args:
            post: VK post data

        Returns:
            List of parsed attachments
        """
        attachments = []

        if "attachments" not in post:
            return attachments

        for att in post["attachments"]:
            att_type = att.get("type")

            if att_type == "photo":
                photo = att["photo"]
                # Get largest photo size
                sizes = photo.get("sizes", [])
                if sizes:
                    largest = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
                    attachments.append(
                        {
                            "type": "photo",
                            "url": largest.get("url"),
                            "width": largest.get("width"),
                            "height": largest.get("height"),
                        }
                    )

            elif att_type == "video":
                video = att["video"]
                attachments.append(
                    {
                        "type": "video",
                        "title": video.get("title"),
                        "duration": video.get("duration"),
                        "views": video.get("views", 0),
                    }
                )

            elif att_type == "link":
                link = att["link"]
                attachments.append(
                    {"type": "link", "url": link.get("url"), "title": link.get("title")}
                )

            elif att_type == "doc":
                doc = att["doc"]
                attachments.append(
                    {"type": "document", "title": doc.get("title"), "url": doc.get("url")}
                )

        return attachments

    def extract_post_stats(self, post: Dict[str, Any]) -> Dict[str, int]:
        """
        Extract statistics from post

        Args:
            post: VK post data

        Returns:
            Dictionary with stats
        """
        return {
            "views": post.get("views", {}).get("count", 0),
            "likes": post.get("likes", {}).get("count", 0),
            "reposts": post.get("reposts", {}).get("count", 0),
            "comments": post.get("comments", {}).get("count", 0),
        }

    async def get_user_info(self) -> Optional[Dict[str, Any]]:
        """
        Get current user information

        Returns:
            User info or None
        """
        try:
            await asyncio.to_thread(self._enforce_rate_limit, "users.get")
            response = self.vk.users.get()
            if response:
                return response[0]
            return None
        except Exception as e:
            logger.error(f"Error getting user info: {e}")
            return None

    async def get_posts(
        self, owner_id: int, count: int = 10, offset: int = 0, extended: int = 0
    ) -> Optional[Dict[str, Any]]:
        """
        Get posts from VK wall (async wrapper)

        Args:
            owner_id: VK group ID (negative for communities)
            count: Number of posts to fetch (max 100)
            offset: Offset for pagination
            extended: Extended information

        Returns:
            Posts data or None
        """
        try:
            await asyncio.to_thread(self._enforce_rate_limit, "wall.get")
            response = self.vk.wall.get(
                owner_id=owner_id, count=min(count, 100), offset=offset, extended=extended
            )
            return response
        except Exception as e:
            logger.error(f"Error getting posts from {owner_id}: {e}")
            return None

    async def get_groups(self, count: int = 10, extended: int = 1) -> Optional[Dict[str, Any]]:
        """
        Get user's groups

        Args:
            count: Number of groups to fetch
            extended: Extended information

        Returns:
            Groups data or None
        """
        try:
            await asyncio.to_thread(self._enforce_rate_limit, "groups.get")
            response = self.vk.groups.get(count=count, extended=extended)
            return response
        except Exception as e:
            logger.error(f"Error getting groups: {e}")
            return None

    async def get_messages(self, count: int = 10) -> Optional[Dict[str, Any]]:
        """
        Get user's conversations (used to probe `messages` permission).

        VK API `messages.get` has been deprecated/removed since 2016;
        `messages.getConversations` is the modern replacement and only
        succeeds when the token actually carries the `messages` scope.
        """
        try:
            await asyncio.to_thread(self._enforce_rate_limit, "messages.getConversations")
            response = self.vk.messages.getConversations(count=count)
            return response
        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error("Error getting messages", e)
            return None
        except Exception as e:
            logger.error(f"Error getting messages: {e}")
            return None

    async def get_message_history(
        self,
        peer_id: int,
        count: int = 50,
        group_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a conversation thread via `messages.getHistory` (ad cabinet, block A).

        Used by the thread-view of an inbound-DM ad request: show the recent
        back-and-forth before the operator replies. `group_id` is required when
        the token is a user-token acting on behalf of a community (positive id);
        community access tokens imply the group and ignore it.

        Returns the raw VK response (`items` newest-last, `profiles`/`groups`
        via `extended=1`) or None on error — the caller decides how to degrade.
        """
        try:
            await asyncio.to_thread(self._enforce_rate_limit, "messages.getHistory")
            params: Dict[str, Any] = {
                "peer_id": int(peer_id),
                "count": min(int(count), 200),
                "extended": 1,
            }
            if group_id:
                params["group_id"] = abs(int(group_id))
            return self.vk.messages.getHistory(**params)
        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error("Error getting message history", e)
            return None
        except Exception as e:
            logger.error(f"Error getting message history for peer {peer_id}: {e}")
            return None

    async def check_token_validity(self) -> bool:
        """
        Check if token is still valid

        Returns:
            True if token is valid, False otherwise
        """
        try:
            # Try to fetch user info
            user_info = await self.get_user_info()
            return user_info is not None
        except Exception as e:
            logger.error(f"Token validation failed: {e}")
            return False

    def api_call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make a synchronous VK API call (for use with VKPublisher).

        Args:
            method: VK API method name (e.g., 'wall.post', 'photos.getWallUploadServer')
            params: Method parameters

        Returns:
            VK API response dict
        """
        try:
            self._enforce_rate_limit(method)
            response = self.session.method(method, params)
            return response
        except vk_api.exceptions.ApiError as e:
            _log_vk_api_error(f"VK API error ({method})", e)
            # Pass through error_code so callers can implement smart retries
            # (e.g. publisher fallback to publish-token on code 15/27).
            return {
                "error": {
                    "error_code": int(getattr(e, "code", 0) or 0),
                    "error_msg": str(e),
                }
            }
        except vk_api.exceptions.ApiHttpError as e:
            # Отказ ШЛЮЗА ВК (HTTP 5xx), а не логический ответ API. Ловится
            # отдельной веткой, потому что ``ApiHttpError`` наследует
            # ``VkApiError``, а НЕ ``ApiError``: до 2026-09-07 он проваливался
            # в generic-ветку ниже и терял всякий признак класса. Наружу уходил
            # голый текст «Response code 502» без кода, а весь механизм
            # устойчивости публикатора (fallback на community-токен по 15/27,
            # ротация publish-кандидата по 5/17/29) сравнивает именно код — ноль
            # не совпадал ни с чем. Отказ ВК на полторы минуты 07.09 доехал до
            # оператора тупиком именно так.
            #
            # Кода у HTTP-отказа нет и выдумывать его нельзя: синтетический код
            # утёк бы в ``vk_error_code`` отчётов и читался бы как настоящий
            # ответ ВК (см. ``_vk_error_code_of``). Поэтому класс несёт
            # отдельный признак.
            status = getattr(getattr(e, "response", None), "status_code", None)
            logger.warning(f"VK gateway error in {method}: HTTP {status} — транзиторно")
            return {
                "error": {
                    "error_msg": str(e),
                    "http_status": int(status) if status else None,
                    "transient": True,
                }
            }
        except Exception as e:
            logger.error(f"Unexpected error in {method}: {e}")
            return {"error": {"error_msg": str(e)}}


class VKTokenRotator:
    """Rotates between multiple VK tokens to avoid rate limits"""

    def __init__(self, tokens: List[str]):
        """
        Initialize token rotator

        Args:
            tokens: List of VK API tokens
        """
        self.tokens = tokens
        self.current_index = 0
        self.clients = [VKClient(token) for token in tokens if token]

    def get_client(self) -> Optional[VKClient]:
        """
        Get next available VK client

        Returns:
            VKClient instance or None if no clients available
        """
        if not self.clients:
            logger.error("No VK clients available")
            return None

        client = self.clients[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.clients)

        return client

    async def check_all_tokens(self) -> int:
        """
        Check validity of all tokens

        Returns:
            Number of valid tokens
        """
        valid_count = 0

        for client in self.clients:
            if await client.check_token_validity():
                valid_count += 1

        return valid_count
