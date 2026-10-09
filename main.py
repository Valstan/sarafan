"""
SETKA - Main FastAPI application
Multimedia management system for news resources
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Восстановление REQUIRED-секретов (DATABASE_URL/REDIS_URL) из vault КАРМАНа
# ДО импорта config.runtime (его тянет database.connection и web.api ниже):
# потеря /etc/setka/setka.env иначе убьёт процесс на `_require` ещё на этапе
# импорта. В норме — no-op (все ключи на месте, ноль сетевых вызовов).
from modules.secrets_bootstrap import bootstrap_secrets  # noqa: E402
from utils.log_redaction import install_log_redaction  # noqa: E402

bootstrap_secrets()  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates  # noqa: E402

from _version import __version__ as APP_VERSION  # noqa: E402
from database.connection import close_db, init_db  # noqa: E402
from middleware.auth_gate import AuthGateMiddleware  # noqa: E402
from middleware.cache_headers import HtmlRevalidationMiddleware  # noqa: E402
from middleware.metrics_middleware import MetricsMiddleware  # noqa: E402
from middleware.rate_limiter import RateLimitMiddleware  # noqa: E402
from modules.module_activity_notifier import notify_system_startup  # noqa: E402
from web.api import (  # noqa: E402
    ad_cabinet,
    ad_crm,
    ad_outreach,
    advertiser_cabinet,
    auth,
    broadcast,
    classifier_ingest,
    classifier_review,
    communities,
    discovery,
    ecosystem,
    filtration,
    gateway,
    gateway_stats,
    health,
    ingest_contract,
    notifications,
    parsing,
    parsing_stats,
    posts,
    promotion,
    publisher,
    radar,
    radar_id,
    regions,
    schedule_management,
    scheduler,
    service_notifications,
    subscriber_growth,
    system_monitoring,
    task_monitoring,
)
from web.api import templates as templates_api  # noqa: E402
from web.api import theme_quotas, token_management, vk_monitoring  # noqa: E402
from web.static_files import RevalidatingStaticFiles  # noqa: E402

# Setup logging
#
# Все логи идут через StreamHandler в stderr. На проде systemd (см.
# setka.service: StandardOutput=append:/home/valstan/SETKA/logs/uvicorn_production.log)
# перенаправляет stdout/stderr в файл — никакого отдельного FileHandler не
# нужно. До 2026-05-25 был дубль через logs/app.log с LOG_LEVEL=WARNING,
# но он:
#   - 100% дублировал содержимое stderr → uvicorn_production.log;
#   - копил почти ничего (порог WARNING, файл не рос неделями);
#   - усложнял main.py обработкой LOG_PATH env с try/except для Windows.
# Дефолт LOG_LEVEL=INFO даёт полезный объём для grep'а; на проде можно
# поднять до WARNING через env если станет шумно.
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Маскирование секретов в логах — сразу после basicConfig и ПОСЛЕ
# bootstrap_secrets() выше (значения из комнаты КАРМАНа попадают в окружение
# именно там). Ставится на фабрику LogRecord, т.е. покрывает и логгеры uvicorn,
# которые про наш basicConfig ничего не знают. Инцидент 2026-08-12: токен
# Telegram-бота утёк в лог из текста исключения requests (URL Bot API содержит
# секрет в пути) и бот был угнан.
install_log_redaction()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan events"""
    # Startup
    logger.info("Starting SETKA application...")
    try:
        from monitoring.metrics import prune_stale_multiproc_files

        pruned = prune_stale_multiproc_files()
        logger.info("Multiproc metrics dir pruned on startup: %s", pruned)
    except Exception:  # noqa: BLE001 — чистка метрик не роняет старт
        logger.warning("Multiproc prune on startup failed", exc_info=True)
    await init_db()
    logger.info("Database initialized")

    # Start system status monitoring (ОТКЛЮЧЕНО - использует старую систему уведомлений)
    # await start_status_monitoring()
    logger.info("System status monitoring disabled - using new workflow notifications")

    # Уведомляем о запуске системы
    notify_system_startup()

    yield

    # Shutdown
    logger.info("Shutting down SETKA application...")
    await close_db()
    logger.info("Database connection closed")


