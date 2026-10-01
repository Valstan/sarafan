"""Auth API (Ф0.1 контент-радара): login / logout / me / register.

Сессия — stateless signed-cookie (modules/radar/auth.py), enforcement —
AuthGateMiddleware (middleware/auth_gate.py). Здесь только выдача/гашение
cookie и регистрация radar-юзеров по инвайт-коду.

Регистрация: открытая форма + инвайт-код из env ``RADAR_INVITE_CODE`` (#008).
Код не задан → регистрация выключена (403): публичный HTTPS-домен без
инвайта мгновенно соберёт спам-ботов. Регистрируются только radar-юзеры;
операторов заводит CLI ``scripts/create_radar_user.py`` (нет эскалации через
публичный endpoint).
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

# noqa: F401 — импорт регистрирует Region/прочие классы в SQLAlchemy-registry,
# иначе конфигурация мапперов падает на relationship ScheduledPublication.region
# при первом инстанцировании RadarUser (та же грабля, что в PR #189).
from database import models  # noqa: F401
from database.connection import AsyncSessionLocal
from database.models_extended import RadarUser
from modules.radar.auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    hash_password,
    issue_session_token,
    password_fragment,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginIn(BaseModel):
    login: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class RegisterIn(BaseModel):
    login: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=256)
    invite_code: str = Field(..., min_length=1, max_length=128)


def _cookie_domain() -> str | None:
    # SSO между поддоменами (вход./радар.вмалмыже.рф — один app): env
    # SESSION_COOKIE_DOMAIN=.<punycode-домен> расшаривает cookie на все
    # поддомены. Пусто (дефолт) = host-only, как раньше.
    return os.getenv("SESSION_COOKIE_DOMAIN") or None


def _set_session_cookie(response: Response, user: RadarUser) -> None:
    # password_hash nullable с миграции 052 (соц-only аккаунты) — fragment от "".
    token = issue_session_token(user.id, user.role, password_fragment(user.password_hash or ""))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        # На проде cookie ходит только по HTTPS; локальный dev (http) выключает env'ом.
        secure=os.getenv("SESSION_COOKIE_SECURE", "1") == "1",
        domain=_cookie_domain(),
    )


@router.post("/login")
async def login(body: LoginIn, response: Response):
    """Проверить креды и выдать сессионную cookie."""
    normalized_login = body.login.strip().lower()
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(RadarUser).where(RadarUser.login == normalized_login))
        ).scalar_one_or_none()
        # verify и на несуществующем юзере (фиктивный хэш не храним — просто
        # ровняем стоимость ответа, не выдавая enumeration по таймингу scrypt).
        # or "": соц-only аккаунт без пароля не роняет verify (честный 401).
        ok = bool(user) and verify_password(body.password, user.password_hash or "")
        if not ok or not user.is_active:
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")
        user.last_login_at = datetime.utcnow()
        await session.commit()
        _set_session_cookie(response, user)
        return {"ok": True, "role": user.role, "login": user.login}


@router.post("/logout")
async def logout(response: Response):
    """Погасить сессию (удалить cookie; токен stateless — сам истечёт)."""
    response.delete_cookie(SESSION_COOKIE, domain=_cookie_domain())
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    """Текущий пользователь (middleware уже положил его в request.state)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user.to_dict()


@router.post("/register")
async def register(body: RegisterIn, response: Response):
    """Регистрация radar-юзера по инвайт-коду (роль radar, без эскалации)."""
    expected = os.getenv("RADAR_INVITE_CODE")
    if not expected:
        raise HTTPException(status_code=403, detail="Регистрация отключена")
    if not hmac.compare_digest(body.invite_code, expected):
        raise HTTPException(status_code=403, detail="Неверный инвайт-код")

    # Логины владельца зарезервированы и не регистрируются — ни в каком
    # регистре. Гейт опознаёт владельца по логину в нижнем регистре
    # (middleware/auth_gate.py::_is_owner), а уникальность login в Postgres
    # регистрозависима: без этой проверки «VALSTAN» завёлся бы рядом с
    # «valstan» и не дал бы 409. Гейт от такого аккаунта защищён и сам
    # (владельческая ветка требует роль operator, а регистрация выдаёт radar),
    # но имя владельца не должно быть занимаемым в принципе.
    from middleware.auth_gate import OWNER_LOGINS_DEFAULT, _csv_env

    if body.login.strip().lower() in _csv_env("SETKA_OWNER_LOGINS", OWNER_LOGINS_DEFAULT):
        logger.warning("Register attempt on reserved owner login: %s", body.login)
        raise HTTPException(status_code=409, detail="Логин занят")

    async with AsyncSessionLocal() as session:
        normalized_login = body.login.strip().lower()
        exists = (
            await session.execute(select(RadarUser.id).where(RadarUser.login == normalized_login))
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(status_code=409, detail="Логин занят")
        user = RadarUser(
            login=normalized_login,
            password_hash=hash_password(body.password),
            role="radar",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        _set_session_cookie(response, user)
        logger.info("Radar user registered: %s", user.login)
        return {"ok": True, "role": user.role, "login": user.login}
