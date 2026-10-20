"""
VK Publisher - Publishes bulletin posts to VK groups

Migrated from old_postopus bin/rw/post_msg.py and bin/rw/posting_post.py publishing logic.
Handles VK API wall.post with proper error handling and token rotation.
"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional, Tuple

# Imported lazily inside __init__ to avoid hard deps for test fixtures.

if TYPE_CHECKING:
    from modules.aggregation.aggregator import AggregatedPost

logger = logging.getLogger(__name__)


# VK error codes, при которых текущий publish-token считается «больше не
# подходящим» — нужно повернуться к следующему кандидату из ``_publish_candidates``.
# 5/17/29 совпадают с :data:`modules.vk_token_router._AUTO_DISABLE_CODES_HOURS` —
# TokenPolicy в эти же моменты ставит cooldown в БД.
# 10 («Internal server error») — так VK отвечает на wall.post от ЗАБАНЕННОГО
# аккаунта (инцидент 2026-07-13: бан VALSTAN → error 10 на каждом посте, каскад
# не крутился и МАМА не пробовалась). Ротируем на следующего кандидата;
# report_error(10) только фиксирует код (auto-disable для 10 нет — код бывает
# и транзиентным серверным сбоем VK, cooldown здорового токена не ставим).
_PUBLISH_ROTATE_CODES = frozenset({5, 10, 17, 29})

# Коды, которые говорят про СТЕНУ, а не про токен: 214 «access to adding post
# denied», 219 «advertisement post was recently added», 220 «wall access denied».
# Пробовать их следующим токеном ЗАПРЕЩЕНО, и это не осторожность, а арифметика:
# каскад community → МАМА → VALSTAN сделал бы три записи подряд в стену, которую
# ВК только что закрыл, а 219 — счётчик рекламных постов ИМЕННО на этой стене,
# и повтор с другого аккаунта его же и добивает, эскалируя до ограничения
# сообщества.
#
# Сегодня это соблюдается САМО СОБОЙ — просто потому, что этих кодов нет в
# наборах ротации. Константа и явная проверка нужны затем, чтобы свойство
# перестало быть случайным: первый же человек, который добавит 214 в
# ``_COMMUNITY_FALLBACK_CODES`` «чтобы попробовать другим токеном», получит
# ровно тот отказ, что описан выше. Инвариант закреплён тестом.
_WALL_SCOPED_CODES = frozenset({214, 219, 220})

# Отказ ШЛЮЗА ВК (HTTP 5xx) — не ответ API: у него нет ``error_code``, и весь
# каскад токенов, который сравнивает коды, для него слеп. Повторять его другим
# токеном бессмысленно вдвойне: лежит сторона ВК, а не право доступа.
#
# Повторяем МОЛЧА только методы, которые ничего не создают. Список — allowlist
# по префиксу, а не «всё кроме записей»: забытая в denylist-е запись повторится
# и задвоит пост, забытое в allowlist-е чтение всего лишь не получит повтора.
# Цена ошибки в двух списках разная, поэтому и список такой.
_TRANSIENT_RETRY_SAFE_PREFIXES = (
    "wall.get",
    "groups.get",
    "users.get",
    "utils.",
    "photos.getWallUploadServer",
    "photos.getMessagesUploadServer",
)
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF_SECONDS = 1.5


def _is_retry_safe_method(method: str) -> bool:
    """Можно ли молча повторить метод после отказа шлюза (ничего не создаёт)."""
    return str(method or "").startswith(_TRANSIENT_RETRY_SAFE_PREFIXES)


def _api_http_error_cls():
    """``vk_api.exceptions.ApiHttpError`` или пустой кортеж, если библиотеки нет.

    Импорт ленивый по той же причине, что и остальные в этом модуле: тестовые
    фикстуры инстанцируют публикатор без vk_api. Пустой кортеж в ``except``
    легален и не ловит ничего — путь просто остаётся прежним.
    """
    try:
        from vk_api.exceptions import ApiHttpError

        return ApiHttpError
    except Exception:  # pragma: no cover — библиотека есть везде, кроме голых фикстур
        return ()


class VKPublisher:
    """
    Publishes posts to VK groups.

    IMPORTANT: This class creates its OWN VK client with the PUBLISH token.
    Never reuse a parsing client for publishing.

    Community access tokens (опционально) передаются либо legacy-картой
    ``community_tokens={abs(group_id): token}``, либо пулом
    ``community_candidates={abs(group_id): [(name, token), ...]}``. Пул
    перебирается целиком до перехода на user-токены.
    """

    # VK API limits
    POSTS_PER_DAY_LIMIT = 50  # Per group
    POST_INTERVAL_SECONDS = 5  # Minimum interval between posts to the SAME group

    # Global rate-limit on the shared publish-token (VALSTAN). Without this,
    # a burst of 13 wall.repost calls in 7 sec earns us a [0] Captcha needed
    # response from VK (observed on 2026-05-21 14:37 right after the
    # community→publish fallback started working). Empirically ~1.5s between
    # publish-token API calls stays under VK's anti-burst threshold without
    # noticeably slowing the hourly copy-setka cycle (13 calls × 1.5 ≈ 20s).
    GLOBAL_PUBLISH_INTERVAL_SECONDS = 1.5

    # Class-level state: one publish-token per process, shared across all
    # VKPublisher instances. Hence a class-var (not instance attribute).
    _last_publish_token_call: ClassVar[Optional[datetime]] = None
    _publish_token_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    # VK error codes where a community-token wall.* call should be retried
    # via the global publish-token. Community access tokens issued via
    # vk.com/club{ID}→API typically lack `manage` scope.
    # 10 («Internal server error») added 2026-07-13: community-токены,
    # выпущенные из-под забаненного аккаунта-админа, замораживаются вместе с
    # ним и отвечают error 10 на wall.post — без fallback'а публикация вставала
    # колом на первом же шаге каскада (МАМА не пробовалась).
    _COMMUNITY_FALLBACK_CODES = {10, 15, 27}

    # VK API methods that are KNOWN to be unsupported with group access tokens
    # — VK docs explicitly state these need a user token. We don't even try
    # the community-token first to save a guaranteed-failure round trip and
    # to keep the publish-token rate-limit budget intact.
    # wall.pin / wall.unpin — community-токен отвечает error 27 (доказано в
    # modules/promotion/group_setup_vk.py, probe 2026-08-28). wall.post с
    # post_id=<предложенный> тоже только user-токеном админа, но метод у него
    # общий с обычной публикацией — поэтому он идёт через ``user_only=True``.
    _USER_TOKEN_ONLY_METHODS = frozenset({"wall.repost", "wall.pin", "wall.unpin"})

    def __init__(
        self,
        vk_client=None,
        test_polygon_mode: bool = False,
        test_polygon_group_id: int = -137760500,
        community_tokens: Optional[Dict[int, str]] = None,
        community_candidates: Optional[Dict[int, List[Tuple[str, str]]]] = None,
        publish_candidates: Optional[List[Tuple[str, str]]] = None,
    ):
        """
        Args:
            vk_client: Optional VK API client. If None, создаётся ленивно из
                первого валидного кандидата (publish_candidates → legacy
                get_publish_token).
            test_polygon_mode: If True, post to test group instead
            test_polygon_group_id: Test polygon VK group ID
            community_tokens: {abs(group_id): token} per-community access tokens —
                имеют приоритет над общим publish-токеном для своих групп.
            community_candidates: именованный каскад community-токенов для
                каждой группы. Имеет приоритет над legacy ``community_tokens``.
            publish_candidates: упорядоченный список ``(name, token)`` user-токенов
                для wall.post / wall.repost. Используется как fallback, если
                community-токен не подходит. Если не задан — берётся legacy
                ``get_publish_token()`` (один токен, обычно VALSTAN). Если задан
                пустой список — публикация через user-token невозможна; будут
                использоваться только community-токены. Имена в верхнем регистре.
        """
        from config.runtime import get_publish_token
        from modules.vk_monitor.vk_client import VKClient

        self._publish_candidates: List[Tuple[str, str]] = list(publish_candidates or [])
        # Имя текущего «активного» publish-токена для лога. Меняется при fallback
        # внутри ``_call_wall_post`` (см. _try_publish_candidates).
        self._active_publish_name: Optional[str] = None
        # Optional TokenPolicy, заполняется фабрикой ``create_with_policy``.
        # Если не None — report_success/report_error будут вызваны автоматически.
        self._policy = None  # type: ignore[assignment]

        if vk_client is not None:
            # Use provided client (for tests that already set up correctly)
            self.vk_client = vk_client
        else:
            # Создаём клиента из лучшего доступного кандидата.
            first_token: Optional[str] = None
            if self._publish_candidates:
                self._active_publish_name, first_token = self._publish_candidates[0]
            else:
                # legacy single-token путь
                first_token = get_publish_token()
                self._active_publish_name = "ENV"
            if first_token:
                self.vk_client = VKClient(first_token)
                logger.info(
                    "VKPublisher: client created with publish token %s",
                    self._active_publish_name,
                )
            else:
                # Нет ни одного user-token'а для публикации — продолжаем работу
                # только с community-токенами. wall.repost станет недоступен,
                # wall.post — только в группы, у которых есть community-токен.
                self.vk_client = None
                logger.warning(
                    "VKPublisher: no publish-token available. wall.post будет работать "
                    "только в группах с community-token; wall.repost недоступен."
                )

        self.test_polygon_mode = test_polygon_mode
        self.test_polygon_group_id = test_polygon_group_id
        self._last_post_time = {}  # group_id -> datetime
        self._community_tokens = dict(community_tokens or {})
        self._community_candidates: Dict[int, List[Tuple[str, str]]] = {
            int(cid): list(items) for cid, items in (community_candidates or {}).items()
        }
        # Кеш community-клиентов (VKClient инициализирует session, не хочется делать заново).
        self._community_clients: Dict[Any, Any] = {}
        # Кеш user-клиентов по имени токена (для fallback'а между кандидатами).
        self._user_clients: Dict[str, Any] = {}
        if self._active_publish_name and self.vk_client is not None:
            self._user_clients[self._active_publish_name] = self.vk_client
        if self._community_tokens:
            logger.info(
                "VKPublisher: %d community tokens available for per-group publish",
                len(self._community_tokens),
            )
        if self._publish_candidates:
            logger.info(
                "VKPublisher: %d publish-candidates available (%s)",
                len(self._publish_candidates),
                ", ".join(n for n, _ in self._publish_candidates),
            )

    @classmethod
    async def create_with_policy(
        cls,
        session,
        target_group_id: Optional[int] = None,
        **kwargs,
    ) -> "VKPublisher":
        """Async factory: VKPublisher с подгруженной TokenPolicy.

        ``target_group_id`` нужен для подбора community-токена конкретной
        группы. Если None — берутся только user-кандидаты (актуально для
        ``copy_setka``: там group_id у каждой target-стены свой).
        """
        from modules.vk_token_router import TokenOp, TokenPolicy

        policy = TokenPolicy(session)
        comm_map = await policy._load_communities()  # noqa: SLF001 — intended internal use
        community_candidates = {
            cid: [(vt.name, vt.token) for vt in rows] for cid, rows in comm_map.items()
        }
        community_tokens = {cid: rows[0].token for cid, rows in comm_map.items() if rows}

        candidates = await policy.pick(
            TokenOp.COMMUNITY_WRITE,
            group_id=target_group_id,
        )
        publish_user_candidates: List[Tuple[str, str]] = [
            (c.name, c.token) for c in candidates if c.source == "user"
        ]
        inst = cls(
            community_tokens=community_tokens,
            community_candidates=community_candidates,
            publish_candidates=publish_user_candidates,
            **kwargs,
        )
        inst._policy = policy
        return inst

    def set_publish_candidates(self, candidates: List[Tuple[str, str]]) -> None:
        """Обновить список user-token кандидатов на лету.

        Применимо, например, в copy_setka: первый раз вызвали для одного
        target-региона, потом для другого с другим community-токеном — но
        список user-кандидатов глобальный и обновляется отдельно.
        """
        self._publish_candidates = list(candidates or [])
        if self._publish_candidates and self.vk_client is None:
            from modules.vk_monitor.vk_client import VKClient

            name, tok = self._publish_candidates[0]
            self._active_publish_name = name
            self.vk_client = VKClient(tok)
            self._user_clients[name] = self.vk_client

    def _client_for_group(self, target_group_id: int):
        """Возвращает клиент, под которым нужно постить в эту группу.

        Если есть community-токен — используется он (создаётся VKClient ленивно).
        Иначе — общий publish-клиент (`self.vk_client`).
        """
        cid = abs(int(target_group_id))
        tok = self._community_tokens.get(cid)
        if not tok:
            return self.vk_client, False
        cli = self._community_clients.get(cid)
        if cli is None:
            from modules.vk_monitor.vk_client import VKClient

            cli = VKClient(tok)
            self._community_clients[cid] = cli
        return cli, True

    async def publish_bulletin(
        self,
        group_id: int,
        text: str,
        attachments: List[str] = None,
        copyright_url: str = None,
        from_group: bool = True,
        publish_date: Optional[int] = None,
        signed: bool = False,
    ) -> Dict[str, Any]:
        """
        Publish bulletin post to VK group.

        Args:
            group_id: VK group ID (negative number)
            text: Post text
            attachments: List of VK attachment strings
            copyright_url: Copyright URL for attribution
            from_group: True = post as group, False = as user
            publish_date: Unix timestamp (seconds). When set (>0), VK schedules
                the post into the community's "Отложенные записи" (postponed) and
                publishes it automatically at that time. ``None`` → publish now
                (default; bulletins are unaffected — zero regression).
            signed: True → add "подпись автора" (VK ``signed=1``); only meaningful
                together with ``from_group``.

        Returns:
            VK API response dict with post_id, url, etc. When ``publish_date`` is
            set, ``postponed=True`` and ``post_id`` is the postponed post id.
        """
        normalized_group_id = self._normalize_group_owner_id(group_id)

        # Determine target group
        if self.test_polygon_mode:
            target_group_id = self._normalize_group_owner_id(self.test_polygon_group_id)
            logger.info(f"🧪 TEST POLYGON MODE: Posting to test group {target_group_id}")
        else:
            target_group_id = normalized_group_id

        # Rate limiting
        await self._enforce_rate_limit(target_group_id)

        # Prepare attachments
        attachments_str = ",".join(attachments) if attachments else ""

        # Build wall.post parameters
        params = {
            "owner_id": target_group_id,
            "message": text,
            "from_group": 1 if from_group else 0,
        }

        if attachments_str:
            params["attachments"] = attachments_str

        if copyright_url:
            params["copyright"] = copyright_url

        if signed:
            params["signed"] = 1

        # publish_date — отложенный пост (VK «Отложенные записи»). Передаём только
        # положительный unix-timestamp; 0/None → публикация сразу (сводки).
        is_postponed = bool(publish_date and int(publish_date) > 0)
        if is_postponed:
            params["publish_date"] = int(publish_date)

        # Execute wall.post под правильным клиентом (community-токен, если есть).
        client, _via_community = self._client_for_group(target_group_id)
        try:
            response, via = await self._call_wall_post(params, client=client)

            post_id = response.get("post_id")
            post_url = f"https://vk.com/wall{target_group_id}_{post_id}"

            logger.info(
                "✅ %s post %s to group %s (via %s)",
                "Scheduled" if is_postponed else "Published",
                post_id,
                target_group_id,
                via,
            )
            logger.info(f"   URL: {post_url}")

            # Track last post time
            self._last_post_time[target_group_id] = datetime.now()

            # Забыть кэш этой стены: следующая тема района читает её как
            # историю публикаций (дедуп), и увидеть там устаревший снимок
            # значило бы не заметить только что вышедшую сводку. Дедуп по
            # ``work_tables.lip`` такой повтор поймал бы вторым рубежом, но
            # полагаться на него там, где первый чинится одной строкой,
            # неправильно. Сбой кэша публикацию не трогает.
            #
            # ⚠️ С 20.10 посты больше не содержат wall-ссылок на источники
            # (ADR-0011): скан стены как источник lip'ов ослеп, штатный дедуп
            # идёт по курсору в БД. Сброс кэша оставлен на будущее — если
            # история начнёт читаться из БД, снимок стены ему не понадобится;
            # пока цена строки нулевая (P206).
            try:
                from modules.vk_monitor.wall_cache import invalidate_wall

                invalidate_wall(-abs(int(target_group_id)))
            except Exception:  # pragma: no cover - кэш не важнее публикации
                logger.debug("wall cache invalidate failed", exc_info=True)

            return {
                "success": True,
                "post_id": post_id,
                "owner_id": target_group_id,
                "url": post_url,
                "text_length": len(text),
                "attachments_count": len(attachments) if attachments else 0,
                "postponed": is_postponed,
            }

        except Exception as e:
            logger.error(f"❌ Failed to publish post to group {target_group_id}: {e}")

            return {
                "success": False,
                "error": str(e),
                "group_id": target_group_id,
                # Числовой код ВК рядом со строкой, а не вместо неё: строку
                # читают и пишут в БД двенадцать call-site'ов, её формат менять
                # нельзя. Ключ добавочный и обратно совместимый.
                "vk_error_code": _vk_error_code_of(e),
                # Отказ шлюза ВК, а не отказ в публикации: вызывающий вправе
                # предложить повтор вместо того, чтобы объявлять поражение.
                "transient": bool(getattr(e, "transient", False)),
            }

    async def publish_suggested(
        self,
        group_id: int,
        post_id: int,
        *,
        signed: bool = True,
        publish_date: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Опубликовать ПРЕДЛОЖЕННЫЙ пост «как есть» (планировщик предложки, Этап 0).

        VK: ``wall.post(owner_id, post_id=<suggest>, from_group=1, signed=1
        [, publish_date])`` — принимает предложенную запись, сохраняя подпись
        автора («Предложил(а): …»). Только user-токеном администратора:
        community-токен на этот вызов отвечает 15/27 (probe
        ``scripts/probe_suggested_wall_post.py``), поэтому идём сразу по
        publish-кандидатам, минуя community, как ``wall.repost``.

        ``publish_date`` (unix) — отложка; ``None`` — публикация сейчас (режим
        ``queue`` диспетчера). Возвращает dict как ``publish_bulletin``; VK может
        вернуть новый ``post_id`` — вызывающий обязан хранить возвращённый.
        """
        target_group_id = self._normalize_group_owner_id(group_id)
        if self.test_polygon_mode:
            target_group_id = self._normalize_group_owner_id(self.test_polygon_group_id)
            logger.info("🧪 TEST POLYGON MODE: suggested post goes to %s", target_group_id)

        await self._enforce_rate_limit(target_group_id)

        params: Dict[str, Any] = {
            "owner_id": target_group_id,
            "post_id": int(post_id),
            "from_group": 1,
        }
        if signed:
            params["signed"] = 1
        is_postponed = bool(publish_date and int(publish_date) > 0)
        if is_postponed:
            params["publish_date"] = int(publish_date)

        try:
            response, via = await self._call_wall_post(params, user_only=True)
            new_id = response.get("post_id") or int(post_id)
            self._last_post_time[target_group_id] = datetime.now()
            logger.info(
                "✅ %s suggested post %s → %s in group %s (via %s)",
                "Scheduled" if is_postponed else "Published",
                post_id,
                new_id,
                target_group_id,
                via,
            )
            return {
                "success": True,
                "post_id": new_id,
                "owner_id": target_group_id,
                "url": f"https://vk.com/wall{target_group_id}_{new_id}",
                "postponed": is_postponed,
                "via": via,
            }
        except Exception as e:
            logger.error("❌ Failed to publish suggested %s in %s: %s", post_id, target_group_id, e)
            return {
                "success": False,
                "error": str(e),
                "group_id": target_group_id,
                "vk_error_code": _vk_error_code_of(e),
            }

    async def pin_post(self, owner_id: int, post_id: int) -> Dict[str, Any]:
        """Закрепить пост на стене сообщества — VK ``wall.pin`` (только user-токен)."""
        return await self._wall_simple_call("wall.pin", owner_id, post_id, "📌 Pinned")

    async def unpin_post(self, owner_id: int, post_id: int) -> Dict[str, Any]:
        """Открепить пост — VK ``wall.unpin`` (только user-токен)."""
        return await self._wall_simple_call("wall.unpin", owner_id, post_id, "📍 Unpinned")

    async def _wall_simple_call(
        self, method: str, owner_id: int, post_id: int, log_prefix: str
    ) -> Dict[str, Any]:
        """Общий каркас для wall.pin/unpin: тот же токен-роутинг, тот же формат ответа."""
        target = self._normalize_group_owner_id(owner_id)
        params = {"owner_id": target, "post_id": int(post_id)}
        client, _via_community = self._client_for_group(target)
        try:
            _response, via = await self._call_wall_post(params, method=method, client=client)
            logger.info("%s wall%s_%s (via %s)", log_prefix, target, post_id, via)
            return {"success": True, "via": via}
        except Exception as e:
            logger.error("❌ %s failed for wall%s_%s: %s", method, target, post_id, e)
            return {"success": False, "error": str(e), "vk_error_code": _vk_error_code_of(e)}

    async def publish_repost(
        self,
        group_id: int,
        source_owner_id: int,
        source_post_id: int,
        message: str = "",
    ) -> Dict[str, Any]:
        """
        Repost from another group.

        Uses VK API wall.repost

        Args:
            group_id: Target group ID
            source_owner_id: Source post owner ID
            source_post_id: Source post ID
            message: Optional message to add

        Returns:
            VK API response dict
        """
        normalized_group_id = self._normalize_group_owner_id(group_id)

        if self.test_polygon_mode:
            target_group_id = self._normalize_group_owner_id(self.test_polygon_group_id)
        else:
            target_group_id = normalized_group_id

        # Rate limiting
        await self._enforce_rate_limit(target_group_id)

        # Build repost object string
        repost_object = f"wall{source_owner_id}_{source_post_id}"

        # VK API expects `object` (e.g. wall-123_456), not an informal `repost` key.
        params = {
            "object": repost_object,
            "message": message,
        }
        if target_group_id < 0:
            params["group_id"] = abs(target_group_id)

        # wall.repost is user-token-only per VK API; _call_wall_post recognises
        # this via _USER_TOKEN_ONLY_METHODS and routes through publish-token.
        # We still pass a client (community) just to keep the signature uniform,
        # but it'll be ignored by _call_wall_post for repost.
        client, _via_community = self._client_for_group(target_group_id)
        try:
            response, via = await self._call_wall_post(
                params,
                method="wall.repost",
                client=client,
            )

            post_id = response.get("post_id")
            success = response.get("success", 0) == 1

            if success:
                post_url = f"https://vk.com/wall{target_group_id}_{post_id}"
                logger.info(
                    "✅ Reposted %s_%s to %s (via %s)",
                    source_owner_id,
                    source_post_id,
                    target_group_id,
                    via,
                )

                self._last_post_time[target_group_id] = datetime.now()

                return {
                    "success": True,
                    "post_id": post_id,
                    "owner_id": target_group_id,
                    "url": post_url,
                    "reposted": True,
                }
            else:
                return {
                    "success": False,
                    "error": "VK API returned success=0",
                }

        except Exception as e:
            logger.error(f"❌ Failed to repost to group {target_group_id}: {e}")

            return {
                "success": False,
                "error": str(e),
                "vk_error_code": _vk_error_code_of(e),
            }

    async def set_post_comments(
        self,
        owner_id: int,
        post_id: int,
        *,
        enabled: bool,
    ) -> Dict[str, Any]:
        """Включить/выключить комментарии у конкретного поста сообщества.

        VK: ``wall.openComments`` / ``wall.closeComments`` (отдельные методы — у
        ``wall.post`` нет параметра для этого). Идёт через тот же токен-роутинг,
        что и публикация (community-token, с fallback на publish-token при 15/27).

        Returns ``{"success": bool, "via"?: str, "error"?: str}``.
        """
        target = self._normalize_group_owner_id(owner_id)
        method = "wall.openComments" if enabled else "wall.closeComments"
        params = {"owner_id": target, "post_id": int(post_id)}
        client, _via_community = self._client_for_group(target)
        try:
            _response, via = await self._call_wall_post(params, method=method, client=client)
            logger.info(
                "💬 Comments %s for wall%s_%s (via %s)",
                "opened" if enabled else "closed",
                target,
                post_id,
                via,
            )
            return {"success": True, "via": via}
        except Exception as e:
            logger.error(
                "❌ Failed to toggle comments for wall%s_%s: %s",
                target,
                post_id,
                e,
            )
            return {"success": False, "error": str(e)}

    async def delete_post(self, owner_id: int, post_id: int) -> Dict[str, Any]:
        """Удалить пост сообщества (в т.ч. отложенный) — VK ``wall.delete``.

        Используется для отмены запланированного поста из рекламного кабинета.
        Идёт через тот же токен-роутинг, что и публикация.

        Returns ``{"success": bool, "via"?: str, "error"?: str}``.
        """
        target = self._normalize_group_owner_id(owner_id)
        params = {"owner_id": target, "post_id": int(post_id)}
        client, _via_community = self._client_for_group(target)
        try:
            _response, via = await self._call_wall_post(params, method="wall.delete", client=client)
            logger.info("🗑️ Deleted wall%s_%s (via %s)", target, post_id, via)
            return {"success": True, "via": via}
        except Exception as e:
            logger.error("❌ Failed to delete wall%s_%s: %s", target, post_id, e)
            return {"success": False, "error": str(e)}

    async def _call_wall_post(
        self,
        params: Dict[str, Any],
        method: str = "wall.post",
        client=None,
        *,
        user_only: bool = False,
    ) -> Tuple[Dict, str]:
        """
        Call VK API wall.post / wall.repost.

        Strategy:
        1. If method in ``_USER_TOKEN_ONLY_METHODS`` (e.g. wall.repost which VK
           fundamentally doesn't allow with a group token) — bypass community
           and either go via the legacy ``self.vk_client`` (если кандидатов нет)
           либо перебираем ``_publish_candidates`` через :meth:`_try_publish_candidates`.
        2. Otherwise — use ``client`` if provided (community-token).
        3. On community-token error 15/27 → fallback на publish-token (legacy путь
           если кандидатов нет, иначе через :meth:`_try_publish_candidates`).
        4. On publish-token error 5/17/29 → попробовать следующего кандидата,
           пометить текущего ``policy.report_error``.

        Returns:
            (response, via_label) — old labels ``publish-token`` /
            ``community-token`` / ``community-fallback-publish`` сохранены для
            обратной совместимости; при работе по кандидатам label принимает
            форму ``publish-token:<NAME>``.
        """
        # Локальные кандидаты — с backward-compat getattr (старые тесты
        # инстанцируют через __new__ без __init__).
        candidates = getattr(self, "_publish_candidates", None) or []
        policy = getattr(self, "_policy", None)
        active_name = getattr(self, "_active_publish_name", None)

        # Step 1: VK API restrictions — never try a group-token call we know fails.
        # ``user_only`` — то же для вызовов, у которых метод общий (wall.post с
        # post_id=<предложенный>), а право — только у user-токена админа.
        if user_only or method in self._USER_TOKEN_ONLY_METHODS:
            if candidates:
                return await self._try_publish_candidates(
                    method, params, via_prefix="publish-token"
                )
            if self.vk_client is None:
                raise RuntimeError(
                    "VKPublisher: no publish-token available for "
                    f"{method} (no candidates, no legacy client)."
                )
            await self._enforce_publish_token_rate_limit()
            response = await self._invoke(self.vk_client, method, params)
            return response, "publish-token"

        owner_id = params.get("owner_id")
        cid = abs(int(owner_id)) if owner_id is not None else None
        community_candidates = (
            getattr(self, "_community_candidates", {}).get(cid, []) if cid else []
        )
        if community_candidates:
            return await self._try_community_candidates(method, params, community_candidates)

        primary_client = client if client is not None else self.vk_client
        is_publish_token = primary_client is self.vk_client and primary_client is not None

        try:
            if is_publish_token:
                await self._enforce_publish_token_rate_limit()
            if primary_client is None:
                raise Exception(
                    "VKPublisher: no token available for this group "
                    "(no community-token, no active publish-token)."
                )
            response = await self._invoke(primary_client, method, params)
            if policy is not None and active_name and is_publish_token:
                try:
                    await policy.report_success(active_name)
                except Exception:  # pragma: no cover — defensive
                    logger.exception("policy.report_success failed")
            if is_publish_token:
                # backward-compat label: только если кандидаты заданы, добавляем имя
                via_label = (
                    f"publish-token:{active_name}"
                    if (candidates and active_name)
                    else "publish-token"
                )
            else:
                via_label = "community-token"
            return response, via_label
        except _VKApiCallError as e:
            if not is_publish_token and e.code in self._COMMUNITY_FALLBACK_CODES:
                logger.info(
                    "wall publish via community-token failed (code %s on %s), "
                    "retrying via publish-token",
                    e.code,
                    method,
                )
                if candidates:
                    return await self._try_publish_candidates(
                        method, params, via_prefix="community-fallback-publish"
                    )
                await self._enforce_publish_token_rate_limit()
                response = await self._invoke(self.vk_client, method, params)
                return response, "community-fallback-publish"
            # publish-token упал с кодом, требующим перехода на следующего кандидата
            if is_publish_token and e.code in _PUBLISH_ROTATE_CODES and candidates:
                logger.warning(
                    "publish-token %s failed with code %s on %s — rotating to next candidate",
                    active_name,
                    e.code,
                    method,
                )
                if policy is not None and active_name:
                    try:
                        await policy.report_error(active_name, e.code)
                    except Exception:  # pragma: no cover — defensive
                        logger.exception("policy.report_error failed")
                self._drop_active_publish_candidate()
                return await self._try_publish_candidates(
                    method, params, via_prefix="publish-token"
                )
            raise VKPublishError(e.code, e.message, transient=e.transient) from e

    async def _try_community_candidates(
        self,
        method: str,
        params: Dict[str, Any],
        candidates: List[Tuple[str, str]],
    ) -> Tuple[Dict, str]:
        """Перебрать все токены сообщества, затем перейти к user-каскаду."""
        from modules.vk_monitor.vk_client import VKClient

        policy = getattr(self, "_policy", None)
        rotate_codes = self._COMMUNITY_FALLBACK_CODES | set(_PUBLISH_ROTATE_CODES)
        for name, token in candidates:
            client = self._community_clients.get(name)
            if client is None:
                client = VKClient(token)
                self._community_clients[name] = client
            try:
                response = await self._invoke(client, method, params)
                if policy is not None:
                    try:
                        await policy.report_success(name)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("policy.report_success failed")
                return response, f"community-token:{name}"
            except _VKApiCallError as e:
                # Явная проверка ДО ротации: «стенные» коды не пробуем другим
                # токеном никогда, даже если кто-то добавит их в rotate_codes.
                if e.code in _WALL_SCOPED_CODES or e.code not in rotate_codes:
                    raise VKPublishError(e.code, e.message, transient=e.transient) from e
                logger.warning(
                    "community-token %s failed with code %s on %s — rotating",
                    name,
                    e.code,
                    method,
                )
                if policy is not None:
                    try:
                        await policy.report_error(name, e.code)
                    except Exception:  # pragma: no cover - defensive
                        logger.exception("policy.report_error failed")

        publish_candidates = getattr(self, "_publish_candidates", None) or []
        if publish_candidates:
            return await self._try_publish_candidates(
                method, params, via_prefix="community-fallback-publish"
            )
        if self.vk_client is not None:
            await self._enforce_publish_token_rate_limit()
            response = await self._invoke(self.vk_client, method, params)
            return response, "community-fallback-publish"
        raise RuntimeError(
            f"VKPublisher: all community tokens failed and no publish-token available for {method}."
        )

    def _drop_active_publish_candidate(self) -> None:
        """Удалить ``_active_publish_name`` из списка кандидатов и сбросить vk_client."""
        name = getattr(self, "_active_publish_name", None)
        if not name:
            return
        self._publish_candidates = [
            (n, t) for (n, t) in getattr(self, "_publish_candidates", []) if n != name
        ]
        getattr(self, "_user_clients", {}).pop(name, None)
        self._active_publish_name = None
        self.vk_client = None

    async def _try_publish_candidates(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        via_prefix: str,
    ) -> Tuple[Dict, str]:
        """Перебрать publish-кандидатов в порядке списка, пока один не сработает.

        Используется и для wall.repost (USER_WRITE), и как fallback для wall.post
        после ошибок community-token. Каждый rotate'нный кандидат, упавший с
        ``_PUBLISH_ROTATE_CODES`` (5/17/29), сообщается в TokenPolicy и
        отбрасывается из локального списка.
        """
        from modules.vk_monitor.vk_client import VKClient

        last_error: Optional[Exception] = None
        # Текущий vk_client может быть устаревшим — пересобираем из первого
        # доступного кандидата каждый раз.
        if not hasattr(self, "_user_clients"):
            self._user_clients = {}
        while getattr(self, "_publish_candidates", []):
            name, tok = self._publish_candidates[0]
            client = self._user_clients.get(name)
            if client is None:
                client = VKClient(tok)
                self._user_clients[name] = client
            self.vk_client = client
            self._active_publish_name = name
            try:
                await self._enforce_publish_token_rate_limit()
                response = await self._invoke(client, method, params)
                policy = getattr(self, "_policy", None)
                if policy is not None:
                    try:
                        await policy.report_success(name)
                    except Exception:  # pragma: no cover — defensive
                        logger.exception("policy.report_success failed")
                return response, f"{via_prefix}:{name}"
            except _VKApiCallError as e:
                last_error = VKPublishError(e.code, e.message)
                if e.code in _PUBLISH_ROTATE_CODES:
                    logger.warning(
                        "publish-token %s failed with code %s on %s — rotating",
                        name,
                        e.code,
                        method,
                    )
                    policy = getattr(self, "_policy", None)
                    if policy is not None:
                        try:
                            await policy.report_error(name, e.code)
                        except Exception:  # pragma: no cover — defensive
                            logger.exception("policy.report_error failed")
                    self._drop_active_publish_candidate()
                    continue
                # Не-ротируемый код — поднимаем как обычно
                raise last_error from e
            except Exception as e:  # network / other
                last_error = e
                logger.exception("publish-token %s: unexpected error, rotating", name)
                self._drop_active_publish_candidate()
                continue

        if last_error is not None:
            raise last_error
        raise RuntimeError(
            "VKPublisher: no publish-token available for "
            f"{method} (whitelist пуст или все в cooldown)."
        )

    @classmethod
    async def _enforce_publish_token_rate_limit(cls) -> None:
        """Throttle calls that go through the publish-token (VALSTAN).

        Shared across all VKPublisher instances in the process via class-vars,
        because every VKPublisher() in this codebase uses the same publish-token
        from `get_publish_token()`. Multiple parallel Celery tasks would each
        wait their turn — currently the worker runs `-c 1` so this is just a
        time gate, not a contention lock, but the asyncio.Lock makes it correct
        if concurrency is bumped later.
        """
        async with cls._publish_token_lock:
            now = datetime.now()
            last = cls._last_publish_token_call
            if last is not None:
                elapsed = (now - last).total_seconds()
                wait = cls.GLOBAL_PUBLISH_INTERVAL_SECONDS - elapsed
                if wait > 0:
                    logger.debug(
                        "Publish-token global rate-limit: waiting %.2fs",
                        wait,
                    )
                    await asyncio.sleep(wait)
            cls._last_publish_token_call = datetime.now()

    async def _invoke(self, target_client, method: str, params: Dict[str, Any]) -> Dict:
        """VK API call с повтором на отказе шлюза; raises ``_VKApiCallError``.

        **Повторяем только то, что ничего не создаёт.** HTTP 5xx от ВК означает
        «шлюз не ответил», а не «ВК не выполнил»: ответ мог потеряться уже после
        того, как запись прошла. Для чтения это неважно — повтор вернёт то же
        самое; для ``wall.post`` слепой повтор — прямой путь к дублю в живом
        канале, ровно тот класс #218, который 27.08 дал одинаковый пост каждые
        полчаса в @gonba_life.

        Поэтому у записей ошибка не гасится, а доезжает наверх с признаком
        ``transient``: вызывающий говорит человеку «ВК временно недоступен,
        повторите» и повторяет по его команде, когда состояние уже видно. Это
        не полумера, а разделение: молча чинится то, что безопасно чинить молча.
        """
        attempts = _TRANSIENT_RETRIES + 1 if _is_retry_safe_method(method) else 1
        for attempt in range(attempts):
            try:
                return await self._invoke_once(target_client, method, params)
            except _VKApiCallError as e:
                if not e.transient or attempt == attempts - 1:
                    raise
                delay = _TRANSIENT_BACKOFF_SECONDS * (attempt + 1)
                logger.warning(
                    "%s: отказ шлюза ВК (%s), повтор %d/%d через %.1f с",
                    method,
                    e.message,
                    attempt + 1,
                    attempts - 1,
                    delay,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _invoke_once(self, target_client, method: str, params: Dict[str, Any]) -> Dict:
        """Single VK API call; raises _VKApiCallError on VK error payload."""
        if hasattr(target_client, "api_call"):
            import asyncio
            import inspect

            api_call_method = getattr(target_client, "api_call")
            if inspect.iscoroutinefunction(api_call_method):
                response = await api_call_method(method, params)
            else:
                response = await asyncio.get_event_loop().run_in_executor(
                    None, api_call_method, method, params
                )
        elif hasattr(target_client, "method"):
            # Сырая сессия vk_api: ApiHttpError прилетает ИСКЛЮЧЕНИЕМ, а не
            # словарём, и мимо всей обработки ниже. Приводим к тому же классу,
            # что и путь через api_call, — иначе признак transient был бы виден
            # только у половины клиентов.
            try:
                response = target_client.method(method, params)
            except _api_http_error_cls() as e:
                raise _VKApiCallError(code=0, message=str(e), transient=True) from e
        else:
            raise NotImplementedError("VK client doesn't support API calls")

        if isinstance(response, dict) and "error" in response:
            err = response.get("error", {}) or {}
            message = str(err.get("error_msg") or "Unknown error")
            code = int(err.get("error_code") or 0)
            transient = bool(err.get("transient"))
            if code == 0:
                # VKClient.api_call historically returned {'error': {'error_msg': str(ApiError)}}
                # without an explicit error_code. The string form starts with "[NN] ..." —
                # parse it as a fallback so the retry-on-15/27 logic still works.
                import re

                m = re.match(r"^\[(\d+)\]", message)
                if m:
                    code = int(m.group(1))
            raise _VKApiCallError(code=code, message=message, transient=transient)

        if isinstance(response, dict) and "response" in response:
            return response["response"]
        return response

    async def _enforce_rate_limit(self, group_id: int):
        """Enforce minimum interval between posts to same group."""
        last_post_time = self._last_post_time.get(group_id)

        if last_post_time:
            elapsed = (datetime.now() - last_post_time).total_seconds()
            if elapsed < self.POST_INTERVAL_SECONDS:
                wait_time = self.POST_INTERVAL_SECONDS - elapsed
                logger.info(f"⏳ Rate limiting: waiting {wait_time:.1f}s before next post")
                await asyncio.sleep(wait_time)

    def is_test_mode(self) -> bool:
        """Check if running in test polygon mode."""
        return self.test_polygon_mode

    def get_posts_remaining_today(self, group_id: int) -> int:
        """
        Get remaining posts that can be published today.

        This is a simplified check - in production would track via DB.
        """
        # Simplified - would need actual tracking in production
        return self.POSTS_PER_DAY_LIMIT

    async def get_group_info(self, group_id: int) -> Optional[Dict[str, Any]]:
        """Fetch VK group info via groups.getById.

        Returns dict with id/name/screen_name/type/url, or None on failure.
        Sign of group_id is irrelevant — VK groups.getById expects positive id.

        VK API contract: param is ``group_ids`` (plural — comma-separated list
        of ids or a single id as string). Old singular ``group_id`` is rejected
        with error 100 since the v5.x deprecation. Zero / negative-after-abs
        ``group_id=0`` is short-circuited to None to avoid a guaranteed VK
        validation failure when ``VK_TEST_GROUP_ID`` env-var is unset.
        """
        positive_id = abs(int(group_id))
        if not positive_id:
            return None
        try:
            response = await self._invoke(
                self.vk_client, "groups.getById", {"group_ids": str(positive_id)}
            )
        except Exception as e:
            logger.error("groups.getById(%s) failed: %s", positive_id, e)
            return None

        items = response
        if isinstance(response, dict) and "groups" in response:
            items = response["groups"]
        if not items:
            return None

        group = items[0]
        return {
            "id": group["id"],
            "name": group["name"],
            "screen_name": group["screen_name"],
            "type": group["type"],
            "url": f"https://vk.com/{group['screen_name']}",
        }

    @staticmethod
    def get_target_group_id(region_code: str, mode: str = "test") -> Optional[int]:
        """Resolve target group id for a region.

        Args:
            region_code: region code like ``mi``, ``nolinsk``.
            mode: ``test`` → test polygon group, anything else → region's main group.

        Returns None if the region is unknown.
        """
        from modules.region_config import RegionConfigManager

        if mode == "test":
            return RegionConfigManager.get_main_group_id("test")
        return RegionConfigManager.get_main_group_id(region_code)

    async def publish_aggregated_post(
        self, bulletin: "AggregatedPost", group_id: int
    ) -> Dict[str, Any]:
        """Publish an ``AggregatedPost`` produced by NewsAggregator.

        Thin wrapper over ``publish_bulletin`` that pulls ``bulletin.aggregated_text``
        and emits a couple of useful log lines. Attachments are not extracted —
        ``publish_bulletin`` accepts them explicitly if a caller needs media.
        """
        try:
            text = bulletin.aggregated_text
        except AttributeError as e:
            return {"success": False, "error": f"bulletin missing aggregated_text: {e}"}

        logger.info(
            "Publishing aggregated post to %s (sources=%s, views=%s, likes=%s)",
            group_id,
            getattr(bulletin, "sources_count", "?"),
            getattr(bulletin, "total_views", "?"),
            getattr(bulletin, "total_likes", "?"),
        )

        return await self.publish_bulletin(group_id=group_id, text=text)

    @staticmethod
    def _normalize_group_owner_id(group_id: int) -> int:
        """
        Normalize region VK group ID to owner_id format expected by wall.post/wall.repost.

        In DB and migration scripts IDs are sometimes stored as positive numbers.
        VK wall methods for groups require negative owner_id.
        """
        gid = int(group_id)
        return -abs(gid)


class _VKApiCallError(Exception):
    def __init__(self, code: int, message: str, *, transient: bool = False):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        # ``transient`` — отказ ШЛЮЗА ВК (HTTP 5xx), а не ответ API. Отдельный
        # признак, а не синтетический код: код 0 в этом классе означает «ВК
        # ответил без error_code» (так приходит капча), и слить эти два случая
        # значило бы снова потерять различие, ради которого признак и заведён.
        self.transient = bool(transient)


class VKPublishError(Exception):
    """Ошибка записи в ВК с СОХРАНЁННЫМ числовым кодом.

    Зачем. Внутри публикатора код известен точно (``_VKApiCallError.code``), но
    наружу до сих пор поднималось голое ``Exception(f"VK API error: {msg}")`` —
    и целое число терялось на границе. Единственный, кто добывал его обратно, —
    модуль раскрутки, регуляркой по тексту; остальные шесть публикующих путей
    видели строку и писали её в лог. Пока код не доезжает до вызывающего, любая
    реакция на 9/14/214/219 невозможна в принципе: это корень техдолга
    «коды VK не обрабатывает никто», а не сами реакции.

    ⚠️ ``str(...)`` обязан остаться байт-в-байт прежним — ``VK API error:
    [<код>] <текст>``. На этот формат завязаны двое: ``web/api/ad_cabinet``
    пишет строку в ``error_message`` карточки, а ``modules/promotion/vk_errors``
    вынимает из неё код регуляркой ``^\\[(\\d+)\\]``. Смена формата сломала бы
    раскрутку молча, поэтому здесь не «улучшаем сообщение», а только добавляем
    рядом атрибут.
    """

    def __init__(self, code: int, message: str, *, transient: bool = False):
        super().__init__(f"VK API error: {message}")
        self.code = int(code or 0)
        self.message = message
        # Отказ шлюза ВК (HTTP 5xx): кода нет, повторять имеет смысл, а вот
        # менять токен — нет. Признак идёт рядом со строкой, потому что саму
        # строку трогать нельзя (см. предупреждение выше).
        self.transient = bool(transient)


def _vk_error_code_of(exc: BaseException) -> Optional[int]:
    """Числовой код ВК из исключения. ``None`` — код неизвестен.

    Ноль наружу не отдаём намеренно: ``_invoke`` ставит ``code=0``, когда ВК
    прислал ошибку без ``error_code`` (так приходит, например, капча), и
    сетевые сбои кода не имеют вовсе. «Код 0» в отчёте выглядел бы как реальный
    ответ ВК — а это отсутствие ответа.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code > 0:
        return code
    return None