# Create FastAPI app
app = FastAPI(
    title="SETKA",
    description="Multimedia Management System for News Resources",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# App-level auth + изоляция ролей operator|radar (Ф0.1 контент-радара).
# Secure by default: всё закрыто, кроме allowlist'а в middleware/auth_gate.py.
app.add_middleware(AuthGateMiddleware)

# Rate limiting middleware (защита от DoS)
app.add_middleware(
    RateLimitMiddleware,
    requests_per_minute=100,  # 100 requests per minute per IP
    burst_size=20,
    whitelist=["127.0.0.1", "localhost"],  # Whitelist localhost
)

# Metrics middleware (мониторинг производительности)
app.add_middleware(MetricsMiddleware)

# HTML нельзя показывать из кэша без переспроса: кэш-бастеры `?v=…` лежат ВНУТРИ
# документа, поэтому старая страница просит старую статику и `no-cache` на самой
# статике до дела не доходит. Замер 06.09 поймал ровно это. Добавлено последним —
# значит, слой внешний и видит финальный ответ. Подробности — middleware/cache_headers.py.
app.add_middleware(HtmlRevalidationMiddleware)

# Setup templates and static files
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))
# APP_VERSION остаётся у FastAPI (поле `version` в OpenAPI), но шаблонам больше
# не отдаётся: учёт версий в проекте не ведётся, и подвал их не показывает
# (решение владельца 2026-07-26). Глобал убран, чтобы не воскрешать «1.5.0»,
# которое давно ничего не значит.

# RevalidatingStaticFiles, а не StaticFiles: без Cache-Control браузер вправе
# показать копию из кэша не спрашивая сервер, и после деплоя выдаёт новый HTML
# со старым CSS. Замер 31.08 поймал ровно это. Подробности — в web/static_files.py.
app.mount(
    "/static",
    RevalidatingStaticFiles(directory=str(BASE_DIR / "web" / "static")),
    name="static",
)

