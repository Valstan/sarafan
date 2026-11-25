"""
VK Tokens Management API
API для управления токенами VK через веб-интерфейс
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db_session
from database.models import VKToken
from modules.vk_monitor.vk_client import VKClient

router = APIRouter()
logger = logging.getLogger(__name__)


def compute_token_stats(tokens: List[Dict[str, Any]]) -> Dict[str, int]:
    """Категоризация токенов для плашек-счётчиков на ``/tokens``.

    Чистая функция (без БД/IO) — единый источник истины для UI, тестируема.

    ``tokens`` — список dict'ов с ключами ``community_id`` (int | None) и
    ``validation_status`` (str | None).

    - ``main``   — валидные **user**-токены (``community_id is None``): костяк
      парсинга/публикации (VALSTAN, VITA…).
    - ``aux``    — валидные **community**-токены (``community_id`` задан):
      публикуют в свою группу (``COMM_*``).
    - ``broken`` — любые невалидные (``validation_status != 'valid'``,
      включая ``unknown``/cooldown-протухшие) — user и community вместе.
    - ``total``  — всего.

    Разбиение чистое: ``main + aux + broken == total``. Это чинит прежнюю
    заглушку (``main=aux=0`` с устаревшим комментарием «type is not stored in
    DB» — на деле ``community_id`` есть в модели с миграции 007).
    """
    main = aux = broken = 0
    for t in tokens:
        is_valid = (t.get("validation_status") or "unknown") == "valid"
        is_community = t.get("community_id") is not None
        if not is_valid:
            broken += 1
        elif is_community:
            aux += 1
        else:
            main += 1
    return {"main": main, "aux": aux, "broken": broken, "total": len(tokens)}


class TokenResponse(BaseModel):
    """Ответ с информацией о токене"""

    id: int
    name: str
    token: str  # Маскированный токен
    community_id: int | None = None  # abs(vk_group_id) если это community-токен
    is_active: bool
    last_used: str | None
    last_validated: str | None
    validation_status: str
    error_message: str | None
    permissions: List[str] | None  # Изменено на список
    user_info: Dict[str, Any] | None
    # TokenPolicy state (миграция 014)
    disabled_until: str | None = None
    last_error_code: int | None = None
    last_error_at: str | None = None
    consecutive_errors: int = 0
    role: str | None = None  # 'publish' = разрешено публиковать (миграция 023)
    created_at: str | None
    # Кому принадлежит токен — человеческим языком, а не только `COMM_240944863`.
    # Имя вида COMM_<id> отвечает на вопрос «какой группе», но не «какой это
    # район»: чтобы понять, что COMM_240944863 — это Юрья, оператору приходилось
    # держать соответствие в голове или лезть в другую таблицу той же страницы.
    subject_kind: str = "unknown"  # community | user | unknown
    subject_label: str | None = None  # «ЮРЬЯ - ИНФО» / «Валентин Савиных»
    subject_url: str | None = None  # ссылка в ВК на сообщество или профиль
    region_code: str | None = None
    region_name: str | None = None
    updated_at: str | None


class TokenDisableRequest(BaseModel):
    """POST /api/tokens/{name}/disable body."""

    hours: float = Field(
        24.0,
        gt=0,
        le=24 * 30,
        description="На сколько часов отключить (макс 30 дней).",
    )
    reason: str = Field(
        "manual",
        max_length=200,
        description="Произвольная причина для аудита (попадёт в error_message).",
    )


class TokenUpdateRequest(BaseModel):
    """Запрос на обновление токена"""

    token: str | None = Field(None, min_length=10, description="VK API токен (опционально)")
    validate_token: bool = Field(True, description="Валидировать токен после обновления")
    is_active: bool | None = Field(None, description="Включить/выключить токен (опционально)")


class TokenCreateRequest(BaseModel):
    """Запрос на создание токена"""

    name: str = Field(..., min_length=2, max_length=50, description="Имя токена (например VALSTAN)")
    token: str = Field(..., min_length=10, description="VK API токен")
    validate_token: bool = Field(True, description="Валидировать токен после создания")
    community_id: int | None = Field(
        None,
        description="abs(vk_group_id) если токен community access token; None для user-токена",
    )


class TokenValidationResponse(BaseModel):
    """Ответ валидации токена"""

    name: str
    is_valid: bool
    validation_status: str
    error_message: str | None
    user_info: Dict[str, Any] | None
    permissions: List[str] | None


async def _region_by_community(db) -> Dict[int, Any]:
    """map ``abs(vk_group_id) → Region``. Один запрос на страницу, не на строку."""
    from database.models import Region

    rows = (await db.execute(select(Region).where(Region.vk_group_id.isnot(None)))).scalars()
    return {abs(int(r.vk_group_id)): r for r in rows}


def _user_display_name(user_info: Any) -> str:
    """Имя владельца user-токена из снапшота ``user_info``."""
    if not isinstance(user_info, dict):
        return ""
    first = str(user_info.get("first_name") or "").strip()
    last = str(user_info.get("last_name") or "").strip()
    full = (first + " " + last).strip()
    return full or str(user_info.get("name") or "").strip()


def describe_subject(
    *,
    community_id: Any,
    user_info: Any,
    region: Any = None,
) -> Dict[str, Any]:
    """Кому принадлежит токен: вид, подпись, ссылка в ВК, район.

    Чистая функция (без БД/IO), поэтому тестируется без прода — как
    ``compute_token_stats`` рядом.

    Подпись community-токена берётся у региона, а не у самого токена: имя
    ``COMM_<id>`` говорит про группу, но не про район, и оператор до сих пор
    держал это соответствие в голове.
    """
    if community_id is not None:
        cid = abs(int(community_id))
        return {
            "subject_kind": "community",
            "subject_label": (getattr(region, "name", None) or f"club{cid}"),
            "subject_url": f"https://vk.com/club{cid}",
            "region_code": getattr(region, "code", None),
            "region_name": getattr(region, "name", None),
        }

    name = _user_display_name(user_info)
    uid = None
    if isinstance(user_info, dict):
        uid = user_info.get("id")
    return {
        "subject_kind": "user" if name or uid else "unknown",
        "subject_label": name or None,
        "subject_url": (f"https://vk.com/id{int(uid)}" if uid else None),
        "region_code": None,
        "region_name": None,
    }


@router.get("/", response_model=List[TokenResponse])
async def get_all_tokens(db: AsyncSession = Depends(get_db_session)):
    """Получить все токены"""
    try:
        result = await db.execute(select(VKToken).order_by(VKToken.name))
        tokens = result.scalars().all()
        regions = await _region_by_community(db)

        out: List[TokenResponse] = []
        for token in tokens:
            data = token.to_dict()
            cid = token.community_id
            data.update(
                describe_subject(
                    community_id=cid,
                    user_info=token.user_info,
                    region=regions.get(abs(int(cid))) if cid is not None else None,
                )
            )
            out.append(TokenResponse(**data))
        return out

    except Exception as e:
        logger.error(f"Error getting tokens: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_token_stats(db: AsyncSession = Depends(get_db_session)):
    """Счётчики для плашек /tokens: main / aux / broken / total.

    Регистрируется ДО ``/{token_name}`` — иначе FastAPI поймает ``/stats``
    как ``token_name="stats"`` (та же причина, что у ``/communities`` ниже).
    """
    try:
        result = await db.execute(select(VKToken))
        tokens = result.scalars().all()
        return compute_token_stats(
            [
                {"community_id": t.community_id, "validation_status": t.validation_status}
                for t in tokens
            ]
        )
    except Exception as e:
        logger.error(f"Error computing token stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/usage")
async def get_token_usage(days: int = 7, db: AsyncSession = Depends(get_db_session)):
    """Таблица контроля расхода: сколько VK-запросов сжёг каждый токен.

    Считает :mod:`modules.vk_monitor.token_usage` — по одному инкременту на
    фактический вызов VK API (узкое место ``VKClient._enforce_rate_limit``).
    Ответ: ``today`` (разбивка по методам), ``window`` (посуточно за ``days``)
    и ``roles`` — какую роль токен играет в каскаде публикации, чтобы расход
    и назначение читались в одной таблице.

    Строка с именем вида ``UNKNOWN:<fp>`` означает расход мимо маршрутизатора
    (клиент собран с токеном напрямую) — это сигнал к правке кода, а не сбой.

    Регистрируется ДО ``/{token_name}`` — иначе FastAPI поймает ``/usage`` как
    имя токена.
    """
    from config.runtime import (
        get_never_publish_token_names,
        get_publish_token_names,
        get_reserve_publish_token_names,
    )
    from modules.vk_monitor.token_usage import get_usage, get_usage_window

    try:
        window = get_usage_window(max(1, min(int(days), 31)))
        today = get_usage()

        never = get_never_publish_token_names()
        primary = get_publish_token_names()
        reserve = get_reserve_publish_token_names()

        def _role(name: str) -> str:
            upper = name.upper()
            if upper.startswith("COMM_"):
                return "community: публикует только в своё сообщество"
            if upper in never:
                return "только чтение (публикация запрещена)"
            if upper in primary:
                return "основной публикатор"
            if upper in reserve:
                return "резерв публикации"
            return "чтение"

        # ВСЕ токены, а не только активные. Прежний фильтр `is_active` молча
        # убирал из таблицы токен, выключенный кнопкой «Выкл. на 24ч»: строка
        # исчезала, и отсутствие расхода было неотличимо от отсутствия токена.
        # Выключенные показываем с пометкой — это и есть «пропуск», который
        # искали при проверке полноты статистики.
        result = await db.execute(select(VKToken))
        rows = result.scalars().all()
        known = {t.name.upper(): t for t in rows}
        regions = await _region_by_community(db)

        names = sorted(set(known) | set(today))
        return {
            "today": {
                name: {
                    "total": int(today.get(name, {}).get("total", 0)),
                    "methods": {
                        k[2:]: v for k, v in today.get(name, {}).items() if k.startswith("m:")
                    },
                    "role": _role(name),
                    "known": name in known,
                    "is_active": (bool(known[name].is_active) if name in known else None),
                    "last_used": (
                        known[name].last_used.isoformat()
                        if name in known and known[name].last_used
                        else None
                    ),
                    **(
                        describe_subject(
                            community_id=known[name].community_id,
                            user_info=known[name].user_info,
                            region=(
                                regions.get(abs(int(known[name].community_id)))
                                if known[name].community_id is not None
                                else None
                            ),
                        )
                        if name in known
                        else {
                            "subject_kind": "unknown",
                            "subject_label": None,
                            "subject_url": None,
                            "region_code": None,
                            "region_name": None,
                        }
                    ),
                }
                for name in names
            },
            "window": window,
        }
    except Exception as e:
        logger.error(f"Error computing token usage: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/capabilities")
async def get_token_capabilities():
    """Последний замер прав «токен × метод» (beat, раз в месяц).

    Отдаёт снапшот, снятый ``tasks.celery_app.probe_token_capabilities``:
    ``measured_at`` + матрица ``{имя_токена: {проба: ok|errNN|net:*}}``.
    ``null`` в ``measured_at`` — замера ещё не было (первый пройдёт 1-го числа).

    Регистрируется ДО ``/{token_name}`` — иначе FastAPI поймает ``/capabilities``
    как имя токена.
    """
    from modules.vk_monitor.token_capabilities import load_snapshot

    snapshot = load_snapshot()
    if not snapshot:
        return {"measured_at": None, "matrix": {}}
    return {"measured_at": snapshot.get("measured_at"), "matrix": snapshot.get("matrix", {})}


# ---------------------------------------------------------------------------
# Community access tokens: пул токенов на сообщество для публикации и сообщений.
# Legacy ``COMM_<id>`` считается основным (VALSTAN), резервные получают имя
# ``COMM_<id>_<ACCOUNT>`` (например ``COMM_158787639_MAMA``). API принимает и
# показывает такие имена, но UI **не рисует пустой слот резерва**: ключ
# сообщества выпускает только ВЛАДЕЛЕЦ группы (у администратора раздела «Работа
# с API» нет вовсе), поэтому слот «резерв из-под МАМЫ» не мог заполниться
# никогда и лишь удваивал таблицу. Отказоустойчивость даёт каскад
# «ключ сообщества → МАМА → VALSTAN» на user-токенах (порядок и обоснование —
# ``docs/VK_TOKEN_ROADMAP.md`` §2: VALSTAN последним, его бан гасит вообще всё).
# Эти роуты должны быть зарегистрированы РАНЬШЕ `/{token_name}`-обработчиков —
# иначе FastAPI поймает `/communities` как `token_name="communities"`.
# ---------------------------------------------------------------------------


class CommunityTokenEntry(BaseModel):
    token_id: int
    token_name: str
    token_masked: str
    is_active: bool
    validation_status: str
    last_validated: str | None = None
    error_message: str | None = None
    permissions: List[str] | None = None
    #: До какого токен отключён (TokenPolicy). Нужно витрине, чтобы отличать
    #: «отключён СЕЙЧАС» от «было отключён, cooldown истёк» — иначе старое
    #: сообщение об auto-disable выглядит как текущая поломка.
    disabled_until: str | None = None
    last_error_code: int | None = None
    last_error_at: str | None = None


class CommunityTokenRow(BaseModel):
    region_id: int
    region_name: str
    region_code: str
    vk_group_id: int
    community_id: int  # abs(vk_group_id)
    token_id: int | None = None
    token_name: str | None = None
    token_masked: str | None = None
    is_active: bool = False
    validation_status: str = "missing"  # missing, valid, invalid, unknown
    last_validated: str | None = None
    error_message: str | None = None
    permissions: List[str] | None = None
    disabled_until: str | None = None
    tokens: List[CommunityTokenEntry] = Field(default_factory=list)


class CommunityTokenUpsert(BaseModel):
    token: str = Field(
        ...,
        min_length=10,
        description="Community access token, выпущенный в Управление → Работа с API",
    )
    validate_token: bool = True


@router.get("/communities", response_model=List[CommunityTokenRow])
async def list_community_tokens(db: AsyncSession = Depends(get_db_session)):
    """Per-region список с состоянием community-токена для каждого региона."""
    from database.models import Region

    regions_q = await db.execute(
        select(Region).where(Region.vk_group_id.isnot(None)).order_by(Region.name)
    )
    regions = list(regions_q.scalars())

    tokens_q = await db.execute(select(VKToken).where(VKToken.community_id.isnot(None)))
    tokens_by_cid: Dict[int, List[VKToken]] = {}
    for token in tokens_q.scalars():
        tokens_by_cid.setdefault(int(token.community_id), []).append(token)

    rows: List[CommunityTokenRow] = []
    for r in regions:
        cid = abs(int(r.vk_group_id))
        community_tokens = tokens_by_cid.get(cid, [])
        community_tokens.sort(
            key=lambda t: (0 if t.name.upper() == f"COMM_{cid}" else 1, t.name.upper())
        )
        entries = [_community_token_entry(t) for t in community_tokens]
        t = community_tokens[0] if community_tokens else None
        if t:
            rows.append(
                CommunityTokenRow(
                    region_id=r.id,
                    region_name=r.name,
                    region_code=r.code,
                    vk_group_id=r.vk_group_id,
                    community_id=cid,
                    token_id=t.id,
                    token_name=t.name,
                    token_masked=(
                        (t.token[:12] + "…" + t.token[-4:])
                        if t.token and len(t.token) > 20
                        else "(short)"
                    ),
                    is_active=bool(t.is_active),
                    validation_status=t.validation_status or "unknown",
                    last_validated=(t.last_validated.isoformat() if t.last_validated else None),
                    error_message=t.error_message,
                    permissions=(t.permissions if isinstance(t.permissions, list) else None),
                    disabled_until=(t.disabled_until.isoformat() if t.disabled_until else None),
                    tokens=entries,
                )
            )
        else:
            rows.append(
                CommunityTokenRow(
                    region_id=r.id,
                    region_name=r.name,
                    region_code=r.code,
                    vk_group_id=r.vk_group_id,
                    community_id=cid,
                    tokens=[],
                )
            )
    return rows


@router.get("/communities/ownership-audit")
async def community_ownership_audit(db: AsyncSession = Depends(get_db_session)):
    """Сверка «токен ↔ сообщество» опросом самих токенов + дыры покрытия.

    Отвечает на три вопроса, которые страница раньше не задавала ни в каком виде
    (заказ владельца 2026-08-19):

    1. **Не перепутаны ли ключи.** У каждого community-токена спрашиваем
       ``groups.getById()`` без аргументов — он отвечает СВОЕЙ группой. Сверяем
       с тем, под каким ``community_id`` он записан. Прежняя проверка спрашивала
       про заданную группу, а такую карточку читает любой токен, поэтому
       перепутанный ключ показывался «ВАЛИДЕН».
    2. **Нет ли районов без ключа** — регион с ``vk_group_id``, но без токена
       публикуется только каскадом МАМА → VALSTAN, то есть вся его нагрузка
       ложится на два общих аккаунта.
    3. **Нет ли ключей без района** — осиротевший токен (ровно так 25.07 удалили
       рабочий ключ Радара, посчитав его мусором).

    Read-only: один VK-вызов на токен, ничего не пишем.
    """
    import vk_api

    from database.models import Region

    tokens = list(
        (await db.execute(select(VKToken).where(VKToken.community_id.isnot(None)))).scalars()
    )
    regions = list((await db.execute(select(Region))).scalars())
    by_cid = {abs(int(r.vk_group_id)): r for r in regions if r.vk_group_id is not None}

    def _owning_group(token_value: str):
        try:
            api = vk_api.VkApi(token=token_value).get_api()
            r = api.groups.getById()
        except Exception as e:  # noqa: BLE001 — аудит не должен падать на одном ключе
            return None, f"{type(e).__name__}: {e}"
        grp = None
        if isinstance(r, list) and r:
            grp = r[0]
        elif isinstance(r, dict) and r.get("groups"):
            grp = r["groups"][0]
        if not isinstance(grp, dict) or grp.get("id") is None:
            return None, "groups.getById() не вернул свою группу"
        return grp, None

    rows: List[Dict[str, Any]] = []
    mismatches = 0
    failed = 0
    for t in sorted(tokens, key=lambda x: x.name):
        cid = abs(int(t.community_id))
        region = by_cid.get(cid)
        grp, err = await asyncio.to_thread(_owning_group, t.token)
        if err:
            verdict, actual_id, actual_name = "probe-failed", None, None
            failed += 1
        else:
            actual_id = abs(int(grp["id"]))
            actual_name = grp.get("name") or ""
            if actual_id == cid:
                verdict = "ok"
            else:
                verdict = "mismatch"
                mismatches += 1
        rows.append(
            {
                "token_name": t.name,
                "community_id": cid,
                "region_code": getattr(region, "code", None),
                "region_name": getattr(region, "name", None),
                "vk_url": f"https://vk.com/club{cid}",
                "actual_community_id": actual_id,
                # Имя из ВК, а не из нашей таблицы: расхождение здесь — сигнал,
                # что группу переименовали, а у нас осталось старое название.
                "actual_name": actual_name,
                "verdict": verdict,
                "error": err,
            }
        )

    have = {abs(int(t.community_id)) for t in tokens}
    regions_without_token = [
        {
            "region_code": r.code,
            "region_name": r.name,
            "vk_group_id": r.vk_group_id,
            "is_active": bool(getattr(r, "is_active", False)),
        }
        for r in regions
        if r.vk_group_id is not None and abs(int(r.vk_group_id)) not in have
    ]
    tokens_without_region = [
        {"token_name": t.name, "community_id": abs(int(t.community_id))}
        for t in tokens
        if abs(int(t.community_id)) not in by_cid
    ]

    return {
        "checked": len(rows),
        "ok": len(rows) - mismatches - failed,
        "mismatches": mismatches,
        "probe_failed": failed,
        "rows": rows,
        "regions_without_token": regions_without_token,
        "tokens_without_region": tokens_without_region,
    }


@router.put("/communities/{community_id}", response_model=CommunityTokenRow)
async def upsert_community_token(
    community_id: int,
    request: CommunityTokenUpsert,
    db: AsyncSession = Depends(get_db_session),
):
    """Создать или обновить community-токен для конкретного сообщества."""
    from database.models import Region

    cid = abs(int(community_id))

    region_q = await db.execute(
        select(Region).where((Region.vk_group_id == cid) | (Region.vk_group_id == -cid))
    )
    region = region_q.scalar_one_or_none()
    if not region:
        raise HTTPException(status_code=404, detail=f"No region with vk_group_id={community_id}")

    legacy_name = f"COMM_{cid}"
    existing_q = await db.execute(select(VKToken).where(VKToken.name == legacy_name))
    token = existing_q.scalar_one_or_none()

    if token is None:
        token = VKToken(
            name=legacy_name,
            token=request.token.strip(),
            community_id=cid,
            is_active=True,
            validation_status="unknown",
        )
        db.add(token)
    else:
        token.token = request.token.strip()
        token.is_active = True

    if request.validate_token:
        v = await validate_community_token(token.token, cid)
        token.validation_status = "valid" if v["is_valid"] else "invalid"
        token.error_message = v.get("error_message")
        token.user_info = v.get("user_info")
        token.permissions = v.get("permissions")
        token.last_validated = datetime.now()

    await db.commit()
    await db.refresh(token)

    return CommunityTokenRow(
        region_id=region.id,
        region_name=region.name,
        region_code=region.code,
        vk_group_id=region.vk_group_id,
        community_id=cid,
        token_id=token.id,
        token_name=token.name,
        token_masked=(
            (token.token[:12] + "…" + token.token[-4:])
            if token.token and len(token.token) > 20
            else "(short)"
        ),
        is_active=bool(token.is_active),
        validation_status=token.validation_status or "unknown",
        last_validated=(token.last_validated.isoformat() if token.last_validated else None),
        error_message=token.error_message,
        permissions=token.permissions if isinstance(token.permissions, list) else None,
        disabled_until=(token.disabled_until.isoformat() if token.disabled_until else None),
        tokens=[_community_token_entry(token)],
    )


def _community_token_entry(token: VKToken) -> CommunityTokenEntry:
    masked = (
        (token.token[:12] + "…" + token.token[-4:])
        if token.token and len(token.token) > 20
        else "(short)"
    )
    return CommunityTokenEntry(
        token_id=token.id,
        token_name=token.name,
        token_masked=masked,
        is_active=bool(token.is_active),
        validation_status=token.validation_status or "unknown",
        last_validated=(token.last_validated.isoformat() if token.last_validated else None),
        error_message=token.error_message,
        permissions=token.permissions if isinstance(token.permissions, list) else None,
        disabled_until=(token.disabled_until.isoformat() if token.disabled_until else None),
        last_error_code=token.last_error_code,
        last_error_at=(token.last_error_at.isoformat() if token.last_error_at else None),
    )


def _validate_community_token_name(community_id: int, token_name: str) -> str:
    """Нормализовать имя и не дать управлять чужой строкой через URL."""
    cid = abs(int(community_id))
    upper = token_name.strip().upper()
    prefix = f"COMM_{cid}"
    if upper != prefix and not re.fullmatch(re.escape(prefix) + r"_[A-Z0-9_]+", upper):
        raise HTTPException(status_code=400, detail=f"Token name must start with {prefix}")
    return upper


async def _region_for_community(db: AsyncSession, cid: int):
    from database.models import Region

    q = await db.execute(
        select(Region).where((Region.vk_group_id == cid) | (Region.vk_group_id == -cid))
    )
    region = q.scalar_one_or_none()
    if not region:
        raise HTTPException(status_code=404, detail=f"No region with vk_group_id={cid}")
    return region


@router.put("/communities/{community_id}/tokens/{token_name}", response_model=CommunityTokenEntry)
async def upsert_named_community_token(
    community_id: int,
    token_name: str,
    request: CommunityTokenUpsert,
    db: AsyncSession = Depends(get_db_session),
):
    """Создать/заменить именованный резерв community-токена."""
    cid = abs(int(community_id))
    name = _validate_community_token_name(cid, token_name)
    await _region_for_community(db, cid)

    q = await db.execute(select(VKToken).where(VKToken.name == name))
    token = q.scalar_one_or_none()
    if token is not None and int(token.community_id or 0) != cid:
        raise HTTPException(status_code=409, detail="Token name belongs to another community")
    if token is None:
        token = VKToken(
            name=name,
            token=request.token.strip(),
            community_id=cid,
            is_active=True,
            validation_status="unknown",
        )
        db.add(token)
    else:
        token.token = request.token.strip()
        token.is_active = True

    if request.validate_token:
        validation = await validate_community_token(token.token, cid)
        token.validation_status = "valid" if validation["is_valid"] else "invalid"
        token.error_message = validation.get("error_message")
        token.user_info = validation.get("user_info")
        token.permissions = validation.get("permissions")
        token.last_validated = datetime.now()

    await db.commit()
    await db.refresh(token)
    return _community_token_entry(token)


@router.post(
    "/communities/{community_id}/tokens/{token_name}/validate",
    response_model=TokenValidationResponse,
)
async def validate_named_community_token(
    community_id: int,
    token_name: str,
    db: AsyncSession = Depends(get_db_session),
):
    cid = abs(int(community_id))
    name = _validate_community_token_name(cid, token_name)
    q = await db.execute(select(VKToken).where(VKToken.name == name, VKToken.community_id == cid))
    token = q.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail=f"No community token {name}")
    validation = await validate_community_token(token.token, cid)
    token.validation_status = "valid" if validation["is_valid"] else "invalid"
    token.error_message = validation.get("error_message")
    token.user_info = validation.get("user_info")
    token.permissions = validation.get("permissions")
    token.last_validated = datetime.now()
    await db.commit()
    return TokenValidationResponse(
        name=token.name,
        is_valid=validation["is_valid"],
        validation_status=token.validation_status,
        error_message=validation.get("error_message"),
        user_info=validation.get("user_info"),
        permissions=validation.get("permissions"),
    )


@router.delete("/communities/{community_id}/tokens/{token_name}")
async def delete_named_community_token(
    community_id: int,
    token_name: str,
    db: AsyncSession = Depends(get_db_session),
):
    cid = abs(int(community_id))
    name = _validate_community_token_name(cid, token_name)
    q = await db.execute(select(VKToken).where(VKToken.name == name, VKToken.community_id == cid))
    token = q.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail=f"No community token {name}")
    await db.execute(delete(VKToken).where(VKToken.id == token.id))
    await db.commit()
    return {"success": True, "community_id": cid, "token_name": name}


@router.post("/communities/{community_id}/validate", response_model=TokenValidationResponse)
async def validate_community_token_endpoint(
    community_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    cid = abs(int(community_id))
    q = await db.execute(select(VKToken).where(VKToken.name == f"COMM_{cid}"))
    token = q.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail=f"No community token for {community_id}")

    v = await validate_community_token(token.token, cid)
    token.validation_status = "valid" if v["is_valid"] else "invalid"
    token.error_message = v.get("error_message")
    token.user_info = v.get("user_info")
    token.permissions = v.get("permissions")
    token.last_validated = datetime.now()
    await db.commit()

    return TokenValidationResponse(
        name=token.name,
        is_valid=v["is_valid"],
        validation_status=token.validation_status,
        error_message=v.get("error_message"),
        user_info=v.get("user_info"),
        permissions=v.get("permissions"),
    )


@router.delete("/communities/{community_id}")
async def delete_community_token(
    community_id: int,
    db: AsyncSession = Depends(get_db_session),
):
    cid = abs(int(community_id))
    q = await db.execute(select(VKToken).where(VKToken.name == f"COMM_{cid}"))
    token = q.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail=f"No community token for {community_id}")
    await db.execute(delete(VKToken).where(VKToken.id == token.id))
    await db.commit()
    return {"success": True, "community_id": cid}


# ---------------------------------------------------------------------------
# TokenPolicy: ручной disable / enable (миграция 014, 2026-05-27).
# Регистрируется ДО `/{token_name}` — иначе FastAPI поймает path как имя токена.
# ---------------------------------------------------------------------------


@router.post("/{token_name}/disable", response_model=TokenResponse)
async def disable_token(
    token_name: str,
    payload: TokenDisableRequest,
    db: AsyncSession = Depends(get_db_session),
):
    """Отключить токен на ``hours`` часов (manual disable).

    Записывает ``disabled_until = now() + hours`` в ``vk_tokens``. Применяется
    TokenPolicy: токен сразу выпадает из ``pick(...)`` для всех операций.

    Если запись в БД ещё нет (а имя есть в env) — создаётся новая. Возвращает
    обновлённую запись.
    """
    from modules.vk_token_router import TokenPolicy

    upper = token_name.upper()
    policy = TokenPolicy(db)
    ok = await policy.disable(upper, hours=payload.hours, reason=payload.reason)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Token {upper} not found in env or DB")
    result = await db.execute(
        select(VKToken).where(VKToken.name == upper, VKToken.community_id.is_(None))
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=500, detail="disable succeeded but row not found")
    return TokenResponse(**token.to_dict())


@router.post("/{token_name}/enable", response_model=TokenResponse)
async def enable_token(
    token_name: str,
    db: AsyncSession = Depends(get_db_session),
):
    """Снять ручной/авто-disable: ``disabled_until=NULL`` + сброс ``consecutive_errors``.

    404 если записи в БД нет (значит токен и так не был disabled — нечего
    «включать»).
    """
    from modules.vk_token_router import TokenPolicy

    upper = token_name.upper()
    policy = TokenPolicy(db)
    ok = await policy.enable(upper)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Token {upper} not in DB")
    result = await db.execute(
        select(VKToken).where(VKToken.name == upper, VKToken.community_id.is_(None))
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=500, detail="enable succeeded but row not found")
    return TokenResponse(**token.to_dict())


class TokenPublishRoleRequest(BaseModel):
    """POST /api/tokens/{name}/publish-role body."""

    enabled: bool = Field(
        ...,
        description="True — разрешить публиковать (role='publish'); False — снять (NULL).",
    )


@router.post("/{token_name}/publish-role", response_model=TokenResponse)
async def set_token_publish_role(
    token_name: str,
    payload: TokenPublishRoleRequest,
    db: AsyncSession = Depends(get_db_session),
):
    """UI-override: пометить user-токен как разрешённый для публикаций.

    ``role='publish'`` добавляет токен к ``VK_PUBLISH_TOKEN_NAMES`` **аддитивно**
    (``TokenPolicy`` объединяет env-whitelist с БД-ролью) — без правки env и
    рестарта. Снятие (``enabled=False``) пишет ``role=NULL``. Лёгкий эндпоинт:
    токен НЕ перевалидируется. Community-токены публикуют в свою группу
    независимо от роли → для них 400.
    """
    upper = token_name.upper()
    result = await db.execute(select(VKToken).where(VKToken.name == upper))
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail=f"Token {upper} not found")
    if token.community_id is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Роль публикации применима только к user-токенам "
                "(community-токен публикует в свою группу)."
            ),
        )
    token.role = "publish" if payload.enabled else None
    await db.commit()
    await db.refresh(token)
    logger.info("Token %s publish-role set to %r", upper, token.role)
    return TokenResponse(**token.to_dict())


@router.get("/{token_name}", response_model=TokenResponse)
async def get_token(token_name: str, db: AsyncSession = Depends(get_db_session)):
    """Получить конкретный токен"""
    try:
        result = await db.execute(select(VKToken).where(VKToken.name == token_name.upper()))
        token = result.scalar_one_or_none()

        if not token:
            raise HTTPException(status_code=404, detail=f"Token {token_name} not found")

        return TokenResponse(**token.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting token {token_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{token_name}", response_model=TokenResponse)
async def update_token(
    token_name: str,
    request: TokenUpdateRequest,
    db: AsyncSession = Depends(get_db_session),
):
    """Обновить токен"""
    try:
        # Найти токен
        result = await db.execute(select(VKToken).where(VKToken.name == token_name.upper()))
        token = result.scalar_one_or_none()

        if not token:
            raise HTTPException(status_code=404, detail=f"Token {token_name} not found")

        # Обновить флаг активности (если передан)
        if request.is_active is not None:
            token.is_active = request.is_active

        # Обновить токен (если передан)
        if request.token is not None and request.token.strip() != "":
            token.token = request.token.strip()
            token.validation_status = "unknown"
            token.error_message = None

        # Валидировать токен если требуется
        if request.validate_token:
            if token.community_id:
                validation_result = await validate_community_token(token.token, token.community_id)
            else:
                validation_result = await validate_single_token(token.token)
            token.validation_status = "valid" if validation_result["is_valid"] else "invalid"
            token.error_message = validation_result.get("error_message")
            token.user_info = validation_result.get("user_info")
            token.permissions = validation_result.get("permissions")
            token.last_validated = datetime.now()

        await db.commit()
        await db.refresh(token)

        logger.info(f"Token {token_name} updated successfully")
        return TokenResponse(**token.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating token {token_name}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add", response_model=TokenResponse)
async def add_token(request: TokenCreateRequest, db: AsyncSession = Depends(get_db_session)):
    """Создать новый токен"""
    try:
        token_name = request.name.strip().upper()
        if not token_name:
            raise HTTPException(status_code=400, detail="Token name is required")

        # Check duplicate
        existing = await db.execute(select(VKToken).where(VKToken.name == token_name))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail=f"Token {token_name} already exists")

        vk_token = VKToken(
            name=token_name,
            token=request.token.strip(),
            community_id=abs(request.community_id) if request.community_id else None,
            is_active=True,
            validation_status="unknown",
            error_message=None,
            permissions=None,
            user_info=None,
        )

        if request.validate_token:
            if vk_token.community_id:
                validation_result = await validate_community_token(
                    vk_token.token, vk_token.community_id
                )
            else:
                validation_result = await validate_single_token(vk_token.token)
            vk_token.validation_status = "valid" if validation_result["is_valid"] else "invalid"
            vk_token.error_message = validation_result.get("error_message")
            vk_token.user_info = validation_result.get("user_info")
            vk_token.permissions = validation_result.get("permissions")
            vk_token.last_validated = datetime.now()

        db.add(vk_token)
        await db.commit()
        await db.refresh(vk_token)
        return TokenResponse(**vk_token.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding token: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{token_name}")
async def delete_token(token_name: str, db: AsyncSession = Depends(get_db_session)):
    """Удалить токен"""
    try:
        token_name = token_name.strip().upper()
        result = await db.execute(select(VKToken).where(VKToken.name == token_name))
        token = result.scalar_one_or_none()
        if not token:
            raise HTTPException(status_code=404, detail=f"Token {token_name} not found")

        await db.execute(delete(VKToken).where(VKToken.name == token_name))
        await db.commit()
        return {"success": True, "name": token_name}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting token {token_name}: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{token_name}/validate", response_model=TokenValidationResponse)
async def validate_token(token_name: str, db: AsyncSession = Depends(get_db_session)):
    """Валидировать токен"""
    try:
        # Найти токен
        result = await db.execute(select(VKToken).where(VKToken.name == token_name.upper()))
        token = result.scalar_one_or_none()

        if not token:
            raise HTTPException(status_code=404, detail=f"Token {token_name} not found")

        if not token.token:
            return TokenValidationResponse(
                name=token.name,
                is_valid=False,
                validation_status="invalid",
                error_message="Token is empty",
                user_info=None,
                permissions=None,
            )

        # Валидировать токен (community-токены — отдельная ветка, у них нет users.get)
        if token.community_id:
            validation_result = await validate_community_token(token.token, token.community_id)
        else:
            validation_result = await validate_single_token(token.token)

        # Обновить статус в БД
        token.validation_status = "valid" if validation_result["is_valid"] else "invalid"
        token.error_message = validation_result.get("error_message")
        token.user_info = validation_result.get("user_info")
        token.permissions = validation_result.get("permissions")
        token.last_validated = datetime.now()

        await db.commit()

        return TokenValidationResponse(
            name=token.name,
            is_valid=validation_result["is_valid"],
            validation_status=token.validation_status,
            error_message=validation_result.get("error_message"),
            user_info=validation_result.get("user_info"),
            permissions=validation_result.get("permissions"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating token {token_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/validate-all", response_model=List[TokenValidationResponse])
async def validate_all_tokens(db: AsyncSession = Depends(get_db_session)):
    """Валидировать все токены"""
    try:
        result = await db.execute(select(VKToken))
        tokens = result.scalars().all()

        validation_results = []

        for token in tokens:
            if not token.token:
                validation_results.append(
                    TokenValidationResponse(
                        name=token.name,
                        is_valid=False,
                        validation_status="invalid",
                        error_message="Token is empty",
                        user_info=None,
                        permissions=None,
                    )
                )
                continue

            # Валидировать токен (community-токены — отдельная ветка)
            if token.community_id:
                validation_result = await validate_community_token(token.token, token.community_id)
            else:
                validation_result = await validate_single_token(token.token)

            # Обновить статус в БД
            token.validation_status = "valid" if validation_result["is_valid"] else "invalid"
            token.error_message = validation_result.get("error_message")
            token.user_info = validation_result.get("user_info")
            token.permissions = validation_result.get("permissions")
            token.last_validated = datetime.now()

            validation_results.append(
                TokenValidationResponse(
                    name=token.name,
                    is_valid=validation_result["is_valid"],
                    validation_status=token.validation_status,
                    error_message=validation_result.get("error_message"),
                    user_info=validation_result.get("user_info"),
                    permissions=validation_result.get("permissions"),
                )
            )

        await db.commit()

        logger.info(f"Validated {len(tokens)} tokens")
        return validation_results

    except Exception as e:
        logger.error(f"Error validating all tokens: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


async def validate_community_token(token: str, community_id: int) -> Dict[str, Any]:
    """Валидировать community access token конкретного сообщества.

    Community-токены не имеют `users.get` — они привязаны к группе. Признак
    валидности: `messages.getConversations` (без group_id) проходит без [15]
    Access denied. Для info берём `groups.getById` под этим же токеном.
    """
    import vk_api

    try:
        session = vk_api.VkApi(token=token)
        api = session.get_api()

        # 1) Токен обязан принадлежать ИМЕННО этому сообществу.
        #
        # Прежняя версия звала ``groups.getById(group_id=<cid>)`` и считала успех
        # доказательством принадлежности. Это не доказательство: карточка
        # публичной группы читается любым токеном, поэтому ключ, вставленный не в
        # ту строку, проходил проверку с зелёным «ВАЛИДЕН». Ровно тот случай, о
        # котором спросил владелец 2026-08-19: «вдруг при вставке токена были
        # перепутаны сообщества».
        #
        # Спрашиваем токен о нём самом: ``groups.getById()`` БЕЗ аргументов
        # community-токен отвечает своей группой. Это единственный ответ, который
        # он подделать не может.
        expected = abs(int(community_id))
        group_info = None
        try:
            r = api.groups.getById()
            if isinstance(r, list) and r:
                group_info = r[0]
            elif isinstance(r, dict) and r.get("groups"):
                group_info = r["groups"][0]
        except vk_api.exceptions.ApiError as e:
            return {
                "is_valid": False,
                "error_message": f"groups.getById failed: {e}",
                "user_info": None,
                "permissions": None,
                "owns_community": None,
            }

        actual = None
        if isinstance(group_info, dict) and group_info.get("id") is not None:
            actual = abs(int(group_info["id"]))
        if actual is None:
            return {
                "is_valid": False,
                "error_message": "groups.getById() did not return the owning group",
                "user_info": group_info,
                "permissions": None,
                "owns_community": None,
            }
        if actual != expected:
            return {
                "is_valid": False,
                "error_message": (
                    f"токен принадлежит сообществу {actual} "
                    f"({group_info.get('name') or '?'}), а записан под {expected}"
                ),
                "user_info": group_info,
                "permissions": None,
                "owns_community": False,
            }

        # 2) Проверим scope `messages` — главная цель этого токена.
        permissions: List[str] = []
        try:
            api.messages.getConversations(count=1)
            permissions.append("messages.read")
        except vk_api.exceptions.ApiError as e:
            return {
                "is_valid": False,
                "error_message": f"messages.getConversations failed: {e}",
                "user_info": group_info,
                "permissions": [],
                "owns_community": True,
            }

        # 3) Дополнительно проверим wall.get (часто доступен community-токену) —
        # это не критично для валидности, но полезно для UI.
        try:
            api.wall.get(owner_id=-abs(int(community_id)), count=1)
            permissions.append("wall.read")
        except vk_api.exceptions.ApiError:
            pass

        return {
            "is_valid": True,
            "error_message": None,
            "user_info": group_info,
            "permissions": permissions,
            "owns_community": True,
        }
    except Exception as e:
        return {
            "is_valid": False,
            "error_message": str(e),
            "user_info": None,
            "permissions": None,
            "owns_community": None,
        }


async def validate_single_token(token: str) -> Dict[str, Any]:
    """Валидировать один токен"""
    try:
        vk_client = VKClient(token)
        user_info = await vk_client.get_user_info()

        if not user_info:
            return {
                "is_valid": False,
                "error_message": "Failed to get user info",
                "user_info": None,
                "permissions": None,
            }

        # Получить права доступа.
        # VKClient.get_posts/get_groups/get_messages глотают ApiError и возвращают None
        # при отказе VK — поэтому try/except не сработает, нужно проверять возвращаемое
        # значение на не-None, чтобы не репортить «есть permission», когда VK его отозвал.
        permissions = []
        if await vk_client.get_posts(owner_id=-1, count=1) is not None:
            permissions.append("wall.read")
        if await vk_client.get_groups(count=1) is not None:
            permissions.append("groups.read")
        if await vk_client.get_messages(count=1) is not None:
            permissions.append("messages.read")

        return {
            "is_valid": True,
            "error_message": None,
            "user_info": user_info,
            "permissions": permissions,
        }

    except Exception as e:
        return {
            "is_valid": False,
            "error_message": str(e),
            "user_info": None,
            "permissions": None,
        }
