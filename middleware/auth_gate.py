"""AuthGateMiddleware — app-level auth + изоляция ролей (Ф0.1 контент-радара).

Secure by default: ВСЁ приложение закрыто, кроме явного allowlist'а
(PUBLIC_PREFIXES). Новый операторский route защищён автоматически — забыть
повесить зависимость невозможно, enforcement живёт в одном месте.

Роли (директива brain 2026-06-11, решение владельца раунд 2 §1):
- ``operator`` — весь setka (регионы/CRM/токены/мониторинг/...), **и только
  для аккаунтов владельца** (заказ владельца 2026-08-02, см. ниже).
- ``radar``    — только контент-радар (RADAR_PREFIXES) + auth-эндпоинты.
  Операторский setka для radar-юзера = 403.

Сессия — stateless signed-cookie (modules/radar/auth.py). На каждом запросе
юзер перечитывается из БД по PK: проверяем is_active и совпадение
password-fragment (смена пароля / деактивация инвалидирует сессию немедленно).

Неаутентифицированный запрос: браузерный GET (Accept: text/html) → 302 на
/login?next=..., API/прочее → 401 JSON. 403 — аутентифицирован, но не та роль.

Исключение — front-channel GET-эндпоинты (OIDC authorize, FRONT_CHANNEL_GET_PATHS):
в них по спеку всегда приходят через redirect браузера, поэтому неаутентифи-
цированный GET **всегда** → 302 на /login, даже без Accept: text/html. Иначе
curl/мониторинг без браузерного заголовка видит ложный 401 «сломан» (запрос
trener через brain 2026-07-10).

Kill-switch: env ``WEB_AUTH_ENABLED=0`` отключает гейт целиком (локальный dev
без БД-юзеров). Дефолт — включено; на проде не выключать.

/metrics — особый случай: Prometheus скрейпит с localhost; снаружи метрики
не отдаём (оператору залогиненным — можно).

Владелец (заказ 2026-08-02): сайт САРАФАНА видит **только его аккаунт**, и
только после входа через ЕСА. Роль ``operator`` сама по себе доступа больше
не даёт — решает личность (``_is_owner``: логин или ВК-id, оба входа
владельца обязаны работать). На домене сети ``сарафан.вмалмыже.рф``
посторонний — хоть аноним, хоть залогиненный radar-юзер — видит ровно одну
публичную ссылку ``/regions/links``; остальное, включая ``/services``,
уходит за вход.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from modules.radar.auth import SESSION_COOKIE, password_fragment, verify_session_token

logger = logging.getLogger(__name__)

# Открыто без auth (префиксное сравнение):
PUBLIC_PREFIXES = (
    "/login",
    "/services",  # каталог сервисов экосистемы — публичная витрина ссылок
    "/static/",
    "/favicon.ico",
    "/api/health",  # internal watchdogs/CI ходят на 127.0.0.1:8000 без cookie
    "/api/gateway/",  # VK-шлюз: своя X-API-Key защита (web/api/gateway.py).
    # trailing slash важен: /api/gateway-stats НЕ public — это операторская
    # статистика под сессионной cookie (web/api/gateway_stats.py).
    "/api/classifier/",  # HITL-классификатор ingest (облачная рутина): своя
    # X-API-Key защита (web/api/classifier_ingest.py). Аналогично шлюзу,
    # trailing slash важен: /api/classifier-review НЕ public (операторская лента).
    "/api/ecosystem/",  # self-serve подключение проектов (ADR-0010): своя
    # X-Ecosystem-Key защита (web/api/ecosystem.py). Заявку шлёт сервер
    # проекта-клиента, сессионной cookie у него нет и быть не может.
    "/api/auth/login",
    "/api/auth/register",
    "/.well-known/",
    # Радар-ID OIDC (web/api/radar_id.py): token/userinfo зовут серверы
    # клиентов без cookie — своя client-auth (secret/PKCE/Bearer).
    # /oidc/authorize сюда НЕ входит — ему нужна сессия пользователя.
    "/oidc/token",
    "/oidc/userinfo",
    # RP-initiated logout: гость (кука уже протухла) тоже должен «выйти» и
    # вернуться на сайт, а не упереться в /login с чужим next.
    "/oidc/logout",
    # ВК-вход Радар-ID: пользователь ещё НЕ аутентифицирован (это и есть вход).
    "/auth/vk/",
)

# Открыто без auth по ТОЧНОМУ совпадению пути (с точностью до хвостового «/»).
# Публичный лендинг сети и его данные лежат здесь, а не в PUBLIC_PREFIXES:
# префиксное сравнение открывало бы заодно любой будущий путь, начинающийся с
# этих строк (`/regions/links-export` и т.п.). Пока лендинг был одной страницей
# среди прочих, цена ошибки была мала; с 2026-08-02 это единственная публичная
# дверь в домен, закрытый для всех, кроме владельца, — и она обязана быть узкой.
PUBLIC_EXACT = (
    "/regions/links",  # публичный лендинг «сообщества сети + реклама»
    "/api/regions/vk-links",  # его же данные (read-only список групп)
    # Карта ingest-контракта источника (пилот варианта B, pool #367): read-only,
    # секретов не содержит (имена/значения ключей намеренно не входят), читают
    # серверы приёмников, у которых сессионной cookie нет и не будет.
    "/api/ingest/contract",
    # Service worker Радара на его домене (там кабинет живёт на корне, поэтому
    # SW регистрируется как «/sw.js» со scope «/»). Это статический файл без
    # приватных данных, и он обязан быть публичным: браузер сам перепроверяет
    # SW фоновым запросом, в том числе когда сессия уже протухла. Под ролевым
    # гейтом он отдавал 403, из-за чего PWA не ставилось, офлайна не было, а
    # web-push молча подвисал на `navigator.serviceWorker.ready`.
    "/sw.js",
    # Манифест PWA — тот же класс, что SW: браузер запрашивает его БЕЗ cookie
    # (`<link rel=manifest>` без crossorigin=use-credentials), поэтому под
    # гейтом он получал редирект/403, и «Установить приложение» не появлялось.
    # Отдаётся маршрутом по хосту (main.radar_manifest), приватных данных нет.
    "/manifest.webmanifest",
    "/radar/manifest.webmanifest",
)

# Публичный домен сети (сарафан.вмалмыже.рф) и единственная публичная ссылка
# на нём. Заказ владельца 2026-08-02: сайт САРАФАНА виден только владельцу и
# только после входа через ЕСА; посторонний получает ровно витрину рекламы.
SARAFAN_CANONICAL_HOST_DEFAULT = "xn--80aaa6cmey.xn--80adkdyec4j.xn--p1ai"
LANDING_PATH = "/regions/links"

# Пути, которые НЕ уезжают на канонический операторский хост — у них свой дом.
# Публичные пути (PUBLIC_PREFIXES/PUBLIC_EXACT) в этот список не нужны: гейт
# отпускает их раньше, до проверки канона.
OPERATOR_CANONICAL_EXEMPT = (
    # Front-channel issuer'а. /oidc/token и /oidc/userinfo и так публичны, а вот
    # /oidc/authorize требует сессии и ОБЯЗАН отработать на вход.вмалмыже.рф:
    # уведи его на сарафан — и сломается вход на сайты-клиенты экосистемы.
    "/oidc/",
    # Своя зона со своим каноническим хостом (radar_canonical_redirect).
    "/radar",
    # Кабинет рекламодателя: своя зона на кабинет.вмалмыже.рф. Без этой строки
    # GET /cabinet на любом хосте утаскивало бы на сарафан, где не-владельцу
    # закрыто всё, — клиент не смог бы войти в собственный кабинет.
    "/cabinet",
    # Машинные вызовы. Страховка: их и так отсекает _wants_html, но цена ошибки
    # здесь — молчаливо сломанный VK-шлюз соседних проектов.
    "/api/",
    "/.well-known/",
)

# Публично везде, КРОМЕ домена сети: на сарафане открыта ровно одна ссылка
# (LANDING_PATH), остальные страницы — под входом. Каталог сервисов при этом
# не пропадает из экосистемы: его публичная поверхность — вход.вмалмыже.рф
# (мандат brain 2026-08-01), туда же ведут кнопки «Сервисы» с других сайтов.
SARAFAN_CLOSED_PUBLIC = ("/services",)

# Обслуживание сессии и OIDC — не «содержимое сайта», поэтому правило домена
# сети их не глотает даже у постороннего. Иначе залогиненный чужой юзер не мог
# бы с этого хоста выйти из сессии (403 на logout — тупик), а front-channel
# OIDC-переход терял бы query и приезжал не туда. Дальше их всё равно судит
# обычная ролевая логика — доступа к содержимому это не даёт.
#
# `/api/radar/` здесь по той же логике: лента Радара — зона самого гостя, а не
# содержимое САРАФАНА. Уже установленные PWA помнят СТАРЫЙ адрес Радара
# (сарафан.вмалмыже.рф/radar, до переезда 26.07): оболочка поднимается из кэша
# service worker'а, и без этой строки её запросы данных получали бы 403.
# Навигация при этом всё равно уводит гостя на радар.вмалмыже.рф — редирект
# ниже про страницы, а не про XHR.
SARAFAN_ALLOWED_FOR_GUESTS = (
    "/api/auth/logout",
    "/api/auth/me",
    "/oidc/",
    "/api/radar/",
)

# Аккаунты владельца — единственные, кому открыт операторский SETKA (заказ
# владельца 2026-08-02: «внутренности сарафана видит только мой аккаунт»).
# Роль ``operator`` сама по себе больше не пропуск: саморегистрация выдаёт
# роль ``radar``, но роль в БД можно и поменять, а список аккаунтов — нет.
#
# Владелец входит ДВУМЯ путями, и оба обязаны работать, иначе он запрёт себя
# сам: логином/паролем (аккаунт ``valstan``) и через ВКонтакте на
# вход.вмалмыже.рф — там он приезжает своей ВК-личностью, у которой другой
# аккаунт и роль ``radar``. Поэтому владельца опознаём по личности, а не по
# роли: совпал логин ИЛИ ВК-id → пускаем в операторскую зону.
OWNER_LOGINS_DEFAULT = "valstan"
OWNER_VK_IDS_DEFAULT = "20002978"  # vk.ru/valstan_valstan, аккаунт «Валентин Савиных»

# Front-channel GET-эндпоинты: в них ходят только через redirect браузера
# (OIDC authorization endpoint). Неаутентифицированный GET сюда → ВСЕГДА 302 на
# /login, независимо от Accept — чтобы curl/мониторинг без браузерного заголовка
# не получал ложный 401 (запрос trener через brain 2026-07-10). Точное сравнение
# пути: не хотим ловить гипотетический /oidc/authorize-что-то.
FRONT_CHANNEL_GET_PATHS = ("/oidc/authorize",)

# Куда пускаем роль radar (плюс PUBLIC сверху):
RADAR_PREFIXES = (
    "/radar",
    "/api/radar/",
    "/api/auth/logout",
    "/api/auth/me",
    "/oidc/",  # OIDC authorize: любой залогиненный RadarUser может входить на сайты
)

# Куда пускаем роль advertiser — клиента рекламного кабинета (плюс PUBLIC
# сверху). Операторские /ad и /api/ad-crm сюда сознательно НЕ входят: клиент
# заперт в своей зоне, изоляция «видит только своё» дальше держится фильтром
# client_id в каждом хендлере /api/advertiser/*.
ADVERTISER_PREFIXES = (
    "/cabinet",
    "/api/advertiser/",
    "/api/auth/logout",
    "/api/auth/me",
    "/oidc/",  # как и radar: залогиненный юзер может входить на сайты экосистемы
)

# Онбординг рекламодателя: страница кабинета, статус-эндпоинт и сам POST
# онбординга открыты ЛЮБОМУ аутентифицированному (точное сравнение путей).
# Иначе radar-юзеру некуда прийти, чтобы стать рекламодателем: роль advertiser
# выдаёт именно онбординг, а до него у юзера её ещё нет.
ADVERTISER_ONBOARDING_EXACT = (
    "/cabinet",
    "/api/advertiser/me",
    "/api/advertiser/onboarding",
    # Телеметрия браузера клиента: ошибка на странице онбординга — самый ценный
    # сигнал, а роли advertiser у такого юзера ещё нет.
    "/api/advertiser/telemetry",
)

UserLoader = Callable[[int], Awaitable[Optional[object]]]


async def _default_user_loader(user_id: int):
    """Достать RadarUser по PK свежей сессией (импорт внутри — лёгкий старт тестов)."""
    from database import models  # noqa: F401 - конфигурация мапперов (PR #189)
    from database.connection import AsyncSessionLocal
    from database.models_extended import RadarUser

    async with AsyncSessionLocal() as session:
        return await session.get(RadarUser, user_id)


def _is_prefixed(path: str, prefixes: tuple) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in prefixes)


def _is_exact(path: str, paths: tuple) -> bool:
    """Точное совпадение пути, с точностью до хвостового «/» (см. PUBLIC_EXACT)."""
    normalized = path.rstrip("/") or "/"
    return any(normalized == p.rstrip("/") for p in paths)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _is_local_client(request: Request) -> bool:
    client = request.client
    return bool(client and client.host in ("127.0.0.1", "::1"))


def _idna(host: str) -> str:
    """Хост в ASCII (кириллический и punycode — одно и то же имя)."""
    host = (host or "").strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return host


def _on_radar_host(request: Request) -> bool:
    """Запрос пришёл на канонический хост Радара (радар.вмалмыже.рф)?"""
    try:
        from modules.radar_id.vk_upstream import is_radar_host

        return is_radar_host(request.url.hostname)
    except Exception:  # noqa: BLE001 - хост-детект не должен ронять гейт
        return False


def _radar_canonical_url(request: Request) -> Optional[str]:
    """Абсолютный адрес Радара на его домене — или ``None``, если он не настроен.

    Нужен, чтобы radar-юзера, забредшего на домен САРАФАНА, отправить домой,
    а не на витрину рекламы. Кука общая на ``.вмалмыже.рф`` — сессия переезд
    переживает.
    """
    try:
        from modules.radar_id.vk_upstream import radar_canonical_redirect

        return radar_canonical_redirect(request.url.hostname)
    except Exception:  # noqa: BLE001 - косметика маршрутизации не роняет гейт
        logger.warning("AuthGate: radar canonical resolve failed", exc_info=True)
        return None


def _on_ad_cabinet_host(request: Request) -> bool:
    """Запрос пришёл на канонический хост кабинета рекламодателя?"""
    try:
        from modules.radar_id.vk_upstream import is_ad_cabinet_host

        return is_ad_cabinet_host(request.url.hostname)
    except Exception:  # noqa: BLE001 - хост-детект не должен ронять гейт
        return False


def _ad_cabinet_canonical_url(request: Request) -> Optional[str]:
    """Абсолютный канонический адрес кабинета рекламодателя — или ``None``.

    Нужен, чтобы advertiser-юзера, забредшего на витрину, отправить домой.
    Канон — сарафан…/cabinet (2026-08-26): с корня домена сети advertiser
    уезжает на путь кабинета того же домена, цикла нет — путь входит в
    ``ADVERTISER_PREFIXES`` и витринной веткой не перехватывается.
    """
    try:
        from modules.radar_id.vk_upstream import ad_cabinet_canonical_url

        return ad_cabinet_canonical_url()
    except Exception:  # noqa: BLE001 - косметика маршрутизации не роняет гейт
        logger.warning("AuthGate: ad cabinet canonical resolve failed", exc_info=True)
        return None


def _on_sarafan_host(request: Request) -> bool:
    """Запрос пришёл на публичный домен сети (сарафан.вмалмыже.рф)?

    Сравнение — в punycode: кириллический хост и его ASCII-форма это одно имя.
    Переопределяется env ``SARAFAN_CANONICAL_HOST`` (пустое значение выключает
    поведение — например, если домен когда-нибудь сменится).
    """
    canonical = _idna(os.getenv("SARAFAN_CANONICAL_HOST", SARAFAN_CANONICAL_HOST_DEFAULT))
    host = _idna(request.url.hostname or "")
    return bool(canonical and host and host == canonical)


def _operator_canonical_redirect(request: Request) -> Optional[str]:
    """Адрес операторской страницы на её домене — или ``None``, если он и так свой.

    Приложение одно, а хостов у него четыре: сарафан, issuer (вход.вмалмыже.рф),
    радар и технический домен провайдера. Операторские страницы (``/tokens``,
    ``/ad``, ``/posts``, ``/monitoring``, дашборд на ``/``) отдавались на КАЖДОМ
    из них, поэтому адрес в строке браузера врал о том, что показано: открыв
    ``вход.вмалмыже.рф``, владелец оказывался в операторском САРАФАНЕ, а адрес
    продолжал утверждать, что он на странице входа.

    Тот же класс, что чинили 26.07 для Радара (``radar_canonical_redirect``), —
    тогда закрыли одну зону из двух.

    На техническом домене провайдера у этого же бага есть вторая, злая половина:
    сессионная кука выдаётся на зону ``.вмалмыже.рф`` (``SESSION_COOKIE_DOMAIN``)
    и на чужой хост браузером просто не отправляется. Вход там зацикливался
    намертво — ``POST /api/auth/login`` отвечал 200, а следующий же запрос
    приезжал неаутентифицированным и снова падал на форму входа. Поэтому
    техдомен уводим тоже, хотя сессия туда и не распространяется: цель как раз
    в том, чтобы браузер там не жил.

    ``None`` (= остаёмся на месте) для: канонического хоста; хоста Радара (у него
    свой канон); не-браузерных запросов; путей из ``OPERATOR_CANONICAL_EXEMPT``.

    Не-браузерные запросы не трогаем принципиально, а не для экономии: на этих
    хостах сидят машинные интеграции — VK-шлюз соседних проектов, ingest,
    диагностические скрипты (``docs/GATEWAY.md``). Редирект со сменой хоста
    пережил бы не всякий клиент, а часть из них ходит именно на ASCII-хост
    потому, что кириллический IDN им не по зубам.
    """
    if request.method != "GET" or not _wants_html(request):
        return None
    # Многохостовая топология включена тем же переключателем, что и общая сессия
    # зоны. Без SESSION_COOKIE_DOMAIN приложение живёт на одном хосте (локальная
    # разработка, тесты) — уводить некуда и не с чего.
    if not (os.getenv("SESSION_COOKIE_DOMAIN") or "").strip():
        return None
    canonical = _idna(os.getenv("SARAFAN_CANONICAL_HOST", SARAFAN_CANONICAL_HOST_DEFAULT))
    if not canonical:
        return None
    host = _idna(request.url.hostname or "")
    if not host or host == canonical:
        return None
    if _on_radar_host(request):
        return None
    # Хост кабинета рекламодателя — своя зона (как радар): любой GET на нём
    # остаётся на месте, интерфейс кабинета живёт на корне этого поддомена.
    if _on_ad_cabinet_host(request):
        return None
    path = request.url.path
    if _is_prefixed(path, OPERATOR_CANONICAL_EXEMPT):
        return None
    target = f"https://{canonical}{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    return target


def _csv_env(name: str, default: str) -> frozenset:
    """Множество значений из env-списка через запятую, в нижнем регистре.

    Пустая или пробельная переменная = «не задано» → берём дефолт. Это не
    придирка: пустой ``SETKA_OWNER_LOGINS=`` в `/etc/setka/setka.env` иначе
    означал бы «владельцев нет вообще» и запер бы владельца снаружи своего же
    сайта. Отключать правило пустым значением нельзя — только сменить состав.
    """
    raw = (os.getenv(name) or "").strip() or default
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def _is_owner(user) -> bool:
    """Аккаунт владельца? Опознаём по логину ИЛИ по ВК-id (см. константы выше).

    Оба идентификатора необязательны у конкретного аккаунта: у ВК-входа логин
    пустой, у парольного аккаунта нет ``vk_user_id``. Совпадения по пустому
    значению быть не должно — отсюда явные проверки на непустоту.

    ⚠️ Логин-ветка ДОПОЛНИТЕЛЬНО требует роль ``operator``, и это не
    перестраховка. Логин выбирает тот, кто регистрируется: ``POST
    /api/auth/register`` публичен, инвайт-код по решению владельца роздан
    соседним проектам, а проверка занятости логина в Postgres регистро-
    зависима — значит «VALSTAN» заводится рядом с «valstan» и без этой
    проверки роли прошёл бы сюда как владелец. Роль же публичная регистрация
    жёстко ставит ``radar`` (web/api/auth.py), поднять её можно только с бокса
    (``scripts/create_radar_user.py --role operator``). ВК-ветке такая защита
    не нужна: ``vk_user_id`` приезжает от ВКонтакте, пользователь его не
    выбирает — поэтому владелец, вошедший через ВК с ролью ``radar``, проходит.
    """
    login = (getattr(user, "login", None) or "").strip().lower()
    if (
        login
        and getattr(user, "role", "") == "operator"
        and login in _csv_env("SETKA_OWNER_LOGINS", OWNER_LOGINS_DEFAULT)
    ):
        return True
    vk_id = getattr(user, "vk_user_id", None)
    if vk_id is not None and str(vk_id).strip():
        return str(vk_id).strip().lower() in _csv_env("SETKA_OWNER_VK_IDS", OWNER_VK_IDS_DEFAULT)
    return False


def is_owner_account(user) -> bool:
    """Публичная обёртка над :func:`_is_owner` для кода вне middleware.

    Нужна, чтобы «владелец ли это» определялось РОВНО в одном месте. Без неё
    прикладной код неизбежно заводит вторую проверку (сравнение логина, флаг в
    БД), и две копии правила расходятся молча: гейт пускает, а фича — нет, или
    наоборот. Здесь тот же класс, что у двух копий канона в ``AGENTS.md``.
    """
    return _is_owner(user)


def _login_redirect(request: Request, next_url: str) -> str:
    """URL страницы входа для неаутентифицированного браузерного GET.

    Единый вход экосистемы (заказ владельца 2026-07-26): пользователь на
    сервисном поддомене ``*.вмалмыже.рф`` (радар, сарафан, ...) отправляется
    не на локальный ``/login`` того же хоста, а на **центральную** страницу
    входа issuer'а (``вход.вмалмыже.рф``) с абсолютным ``next`` — выбрал там
    способ входа, вернулся на родной домен авторизованным (кука выдаётся на
    весь ``.вмалмыже.рф``).

    Граница доверия та же, что у возврата после ВК-входа: хост обязан делить
    с нами сессионную куку (``SESSION_COOKIE_DOMAIN``). Хост вне зоны, сам
    issuer или локальная разработка (зона не задана) → прежний относительный
    ``/login`` — деградация, не смена поведения.
    """
    local = f"/login?next={quote(next_url)}"
    try:
        from urllib.parse import urlsplit

        from config.radar_id import get_issuer
        from modules.radar_id.vk_upstream import host_shares_session

        host = _idna(request.url.hostname or "")
        issuer = get_issuer()
        issuer_host = _idna(urlsplit(issuer).hostname or "")
        if not host or not issuer_host or host == issuer_host:
            return local
        if not host_shares_session(host):
            return local
        absolute_next = f"https://{host}{next_url}"
        return f"{issuer}/login?next={quote(absolute_next)}"
    except Exception:  # noqa: BLE001 - логин важнее косметики единого входа
        logger.warning("AuthGate: central login resolve failed", exc_info=True)
        return local


class AuthGateMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, user_loader: Optional[UserLoader] = None):
        super().__init__(app)
        self._user_loader = user_loader or _default_user_loader

    async def dispatch(self, request: Request, call_next):
        if os.getenv("WEB_AUTH_ENABLED", "1") == "0":
            return await call_next(request)

        path = request.url.path

        on_sarafan = _on_sarafan_host(request)
        if _is_exact(path, PUBLIC_EXACT):
            return await call_next(request)
        if _is_prefixed(path, PUBLIC_PREFIXES) and not (
            on_sarafan and _is_prefixed(path, SARAFAN_CLOSED_PUBLIC)
        ):
            return await call_next(request)

        # Prometheus скрейпит /metrics с localhost — пускаем без cookie.
        if path == "/metrics" and _is_local_client(request):
            return await call_next(request)

        # Операторская страница открыта не на своём домене — уводим на канон
        # ДО аутентификации: иначе на техдомене провайдера пользователь сперва
        # прошёл бы вход, чья кука туда не доедет, и вернулся бы на форму входа.
        canonical_target = _operator_canonical_redirect(request)
        if canonical_target:
            return RedirectResponse(canonical_target, status_code=302)

        user = await self._authenticate(request)
        if user is None:
            # Единственное исключение из «нет сессии → на /login»: OIDC-запрос
            # с `prompt=none`. Клиент этим параметром говорит «проверь тихо, UI
            # не показывай»; редирект на форму входа — ровно то, что он
            # запретил, и в iframe он выглядит как молчаливое зависание вместо
            # честного `login_required`. Пускаем запрос к эндпоинту: тот
            # проверит client_id и redirect_uri и вернёт ошибку КЛИЕНТУ, а не
            # человеку. Данных это не открывает — ответом будет только
            # redirect с `error=login_required`.
            if request.method == "GET" and path in FRONT_CHANNEL_GET_PATHS:
                if "none" in (request.query_params.get("prompt") or "").split():
                    return await call_next(request)
            # Редирект на login для браузерного GET, а также для front-channel
            # GET-путей (OIDC authorize) даже без браузерного Accept — они
            # достижимы только через redirect user-agent'а, 401 там бессмыслен.
            redirect_to_login = request.method == "GET" and (
                _wants_html(request) or path in FRONT_CHANNEL_GET_PATHS
            )
            if redirect_to_login:
                # next с query string: OIDC authorize (и любой GET с параметрами)
                # обязан вернуться на полный URL, не только path.
                next_url = request.url.path
                if request.url.query:
                    next_url += f"?{request.url.query}"
                return RedirectResponse(_login_redirect(request, next_url), status_code=302)
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)

        request.state.user = user

        # Владелец — единственный, кому открыт сайт САРАФАНА; его личность
        # старше роли (ВК-вход даёт ему аккаунт с ролью radar, см. _is_owner).
        if _is_owner(user):
            return await call_next(request)

        # Дальше — все, кто НЕ владелец. На домене сети им не видно ничего,
        # кроме витрины: заказ владельца 2026-08-02. Роль radar своей зоной
        # пользуется на своём домене (радар.вмалмыже.рф), туда и отправляем.
        # Исключение (решение владельца 2026-08-26): кабинет рекламодателя
        # канонически живёт на сарафан…/cabinet — его зона и онбординг на
        # домене сети открыты, дальше их разбирают ветки ролей ниже.
        if on_sarafan and not (
            _is_prefixed(path, SARAFAN_ALLOWED_FOR_GUESTS)
            or _is_prefixed(path, ADVERTISER_PREFIXES)
            or _is_exact(path, ADVERTISER_ONBOARDING_EXACT)
        ):
            if request.method == "GET" and _wants_html(request):
                target = None
                if user.role == "radar":
                    target = _radar_canonical_url(request)
                elif user.role == "advertiser":
                    target = _ad_cabinet_canonical_url(request)
                return RedirectResponse(target or LANDING_PATH, status_code=302)
            return JSONResponse({"detail": "Forbidden on this host"}, status_code=403)

        if user.role == "radar" and (
            _is_prefixed(path, RADAR_PREFIXES) or (path == "/" and _on_radar_host(request))
        ):
            # На каноническом радар-хосте интерфейс Радара живёт на корне —
            # для radar-роли «/» там своя зона, а не операторский дашборд.
            return await call_next(request)

        # Клиент рекламного кабинета: своя зона — страница /cabinet и
        # /api/advertiser/*. На каноническом хосте кабинета интерфейс живёт
        # на корне (как у Радара).
        if user.role == "advertiser" and (
            _is_prefixed(path, ADVERTISER_PREFIXES)
            or (path == "/" and _on_ad_cabinet_host(request))
        ):
            return await call_next(request)

        # Онбординг рекламодателя открыт любому аутентифицированному: страница
        # кабинета (и корень его хоста) показывает не-advertiser'у экран «стать
        # рекламодателем», а точечные эндпоинты обслуживают этот экран.
        if _is_exact(path, ADVERTISER_ONBOARDING_EXACT) or (
            path == "/" and _on_ad_cabinet_host(request)
        ):
            return await call_next(request)

        # Аутентифицирован, но зона не его: не-владелец в операторском setka
        # (роль radar — обычный случай; роль operator без владельческого
        # аккаунта — тоже сюда, роль сама по себе доступа больше не даёт).
        if _wants_html(request) and request.method == "GET":
            if user.role == "advertiser":
                return RedirectResponse(
                    "/" if _on_ad_cabinet_host(request) else "/cabinet", status_code=302
                )
            return RedirectResponse("/" if _on_radar_host(request) else "/radar", status_code=302)
        return JSONResponse({"detail": "Forbidden for this role"}, status_code=403)

    async def _authenticate(self, request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        payload = verify_session_token(token)
        if not payload:
            return None
        try:
            user = await self._user_loader(int(payload["uid"]))
        except Exception:  # noqa: BLE001 - БД недоступна → запрос не аутентифицирован
            logger.warning("AuthGate: user lookup failed", exc_info=True)
            return None
        if user is None or not user.is_active:
            return None
        # Смена пароля инвалидирует старые сессии: fragment в токене ≠ актуальному.
        # password_hash nullable с миграции 052 (соц-only аккаунты) — fragment
        # считаем от пустой строки, семантика инвалидации сохраняется.
        if payload.get("pf") != password_fragment(user.password_hash or ""):
            return None
        # Время реального входа — из сессии, не из «сейчас»: ЕСА кладёт его в
        # `auth_time` ID-токена (modules/radar_id/service.issue_auth_code).
        # Cookie, выданная до 2026-09-18, `iat` не несёт → None, и тогда
        # `auth_time` не заявляется вовсе.
        session_iat = payload.get("iat")
        request.state.session_iat = int(session_iat) if session_iat else None
        return user