# Include routers
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(radar.router, prefix="/api/radar", tags=["Content Radar"])
# Радар-ID (OIDC): абсолютные пути /.well-known/* и /oidc/* — без префикса.
app.include_router(radar_id.router, tags=["Radar ID / OIDC"])
app.include_router(health.router, prefix="/api/health", tags=["Health"])
app.include_router(regions.router, prefix="/api/regions", tags=["Regions"])
app.include_router(communities.router, prefix="/api/communities", tags=["Communities"])
app.include_router(posts.router, prefix="/api/posts", tags=["Posts"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications"])
app.include_router(scheduler.router, prefix="/api/scheduler", tags=["Smart Scheduler"])
app.include_router(vk_monitoring.router, prefix="/api/vk", tags=["VK Monitoring"])
app.include_router(token_management.router, prefix="/api/tokens", tags=["Token Management"])
app.include_router(service_notifications.router, tags=["Service Notifications"])
app.include_router(schedule_management.router, tags=["Schedule Management"])
app.include_router(system_monitoring.router, tags=["System Monitoring"])
app.include_router(task_monitoring.router, tags=["Task Monitoring"])
app.include_router(publisher.router, prefix="/api/publisher", tags=["VK Publisher"])
app.include_router(parsing.router, tags=["Parsing"])
app.include_router(parsing_stats.router, tags=["Parsing Stats"])  # Postopus migration
app.include_router(filtration.router, prefix="/api/filtration", tags=["Filtration"])
app.include_router(templates_api.router, prefix="/api/templates", tags=["Message Templates"])
app.include_router(ad_cabinet.router, prefix="/api/ad-cabinet", tags=["Ad Cabinet"])
app.include_router(ad_crm.router, prefix="/api/ad-crm", tags=["Ad CRM"])
app.include_router(ad_outreach.router, prefix="/api/ad-outreach", tags=["Ad Outreach"])
# Кабинет рекламодателя (клиентская половина ad-CRM): зона роли advertiser,
# изоляция «только своё» — фильтр client_id из сессии в каждом хендлере.
app.include_router(advertiser_cabinet.router, prefix="/api/advertiser", tags=["Advertiser Cabinet"])
app.include_router(broadcast.router, prefix="/api/broadcast", tags=["Network Broadcast"])
app.include_router(
    subscriber_growth.router,
    prefix="/api/subscriber-growth",
    tags=["Subscriber Growth"],
)
app.include_router(discovery.router, prefix="/api/discovery", tags=["Region Discovery"])
app.include_router(gateway.router, prefix="/api/gateway", tags=["VK Gateway"])
app.include_router(gateway_stats.router, prefix="/api/gateway-stats", tags=["VK Gateway Stats"])
app.include_router(promotion.router, prefix="/api/promotion", tags=["Promotion"])
# Доли наполнения ленты по темам (страница /themes). Операторская зона:
# префикс НЕ добавляется в PUBLIC_PREFIXES, гейт закрывает его сам.
app.include_router(theme_quotas.router, prefix="/api/theme-quotas", tags=["Theme Quotas"])
# Self-serve подключение проектов экосистемы (ADR-0010): своя X-Ecosystem-Key
# защита, поэтому префикс — публичный в middleware/auth_gate.py.
app.include_router(ecosystem.router, prefix="/api/ecosystem", tags=["Ecosystem Self-Serve"])
# HITL-классификатор (ADR-0003). Ingest — публичный (X-API-Key рутины), в PUBLIC_PREFIXES;
# review — операторская сессия (как gateway / gateway-stats).
app.include_router(classifier_ingest.router, prefix="/api/classifier", tags=["Classifier Ingest"])
app.include_router(
    classifier_review.router,
    prefix="/api/classifier-review",
    tags=["Classifier Review"],
)
# Карта ingest-контракта источника (пилот варианта B, pool #367): публичный
# read-only ответ для приёмников конвейера, своя дверь в PUBLIC_EXACT.
app.include_router(ingest_contract.router, prefix="/api/ingest", tags=["Ingest Contract"])


def _radar_template_ctx(request: Request) -> dict:
    """Контекст radar.html: home-ссылка, манифест PWA, адрес входа и каталога.

    ``login_url`` ведёт на ЕСА (issuer), а не на локальный ``/login`` того
    хоста, где открыт Радар: на радар.вмалмыже.рф локальный ``/login`` — та же
    страница, но без ``next`` на корень хоста человек после входа приземлялся
    в ``/radar`` и ловил лишний редирект; на техдомене вход вообще должен идти
    через единый вход, иначе кука ставится не на зону.
    """
    from urllib.parse import quote

    from config.radar_id import get_issuer
    from modules.radar_id import vk_upstream

    at_root = vk_upstream.is_radar_host(request.url.hostname)
    own = f"https://{request.url.hostname}/" if at_root else "/radar"
    return {
        "request": request,
        "home": "/" if at_root else "/radar",
        "manifest_url": ("/manifest.webmanifest" if at_root else "/radar/manifest.webmanifest"),
        "login_url": f"{get_issuer()}/login?next={quote(own, safe='')}",
        "services_url": f"{get_issuer()}/services",
    }


# Иконки PWA — статика, одна на оба манифеста.
_RADAR_MANIFEST_ICONS = [
    {
        "src": "/static/radar/icon-192.png",
        "sizes": "192x192",
        "type": "image/png",
        "purpose": "any maskable",
    },
    {
        "src": "/static/radar/icon-512.png",
        "sizes": "512x512",
        "type": "image/png",
        "purpose": "any maskable",
    },
    {
        "src": "/static/radar/icon.svg",
        "sizes": "any",
        "type": "image/svg+xml",
        "purpose": "any maskable",
    },
]


def radar_manifest(at_root: bool) -> dict:
    """Манифест PWA Радара — маршрутом по хосту, а не статическим файлом.

    Раньше манифест лежал в ``/static/radar/manifest.webmanifest`` с жёстким
    ``start_url``/``scope`` = ``/radar``. На радар.вмалмыже.рф Радар живёт на
    корне, ``/radar`` там — редирект 302 на ``/``; установленное PWA стартовало
    через редирект, а scope ``/radar`` не покрывал реальный корень — SW со scope
    ``/`` и манифест друг о друге не знали. Поэтому ``start_url``/``scope``
    считаются по хосту.

    ``id`` — ВСЕГДА ``/radar``: это идентичность приложения для браузера.
    Chrome сравнивает установленные PWA по ``id`` (по умолчанию = start_url), и
    смени мы его вместе со start_url, уже установленные Радары считались бы
    другим приложением — не обновились бы, а встали бы вторым значком.
    """
    base = "/" if at_root else "/radar"
    return {
        "id": "/radar",
        "name": "Радар",
        "short_name": "Радар",
        "description": "Личная лента источников: VK, RSS, Telegram",
        "start_url": base,
        "scope": base,
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#0d6efd",
        "icons": _RADAR_MANIFEST_ICONS,
    }


_MANIFEST_MEDIA_TYPE = "application/manifest+json"


@app.get("/")
async def root(request: Request):
    """Main dashboard page.

    На каноническом хосте Радара (радар.вмалмыже.рф) корень — это сам Радар
    (заказ владельца 2026-07-26: без лишнего «/radar» в адресе). На остальных
    хостах — операторский дашборд, как раньше.
    """
    from modules.radar_id import vk_upstream

    if vk_upstream.is_radar_host(request.url.hostname):
        return templates.TemplateResponse("radar.html", _radar_template_ctx(request))
    if vk_upstream.is_ad_cabinet_host(request.url.hostname):
        # Прежний выделенный поддомен кабинета: уводим на канонический адрес
        # сарафан…/cabinet (решение владельца 2026-08-26 — бренд САРАФАН).
        return RedirectResponse(vk_upstream.ad_cabinet_canonical_url(), status_code=302)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login")
async def login_page(request: Request):
    """Логин/регистрация (Ф0.1) — единственная публичная страница.

    Единый вход экосистемы: страница распознаёт, откуда пришёл посетитель
    (OIDC-клиент из next= / сервисный поддомен), и показывает «Войти в <Сервис>»
    (modules/radar_id/branding.py).
    """
    from modules.radar_id import vk_upstream
    from modules.radar_id.branding import resolve_brand

    raw_next = request.query_params.get("next")
    # Валидация next на сервере (клиентский JS ей только пользуется): свои
    # относительные пути и абсолютные URL в зоне сессионной куки (единый вход:
    # AuthGate сервисного поддомена шлёт сюда next=https://<родной-хост>/...).
    # Чужое/битое → None (safe_next вернул дефолт, а не сам вход).
    safe_next = None
    if raw_next:
        resolved = vk_upstream.safe_next(raw_next)
        safe_next = resolved if resolved == raw_next else None

    brand = await resolve_brand(raw_next, request.headers.get("host"))
    # Уже вошедший видит, кто он, и кнопку «Выйти из всех сервисов»: выход на
    # сайте-клиенте гасит только сайт, а сессия ЕСА живёт кукой всего домена —
    # без этой кнопки человеку из ЕСА не выйти (владелец 03.09).
    current = getattr(request.state, "user", None)
    current_login = None
    if current is not None:
        current_login = (
            getattr(current, "display_name", None) or getattr(current, "login", None) or "аккаунт"
        )
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "brand": brand,
            "safe_next": safe_next,
            "current_login": current_login,
            "logged_out": request.query_params.get("logged_out") == "1",
            # Сервис попросил переспросить вход (OIDC prompt=login / max_age):
            # человек уже залогинен, и без объяснения форма выглядит поломкой.
            "reauth": request.query_params.get("reauth") == "1",
        },
    )


