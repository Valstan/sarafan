"""ВК-вход Радар-ID: VK ID OAuth 2.1 + PKCE (upstream-метод логина, R16).

Протокол id.vk.ru (НЕ старый oauth.vk.com), заземлён на рабочую реализацию
Sabantuy (brain REFERENCE R16):

- authorize ``https://id.vk.ru/authorize`` (code + PKCE S256 + state);
- token ``https://id.vk.ru/oauth2/auth`` — **client_secret НЕ участвует**
  (PKCE заменяет, даже для типа «Веб»); **``device_id`` из callback-query
  ОБЯЗАТЕЛЕН** в обмене (грабля R16 — нет в типовых гайдах);
- профиль ``https://id.vk.ru/oauth2/user_info``;
- ``redirect_uri`` символ-в-символ как зарегистрирован; ``.рф`` — punycode (G108).

Состояние между /login и /callback (state + code_verifier + next) — в
подписанной HMAC-cookie (тот же паттерн, что сессии radar: stateless,
SETKA_WEB_SECRET), TTL 10 минут.

Связывание аккаунтов (ADR-0002 §2): vk_user_id → существующий; иначе по
email ТОЛЬКО если у нашего аккаунта email_verified (анти-захват); иначе —
новый RadarUser (role=radar, sub авто). Email от VK считаем verified
(VK подтверждает его на своей стороне).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import func, select

from database.models_extended import RadarUser

logger = logging.getLogger(__name__)
audit = logging.getLogger("radar_id.audit")

VK_AUTHORIZE_URL = "https://id.vk.ru/authorize"
VK_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
VK_USERINFO_URL = "https://id.vk.ru/oauth2/user_info"

OAUTH_STATE_COOKIE = "radar_vk_oauth"
OAUTH_STATE_TTL = 600  # 10 минут на прохождение VK-диалога


class VkUpstreamError(RuntimeError):
    """Ошибка ВК-обмена (человекочитаемая — уходит на страницу логина)."""


def get_vk_app_id() -> str:
    """App ID VK ID-приложения Радара (env ``RADAR_ID_VK_APP_ID``; публичный)."""
    return os.getenv("RADAR_ID_VK_APP_ID", "").strip()


def get_redirect_uri() -> str:
    """redirect_uri ВК-callback — символ-в-символ как в ВК-приложении (punycode)."""
    from config.radar_id import get_issuer

    return os.getenv("RADAR_ID_VK_REDIRECT_URI", f"{get_issuer()}/auth/vk/callback")


def vk_login_available() -> bool:
    return bool(get_vk_app_id())


# ---------------------------------------------------------------------------
# Подписанный state-blob (cookie между /login и /callback)
# ---------------------------------------------------------------------------


def _secret() -> bytes:
    env = os.getenv("SETKA_WEB_SECRET")
    if env:
        return env.encode()
    # Тот же fallback, что у сессий radar: ephemeral деградация для dev.
    from modules.radar.auth import _secret as session_secret

    return session_secret()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_oauth_state(payload: Dict[str, Any], *, _now: Optional[float] = None) -> str:
    now = time.time() if _now is None else _now
    body = dict(payload)
    body["exp"] = int(now + OAUTH_STATE_TTL)
    body_b64 = _b64e(json.dumps(body, separators=(",", ":")).encode())
    sig = hmac.new(_secret(), body_b64.encode(), hashlib.sha256).digest()
    return f"{body_b64}.{_b64e(sig)}"


def verify_oauth_state(blob: str, *, _now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    try:
        body_b64, sig_b64 = blob.split(".")
        expected = hmac.new(_secret(), body_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        payload = json.loads(_b64d(body_b64))
        now = time.time() if _now is None else _now
        if int(payload.get("exp", 0)) < now:
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _cookie_domain_suffix() -> Optional[str]:
    """Суффикс, на который выдаётся сессионная кука (``.вмалмыже.рф`` в punycode).

    ``None`` — кука host-only (локальная разработка), межхостовых возвратов нет.
    """
    raw = (os.getenv("SESSION_COOKIE_DOMAIN") or "").strip().lower()
    return raw or None


def _host_shares_session(host: str) -> bool:
    """True, если на этот хост уже распространяется наша сессионная кука.

    Граница доверия для возврата = граница действия сессии. Шире пускать нельзя
    (open-redirect), уже — бессмысленно: если кука там не действует, пользователь
    всё равно приедет неавторизованным.
    """
    suffix = _cookie_domain_suffix()
    if not suffix:
        return False
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    # Кириллический хост и его punycode — одно и то же; сравниваем в ASCII.
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return False
    bare = suffix.lstrip(".")
    return host == bare or host.endswith("." + bare)


def host_shares_session(host: str) -> bool:
    """Публичная обёртка ``_host_shares_session`` (нужна AuthGate для решения
    «отправлять ли на центральный /login вход.вмалмыже.рф»)."""
    return _host_shares_session(host)


# Канонический публичный хост Радара (радар.вмалмыже.рф, punycode).
RADAR_CANONICAL_HOST_DEFAULT = "xn--80aal0cd.xn--80adkdyec4j.xn--p1ai"


def radar_canonical_redirect(current_host: Optional[str]) -> Optional[str]:
    """Абсолютный URL радара на каноническом хосте — или ``None``, если редирект не нужен.

    Заказ владельца 2026-07-26: интерфейс Радара живёт на ``радар.вмалмыже.рф``;
    ``вход.вмалмыже.рф`` — только страница входа. Вход, начатый прямо на issuer
    (без ``next`` с сервиса), по дефолту приземлял в ``/radar`` относительным
    путём — пользователь оставался смотреть радар на адресе входа. Открытый на
    любом не-каноническом хосте зоны ``/radar`` уводим на канонический
    (сессия переживает переезд — кука на весь ``.вмалмыже.рф``).

    ``None`` (= остаёмся на месте): уже на каноническом хосте; хост вне
    куки-зоны (техдомен, localhost — там канонический адрес недостижим или
    кука не переедет); зона/канон не настроены.
    """
    canonical = os.getenv("RADAR_CANONICAL_HOST", RADAR_CANONICAL_HOST_DEFAULT).strip().lower()
    if not canonical:
        return None
    host = (current_host or "").strip().lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
        canonical = canonical.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None
    if not host or host == canonical:
        return None
    if not (_host_shares_session(host) and _host_shares_session(canonical)):
        return None
    # Радар живёт на КОРНЕ своего поддомена (заказ владельца 2026-07-26):
    # адрес — радар.вмалмыже.рф, без лишнего «/radar» в строке.
    return f"https://{canonical}/"


def is_radar_host(current_host: Optional[str]) -> bool:
    """True, если запрос пришёл на канонический хост Радара (радар.вмалмыже.рф).

    На этом хосте интерфейс Радара отдаётся с корня ``/``, а ``/radar``
    редиректится на корень. Сравнение — в punycode (кириллица == punycode).
    """
    canonical = os.getenv("RADAR_CANONICAL_HOST", RADAR_CANONICAL_HOST_DEFAULT).strip().lower()
    host = (current_host or "").strip().lower().rstrip(".")
    if not canonical or not host:
        return False
    try:
        return host.encode("idna").decode("ascii") == canonical.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return False


# Публичный домен витрины САРАФАНА (валентин.вмалмыже.рф, punycode).
#
# Заказ владельца 2026-11-27: витрина сети дублируется на этот домен — там
# клиент жмёт кнопку и создаёт заявку. Приложение отдаёт на этом хосте
# витрину с КОРНЯ (``/``), а не операторский дашборд.
#
# ⚠️ Домен принадлежит владельцу (на нём же живёт его личная визитка), и
# транспорт — nginx на боевом VPS, который проксирует только разрешённые
# пути в локальную машину (config/nginx_valentin.conf). Поэтому витрина на
# этом хосте — единственная страница приложения, доступная из интернета.
PUBLIC_LANDING_HOST_DEFAULT = "xn--80adkmnnb2b.xn--80adkdyec4j.xn--p1ai"


def is_public_landing_host(current_host: Optional[str]) -> bool:
    """True, если запрос пришёл на хост публичной витрины САРАФАНА.

    Сравнение — в punycode (кириллица == punycode), как у остальных хостов.
    Хост переопределяется env ``PUBLIC_LANDING_HOST`` (пустое значение
    выключает правило).
    """
    canonical = os.getenv("PUBLIC_LANDING_HOST", PUBLIC_LANDING_HOST_DEFAULT).strip().lower()
    host = (current_host or "").strip().lower().rstrip(".")
    if not canonical or not host:
        return False
    try:
        return host.encode("idna").decode("ascii") == canonical.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return False


# Канонический адрес кабинета рекламодателя (решение владельца 2026-08-26):
# ПУТЬ на домене сети — сарафан.вмалмыже.рф/cabinet. Бренд САРАФАН знаком
# клиентам всех районов, TLS у домена уже есть — отдельный поддомен не нужен.
# Хост переопределяется env AD_CABINET_CANONICAL_HOST (punycode).
AD_CABINET_CANONICAL_HOST_DEFAULT = "xn--80aaa6cmey.xn--80adkdyec4j.xn--p1ai"
AD_CABINET_CANONICAL_PATH = "/cabinet"

# Прежний выделенный поддомен (кабинет.вмалмыже.рф, до TLS не дожил): корень
# этого хоста уводим на новый канонический адрес — ссылки не должны умирать.
AD_CABINET_LEGACY_HOST = "xn--80acmlhv0b.xn--80adkdyec4j.xn--p1ai"


def _ad_cabinet_canonical_host() -> str:
    return os.getenv("AD_CABINET_CANONICAL_HOST", AD_CABINET_CANONICAL_HOST_DEFAULT).strip().lower()


def ad_cabinet_canonical_url() -> str:
    """Абсолютный канонический адрес кабинета (для редиректов и ссылок)."""
    return f"https://{_ad_cabinet_canonical_host()}{AD_CABINET_CANONICAL_PATH}"


def ad_cabinet_canonical_redirect(current_host: Optional[str]) -> Optional[str]:
    """Абсолютный URL кабинета, если запрос пришёл НЕ на канонический хост.

    ``None`` — уже на каноническом хосте (страницу отдаём на месте), хост вне
    куки-зоны или канон не настроен (паттерн ``radar_canonical_redirect``).
    """
    canonical = _ad_cabinet_canonical_host()
    if not canonical:
        return None
    host = (current_host or "").strip().lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
        canonical = canonical.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None
    if not host or host == canonical:
        return None
    if host != AD_CABINET_LEGACY_HOST and not (
        _host_shares_session(host) and _host_shares_session(canonical)
    ):
        return None
    return f"https://{canonical}{AD_CABINET_CANONICAL_PATH}"


def is_ad_cabinet_host(current_host: Optional[str]) -> bool:
    """True, если запрос пришёл на ПРЕЖНИЙ выделенный хост кабинета.

    Раньше кабинет жил на корне своего поддомена; теперь корень такого хоста —
    только редирект на канонический адрес (``ad_cabinet_canonical_url``).
    """
    host = (current_host or "").strip().lower().rstrip(".")
    if not host:
        return False
    try:
        return host.encode("idna").decode("ascii") == AD_CABINET_LEGACY_HOST
    except (UnicodeError, UnicodeDecodeError):
        return False


def safe_next(next_url: Optional[str]) -> str:
    """Куда вернуть пользователя после ВК-входа.

    Пускаем два вида значений:

    1. **Относительный путь** (``/radar``) — как было.
    2. **Абсолютный URL на хосте, который уже делит с нами сессионную куку**
       (``https://радар.вмалмыже.рф/radar``).

    Зачем второе (заказ владельца 2026-07-25). ВК-callback по устройству
    единого входа всегда приземляется на issuer-хост ``вход.вмалмыже.рф``, и
    относительный ``next`` разворачивается уже относительно него — пользователь,
    начавший на ``радар.вмалмыже.рф``, оставался на чужом адресе. Абсолютный
    возврат отправляет его ровно туда, откуда он пришёл; сессия при этом не
    теряется, потому что кука выдана на весь ``.вмалмыже.рф``.

    Анти-open-redirect. Список разрешённых хостов не задаётся отдельно, а
    выводится из ``SESSION_COOKIE_DOMAIN``: возврат разрешён ровно туда, где
    наша сессия и так действует. Любой чужой хост, схема кроме http/https,
    протокол-относительный ``//evil`` — отбрасываются в дефолт.
    """
    if not next_url:
        return "/radar"
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    if next_url.startswith("http://") or next_url.startswith("https://"):
        from urllib.parse import urlsplit

        parts = urlsplit(next_url)
        if parts.scheme in ("http", "https") and _host_shares_session(parts.hostname or ""):
            return next_url
    return "/radar"


def absolutize_next(next_url: Optional[str], origin_host: Optional[str]) -> str:
    """Привязать относительный ``next`` к хосту, с которого начали вход.

    Вызывается на ``/auth/vk/login``: там ``Host`` — это ещё родной хост сервиса
    (радар / сарафан), а на callback'е его уже не узнать (он всегда issuer).

    Схему берём **https**, а не из запроса: nginx на боксе слушает 80, TLS
    терминируется на edge провайдера, поэтому ``X-Forwarded-Proto`` приезжает
    ``http`` и вернул бы пользователя на небезопасный адрес. Публичные хосты
    экосистемы https-only — так же жёстко задан и issuer в ``config/radar_id.py``.

    Хост не из нашей куки-зоны → оставляем путь относительным: деградация к
    прежнему поведению, а не открытый редирект.

    ``next`` может приехать уже абсолютным — с центральной страницы входа
    (AuthGate шлёт туда ``next=https://<родной-хост>/...``). Такой пропускаем
    через ``safe_next`` (тот же анти-open-redirect), не затирая в ``/radar``.
    """
    if next_url and (next_url.startswith("http://") or next_url.startswith("https://")):
        return safe_next(next_url)
    path = next_url if (next_url and next_url.startswith("/")) else "/radar"
    if path.startswith("//"):
        path = "/radar"
    if origin_host and _host_shares_session(origin_host):
        return f"https://{origin_host.strip().rstrip('.')}{path}"
    return path


# ---------------------------------------------------------------------------
# OAuth-шаги
# ---------------------------------------------------------------------------


def build_vk_authorize(next_url: str) -> Tuple[str, str]:
    """Собрать VK authorize-URL. Возвращает ``(url, signed_state_blob)``."""
    if not vk_login_available():
        raise VkUpstreamError("ВК-вход не настроен (RADAR_ID_VK_APP_ID пуст)")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": get_vk_app_id(),
        "redirect_uri": get_redirect_uri(),
        "state": state,
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
        "scope": "email",
    }
    url = f"{VK_AUTHORIZE_URL}?{urlencode(params)}"
    blob = sign_oauth_state({"st": state, "cv": verifier, "next": safe_next(next_url)})
    return url, blob


async def exchange_vk_code(
    *,
    code: str,
    device_id: str,
    state: str,
    code_verifier: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Обменять code на access_token VK (device_id обязателен — R16)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": get_vk_app_id(),
        "device_id": device_id,
        "redirect_uri": get_redirect_uri(),
        "state": state,
    }
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(VK_TOKEN_URL, data=data)
        body = resp.json()
    except Exception as e:
        raise VkUpstreamError(f"VK token endpoint недоступен: {e}") from e
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
        raise VkUpstreamError(f"VK не принял обмен кода: {err}")
    return body


async def fetch_vk_user(
    access_token: str, *, client: Optional[httpx.AsyncClient] = None
) -> Dict[str, Any]:
    """Профиль по access_token: ``{user_id, first_name, last_name, email?}``."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(
            VK_USERINFO_URL,
            data={"access_token": access_token, "client_id": get_vk_app_id()},
        )
        body = resp.json()
    except Exception as e:
        raise VkUpstreamError(f"VK user_info недоступен: {e}") from e
    finally:
        if own:
            await client.aclose()
    user = (body or {}).get("user") or {}
    if resp.status_code != 200 or not user.get("user_id"):
        raise VkUpstreamError("VK не вернул профиль пользователя")
    return user


# ---------------------------------------------------------------------------
# Связывание с RadarUser
# ---------------------------------------------------------------------------


async def _backfill_email(session, row: RadarUser, email: Optional[str]) -> None:
    """Дозаполнить пустой ``email`` у найденного ряда (мандат brain 2026-09-05).

    До этого ``email`` писался **только при создании ряда**: аккаунт, заведённый
    когда ВК адреса не прислал, оставался без него навсегда, и портал получал
    вход без claim'а ``email``, хотя scope его разрешает (G324). Из четырёх
    аккаунтов ЕСА адреса не было ни у одного.

    Дозаполнение безопасно, потому что ряд уже найден **по тому же
    ``vk_user_id``** — это тот же человек, а не привязка чужой личности.
    Привязка к *чужому* ряду по адресу остаётся там, где была: только через
    ``email_verified`` (ADR-0002 §2).

    Три вещи, которых здесь сознательно нет:

    * **не перезаписываем непустой адрес.** Смена почты — заявленное действие
      владельца аккаунта, а не побочный эффект входа;
    * **не занимаем чужой адрес.** ``lower(email)`` уникален в БД, поэтому
      адрес, уже принадлежащий другому ряду, вызвал бы ``IntegrityError`` —
      то есть **500 на входе**, а не «claim не приехал». Проверяем до записи;
    * **не молчим.** Строка журнала обязательна: следующий ответ порталу должен
      быть фактом из лога, а не рассуждением.
    """
    if not email or row.email:
        return

    taken = (
        await session.execute(
            select(RadarUser.id).where(
                func.lower(RadarUser.email) == email.lower(), RadarUser.id != row.id
            )
        )
    ).scalar_one_or_none()
    if taken is not None:
        # Тот же адрес у другого ряда — запись упала бы на уникальном индексе.
        # Вход важнее claim'а: пускаем, но оставляем след для разбора.
        audit.warning(
            "vk-login: email backfill skipped for sub=%s — address already held by id=%s",
            row.sub,
            taken,
        )
        return

    row.email = email
    row.email_verified = True  # адрес пришёл от ВК — ВК его подтверждал
    await session.commit()
    audit.info("vk-login: email backfilled for sub=%s vk_id=%s", row.sub, row.vk_user_id)


async def find_or_create_user(session, vk_user: Dict[str, Any]) -> RadarUser:
    """vk_user_id → существующий (+ дозаполнение email); по verified-email →
    привязка; иначе новый."""
    vk_id = int(vk_user["user_id"])
    email = (vk_user.get("email") or "").strip() or None
    name = (
        " ".join(p for p in (vk_user.get("first_name"), vk_user.get("last_name")) if p).strip()
        or None
    )

    row: Optional[RadarUser] = (
        await session.execute(select(RadarUser).where(RadarUser.vk_user_id == vk_id))
    ).scalar_one_or_none()
    if row is not None:
        if not row.is_active:
            raise VkUpstreamError("Аккаунт деактивирован")
        await _backfill_email(session, row, email)
        return row

    if email:
        by_email: Optional[RadarUser] = (
            await session.execute(
                select(RadarUser).where(func.lower(RadarUser.email) == email.lower())
            )
        ).scalar_one_or_none()
        # Привязка соц-личности к существующему аккаунту — только через
        # verified email (ADR-0002 §2: иначе захват аккаунта по чужому email).
        if by_email is not None and by_email.email_verified and by_email.is_active:
            by_email.vk_user_id = vk_id
            if name and not by_email.display_name:
                by_email.display_name = name
            await session.commit()
            audit.info("vk-login: linked vk_id=%s to sub=%s via email", vk_id, by_email.sub)
            return by_email

    user = RadarUser(
        login=None,
        password_hash=None,
        role="radar",
        vk_user_id=vk_id,
        email=email,
        email_verified=bool(email),  # email пришёл от VK — VK его подтверждал
        display_name=name,
    )
    session.add(user)
    await session.commit()
    audit.info("vk-login: created RadarUser sub=%s for vk_id=%s", user.sub, vk_id)
    return user
