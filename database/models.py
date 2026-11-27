"""
SQLAlchemy models for SETKA project
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    literal_column,
    text,
)
from sqlalchemy.orm import relationship

from database.connection import Base


class Region(Base):
    """Регион в иерархии strana → oblast → raion.

    Тип задаётся полем ``kind``:
      * ``raion``  — район (низший уровень). Источники сводки — записи в
        ``communities`` с ``region_id = region.id`` (текущая логика).
      * ``oblast`` — область. Источники — ``vk_group_id`` подчинённых районов
        (``parent_region_id = region.id``). Каскадная логика в
        ``modules/cascaded_bulletin.py``.
      * ``strana`` — страна. Источники — ``vk_group_id`` подчинённых областей.

    ``parent_region_id`` — FK self-ref на родителя в иерархии (NULL для strana
    и для legacy-районов без области). См. ``docs/REGIONS_HIERARCHY.md``.
    """

    __tablename__ = "regions"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(
        String(50), unique=True, nullable=False, index=True
    )  # mi, nolinsk, arbazh, kirov_obl
    name = Column(String(200), nullable=False)  # МАЛМЫЖ - ИНФО

    # Иерархия (миграция 015) — тип региона и родитель.
    kind = Column(String(20), nullable=False, default="raion", index=True)
    parent_region_id = Column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # VK and Telegram
    vk_group_id = Column(Integer, nullable=True)  # ID группы VK для публикации
    telegram_channel = Column(String(100), nullable=True)  # @malmig_info

    # Geo (для модуля авто-регистрации сообществ, миграция 011)
    vk_city_id = Column(Integer, nullable=True)  # VK API city_id (database.getCities)
    center_city = Column(String(200), nullable=True)  # human-readable: "Малмыж"

    # Neighboring regions
    neighbors = Column(String(500), nullable=True)  # советск,лебяж,уржум

    # Hashtags
    local_hashtags = Column(Text, nullable=True)

    # Configuration
    config = Column(JSON, nullable=True)  # Дополнительные настройки

    # Discovery (миграция 013) — когда последний раз искали новые сообщества.
    # NULL = discovery никогда не запускался для региона. Обновляется при
    # успехе ``POST /api/discovery/trigger``.
    last_discovery_at = Column(DateTime, nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    communities = relationship("Community", back_populates="region")
    posts = relationship("Post", back_populates="region")
    candidates = relationship("CommunityCandidate", back_populates="region")
    parent = relationship(
        "Region",
        remote_side="Region.id",
        back_populates="children",
        foreign_keys=[parent_region_id],
    )
    children = relationship(
        "Region",
        back_populates="parent",
        foreign_keys=[parent_region_id],
    )

    def __repr__(self):
        return f"<Region {self.code}: {self.name} kind={self.kind}>"


class Community(Base):
    """Сообщество VK для мониторинга"""

    __tablename__ = "communities"

    id = Column(Integer, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id"), nullable=False)

    # VK info
    vk_id = Column(Integer, nullable=False, index=True)  # ID группы VK (отрицательное)
    screen_name = Column(String(100), nullable=True)  # short name
    name = Column(String(300), nullable=False)

    # Category
    category = Column(String(50), nullable=False, index=True)  # admin, novost, reklama, etc

    # Monitoring settings
    is_active = Column(Boolean, default=True)
    check_interval = Column(Integer, default=300)  # seconds
    last_checked = Column(DateTime, nullable=True)
    last_post_id = Column(Integer, nullable=True)  # ID последнего поста

    # Statistics
    posts_count = Column(Integer, default=0)
    errors_count = Column(Integer, default=0)

    # Health (миграция 011 — модуль авто-регистрации/recheck)
    health_status = Column(String(30), default="active", index=True)
    # active / dormant / dead / changed_category
    last_post_at = Column(DateTime, nullable=True)  # timestamp последнего поста на стене
    checked_at = Column(DateTime, nullable=True)  # когда последний раз делали health-check
    suggested_category = Column(String(50), nullable=True)  # если AI считает что category устарел
    # VK error_code последнего recheck'а (миграция 058, NULL = без ошибки):
    # машинный split dead — 18/100 «удалён» vs 15/203 «недоступен» (re-probe перед kill, cf #041)
    last_error_code = Column(Integer, nullable=True)

    # Dormant-политика (миграция 051): когда/почему выведено из парса.
    # disabled_reason: dormant_t1_auto / dead_migration_050 / NULL (ручное/старое)
    disabled_at = Column(DateTime, nullable=True)
    disabled_reason = Column(String(50), nullable=True)

    # Telegram repost target (миграция 020, Flow B) — канал + ИМЯ бота
    # (токен только в env, pool #008). NULL = сообщество не зеркалится в TG.
    telegram_channel = Column(String(100), nullable=True)  # "@gonba_life" / chat_id
    telegram_bot = Column(String(50), nullable=True)  # ключ в TELEGRAM_TOKENS, напр. "VALSTANBOT"

    # Размер донорского сообщества (миграция 087) — сырьё ранжирования аутрича и
    # оценки «сколько людей в районе вообще можно достать». NULL значит «не мерили»,
    # 0 — «пустая группа»; дефолта 0 нет сознательно (конвенция миграции 080).
    members_count = Column(Integer, nullable=True)
    members_checked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    region = relationship("Region", back_populates="communities")
    posts = relationship("Post", back_populates="community")

    __table_args__ = (
        Index("ix_communities_region_category", "region_id", "category"),
        # Композитный индекс (region_id, vk_id) — для discovery exclude-filter.
        # НЕ unique: одна VK-группа может быть в communities несколько раз с
        # разными category (см. database/migrations/011 — комментарий к индексу).
        Index("idx_communities_region_vk", "region_id", "vk_id"),
    )

    def __repr__(self):
        return f"<Community {self.name} ({self.category})>"


class RegionMemberSnapshot(Base):
    """Дневной снимок подписчиков ГЛАВНОЙ ИНФО-группы региона (миграция 033).

    Копится суточной beat-таской `collect_member_snapshots` (groups.getById
    fields=members_count по `regions.vk_group_id` активных регионов). Иммутабелен;
    один снимок на (region_id, день) — повторный прогон за день перезаписывает
    count (ON CONFLICT). Основа графика роста подписчиков (owner-request
    2026-06-05). Учитываем **только** главные группы (куда выпускаем сводки),
    а не весь пул источников — чтобы не жечь VK API (миграция 033 заменила
    per-community `community_member_snapshots`).
    """

    __tablename__ = "region_member_snapshots"

    id = Column(BigInteger, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)
    members_count = Column(Integer, nullable=False)
    snapshot_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index(
            "uq_region_member_snapshot_day",
            "region_id",
            "snapshot_date",
            unique=True,
        ),
    )

    def __repr__(self):
        return (
            f"<RegionMemberSnapshot r={self.region_id} "
            f"{self.snapshot_date} n={self.members_count}>"
        )


class OblastUniqueMemberSnapshot(Base):
    """Еженедельный снимок УНИКАЛЬНЫХ подписчиков области без дублей (миграция 034).

    Для каждой области (``kind='oblast'``) объединяем множества member-id всех её
    главных ИНФО-групп (сама область + районы, ``parent_region_id=oblast.id``)
    через ``groups.getMembers`` и считаем уникальных. Позволяет сравнивать
    «чистый» охват областей: сумма ``members_count`` по группам завышена (человек,
    подписанный на N групп области, учтён N раз). Копится еженедельной ночной
    beat-таской — getMembers по ~16 главным группам дёшев (1000 id/запрос).
    Иммутабелен; один снимок на (oblast, день), повторный прогон перезаписывает
    (ON CONFLICT). ``group_count`` — сколько групп реально вошло (закрытые
    пропущены), ``total_with_dupes`` — сумма |members| (для коэффициента дублей).
    """

    __tablename__ = "oblast_unique_member_snapshots"

    id = Column(BigInteger, primary_key=True, index=True)
    oblast_region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)
    unique_count = Column(Integer, nullable=False)
    total_with_dupes = Column(Integer, nullable=False)
    group_count = Column(Integer, nullable=False)
    snapshot_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index(
            "uq_oblast_unique_member_snapshot_day",
            "oblast_region_id",
            "snapshot_date",
            unique=True,
        ),
    )

    def __repr__(self):
        return (
            f"<OblastUniqueMemberSnapshot o={self.oblast_region_id} "
            f"{self.snapshot_date} uniq={self.unique_count}/{self.total_with_dupes}>"
        )


class CommunityCandidate(Base):
    """Кандидат на добавление в communities — буфер discovery до approve.

    Заполняется taskами `modules.discovery` (groups.search по гео + ключевикам,
    AI-категоризация через DeepSeek). Модератор через UI `/regions/<code>/discovery`
    одним кликом переводит ``approved`` → запись копируется в `communities`,
    либо ``rejected`` / ``deferred``. См. DEV_HISTORY 2026-05-22 (big idea).
    """

    __tablename__ = "community_candidates"

    id = Column(Integer, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)

    # VK group snapshot (на момент discovery)
    vk_id = Column(Integer, nullable=False)  # abs(group_id), положительный
    name = Column(String(300), nullable=False)
    screen_name = Column(String(100), nullable=True)
    photo_url = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    members_count = Column(Integer, nullable=True)

    # AI suggestions
    ai_category = Column(String(50), nullable=True)
    ai_confidence = Column(Integer, nullable=True)  # 0-100
    ai_reasoning = Column(Text, nullable=True)
    ai_is_info_page = Column(Boolean, default=False)
    # Геопринадлежность району (миграция 012). NULL — ещё не оценено,
    # TRUE/FALSE — явный ответ нейросети или модератора. UI фильтрует.
    ai_is_relevant = Column(Boolean, nullable=True, default=None)

    # Moderation
    status = Column(String(20), nullable=False, default="pending", index=True)
    # pending / approved / rejected / deferred

    # Source (для дебага: каким запросом нашли)
    discovered_via = Column(String(80), nullable=True)
    # 'geo_search', 'kw:novosti', 'kw:dtp', 'reposts_of_main', ...

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    region = relationship("Region", back_populates="candidates")

    __table_args__ = (
        Index("uq_candidates_region_vk", "region_id", "vk_id", unique=True),
        Index("idx_candidates_status_region", "status", "region_id"),
    )

    def __repr__(self):
        return f"<CommunityCandidate {self.vk_id} {self.name!r} [{self.status}]>"

    def to_dict(self):
        return {
            "id": self.id,
            "region_id": self.region_id,
            "vk_id": self.vk_id,
            "name": self.name,
            "screen_name": self.screen_name,
            "photo_url": self.photo_url,
            "description": self.description,
            "members_count": self.members_count,
            "ai_category": self.ai_category,
            "ai_confidence": self.ai_confidence,
            "ai_reasoning": self.ai_reasoning,
            "ai_is_info_page": self.ai_is_info_page,
            "ai_is_relevant": self.ai_is_relevant,
            "status": self.status,
            "discovered_via": self.discovered_via,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Post(Base):
    """Пост из VK"""

    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id"), nullable=False)
    community_id = Column(Integer, ForeignKey("communities.id"), nullable=False)

    # VK post info
    vk_post_id = Column(Integer, nullable=False)
    vk_owner_id = Column(Integer, nullable=False)

    # Content
    text = Column(Text, nullable=True)
    attachments = Column(JSON, nullable=True)  # photos, videos, links

    # Metadata
    date_published = Column(DateTime, nullable=False)

    # Statistics from VK
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    reposts = Column(Integer, default=0)
    comments = Column(Integer, default=0)

    # AI Analysis
    ai_category = Column(String(50), nullable=True)  # novost, reklama, etc
    ai_relevance = Column(Integer, nullable=True)  # 0-100
    ai_score = Column(Integer, nullable=True)  # Общая оценка
    ai_analyzed = Column(Boolean, default=False)
    ai_analysis_date = Column(DateTime, nullable=True)

    # Sentiment Analysis
    sentiment_label = Column(String(20), nullable=True)  # positive, neutral, negative
    sentiment_score = Column(Float, nullable=True)  # 0.0-1.0
    sentiment_emotions = Column(JSON, nullable=True)  # {joy, sadness, anger, fear}

    # Publishing status
    # NB: индекс по status объявлен явно в __table_args__ (ix_posts_status).
    # НЕ добавлять index=True сюда — иначе SQLAlchemy создаст второй одноимённый
    # индекс и create_all() на чистой БД упадёт DuplicateTableError.
    status = Column(String(20), default="new")  # new, analyzed, approved, published, rejected
    published_at = Column(DateTime, nullable=True)
    published_vk = Column(Boolean, default=False)
    published_telegram = Column(Boolean, default=False)
    published_wordpress = Column(Boolean, default=False)

    # Fingerprints for deduplication (inspired by Postopus)
    fingerprint_lip = Column(
        String(50), nullable=True, index=True
    )  # Структурный: "owner_id_post_id"
    fingerprint_media = Column(JSON, nullable=True)  # [photo_id1, photo_id2, video_id1]
    fingerprint_text = Column(String(100), nullable=True, index=True)  # Hash полного "рафинада"
    fingerprint_text_core = Column(
        String(100), nullable=True, index=True
    )  # Hash "сердцевины" (20-70%)

    # Flags
    is_duplicate = Column(Boolean, default=False)
    is_spam = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    region = relationship("Region", back_populates="posts")
    community = relationship("Community", back_populates="posts")

    __table_args__ = (
        Index("ix_posts_vk_id", "vk_owner_id", "vk_post_id"),
        Index("ix_posts_status", "status"),
        Index("ix_posts_region_status", "region_id", "status"),
        Index("ix_posts_date", "date_published"),
    )

    def __repr__(self):
        return f"<Post {self.vk_owner_id}_{self.vk_post_id}>"


class Filter(Base):
    """Фильтры для контента"""

    __tablename__ = "filters"

    id = Column(Integer, primary_key=True, index=True)

    # Filter info
    type = Column(
        String(50), nullable=False, index=True
    )  # blacklist_word, spam_pattern, region_word
    category = Column(String(50), nullable=True)  # admin, novost, etc

    # Pattern/word
    pattern = Column(Text, nullable=False)

    # Action
    action = Column(String(20), nullable=False)  # delete, flag, score
    score_modifier = Column(Integer, default=0)  # Изменение оценки

    # Metadata
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Filter {self.type}: {self.pattern[:30]}>"


class VKToken(Base):
    """VK токены для динамического управления.

    Если ``community_id`` задан — это community access token, привязанный к
    конкретному сообществу (используется для ``messages.getConversations`` без
    группового scope у user-токена). Хранится как ``abs(group_id)``.

    Поля ``disabled_until`` / ``last_error_code`` / ``consecutive_errors``
    добавлены миграцией 014 — описывают «динамическое» состояние токена для
    :class:`modules.vk_token_router.TokenPolicy` (cooldown после VK error 5/17/29,
    ручное отключение на N часов через ``/api/tokens/{name}/disable``).
    """

    __tablename__ = "vk_tokens"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(
        String(50), unique=True, nullable=False, index=True
    )  # VALSTAN, OLGA, VITA, COMM_158787639...
    token = Column(Text, nullable=False)  # VK API токен
    community_id = Column(
        Integer, nullable=True, index=True
    )  # abs(vk_group_id), если это community token
    is_active = Column(Boolean, default=True, index=True)  # Активен ли токен (hard-флаг)
    last_used = Column(DateTime, nullable=True)  # Последнее использование
    last_validated = Column(DateTime, nullable=True)  # Последняя валидация
    validation_status = Column(String(20), default="unknown", index=True)  # valid, invalid, unknown
    error_message = Column(Text)  # Сообщение об ошибке при валидации
    permissions = Column(JSON)  # Права доступа токена
    user_info = Column(JSON)  # Информация о пользователе / community-info для community-токенов

    # --- TokenPolicy (миграция 014) ---
    disabled_until = Column(DateTime, nullable=True)
    last_error_code = Column(Integer, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    consecutive_errors = Column(Integer, nullable=False, default=0)

    # --- Роль (миграция 023): 'publish' = разрешено публиковать (доп. к env). ---
    role = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return (
            f"<VKToken(name='{self.name}', status='{self.validation_status}', "
            f"active={self.is_active}, community_id={self.community_id})>"
        )

    def to_dict(self):
        """Преобразовать в словарь для API"""
        return {
            "id": self.id,
            "name": self.name,
            "token": (
                self.token[:20] + "..." if len(self.token) > 20 else self.token
            ),  # Маскируем токен
            "community_id": self.community_id,
            "is_active": self.is_active,
            "last_used": self.last_used.isoformat() if self.last_used else None,
            "last_validated": (self.last_validated.isoformat() if self.last_validated else None),
            "validation_status": self.validation_status,
            "error_message": self.error_message,
            "permissions": (
                self.permissions
                if isinstance(self.permissions, list)
                else (self.permissions.get("permissions", []) if self.permissions else [])
            ),
            "user_info": self.user_info,
            "disabled_until": (self.disabled_until.isoformat() if self.disabled_until else None),
            "last_error_code": self.last_error_code,
            "last_error_at": (self.last_error_at.isoformat() if self.last_error_at else None),
            "consecutive_errors": int(self.consecutive_errors or 0),
            "role": self.role,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def get_full_token(self):
        """Получить полный токен (для внутреннего использования)"""
        return self.token


class GatewayKey(Base):
    """API-ключ потребителя VK-шлюза (``/api/gateway``) — БД как единый источник.

    Миграция 059 (мандат brain 2026-07-12, паттерн #072): ключи живут в БД,
    env ``GATEWAY_KEY_<PROJECT>`` — только bootstrap-fallback. Семантика merge
    как у ``vk_tokens`` (#336): env добавляет лишь имена, которых нет в БД;
    выключенный в БД ключ env НЕ воскрешает. Выдача/ротация — через
    ``scripts/issue_gateway_key.py`` (событие пишется в usage-лог шлюза).

    Миграции 074/075 (мандат brain 2026-08-01, 🔴): секрет **не хранится**.
    В БД лежит ``secret_sha256`` (по нему идёт проверка, см.
    ``modules/gateway/keys.py``) и ``secret_prefix`` — несколько первых
    символов для опознания ключа человеком. Колонка ``secret`` осталась
    занулённой на один цикл деплоя и удаляется миграцией 076.
    """

    __tablename__ = "gateway_keys"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, nullable=False, index=True)  # KAZANSKAYA, GONBA...
    secret = Column(Text, nullable=True)  # DEPRECATED (075 занулила, 076 удалит колонку)
    secret_sha256 = Column(String(64), nullable=True, index=True)  # hex sha256 — по нему проверка
    secret_prefix = Column(String(16), nullable=True)  # первые символы, для опознания человеком
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    note = Column(Text, nullable=True)  # кто/зачем (заявка, письмо brain)
    # D-047 (миграция 085): разрешённые цели ключа. NULL/[] = не привязан →
    # отказ по owner-scoped методам (modules/gateway/scope.py).
    allowed_owner_ids = Column(JSON, nullable=True)  # list[int], подписанные owner_id
    allowed_screen_names = Column(JSON, nullable=True)  # list[str], lowercase
    created_at = Column(DateTime, default=datetime.utcnow)
    rotated_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<GatewayKey(name='{self.name}', active={self.is_active})>"


class MessageTemplate(Base):
    """Шаблон ответа на сообщение сообщества (этап 4b).

    Используется UI `/templates` для CRUD и dropdown'ом в модалке ответа
    на VK direct message. Шаблоны общие на все регионы — это сознательно:
    модератор обычно один, и шаблоны типа «спасибо за обращение, передадим»
    не зависят от региона. Если когда-то понадобится per-region — добавим
    region_id nullable + UI-фильтр.
    """

    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(120), nullable=False)
    body = Column(Text, nullable=False)
    category = Column(
        String(50), nullable=True, index=True
    )  # 'greeting', 'thanks', 'redirect', ...
    is_active = Column(Boolean, default=True, index=True)
    # region_id (миграция 024): NULL = общий шаблон (все регионы); иначе —
    # привязан к региону. ON DELETE SET NULL: удаление региона делает шаблон общим.
    region_id = Column(
        Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<MessageTemplate {self.id} {self.title!r}>"

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "category": self.category,
            "is_active": self.is_active,
            "region_id": self.region_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AdRequest(Base):
    """Заявка рекламного кабинета — реклама, пойманная в предложке сообщества.

    scanner (`modules/ad_cabinet/scanner.py`) детектит рекламу в предложенных
    постах главных групп регионов (`AdvertisementFilter` + предложка-сигналы) и
    складывает сюда. UI `/ad-cabinet` готовит персонализированный ответ
    (`modules/ad_cabinet/message_builder.py`) для отправки автору в 1 клик.

    Жизненный цикл (`status`) переживает рескан, когда предложенный пост уже
    опубликован/удалён — поэтому Postgres, а не Redis-снимки notifications.
    Forward-compatible с CRM фазы 3 (клиенты/оплаты по `author_vk_id`).
    Миграция 021.
    """

    __tablename__ = "ad_requests"

    id = Column(BigInteger, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True)

    # Источник заявки: 'suggested' (предложка) | 'inbound_dm' (входящие ЛС). Блок A.
    origin = Column(String(20), nullable=False, default="suggested", index=True)

    # VK source
    community_vk_id = Column(BigInteger, nullable=False)  # owner_id группы (отрицательный)
    community_name = Column(String(300), nullable=True)  # снимок для подстановки/устойчивости
    vk_post_id = Column(
        BigInteger, nullable=True
    )  # id предложенного поста (стабилен, пока pending); NULL для inbound_dm
    last_message_id = Column(BigInteger, nullable=True)  # id последнего входящего ЛС (inbound_dm)

    # Author / messaging target
    author_vk_id = Column(BigInteger, nullable=True)  # from_id (signed; neg=группа)
    signer_id = Column(BigInteger, nullable=True)  # человек-автор (если подписан)
    peer_id = Column(BigInteger, nullable=True)  # цель для ЛС (обычно user)
    author_name = Column(
        String(300), nullable=True
    )  # "Имя Фамилия"/имя группы; NULL если не резолвится
    author_is_group = Column(Boolean, nullable=False, default=False)

    # Post snapshot
    text_snapshot = Column(Text, nullable=True)
    attachments_json = Column(JSON, nullable=True)
    photo_urls_json = Column(JSON, nullable=True)  # прямые CDN-ссылки картинок поста (показ)

    # Classification
    score = Column(Integer, nullable=False, default=0)
    reasons_json = Column(JSON, nullable=True)  # list[str] причины

    # Lifecycle
    status = Column(
        String(20), nullable=False, default="new", index=True
    )  # new|contacted|skipped|published

    # Единый роутер входящих ЛС (Этап 1, миграция 032). Где сейчас «живёт» сообщение
    # (инвариант R1: ровно одно текущее место) и наш собственный статус обработки
    # (R2 — источник истины вместо VK read/unread). Для предложки оба остаются на
    # дефолтах (заявка всегда в кабинете).
    route = Column(
        String(16), nullable=False, default="ad_cabinet", index=True
    )  # ad_cabinet|notifications
    handling_status = Column(String(16), nullable=False, default="new")  # new|in_progress|done
    handled_at = Column(DateTime, nullable=True)  # когда оператор пометил обработанным

    # CRM (блок C): к какому клиенту привязана заявка (миграция 027). FK SET NULL.
    client_id = Column(BigInteger, ForeignKey("ad_clients.id", ondelete="SET NULL"), nullable=True)

    # Messaging permission cache (isMessagesFromGroupAllowed)
    can_message = Column(Boolean, nullable=True)
    can_message_checked_at = Column(DateTime, nullable=True)

    # Prepared reply
    template_id = Column(Integer, nullable=True)
    prepared_message = Column(Text, nullable=True)
    message_attachments = Column(Text, nullable=True)  # "photo<o>_<id>,..." кэш после загрузки

    # Send audit
    via = Column(String(30), nullable=True)  # community-token|user-token|personal
    vk_message_id = Column(BigInteger, nullable=True)

    detected_at = Column(DateTime, default=datetime.utcnow)
    contacted_at = Column(DateTime, nullable=True)
    # Авто-приветствие рекламодателю (улучшение отклика 2026-06-13, миграция 043):
    # момент авто-ответа на новую заявку. NULL — ещё не приветствовали; ставится
    # один раз (идемпотентность) фоновой таской auto-greet-ad-requests.
    greeting_sent_at = Column(DateTime, nullable=True)
    # Уведомление в мессенджер MAX (пилот #386, миграция 116): момент отправки
    # карточки заявки боту «САРАФАН». NULL — ещё не отправляли. Отдельная метка
    # от greeting_sent_at: то приветствие рекламодателю, это уведомление владельцу.
    max_notified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return (
            f"<AdRequest {self.id} comm={self.community_vk_id} "
            f"post={self.vk_post_id} {self.status}>"
        )

    def to_dict(self):
        return {
            "id": self.id,
            "region_id": self.region_id,
            "origin": self.origin,
            "community_vk_id": self.community_vk_id,
            "community_name": self.community_name,
            "vk_post_id": self.vk_post_id,
            "last_message_id": self.last_message_id,
            "author_vk_id": self.author_vk_id,
            "signer_id": self.signer_id,
            "peer_id": self.peer_id,
            "author_name": self.author_name,
            "author_is_group": self.author_is_group,
            "text_snapshot": self.text_snapshot,
            "attachments_json": self.attachments_json,
            "photo_urls_json": self.photo_urls_json,
            "score": self.score,
            "reasons_json": self.reasons_json,
            "status": self.status,
            "route": self.route,
            "handling_status": self.handling_status,
            "handled_at": self.handled_at.isoformat() if self.handled_at else None,
            "client_id": self.client_id,
            "can_message": self.can_message,
            "can_message_checked_at": (
                self.can_message_checked_at.isoformat() if self.can_message_checked_at else None
            ),
            "template_id": self.template_id,
            "prepared_message": self.prepared_message,
            "message_attachments": self.message_attachments,
            "via": self.via,
            "vk_message_id": self.vk_message_id,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "contacted_at": (self.contacted_at.isoformat() if self.contacted_at else None),
            "greeting_sent_at": (
                self.greeting_sent_at.isoformat() if self.greeting_sent_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            # Производные для UI:
            "vk_post_url": (
                f"https://vk.com/wall{self.community_vk_id}_{self.vk_post_id}"
                if self.community_vk_id and self.vk_post_id
                else None
            ),
            "author_url": self._author_url(),
            "dialog_url": self._dialog_url(),
        }

    def _dialog_url(self):
        """Ссылка на диалог автора в менеджере сообщений сообщества (inbound_dm).

        ``vk.com/gim{group}?sel={peer}`` открывает входящие сообщества с фокусом
        на нужном диалоге — оператор отвечает в VK, если кабинет не смог.
        """
        if self.origin != "inbound_dm" or not self.community_vk_id or not self.peer_id:
            return None
        return f"https://vk.com/gim{abs(int(self.community_vk_id))}?sel={int(self.peer_id)}"

    def _author_url(self):
        if self.author_is_group and self.author_vk_id:
            return f"https://vk.com/club{abs(int(self.author_vk_id))}"
        target = self.peer_id or self.author_vk_id
        if target and int(target) > 0:
            return f"https://vk.com/id{int(target)}"
        return None


class AdScheduledPost(Base):
    """Запланированный пост рекламного кабинета (VK-«Отложенные записи»).

    Оператор формирует график постов по датам в `/ad-cabinet` и отправляет их в
    VK-отложку целевого сообщества: VK сам публикует в назначенное время.
    Публикация — через ``VKPublisher.publish_bulletin(publish_date=…)`` (seam B1-a).

    ВАЖНО про время: ``publish_date`` хранится как МСК wall-clock (то, что ввёл
    оператор). В unix для VK конвертит API-слой (МСК = UTC+3, без DST).

    Forward-compatible с учётом (фаза C): ``client_id``/``price`` пока без FK.
    Миграция 025.
    """

    __tablename__ = "ad_scheduled_posts"
    # Планировщик предложки (миграция 095). Уникумы держат идемпотентность в БД:
    # один репост в одно сообщество на один оригинал; одна активная публикация
    # оригинала на заявку. Объявлены и для sqlite — тесты обязаны ловить дубль.
    __table_args__ = (
        Index(
            "uq_ad_sched_repost_target",
            "source_post_id",
            "community_vk_id",
            unique=True,
            postgresql_where=text("source_post_id IS NOT NULL"),
            sqlite_where=text("source_post_id IS NOT NULL"),
        ),
        Index(
            "uq_ad_sched_suggested_active",
            "source_ad_request_id",
            unique=True,
            postgresql_where=text("kind = 'suggested' AND status IN ('scheduled', 'published')"),
            sqlite_where=text("kind = 'suggested' AND status IN ('scheduled', 'published')"),
        ),
        Index("ix_ad_sched_due", "kind", "status", "next_attempt_at"),
        # Анти-спам «один рекламный пост клиента в одно сообщество в календарный
        # день МСК» держится БД, а не только чтением в busy_days (миграция 096,
        # аудит 2026-09-05: двойной сабмит проходил мимо проверки).
        Index(
            "uq_ad_sched_client_day_slot",
            "client_id",
            "community_vk_id",
            func.date(literal_column("publish_date")),
            unique=True,
            postgresql_where=text(
                "client_id IS NOT NULL AND status IN ('pending', 'scheduled', 'published')"
            ),
            sqlite_where=text(
                "client_id IS NOT NULL AND status IN ('pending', 'scheduled', 'published')"
            ),
        ),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    community_vk_id = Column(BigInteger, nullable=False)  # owner_id группы (отрицательный)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True)

    text = Column(Text, nullable=True)
    image_names = Column(JSON, nullable=True)  # выбранные офферные картинки (имена файлов)
    attachments = Column(Text, nullable=True)  # "photo<o>_<id>,…" после заливки на стену (кэш)

    publish_date = Column(DateTime, nullable=False)  # МСК wall-clock
    # Срок размещения (С2): когда авто-снять пост. МСК wall-clock naive (как
    # publish_date), nullable — нет срока → висит вечно. Миграция 041.
    expires_at = Column(DateTime, nullable=True)
    from_group = Column(Boolean, nullable=False, default=True)
    signed = Column(Boolean, nullable=False, default=False)
    pinned = Column(Boolean, nullable=False, default=False)  # закреп на сутки (099)
    comments_enabled = Column(Boolean, nullable=False, default=True)

    # Клиентский флоу (миграция 082) добавил значения:
    #   pending  — создан не-trusted клиентом, ждёт одобрения владельца,
    #              в VK НИЧЕГО не отправлено;
    #   rejected — отклонён владельцем (moderation_comment виден клиенту).
    # publish_reconciler не задевается: он выбирает только 'scheduled'.
    status = Column(
        String(20), nullable=False, default="draft", index=True
    )  # draft|pending|scheduled|published|failed|cancelled|rejected
    vk_postponed_post_id = Column(BigInteger, nullable=True)  # id в VK-отложке (отмена/трекинг)
    source_ad_request_id = Column(BigInteger, nullable=True)  # из какой заявки/предложки

    # Учёт (фаза C): FK довешен миграцией 082 — клиентская изоляция стоит на нём.
    client_id = Column(BigInteger, ForeignKey("ad_clients.id", ondelete="SET NULL"), nullable=True)
    price = Column(Numeric(12, 2), nullable=True)

    # Кабинет рекламодателя (миграция 082). created_by_user_id: кто создал пост
    # из кабинета, NULL = оператор (как раньше). order_ref: N районов одного
    # сабмита несут один uuid — только группировка UI, деньги не считаются.
    created_by_user_id = Column(
        Integer, ForeignKey("radar_users.id", ondelete="SET NULL"), nullable=True
    )
    order_ref = Column(String(36), nullable=True)
    moderated_at = Column(DateTime, nullable=True)
    moderation_comment = Column(Text, nullable=True)
    # Пакет, в счёт которого создан пост (миграция 086). NULL — обычный платный
    # заказ по прайсу. Терминальный не-published статус возвращает пост в пакет.
    package_id = Column(
        BigInteger, ForeignKey("ad_client_packages.id", ondelete="SET NULL"), nullable=True
    )

    # Планировщик предложки (миграция 095, план 2026-09-05):
    #   kind — 'post' (всё, что было) | 'suggested' (оригинал предложки: сам
    #          предложенный пост с подписью автора) | 'repost' (дублёр: wall.repost
    #          оригинала от имени сообщества-дублёра после его выхода);
    #   source_post_id — для 'repost' строка-оригинал;
    #   next_attempt_at — МСК wall-clock naive, когда диспетчер вправе взять
    #          строку; сдвигается вперёд как lease при claim;
    #   attempts — счётчик попыток (предел ретраев + UI).
    # У 'repost' vk_postponed_post_id пуст до выхода — реконсилер их не берёт.
    kind = Column(String(20), nullable=False, default="post", index=True)
    source_post_id = Column(
        BigInteger, ForeignKey("ad_scheduled_posts.id", ondelete="CASCADE"), nullable=True
    )
    next_attempt_at = Column(DateTime, nullable=True)
    attempts = Column(SmallInteger, nullable=False, default=0)

    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return (
            f"<AdScheduledPost {self.id} comm={self.community_vk_id} "
            f"@{self.publish_date} {self.status}>"
        )

    def to_dict(self):
        return {
            "id": self.id,
            "community_vk_id": self.community_vk_id,
            "region_id": self.region_id,
            "text": self.text,
            "image_names": self.image_names or [],
            "attachments": self.attachments,
            "publish_date": self.publish_date.isoformat() if self.publish_date else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "from_group": self.from_group,
            "signed": self.signed,
            "pinned": bool(self.pinned),
            "comments_enabled": self.comments_enabled,
            "status": self.status,
            "vk_postponed_post_id": self.vk_postponed_post_id,
            "source_ad_request_id": self.source_ad_request_id,
            "client_id": self.client_id,
            "price": float(self.price) if self.price is not None else None,
            "created_by_user_id": self.created_by_user_id,
            "order_ref": self.order_ref,
            "moderated_at": self.moderated_at.isoformat() if self.moderated_at else None,
            "moderation_comment": self.moderation_comment,
            "package_id": self.package_id,
            "kind": self.kind or "post",
            "source_post_id": self.source_post_id,
            "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            "attempts": int(self.attempts or 0),
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            # Производное для UI: ссылка на пост в VK-отложке (видна после публикации).
            "vk_post_url": (
                f"https://vk.com/wall{self.community_vk_id}_{self.vk_postponed_post_id}"
                if self.community_vk_id and self.vk_postponed_post_id
                else None
            ),
        }


class AdClient(Base):
    """Клиент-рекламодатель рекламного кабинета (блок C, CRM).

    Карточка заказчика, сводящая его заявки из предложки и ЛС в одну сущность по
    ключу ``author_vk_id``. Несёт воронку сделки (``stage``) и контактные данные.
    Оплаты и публикации висят на ней отдельными таблицами (``ad_payments`` /
    ``ad_publications``). Миграция 027.
    """

    __tablename__ = "ad_clients"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    author_vk_id = Column(
        BigInteger, nullable=True
    )  # VK id заказчика (neg=группа); ключ сведения; NULL — без VK
    author_is_group = Column(Boolean, nullable=False, default=False)
    name = Column(String(300), nullable=True)
    vk_url = Column(String(300), nullable=True)
    contact = Column(Text, nullable=True)  # телефон/почта/как связаться (заметки оператора)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True)
    # Воронка: detected|contacted|scheduled|published|paid|lost
    stage = Column(String(20), nullable=False, default="detected", index=True)
    notes = Column(Text, nullable=True)
    # Новые контактные поля (миграция 077)
    phone = Column(String(50), nullable=True)
    telegram = Column(String(100), nullable=True)
    email = Column(String(200), nullable=True)
    postal_address = Column(Text, nullable=True)
    # Дедуп Telegram-напоминания о перерасходе пакета (миграция 048). Ставится при
    # отправке алёрта, сбрасывается в NULL при новой оплате — «доплатил → можно
    # напомнить снова». NULL — ещё не напоминали / пакет в норме.
    spend_alerted_at = Column(DateTime, nullable=True)
    # Кабинет рекламодателя (миграция 081): аккаунт ЕСА, которому принадлежит
    # карточка. NULL — клиент в кабинет не входил. Линковка только по
    # VK-identity либо при самозаводе (advertiser_link.py) — никогда из ручного
    # ввода клиента. Partial-unique: один аккаунт — максимум одна карточка.
    radar_user_id = Column(
        Integer, ForeignKey("radar_users.id", ondelete="SET NULL"), nullable=True
    )
    # Модерация новых клиентов (решение владельца 2026-08-25): не-trusted
    # публикует через одобрение; после N одобренных постов — trusted.
    trusted = Column(Boolean, nullable=False, default=False)
    # Архив вместо удаления (миграция 096, аудит 2026-09-05): DELETE каскадом
    # стирал оплаты, пакеты и чат, а осиротевшие отложки выходили бесплатно.
    # Архивный клиент скрыт из списков, но история денег и постов цела.
    is_archived = Column(Boolean, nullable=False, default=False)
    approved_posts_count = Column(SmallInteger, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AdClient {self.id} vk={self.author_vk_id} {self.stage}>"

    def _vk_url(self):
        if self.vk_url:
            return self.vk_url
        if not self.author_vk_id:
            return None
        if self.author_is_group or int(self.author_vk_id) < 0:
            return f"https://vk.com/club{abs(int(self.author_vk_id))}"
        return f"https://vk.com/id{int(self.author_vk_id)}"

    def to_dict(self):
        return {
            "id": self.id,
            "author_vk_id": self.author_vk_id,
            "author_is_group": self.author_is_group,
            "name": self.name,
            "vk_url": self._vk_url(),
            "contact": self.contact,
            "region_id": self.region_id,
            "stage": self.stage,
            "notes": self.notes,
            "phone": self.phone,
            "telegram": self.telegram,
            "email": self.email,
            "postal_address": self.postal_address,
            "radar_user_id": self.radar_user_id,
            "trusted": bool(self.trusted),
            "is_archived": bool(self.is_archived),
            "approved_posts_count": self.approved_posts_count or 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AdPayment(Base):
    """Оплата от клиента-рекламодателя (блок C, CRM). Миграция 027."""

    __tablename__ = "ad_payments"
    # Один awaiting-платёж на одну отложку (миграция 096): параллельный прогон
    # реконсилера не выставит второй счёт за тот же пост.
    __table_args__ = (
        Index(
            "uq_ad_payments_awaiting_per_post",
            "scheduled_post_id",
            unique=True,
            postgresql_where=text("scheduled_post_id IS NOT NULL AND status = 'awaiting'"),
            sqlite_where=text("scheduled_post_id IS NOT NULL AND status = 'awaiting'"),
        ),
        # На проде создан миграцией 083; здесь — чтобы sqlite-тесты ловили дубль
        # платежа за пакет (provider='package', external_id=id пакета).
        Index(
            "uq_ad_payments_provider_ext",
            "provider",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    client_id = Column(
        BigInteger, ForeignKey("ad_clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(String(40), nullable=True)  # нал | карта | перевод | …
    # Статус оплаты (миграция 029): awaiting (ждём деньги) | paid (получено).
    status = Column(String(20), nullable=False, default="paid")
    # Штучный учёт пакета (миграция 048): за сколько публикаций эта оплата.
    # NULL — штучно не указано (баланс только в рублях). Решение владельца 2026-06-25.
    units_paid = Column(SmallInteger, nullable=True)
    bank = Column(String(40), nullable=True)  # банк зачисления (фикс-список AD_PAYMENT_BANKS)
    ad_request_id = Column(BigInteger, nullable=True)  # опц. за какую заявку
    scheduled_post_id = Column(BigInteger, nullable=True)  # опц. за какой отложенный пост
    # Задел под платёжного провайдера (миграция 083): NULL = ручная/legacy,
    # 'manual' — подтверждено владельцем из кабинета, позже 'yookassa' и т.п.
    # (provider, external_id) уникальны — идемпотентность будущих вебхуков.
    provider = Column(String(30), nullable=True)
    external_id = Column(String(100), nullable=True)
    note = Column(Text, nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)
    paid_confirmed_at = Column(DateTime, nullable=True)  # когда awaiting → paid
    claimed_at = Column(DateTime, nullable=True)  # клиент нажал «Я оплатил» (098)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AdPayment {self.id} client={self.client_id} {self.amount} {self.status}>"

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "amount": float(self.amount) if self.amount is not None else None,
            "method": self.method,
            "status": self.status,
            "units_paid": self.units_paid,
            "bank": self.bank,
            "ad_request_id": self.ad_request_id,
            "scheduled_post_id": self.scheduled_post_id,
            "provider": self.provider,
            "external_id": self.external_id,
            "note": self.note,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "paid_confirmed_at": (
                self.paid_confirmed_at.isoformat() if self.paid_confirmed_at else None
            ),
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdOutreachCampaign(Base):
    """Кампания рассылки рекламного оффера (Этап 4, миграция 100).

    ``status``: draft → running → done | stopped. VK 9/14 ставит паузу DM-канала
    сообщества в ``dm_channel``; кампания хранит только ``paused_reason``
    (последний стоп), ``paused_until`` — ручная пауза владельца на будущее.
    ``dry_run`` — тексты собираются и кладутся в адресатов, VK не трогается.
    Лимиты: ``per_community_daily`` (30) и ``total_daily`` (150) — отправок в
    МСК-сутки; тихие часы ``quiet_start``–``quiet_end`` (21–9 МСК).
    """

    __tablename__ = "ad_outreach_campaigns"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    template_id = Column(
        Integer, ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True
    )
    months_back = Column(SmallInteger, nullable=False, default=6)
    per_community_daily = Column(SmallInteger, nullable=False, default=30)
    total_daily = Column(SmallInteger, nullable=False, default=150)
    quiet_start = Column(SmallInteger, nullable=False, default=21)
    quiet_end = Column(SmallInteger, nullable=False, default=9)
    dry_run = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="draft")
    paused_until = Column(DateTime, nullable=True)
    paused_reason = Column(Text, nullable=True)
    images_json = Column(JSON, nullable=True)  # имена офферных картинок
    note = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "template_id": self.template_id,
            "months_back": int(self.months_back or 0),
            "per_community_daily": int(self.per_community_daily or 0),
            "total_daily": int(self.total_daily or 0),
            "quiet_start": int(self.quiet_start or 0),
            "quiet_end": int(self.quiet_end or 0),
            "dry_run": bool(self.dry_run),
            "status": self.status,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "paused_reason": self.paused_reason,
            "images": list(self.images_json or []),
            "note": self.note,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdOutreachRecipient(Base):
    """Адресат кампании (миграция 100). Уникум ``(campaign_id, vk_user_id)``.

    ``mode``: ``auto`` — шлём от сообщества ``community_vk_id``; ``manual`` —
    владелец пишет сам (deeplink + текст). ``status``: pending → claimed →
    sent | dry_run | manual (VK не разрешил) | failed | skipped; для ручных —
    manual → done_manual.
    """

    __tablename__ = "ad_outreach_recipients"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    campaign_id = Column(
        BigInteger, ForeignKey("ad_outreach_campaigns.id", ondelete="CASCADE"), nullable=False
    )
    vk_user_id = Column(BigInteger, nullable=False)
    community_vk_id = Column(BigInteger, nullable=False)  # owner_id сообщества (отрицательный)
    ad_request_id = Column(BigInteger, nullable=True)
    client_id = Column(BigInteger, ForeignKey("ad_clients.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(200), nullable=True)
    origin = Column(String(20), nullable=True)  # inbound_dm | suggested
    mode = Column(String(10), nullable=False, default="manual")
    status = Column(String(20), nullable=False, default="pending")
    body = Column(Text, nullable=True)
    attempts = Column(SmallInteger, nullable=False, default=0)
    claimed_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    vk_message_id = Column(BigInteger, nullable=True)
    error_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_ad_outreach_recipient", "campaign_id", "vk_user_id", unique=True),
        Index("ix_ad_outreach_recipient_status", "campaign_id", "status"),
        Index("ix_ad_outreach_recipient_user", "vk_user_id"),
        Index("ix_ad_outreach_recipient_sent", "status", "sent_at"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "vk_user_id": self.vk_user_id,
            "community_vk_id": self.community_vk_id,
            "ad_request_id": self.ad_request_id,
            "client_id": self.client_id,
            "name": self.name,
            "origin": self.origin,
            "mode": self.mode,
            "status": self.status,
            "body": self.body,
            "attempts": int(self.attempts or 0),
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "vk_message_id": self.vk_message_id,
            "error_code": self.error_code,
            "error": self.error,
            "deeplink": f"https://vk.com/im?sel={int(self.vk_user_id)}",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdOutreachBlacklist(Base):
    """Кому оффер не слать: просьба, жалоба, VK 900 (миграция 100)."""

    __tablename__ = "ad_outreach_blacklist"

    vk_user_id = Column(BigInteger, primary_key=True)
    reason = Column(Text, nullable=True)
    until = Column(DateTime, nullable=True)  # NULL — навсегда
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "vk_user_id": self.vk_user_id,
            "reason": self.reason,
            "until": self.until.isoformat() if self.until else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdClientPackage(Base):
    """Пакет постов клиента кабинета (заказ владельца 2026-08-26). Миграция 086.

    Три вида (``kind``):

    * ``free_promo`` — акция «бесплатная реклама местным»: N постов бесплатно,
      бессрочно; ``site_ad`` — малмыжским дополнительно размещение на сайте
      вМалмыже.рф (руками, SETKA порталом не управляет — отметка о выполнении).
    * ``prepaid``  — «оплатил N постов»: доступен ТОЛЬКО после галочки владельца
      (``paid_at``); без периода — размещай хоть год, хоть за день.
    * ``postpaid`` — месячный пакет с оплатой в конце: доступен сразу, по
      исчерпании/истечении месяца новые посты БЛОКИРУЮТСЯ, пока владелец не
      отметит оплату и не продлит (или не продлит в долг). Авто-продления нет —
      решение владельца 2026-08-26.

    Цена пакета — целиком в ``price``; посты в счёт пакета идут с ``price=0``
    (реконсилер не создаёт по ним awaiting-платежей — деньги живут на пакете).
    Отменённый/отклонённый/несостоявшийся пост возвращается в пакет
    (``posts_used`` уменьшается) — списание только за реальные размещения.
    """

    __tablename__ = "ad_client_packages"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    client_id = Column(
        BigInteger, ForeignKey("ad_clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind = Column(String(20), nullable=False)  # free_promo|prepaid|postpaid
    posts_total = Column(SmallInteger, nullable=False)
    posts_used = Column(SmallInteger, nullable=False, default=0)
    price = Column(Numeric(10, 2), nullable=False, default=0)  # цена пакета целиком
    period_start = Column(Date, nullable=True)  # NULL = бессрочный пакет
    period_end = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    paid_at = Column(DateTime, nullable=True)  # галочка владельца «оплачено»
    site_ad = Column(Boolean, nullable=False, default=False)  # акция: реклама на сайте
    site_ad_done_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "kind": self.kind,
            "posts_total": int(self.posts_total or 0),
            "posts_used": int(self.posts_used or 0),
            "posts_left": (
                None
                if self.kind == "unlimited"
                else max(0, int(self.posts_total or 0) - int(self.posts_used or 0))
            ),
            "unlimited": self.kind == "unlimited",
            "price": float(self.price) if self.price is not None else 0.0,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "is_active": bool(self.is_active),
            "paid": self.paid_at is not None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "site_ad": bool(self.site_ad),
            "site_ad_done_at": (self.site_ad_done_at.isoformat() if self.site_ad_done_at else None),
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdPublication(Base):
    """Реально вышедшая рекламная публикация (блок C, CRM). Миграция 027."""

    __tablename__ = "ad_publications"
    # Одна публикация на одну отложку (миграция 096): дубли от параллельного
    # реконсилера ловит БД, а не «идемпотентность в рамках одного прогона».
    __table_args__ = (
        Index(
            "uq_ad_publications_per_post",
            "scheduled_post_id",
            unique=True,
            postgresql_where=text("scheduled_post_id IS NOT NULL"),
            sqlite_where=text("scheduled_post_id IS NOT NULL"),
        ),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    client_id = Column(
        BigInteger, ForeignKey("ad_clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    community_vk_id = Column(BigInteger, nullable=False)  # owner_id группы (отрицательный)
    vk_post_id = Column(BigInteger, nullable=True)  # id опубликованного поста (если известен)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True)
    ad_request_id = Column(BigInteger, nullable=True)  # опц. из какой заявки
    scheduled_post_id = Column(BigInteger, nullable=True)  # опц. из какого отложенного поста
    price = Column(Numeric(12, 2), nullable=True)  # согласованная цена размещения
    status = Column(String(20), nullable=False, default="published")  # published | removed
    note = Column(Text, nullable=True)
    published_at = Column(DateTime, default=datetime.utcnow)
    # Срок размещения и факт авто-снятия (С2, миграция 041). expires_at — МСК
    # wall-clock naive (копируется из отложки при фиксации публикации); removed_at —
    # момент фактического wall.delete (UTC). nullable — без срока висит вечно.
    expires_at = Column(DateTime, nullable=True)
    removed_at = Column(DateTime, nullable=True)
    # Закреп на сутки (099): когда закрепили, до когда держим, когда сняли.
    pinned_at = Column(DateTime, nullable=True)
    pinned_until = Column(DateTime, nullable=True)
    unpinned_at = Column(DateTime, nullable=True)
    # Метрики поста (С3, миграция 042): просмотры/лайки/репосты из wall.getById.
    # NULL — ещё не собирали; stats_updated_at — момент последнего сбора (UTC).
    views = Column(Integer, nullable=True)
    likes = Column(Integer, nullable=True)
    reposts = Column(Integer, nullable=True)
    stats_updated_at = Column(DateTime, nullable=True)
    # Комментарии (миграция 076) + статус оплаты конкретного поста (paid/unpaid).
    comments = Column(Integer, default=0)
    paid_status = Column(String(20), nullable=False, default="unpaid")
    # Кэш имени сообщества для группировки в UI без лишних VK-запросов (076).
    community_name = Column(String(300), nullable=True)
    community_screen_name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AdPublication {self.id} client={self.client_id} comm={self.community_vk_id}>"

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "community_vk_id": self.community_vk_id,
            "vk_post_id": self.vk_post_id,
            "region_id": self.region_id,
            "ad_request_id": self.ad_request_id,
            "scheduled_post_id": self.scheduled_post_id,
            "price": float(self.price) if self.price is not None else None,
            "status": self.status,
            "note": self.note,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "removed_at": self.removed_at.isoformat() if self.removed_at else None,
            "pinned_at": self.pinned_at.isoformat() if self.pinned_at else None,
            "pinned_until": self.pinned_until.isoformat() if self.pinned_until else None,
            "unpinned_at": self.unpinned_at.isoformat() if self.unpinned_at else None,
            "views": self.views,
            "likes": self.likes,
            "reposts": self.reposts,
            "comments": self.comments or 0,
            "paid_status": self.paid_status or "unpaid",
            "community_name": self.community_name,
            "community_screen_name": self.community_screen_name,
            "stats_updated_at": (
                self.stats_updated_at.isoformat() if self.stats_updated_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            # Производное для UI: ссылка на пост в VK.
            "vk_post_url": (
                f"https://vk.com/wall{self.community_vk_id}_{self.vk_post_id}"
                if self.community_vk_id and self.vk_post_id
                else None
            ),
        }


class AdInteraction(Base):
    """Событие журнала взаимодействий рекламного кабинета (audit-log). Миграция 028.

    Единая хронология действий поверх заявок/клиентов/отложек/публикаций/оплат:
    ответ клиенту, смена статуса, планирование, публикация, оплата, ручная
    заметка. Пишется через ``modules.ad_cabinet.interaction_log.log_interaction``
    из существующих мутаций. Все ссылки nullable — событие может относиться к
    нескольким сущностям сразу или ни к чему (ручная заметка).
    """

    __tablename__ = "ad_interactions"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    client_id = Column(BigInteger, ForeignKey("ad_clients.id", ondelete="SET NULL"), nullable=True)
    ad_request_id = Column(BigInteger, nullable=True)
    scheduled_post_id = Column(BigInteger, nullable=True)
    publication_id = Column(BigInteger, nullable=True)
    payment_id = Column(BigInteger, nullable=True)
    kind = Column(String(40), nullable=False)
    summary = Column(Text, nullable=True)
    meta_json = Column(JSON, nullable=True)
    actor = Column(String(40), nullable=False, default="operator")
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AdInteraction {self.id} client={self.client_id} {self.kind}>"

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "ad_request_id": self.ad_request_id,
            "scheduled_post_id": self.scheduled_post_id,
            "publication_id": self.publication_id,
            "payment_id": self.payment_id,
            "kind": self.kind,
            "summary": self.summary,
            "meta_json": self.meta_json,
            "actor": self.actor,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AdOrderItem(Base):
    """Позиция заказа клиента рекламного кабинета. Миграция 030.

    Что и сколько реклам клиент заказал на период — из предложки или вписано
    вручную. Тонкий список поверх ``ad_clients``; ссылки на заявку/отложку/
    публикацию опциональны (откуда позиция и чем реализована).
    """

    __tablename__ = "ad_order_items"

    id = Column(BigInteger, primary_key=True, index=True)
    client_id = Column(
        BigInteger, ForeignKey("ad_clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ad_request_id = Column(BigInteger, nullable=True)
    scheduled_post_id = Column(BigInteger, nullable=True)
    publication_id = Column(BigInteger, nullable=True)
    description = Column(Text, nullable=True)
    quantity = Column(Integer, nullable=False, default=1)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    status = Column(
        String(20), nullable=False, default="planned"
    )  # planned|scheduled|published|cancelled
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<AdOrderItem {self.id} client={self.client_id} x{self.quantity} {self.status}>"

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "ad_request_id": self.ad_request_id,
            "scheduled_post_id": self.scheduled_post_id,
            "publication_id": self.publication_id,
            "description": self.description,
            "quantity": self.quantity,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "status": self.status,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AdChatMessage(Base):
    """Сообщение чата клиент↔владелец в кабинете рекламодателя. Миграция 084.

    Отдельная таблица, НЕ ``ad_interactions``: у аудит-журнала другой жизненный
    цикл (рендер в таймлайн карточки, операторское удаление событий), а чату
    нужны unread-семантика (``read_at``) и выборка «после id N» под polling.
    VK-тред (``client_thread``/``client_reply``) остаётся вторым каналом для
    VK-клиентов; здесь — in-app канал, работающий и для клиентов без VK.
    """

    __tablename__ = "ad_chat_messages"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    client_id = Column(
        BigInteger, ForeignKey("ad_clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender = Column(String(10), nullable=False)  # client | owner
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    read_at = Column(DateTime, nullable=True)  # NULL = не прочитано другой стороной

    def __repr__(self):
        return f"<AdChatMessage {self.id} client={self.client_id} {self.sender}>"

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "sender": self.sender,
            "body": self.body,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
        }


class PublishSchedule(Base):
    """Расписание публикаций"""

    __tablename__ = "publish_schedules"

    id = Column(Integer, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id"), nullable=False)

    # Schedule
    category = Column(String(50), nullable=False)  # novost, reklama, etc
    hour = Column(Integer, nullable=False)  # 0-23
    minute = Column(Integer, nullable=False)  # 0-59

    # Days of week (0-6, Monday-Sunday)
    days_of_week = Column(String(20), default="0,1,2,3,4,5,6")  # All days by default

    # Status
    is_active = Column(Boolean, default=True)
    last_run = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PublishSchedule {self.category} at {self.hour}:{self.minute}>"


class BroadcastCampaign(Base):
    """Кампания сетевой рассылки (внутренний планировщик-публикатор). Миграция 044.

    Канон владельца (директива brain 2026-06-14): собрать пост в SARAFAN →
    разослать по выбранным сообществам (по умолчанию все паблики сети) → свой
    беат публикует ``wall.post`` НЕМЕДЛЕННО в заданное время, повтор N раз. НЕ
    кладём в VK-отложку — всё управление (текст/расписание/очередь) в программе.

    Время: ``scheduled_at``/``next_run_at`` — МСК wall-clock naive (как
    ``AdScheduledPost.publish_date``); диспетчер сравнивает с МСК-now.
    """

    __tablename__ = "broadcast_campaigns"

    id = Column(BigInteger, primary_key=True, index=True)
    title = Column(String(300), nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    image_names = Column(JSON, nullable=True)  # имена загруженных картинок (до заливки)
    attachments = Column(Text, nullable=True)  # кэш "photo<o>_<id>,…" после первой заливки

    status = Column(
        String(20), nullable=False, default="draft", index=True
    )  # draft|scheduled|done|cancelled
    scheduled_at = Column(DateTime, nullable=True)  # МСК wall-clock: первый запуск
    repeat_count = Column(Integer, nullable=False, default=1)  # сколько раз разослать (≥1)
    repeat_interval_hours = Column(Float, nullable=False, default=24)  # интервал между запусками
    runs_done = Column(Integer, nullable=False, default=0)  # завершённых прогонов
    next_run_at = Column(DateTime, nullable=True, index=True)  # МСК wall-clock: следующий запуск
    vary_per_target = Column(Boolean, nullable=False, default=False)  # hook лёгкой вариации (off)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    targets = relationship(
        "BroadcastTarget", back_populates="campaign", cascade="all, delete-orphan"
    )
    publications = relationship(
        "BroadcastPublication", back_populates="campaign", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return (
            f"<BroadcastCampaign {self.id} {self.status} runs={self.runs_done}/{self.repeat_count}>"
        )

    def to_dict(self, *, targets=None, publications=None):
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "image_names": self.image_names or [],
            "attachments": self.attachments,
            "status": self.status,
            "scheduled_at": self.scheduled_at.isoformat() if self.scheduled_at else None,
            "repeat_count": self.repeat_count,
            "repeat_interval_hours": self.repeat_interval_hours,
            "runs_done": self.runs_done,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "vary_per_target": self.vary_per_target,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "targets": [t.to_dict() for t in targets] if targets is not None else None,
            "publications": (
                [p.to_dict() for p in publications] if publications is not None else None
            ),
        }


class BroadcastTarget(Base):
    """Целевое сообщество кампании рассылки. Миграция 044.

    ``group_id`` — owner_id группы VK (как ``regions.vk_group_id``). Уникален в
    рамках кампании. ``name`` — снимок имени паблика/региона для UI.
    """

    __tablename__ = "broadcast_targets"

    id = Column(BigInteger, primary_key=True, index=True)
    campaign_id = Column(
        BigInteger,
        ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id = Column(BigInteger, nullable=False)
    name = Column(String(300), nullable=True)

    campaign = relationship("BroadcastCampaign", back_populates="targets")

    __table_args__ = (Index("uq_broadcast_target", "campaign_id", "group_id", unique=True),)

    def __repr__(self):
        return f"<BroadcastTarget c={self.campaign_id} g={self.group_id}>"

    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "group_id": self.group_id,
            "name": self.name,
        }


class BroadcastPublication(Base):
    """Per-(цель, прогон) публикация рассылки — защёлка идемпотентности. Миграция 044.

    Уникум ``(campaign_id, group_id, run_index)``: диспетчер клеймит строку через
    INSERT ... ON CONFLICT DO NOTHING ПЕРЕД публикацией, поэтому под конкурентным
    беатом один пост уходит в одну цель один раз за прогон. ``status``:
    ``pending`` (заклеймлено, публикуется) → ``published`` (+ vk_post_id/url) |
    ``error`` (+ причина).
    """

    __tablename__ = "broadcast_publications"

    id = Column(BigInteger, primary_key=True, index=True)
    campaign_id = Column(
        BigInteger,
        ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id = Column(BigInteger, nullable=False)
    run_index = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="pending")  # pending|published|error
    vk_post_id = Column(BigInteger, nullable=True)
    post_url = Column(String(300), nullable=True)
    error = Column(Text, nullable=True)
    published_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("BroadcastCampaign", back_populates="publications")

    __table_args__ = (
        Index("uq_broadcast_publication", "campaign_id", "group_id", "run_index", unique=True),
    )

    def __repr__(self):
        return (
            f"<BroadcastPublication c={self.campaign_id} g={self.group_id} "
            f"run={self.run_index} {self.status}>"
        )

    def to_dict(self):
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "group_id": self.group_id,
            "run_index": self.run_index,
            "status": self.status,
            "vk_post_id": self.vk_post_id,
            "post_url": self.post_url,
            "error": self.error,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


class GatewayRequest(Base):
    """Лог одного запроса к VK-шлюзу (``/api/gateway``) — для страницы статистики.

    Пишется best-effort после исполнения (``modules/gateway/usage.py``), не
    блокирует ответ шлюза. Хранит: кто (``project`` = имя API-ключа), когда,
    какой эндпоинт/метод, что искали (``params``) и результат. Логируются
    запросы, прошедшие auth+квоту (включая VK-ошибку и 503), а также отказы
    **429** (превышение квоты, известный проект) и **401** с неверным ключом
    (под проектом ``(unknown)``; пустой ключ не пишем — шум сканеров). Ретеншн —
    beat ``prune_gateway_requests`` (env ``GATEWAY_REQUESTS_RETENTION_DAYS``).
    Миграция 049.
    """

    __tablename__ = "gateway_requests"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    project = Column(String(64), index=True)
    endpoint = Column(String(32))  # call | community | wall
    method = Column(String(64))
    params = Column(JSON)  # что искали/спрашивали
    status = Column(Integer)  # HTTP-статус ответа
    ok = Column(Boolean, default=False)  # успешный VK-ответ
    error_code = Column(Integer, nullable=True)  # VK error_code, если был
    duration_ms = Column(Integer, nullable=True)

    __table_args__ = (Index("ix_gateway_requests_project_created", "project", "created_at"),)

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "project": self.project,
            "endpoint": self.endpoint,
            "method": self.method,
            "params": self.params,
            "status": self.status,
            "ok": self.ok,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
        }


class PromoEnrollment(Base):
    """Зачисление района в раскрутку. Миграция 087.

    Заполняется pull-моделью: диспетчер сам добирает активные районы запросом на
    каждом прогоне (``INSERT … ON CONFLICT DO NOTHING``) — скрипт активации
    регионов не правится, поэтому «забытый шаг» невозможен по построению.

    ``members_at_enroll`` = NULL — легальное состояние, а не ошибка: район,
    активированный сегодня, суточного снимка ещё не имеет. Отсутствие данных
    не равно «подписчиков много», поэтому такой район зачисляется.

    Пороги входа и выхода в ``promo_settings`` РАЗНЫЕ (гистерезис): при равных
    район мигал бы вход/выход на границе каждую ночь.
    """

    __tablename__ = "promo_enrollments"

    id = Column(BigInteger, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), nullable=False, default="active")  # active|graduated|paused
    cohort = Column(
        String(20), nullable=False, default="pending"
    )  # pending|wave_a|wave_b|wave_c|control
    members_at_enroll = Column(Integer, nullable=True)
    members_at_graduate = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    enrolled_at = Column(DateTime, default=datetime.utcnow)
    graduated_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    region = relationship("Region")

    __table_args__ = (
        Index("uq_promo_enrollment_region", "region_id", unique=True),
        Index("ix_promo_enrollments_status", "status", "cohort"),
    )

    def __repr__(self):
        return f"<PromoEnrollment r={self.region_id} {self.status}/{self.cohort}>"

    def to_dict(self):
        return {
            "id": self.id,
            "region_id": self.region_id,
            "status": self.status,
            "cohort": self.cohort,
            "members_at_enroll": self.members_at_enroll,
            "members_at_graduate": self.members_at_graduate,
            "reason": self.reason,
            "enrolled_at": self.enrolled_at.isoformat() if self.enrolled_at else None,
            "graduated_at": self.graduated_at.isoformat() if self.graduated_at else None,
        }


class PromoAction(Base):
    """План и факт одного действия раскрутки — защёлка идемпотентности. Миграция 087.

    Строка заводится ДО обращения к VK (``INSERT … ON CONFLICT DO NOTHING`` +
    commit), поэтому под конкурентным беатом действие уходит один раз за слот.

    Два уникума и есть настоящее соблюдение квот. ``uq_promo_action_donor_slot``
    держит «1 промо в неделю на донора», ``uq_promo_action_target_slot`` — «1 в
    неделю на цель». Оба нужны: у канала ``setup`` донора нет, а Postgres считает
    NULL в уникуме различными — там защёлкой работает второй индекс.

    ``dry_run`` по умолчанию TRUE: канал, включённый по недосмотру, запишет в
    ``body`` текст, который ушёл бы, и остановится, ничего не опубликовав.
    """

    __tablename__ = "promo_actions"

    id = Column(BigInteger, primary_key=True, index=True)
    channel = Column(String(20), nullable=False)
    donor_group_id = Column(BigInteger, nullable=True)
    donor_region_id = Column(Integer, ForeignKey("regions.id", ondelete="SET NULL"), nullable=True)
    target_region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)
    hop = Column(SmallInteger, nullable=False, default=0)  # 0 нет донора,1 сосед,2 через,3 область
    slot_key = Column(String(16), nullable=False)  # ISO-неделя '2026-W35' или дата
    status = Column(
        String(20), nullable=False, default="pending"
    )  # pending|published|error|skipped|dry_run
    dry_run = Column(Boolean, nullable=False, default=True)
    body = Column(Text, nullable=True)
    vk_method = Column(String(40), nullable=True)
    vk_post_id = Column(BigInteger, nullable=True)
    post_url = Column(String(300), nullable=True)
    vk_error_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    token_name = Column(String(50), nullable=True)
    api_calls = Column(SmallInteger, nullable=False, default=0)
    planned_at = Column(DateTime, default=datetime.utcnow)
    published_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("uq_promo_action_donor_slot", "channel", "donor_group_id", "slot_key", unique=True),
        Index(
            "uq_promo_action_target_slot", "channel", "target_region_id", "slot_key", unique=True
        ),
        Index("ix_promo_actions_status", "status", "planned_at"),
        Index("ix_promo_actions_target", "target_region_id", "published_at"),
    )

    def __repr__(self):
        return f"<PromoAction {self.channel} →r{self.target_region_id} {self.status}>"

    def to_dict(self):
        return {
            "id": self.id,
            "channel": self.channel,
            "donor_group_id": self.donor_group_id,
            "donor_region_id": self.donor_region_id,
            "target_region_id": self.target_region_id,
            "hop": self.hop,
            "slot_key": self.slot_key,
            "status": self.status,
            "dry_run": self.dry_run,
            "body": self.body,
            "vk_method": self.vk_method,
            "vk_post_id": self.vk_post_id,
            "post_url": self.post_url,
            "vk_error_code": self.vk_error_code,
            "error": self.error,
            "token_name": self.token_name,
            "api_calls": self.api_calls,
            "planned_at": self.planned_at.isoformat() if self.planned_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


class PromoSettings(Base):
    """Настройки раскрутки, которые крутит владелец из UI. Миграция 087.

    Одна строка (``id=1``), а не таблица-справочник — CHECK в схеме это держит.
    ``channels`` — JSON вида ``{"promo_post": {"enabled": false, "dry_run": true}}``:
    у каждого канала три независимых состояния (выключен / сухой прогон / боевой),
    поверх которых стоит глобальный env-килл-свитч ``PROMO_DISABLED``.
    """

    __tablename__ = "promo_settings"

    id = Column(SmallInteger, primary_key=True, default=1)
    threshold_members = Column(Integer, nullable=False, default=300)  # вход: меньше порога
    graduate_members = Column(Integer, nullable=False, default=400)  # выход: не меньше порога
    donor_min_members = Column(Integer, nullable=False, default=1000)
    max_per_donor_per_week = Column(SmallInteger, nullable=False, default=1)
    max_per_target_per_week = Column(SmallInteger, nullable=False, default=1)
    max_actions_per_day = Column(SmallInteger, nullable=False, default=3)
    quiet_hours_start = Column(SmallInteger, nullable=False, default=19)  # с этого часа молчим
    quiet_hours_end = Column(SmallInteger, nullable=False, default=10)  # до этого часа молчим
    second_hop_enabled = Column(Boolean, nullable=False, default=True)
    oblast_fallback_enabled = Column(Boolean, nullable=False, default=True)
    oblast_group_id = Column(BigInteger, nullable=True, default=-168170001)
    channels = Column(JSON, nullable=False, default=dict)

    # Пауза всего модуля после «ВК велел замолчать» (коды 9/14). Миграция 088.
    # Живёт в БД, а не в Redis, сознательно: Redis-квоты у нас fail-open, и
    # перезапуск снял бы паузу ровно тогда, когда публиковать нельзя категорически.
    paused_until = Column(DateTime, nullable=True)
    paused_reason = Column(Text, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<PromoSettings threshold={self.threshold_members}>"

    def to_dict(self):
        return {
            "threshold_members": self.threshold_members,
            "graduate_members": self.graduate_members,
            "donor_min_members": self.donor_min_members,
            "max_per_donor_per_week": self.max_per_donor_per_week,
            "max_per_target_per_week": self.max_per_target_per_week,
            "max_actions_per_day": self.max_actions_per_day,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "second_hop_enabled": self.second_hop_enabled,
            "oblast_fallback_enabled": self.oblast_fallback_enabled,
            "oblast_group_id": self.oblast_group_id,
            "channels": self.channels or {},
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "paused_reason": self.paused_reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PromoDonorBlacklist(Base):
    """Донор, которому сейчас нельзя отдавать промо. Миграция 087.

    ``until`` = NULL — бан навсегда (ручной, владельцем). Непустой ``until``
    ставит автоматика по кодам VK: 214/220 → сутки, 219 («рекламный пост недавно
    добавлен») → семь суток, потому что 219 — это сигнал «VK читает наш промо как
    рекламу», то есть повод переписать шаблон, а не повторить через час.
    """

    __tablename__ = "promo_donor_blacklist"

    id = Column(BigInteger, primary_key=True, index=True)
    donor_group_id = Column(BigInteger, nullable=False)
    reason = Column(Text, nullable=False)
    until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("uq_promo_blacklist_donor", "donor_group_id", unique=True),)

    def __repr__(self):
        return f"<PromoDonorBlacklist g={self.donor_group_id} until={self.until}>"

    def to_dict(self):
        return {
            "id": self.id,
            "donor_group_id": self.donor_group_id,
            "reason": self.reason,
            "until": self.until.isoformat() if self.until else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PromoGroupSetup(Base):
    """Журнал автооформления сообщества со снимком «до». Миграция 087.

    ``before`` обязателен: канал перезаписывает описание, которое владелец мог
    писать руками, и без снимка откат невозможен.

    ``setup_version`` — версия шаблона оформления. Повтор той же версии no-op по
    уникуму; правка шаблона = бамп версии = оформление переприменяется ровно один
    раз, а не на каждом прогоне.
    """

    __tablename__ = "promo_group_setup"

    id = Column(BigInteger, primary_key=True, index=True)
    region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)
    setup_version = Column(SmallInteger, nullable=False, default=1)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    applied_fields = Column(JSON, nullable=True)
    pinned_post_url = Column(String(300), nullable=True)
    status = Column(
        String(20), nullable=False, default="dry_run"
    )  # dry_run|applied|error|rolled_back
    vk_error_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    applied_at = Column(DateTime, default=datetime.utcnow)

    region = relationship("Region")

    __table_args__ = (
        Index("uq_promo_setup_region_version", "region_id", "setup_version", unique=True),
    )

    def __repr__(self):
        return f"<PromoGroupSetup r={self.region_id} v{self.setup_version} {self.status}>"

    def to_dict(self):
        return {
            "id": self.id,
            "region_id": self.region_id,
            "setup_version": self.setup_version,
            "before": self.before,
            "after": self.after,
            "applied_fields": self.applied_fields,
            "pinned_post_url": self.pinned_post_url,
            "status": self.status,
            "vk_error_code": self.vk_error_code,
            "error": self.error,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }


class PromoOutreachCandidate(Base):
    """Местная группа-кандидат для РУЧНОГО аутрича владельца. Миграция 087.

    SETKA такие группы только находит, ранжирует и готовит текст обращения.
    **Ничего не отправляет** — ни сообщений, ни постов на чужие стены: массовая
    рассылка рекламы инструментами, для рекламы не предназначенными, и есть то,
    за что VK удаляет аккаунты. Отправляет владелец сам, из своего профиля.

    ``status`` ведёт владелец: new → contacted → agreed | declined | ignored.
    Правило «один контакт на группу навсегда» держится уникумом по (район, группа).
    """

    __tablename__ = "promo_outreach_candidates"

    id = Column(BigInteger, primary_key=True, index=True)
    target_region_id = Column(Integer, ForeignKey("regions.id", ondelete="CASCADE"), nullable=False)
    vk_group_id = Column(BigInteger, nullable=False)
    name = Column(String(300), nullable=False, default="")
    screen_name = Column(String(100), nullable=True)
    members_count = Column(Integer, nullable=True)
    score = Column(Float, nullable=True)
    draft_text = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="new")
    owner_note = Column(Text, nullable=True)
    found_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_promo_outreach_group", "target_region_id", "vk_group_id", unique=True),
        Index("ix_promo_outreach_region_status", "target_region_id", "status"),
    )

    def __repr__(self):
        return f"<PromoOutreachCandidate r={self.target_region_id} g={self.vk_group_id}>"

    def to_dict(self):
        return {
            "id": self.id,
            "target_region_id": self.target_region_id,
            "vk_group_id": self.vk_group_id,
            "name": self.name,
            "screen_name": self.screen_name,
            "members_count": self.members_count,
            "score": self.score,
            "draft_text": self.draft_text,
            "status": self.status,
            "owner_note": self.owner_note,
            "found_at": self.found_at.isoformat() if self.found_at else None,
        }


class LandingOrder(Base):
    """Заявка на рекламу, оставленная с публичного лендинга /regions/links.

    Отдельная таблица, а не запись в ``ad_requests``: там ``community_vk_id``
    NOT NULL и вся строка описывает рекламу, пойманную в предложке сообщества
    или во входящем ЛС. У заявки с сайта нет ни группы, ни поста — только
    «кто, куда пишет, какие сообщества выбрал». Миграция 119.
    """

    __tablename__ = "landing_orders"

    # with_variant: тесты гоняют SQLite, где чистый BigInteger не
    # autoincrement'ится (прецедент — AdClient).
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Кто оставил заявку: имя и контакт (телефон/telegram/vk/почта).
    name = Column(String(300), nullable=False)
    contact = Column(String(300), nullable=False)

    # Снимок выбранных сообществ: [{code, name, members, url}].
    regions_json = Column(JSON, nullable=False, default=list)
    regions_count = Column(Integer, nullable=False, default=0)
    reach = Column(Integer, nullable=True)
    price_estimate = Column(Integer, nullable=True)

    comment = Column(Text, nullable=True)

    # Токен одной отправки (дедуп двойного клика/обновления страницы).
    request_token = Column(String(64), nullable=True, index=True)

    # Откуда пришла заявка.
    source_host = Column(String(300), nullable=True)
    source_page = Column(String(500), nullable=True)

    # new | contacted | done | rejected
    status = Column(String(20), nullable=False, default="new", index=True)
    owner_note = Column(Text, nullable=True)
    notified_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<LandingOrder {self.id} {self.status} regions={self.regions_count}>"

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "name": self.name,
            "contact": self.contact,
            "regions": self.regions_json or [],
            "regions_count": self.regions_count,
            "reach": self.reach,
            "price_estimate": self.price_estimate,
            "comment": self.comment,
            "request_token": self.request_token,
            "source_host": self.source_host,
            "source_page": self.source_page,
            "status": self.status,
            "owner_note": self.owner_note,
            "notified_at": self.notified_at.isoformat() if self.notified_at else None,
        }