@app.get("/radar")
async def radar_page(request: Request):
    """Радар (Ф0.4): PWA-лента по подпискам + архив + управление источниками.

    На не-каноническом хосте зоны (вход.вмалмыже.рф после логина без next)
    уводим на радар.вмалмыже.рф — интерфейс живёт на своём поддомене,
    issuer остаётся чистой страницей входа. Сессия переживает переезд
    (кука на весь .вмалмыже.рф).
    """
    from modules.radar_id import vk_upstream

    if vk_upstream.is_radar_host(request.url.hostname):
        # На своём поддомене Радар живёт на корне — «/radar» лишний.
        return RedirectResponse("/", status_code=302)
    canonical = vk_upstream.radar_canonical_redirect(request.url.hostname)
    if canonical:
        return RedirectResponse(canonical, status_code=302)
    return templates.TemplateResponse("radar.html", _radar_template_ctx(request))


@app.get("/cabinet")
async def advertiser_cabinet_page(request: Request):
    """Кабинет рекламодателя (клиентская половина ad-CRM).

    Канонический адрес — сарафан.вмалмыже.рф/cabinet (решение владельца
    2026-08-26): бренд САРАФАН знаком клиентам всех районов, TLS уже есть.
    С других хостов зоны путь уводит на канон. Не-рекламодателю страница
    показывает онбординг.
    """
    from modules.radar_id import vk_upstream

    canonical = vk_upstream.ad_cabinet_canonical_redirect(request.url.hostname)
    if canonical:
        # Query переносим явно: без этого редирект глотал параметры, и
        # `?as_client=<id>` (вход владельца в кабинет клиента) терялся при
        # заходе с неканонического хоста — страница молча открывала свой
        # кабинет вместо чужого. Операторский `_operator_canonical_redirect`
        # query сохраняет давно; здесь это просто не было сделано.
        if request.url.query:
            canonical += f"?{request.url.query}"
        return RedirectResponse(canonical, status_code=302)
    return templates.TemplateResponse("advertiser_cabinet.html", {"request": request})


@app.get("/radar/manifest.webmanifest")
async def radar_manifest_route():
    """Манифест PWA для хостов, где Радар живёт на /radar (техдомен, локалка).
    Публичен (PUBLIC_EXACT): браузер тянет манифест без cookie."""
    return JSONResponse(radar_manifest(at_root=False), media_type=_MANIFEST_MEDIA_TYPE)


@app.get("/manifest.webmanifest")
async def radar_root_manifest_route(request: Request):
    """Манифест PWA для корневого scope — только на радар.вмалмыже.рф, как /sw.js."""
    from fastapi import HTTPException

    from modules.radar_id import vk_upstream

    if not vk_upstream.is_radar_host(request.url.hostname):
        raise HTTPException(status_code=404)
    return JSONResponse(radar_manifest(at_root=True), media_type=_MANIFEST_MEDIA_TYPE)


@app.get("/radar/sw.js")
async def radar_service_worker():
    """Service worker Радара. Отдаётся с /radar/* (внутри RADAR_PREFIXES гейта);
    Service-Worker-Allowed расширяет scope до /radar (сам файл лежит глубже)."""
    return FileResponse(
        str(BASE_DIR / "web" / "static" / "radar" / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/radar"},
    )


@app.get("/sw.js")
async def radar_root_service_worker(request: Request):
    """Service worker Радара для корневого scope (радар.вмалмыже.рф/).

    Только на каноническом радар-хосте: на операторских хостах корневой SW
    не нужен и не отдаётся (404)."""
    from fastapi import HTTPException

    from modules.radar_id import vk_upstream

    if not vk_upstream.is_radar_host(request.url.hostname):
        raise HTTPException(status_code=404)
    return FileResponse(
        str(BASE_DIR / "web" / "static" / "radar" / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/services")
async def services_catalog(request: Request):
    """Каталог сервисов экосистемы вмалмыже.рф (public).

    Заказ владельца 2026-07-26: на ЕСА (вход.вмалмыже.рф) — список всех
    сайтов-сервисов Малмыжа, а на каждом сервисе — кнопка сюда, чтобы
    пользователь быстро перемещался между сервисами."""
    from config.services_catalog import get_services

    return templates.TemplateResponse(
        "services.html", {"request": request, "services": get_services()}
    )


@app.get("/regions")
async def regions_page(request: Request):
    """Regions page"""
    return templates.TemplateResponse("regions.html", {"request": request})


@app.get("/promotion")
async def promotion_page(request: Request):
    """Раскрутка молодых сообществ сети: состав, план, журнал, настройки."""
    return templates.TemplateResponse("promotion.html", {"request": request})


@app.get("/themes")
async def themes_page(request: Request):
    """Доли наполнения ленты по темам: план, кандидаты, факт."""
    return templates.TemplateResponse("themes.html", {"request": request})


@app.get("/regions/links")
async def region_links_page(request: Request):
    """Публичный лендинг сети САРАФАН — сообщества + реклама.

    Заказ владельца 2026-07-29 (вторая итерация): страница открыта без входа
    (allowlist в ``middleware/auth_gate.py`` — только этот путь и его API,
    остальной интерфейс под сессией). Две вкладки: список сообществ по областям
    (имя — подписчики — ссылка, готов к копированию в пост/сообщение VK) и
    расценки на рекламу с контактами. Цены/контакты — ``config/ad_landing.py``.
    Данные списка — ``GET /api/regions/vk-links``, подписчики и красивые адреса
    групп обновляются ночной таской ``collect_member_snapshots``.
    """
    import json

    from config.ad_landing import get_ad_landing_context

    ctx = get_ad_landing_context()
    # Таблица цен считается на сервере (config/ad_landing.py, покрыта тестами) и
    # уезжает в <script type=application/json>: панель выбора только показывает
    # готовое число, чтобы правило скидок не жило в двух местах.
    #
    # Markup внутри <script> экранировать НЕЛЬЗЯ (в шаблоне стоит |safe): это
    # raw-text элемент, HTML-сущности в нём не декодируются, и autoescape Jinja
    # превращает кавычки в &#34; — JSON.parse падает, а панель молча остаётся
    # без цены (так и было на проде, пока не поймали curl'ом). Экранируем
    # только «<», чтобы строка вида "</script>" не закрыла тег.
    ctx["price_table_json"] = json.dumps(ctx["price_table"], ensure_ascii=False).replace(
        "<", "\\u003c"
    )
    return templates.TemplateResponse("region_links.html", {"request": request, **ctx})


@app.get("/posts")
async def posts_page(request: Request):
    """Posts page"""
    return templates.TemplateResponse("posts.html", {"request": request})


@app.get("/communities")
async def communities_page(request: Request):
    """Communities page"""
    return templates.TemplateResponse("communities.html", {"request": request})


@app.get("/notifications")
async def notifications_page(request: Request):
    """Notifications page"""
    return templates.TemplateResponse("notifications.html", {"request": request})


@app.get("/templates")
async def templates_page(request: Request):
    """Message templates CRUD page (etap 4b)"""
    return templates.TemplateResponse("templates.html", {"request": request})


@app.get("/ad")
async def ad_page(request: Request):
    """Единый рекламный кабинет (С1): инбокс, CRM, планировщик и статистика во вкладках."""
    return templates.TemplateResponse("ad.html", {"request": request})


@app.get("/ad/client/{client_id}")
async def ad_client_detail_page(client_id: int, request: Request):
    """Страница работы с одним клиентом: профиль, оплаты, публикации, переписка."""
    return templates.TemplateResponse(
        "ad_client_detail.html",
        {"request": request, "client_id": client_id},
    )


@app.get("/broadcast")
async def broadcast_page(request: Request):
    """Сетевая рассылка: композер кампании + цели + расписание/повтор + очередь."""
    return templates.TemplateResponse("broadcast.html", {"request": request})


@app.get("/gateway-stats")
async def gateway_stats_page(request: Request):
    """Статистика использования VK-шлюза: кто/когда/сколько + последние запросы."""
    return templates.TemplateResponse("gateway_stats.html", {"request": request})


@app.get("/classifier")
async def classifier_page(request: Request):
    """Лента вердиктов HITL-классификатора (shadow): пост + вердикт + кнопки."""
    return templates.TemplateResponse("classifier.html", {"request": request})


@app.get("/ad-cabinet")
async def ad_cabinet_page():
    """Старый путь — редирект на единый /ad (вкладка «Входящие заявки»)."""
    return RedirectResponse(url="/ad")


@app.get("/ad-crm")
async def ad_crm_page():
    """Старый путь — редирект на единый /ad (вкладка «Клиенты и воронка»)."""
    return RedirectResponse(url="/ad#crm")


@app.get("/subscriber-growth")
async def subscriber_growth_page(request: Request):
    """Сравнительная динамика роста подписчиков сообществ (один график + чекбоксы)."""
    return templates.TemplateResponse("subscriber_growth.html", {"request": request})


@app.get("/regions/new")
async def region_new_page(request: Request):
    """Wizard для добавления нового региона (big idea 2026-05-22)."""
    return templates.TemplateResponse("region_new.html", {"request": request})


@app.get("/regions/{region_code}/discovery")
async def region_discovery_page(request: Request, region_code: str):
    """Список кандидатов на сообщества для региона (big idea 2026-05-22)."""
    from config.runtime import discovery_ai_batch_mode

    return templates.TemplateResponse(
        "region_discovery.html",
        {
            "request": request,
            "region_code": region_code,
            "ai_batch_mode": discovery_ai_batch_mode(),
        },
    )


@app.get("/regions/{region_code}/prepare")
async def region_prepare_page(request: Request, region_code: str):
    """Подготовка discovery: localities + keywords для региона."""
    return templates.TemplateResponse(
        "region_prepare.html",
        {"request": request, "region_code": region_code},
    )


@app.get("/regions/{region_code}/diagnostics")
async def region_diagnostics_page(request: Request, region_code: str):
    """Прогон пайплайна без публикации (dry-run): что отфильтровалось / собралось."""
    return templates.TemplateResponse(
        "region_diagnostics.html",
        {"request": request, "region_code": region_code},
    )


@app.get("/regions/{region_code}/discovery/ai-batch")
async def region_ai_batch_page(request: Request, region_code: str):
    """Human-in-the-loop AI categorisation через clipboard."""
    from config.runtime import discovery_ai_batch_mode

    return templates.TemplateResponse(
        "region_ai_batch.html",
        {
            "request": request,
            "region_code": region_code,
            "ai_batch_mode": discovery_ai_batch_mode(),
        },
    )


@app.get("/tokens")
async def tokens_page(request: Request):
    """Token management page"""
    return templates.TemplateResponse("tokens.html", {"request": request})


@app.get("/publisher")
async def publisher_page(request: Request):
    """VK Publisher page"""
    return templates.TemplateResponse("publisher.html", {"request": request})


@app.get("/monitoring")
async def monitoring_page(request: Request):
    """Service monitoring page"""
    return templates.TemplateResponse("monitoring.html", {"request": request})


@app.get("/test_monitoring")
async def test_monitoring_page(request: Request):
    """Test monitoring page"""
    with open("test_monitoring.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


@app.get("/diagnostic_monitoring")
async def diagnostic_monitoring_page(request: Request):
    """Diagnostic monitoring page"""
    with open("diagnostic_monitoring.html", "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


@app.get("/schedule")
async def schedule_page(request: Request):
    """Schedule management page"""
    return templates.TemplateResponse("schedule.html", {"request": request})


@app.get("/parsing")
async def parsing_page(request: Request):
    """VK parsing page"""
    return templates.TemplateResponse("parsing.html", {"request": request})


@app.get("/parsing-stats")
async def parsing_stats_page(request: Request):
    """Parsing statistics page (Postopus migration)"""
    return templates.TemplateResponse("parsing_stats.html", {"request": request})


@app.get("/publications")
async def publications_page(request: Request):
    """История публикаций сводок по регионам/темам (со ссылками на VK)."""
    return templates.TemplateResponse("publications.html", {"request": request})


@app.get("/filtration")
async def filtration_page(request: Request):
    """Настройки фильтрации сводок и правил отбора постов"""
    return templates.TemplateResponse("filtration.html", {"request": request})


@app.get("/favicon.ico")
async def favicon():
    """Favicon"""
    from fastapi.responses import Response

    # Простой SVG favicon
    svg_content = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" '
        'viewBox="0 0 32 32">'
        '<rect width="32" height="32" fill="#0d6efd"/>'
        '<text x="16" y="20" text-anchor="middle" fill="white" '
        'font-family="Arial" font-size="16" font-weight="bold">S</text>'
        "</svg>"
    )
    return Response(content=svg_content, media_type="image/svg+xml")


@app.get("/metrics")
async def metrics(request: Request):
    """
    Prometheus metrics endpoint.

    Доступ — только локально (127.0.0.1). Prometheus scrape'ит из того же
    хоста, для внешних запросов отдаём 404, чтобы не светить structure.
    Override через env ``SETKA_METRICS_PUBLIC=1`` если надо открыть наружу
    (например, dev-локально или специально настроенный proxy).
    """
    import os

    from fastapi import HTTPException
    from fastapi.responses import Response

    from monitoring.metrics import get_metrics, update_business_metrics

    if os.getenv("SETKA_METRICS_PUBLIC", "").strip() not in ("1", "true", "yes"):
        client_host = request.client.host if request.client else None
        # Через nginx-proxy client.host = 127.0.0.1, но реальный IP — в X-Forwarded-For.
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        effective_ip = forwarded or client_host or ""
        if effective_ip not in ("127.0.0.1", "::1", "localhost", ""):
            raise HTTPException(status_code=404)

    await update_business_metrics()
    content, content_type = await get_metrics()
    return Response(content=content, media_type=content_type)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
