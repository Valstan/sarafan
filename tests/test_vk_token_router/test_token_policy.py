"""Tests for :class:`modules.vk_token_router.TokenPolicy`.

Покрывают:
- pick(READ): возвращает все active user-токены, включая Vita; порядок —
  балансировка по фактическому расходу запросов за сутки (2026-07-25).
- pick(COMMUNITY_WRITE): каскад 2026-07-25 — community-токен первым, потом
  whitelist (МАМА), резерв (VALSTAN) строго последним; Vita под hard deny.
- pick(USER_WRITE): whitelist, затем резерв; hard deny-list побеждает всё.
- Valstan в cooldown (disabled_until > now) → выпадает из всех op'ов.
- pick_healthy_read_token: probe отсеивает мёртвый токен (cooldown) и отдаёт
  следующий живой (инцидент 2026-07-12).
- report_error(name, 5) → disabled_until = now + 24h; (29) → 1h; (100) → только consecutive++
- report_success(name) → consecutive_errors = 0.
- disable(name, hours) / enable(name) — manual control.

Сессия мокается через ``AsyncMock`` (см. tests/conftest.py — паттерн mock_db_session).
"""

import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Гарантируем env до импорта rt-модулей (общий паттерн conftest, но локально тоже).
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from database.models import VKToken  # noqa: E402
from modules.vk_token_router import TokenOp, TokenPolicy  # noqa: E402


def _vk_token_row(
    name: str,
    token_value: str,
    *,
    community_id=None,
    is_active=True,
    disabled_until=None,
    consecutive_errors=0,
    validation_status=None,
    role=None,
):
    """Лёгкий builder реальной модели VKToken (не Mock — модель ловит атрибуты).

    Используем настоящий ``VKToken`` чтобы код TokenPolicy получал атрибуты как
    в проде; mockSession.execute возвращает iterable scalars().
    """
    return VKToken(
        id=1,
        name=name,
        token=token_value,
        community_id=community_id,
        is_active=is_active,
        disabled_until=disabled_until,
        consecutive_errors=consecutive_errors,
        validation_status=validation_status,
        role=role,
    )


class _ScalarsResult:
    """Минимальная замена .scalars() — итерабельна и поддерживает .all()."""

    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)


def _make_session_with_rows(rows_by_query=None, single_row=None):
    """Async session-mock с queue execute-результатов.

    ``rows_by_query`` — список iterables, в порядке вызовов execute(). Каждый
    очередной execute(...) возвращает результат с .scalars() == следующий.
    ``single_row`` — для запросов scalar_one_or_none.
    """
    session = AsyncMock()

    queue = list(rows_by_query or [])

    async def _execute(*a, **kw):
        result = MagicMock()
        if queue:
            rows = queue.pop(0)
            result.scalars.return_value = _ScalarsResult(rows)
        else:
            result.scalars.return_value = _ScalarsResult([])
        result.scalar_one_or_none.return_value = single_row
        result.rowcount = 1
        return result

    session.execute.side_effect = _execute
    session.commit = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# pick(): семантика операций
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_read_includes_vita():
    """READ — любой active user-token, включая Vita."""
    rows_active = [
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("VITA", "tok_vita"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_VITA": "tok_vita",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.READ)
    names = [c.name for c in out]
    assert "VALSTAN" in names and "VITA" in names
    assert all(c.source == "user" for c in out)


@pytest.mark.asyncio
async def test_pick_user_write_excludes_vita():
    """USER_WRITE — только whitelist минус Vita."""
    rows_active = [
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("VITA", "tok_vita"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_VITA": "tok_vita",
            "VK_PUBLISH_TOKEN_NAMES": "VALSTAN,VITA",  # даже если кто-то поставит VITA
            "VK_NEVER_PUBLISH_TOKEN_NAMES": "VITA",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.USER_WRITE)
    names = [c.name for c in out]
    assert names == ["VALSTAN"]
    assert all(c.source == "user" for c in out)


@pytest.mark.asyncio
async def test_pick_community_write_prefers_community_token():
    """COMMUNITY_WRITE: для group_id=158 community-токен идёт первым."""
    user_rows = [_vk_token_row("VALSTAN", "tok_v")]
    comm_rows = [_vk_token_row("COMM_158", "tok_comm", community_id=158)]
    # Порядок execute(): active user → communities (внутри pick'а).
    session = _make_session_with_rows(rows_by_query=[user_rows, comm_rows])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_PUBLISH_TOKEN_NAMES": "VALSTAN",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.COMMUNITY_WRITE, group_id=-158)
    assert len(out) == 2
    assert out[0].source == "community"
    assert out[0].community_id == 158
    assert out[1].name == "VALSTAN"
    assert out[1].source == "user"


@pytest.mark.asyncio
async def test_pick_community_write_returns_all_community_tokens_before_users():
    """VALSTAN+MAMA community pool precedes the user-token cascade."""
    user_rows = [_vk_token_row("VALSTAN", "tok_user")]
    comm_rows = [
        _vk_token_row("COMM_158_MAMA", "tok_comm_mama", community_id=158),
        _vk_token_row("COMM_158", "tok_comm_valstan", community_id=158),
    ]
    session = _make_session_with_rows(rows_by_query=[user_rows, comm_rows])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_user",
            "VK_PUBLISH_TOKEN_NAMES": "VALSTAN",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        out = await TokenPolicy(session).pick(TokenOp.COMMUNITY_WRITE, group_id=-158)

    assert [(candidate.name, candidate.source) for candidate in out] == [
        ("COMM_158", "community"),
        ("COMM_158_MAMA", "community"),
        ("VALSTAN", "user"),
    ]


@pytest.mark.asyncio
async def test_db_role_publish_augments_env_whitelist():
    """role='publish' в БД добавляет токен к env-whitelist'у (аддитивно)."""
    rows_active = [
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("OLGA", "tok_o", role="publish"),  # не в env-whitelist
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_OLGA": "tok_o",
            "VK_PUBLISH_TOKEN_NAMES": "VALSTAN",  # env знает только VALSTAN
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.USER_WRITE)
    names = [c.name for c in out]
    assert "VALSTAN" in names
    assert "OLGA" in names  # добавлена через role='publish'


@pytest.mark.asyncio
async def test_db_role_publish_still_excluded_by_deny_list():
    """Deny-list (VK_NEVER_PUBLISH_TOKEN_NAMES) имеет приоритет над role='publish'."""
    rows_active = [
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("VITA", "tok_vita", role="publish"),  # роль не спасёт от deny
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_VITA": "tok_vita",
            "VK_PUBLISH_TOKEN_NAMES": "VALSTAN",
            "VK_NEVER_PUBLISH_TOKEN_NAMES": "VITA",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.USER_WRITE)
    names = [c.name for c in out]
    assert "VALSTAN" in names
    assert "VITA" not in names  # deny-list побеждает роль


@pytest.mark.asyncio
async def test_pick_skips_valstan_in_cooldown():
    """Valstan с disabled_until > now → не в pick(READ) и не в pick(USER_WRITE)."""
    future = datetime.utcnow() + timedelta(hours=10)
    rows_active = [
        # disabled_until > now — _load_active отбросит запись Valstan.
        # Помещаем только Vita в active, имитируя что _load_active фильтрует.
        _vk_token_row("VITA", "tok_vita"),
    ]
    # Для _token_exists_but_disabled — отдельный execute с одним row Valstan.
    valstan_disabled = _vk_token_row("VALSTAN", "tok_v", disabled_until=future, is_active=True)

    session = AsyncMock()
    call_count = {"n": 0}

    async def _execute(*a, **kw):
        call_count["n"] += 1
        result = MagicMock()
        # 1-й вызов — _load_active (только Vita)
        # 2-й вызов (если будет) — _token_exists_but_disabled на VALSTAN
        if call_count["n"] == 1:
            result.scalars.return_value = _ScalarsResult(rows_active)
        else:
            result.scalars.return_value = _ScalarsResult([])
            result.scalar_one_or_none.return_value = valstan_disabled
        return result

    session.execute.side_effect = _execute
    session.commit = AsyncMock()

    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_VITA": "tok_vita",
            "VK_PUBLISH_TOKEN_NAMES": "VALSTAN",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)

        read_out = await policy.pick(TokenOp.READ)
        read_names = [c.name for c in read_out]
        assert "VITA" in read_names
        assert "VALSTAN" not in read_names

        write_out = await policy.pick(TokenOp.USER_WRITE)
        # Valstan в cooldown, а Vita с 2026-07-25 — hard deny на публикацию
        # (чистый сборщик). Значит публиковать user-токеном нечем: пустой
        # список — правильный ответ, а не деградация к Vita. Caller обязан
        # вернуть внятную ошибку «сейчас публиковать нечем».
        assert [c.name for c in write_out] == []


# ---------------------------------------------------------------------------
# report_error / report_success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_error_5_sets_24h_cooldown():
    row = _vk_token_row("VALSTAN", "tok_v")
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalars.return_value = _ScalarsResult([])
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    with patch("modules.vk_token_router._send_telegram_alert_safe", new_callable=AsyncMock):
        policy = TokenPolicy(session)
        before = datetime.utcnow()
        await policy.report_error("VALSTAN", 5)

    assert row.last_error_code == 5
    assert row.disabled_until is not None
    delta = row.disabled_until - before
    assert timedelta(hours=23, minutes=58) < delta < timedelta(hours=24, minutes=2)
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_report_error_29_sets_1h_cooldown():
    row = _vk_token_row("VALSTAN", "tok_v")
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalars.return_value = _ScalarsResult([])
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    with patch("modules.vk_token_router._send_telegram_alert_safe", new_callable=AsyncMock):
        policy = TokenPolicy(session)
        before = datetime.utcnow()
        await policy.report_error("VALSTAN", 29)

    assert row.disabled_until is not None
    delta = row.disabled_until - before
    assert timedelta(minutes=58) < delta < timedelta(minutes=62)


@pytest.mark.asyncio
async def test_report_error_other_code_only_counts():
    """Код 100 не в auto-disable списке — только consecutive++ без cooldown."""
    row = _vk_token_row("VALSTAN", "tok_v")
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    result.scalars.return_value = _ScalarsResult([])
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    policy = TokenPolicy(session)
    await policy.report_error("VALSTAN", 100)

    assert row.disabled_until is None
    assert row.consecutive_errors == 1


@pytest.mark.asyncio
async def test_report_success_resets_consecutive():
    """report_success — UPDATE с consecutive_errors=0."""
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 1
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    policy = TokenPolicy(session)
    await policy.report_success("VALSTAN")
    # Был один UPDATE-вызов.
    session.execute.assert_awaited()
    session.commit.assert_awaited()


# ---------------------------------------------------------------------------
# Manual disable / enable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_creates_row_if_missing():
    """Если записи в БД ещё нет — disable создаёт новую из env-токена."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalars.return_value = _ScalarsResult([])
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    captured = {}

    def _add(obj):
        captured["row"] = obj

    session.add = MagicMock(side_effect=_add)

    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        ok = await policy.disable("VALSTAN", hours=24, reason="prod ban 24h")

    assert ok is True
    new_row = captured.get("row")
    assert new_row is not None
    assert new_row.name == "VALSTAN"
    assert new_row.disabled_until is not None
    assert "prod ban" in (new_row.error_message or "")


@pytest.mark.asyncio
async def test_enable_returns_false_if_no_row():
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 0
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    policy = TokenPolicy(session)
    ok = await policy.enable("NONEXISTENT")
    assert ok is False


# ---------------------------------------------------------------------------
# get_active_parse_tokens: значения берутся из БД (single source of truth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_parse_tokens_returns_db_values():
    """Возвращает {name: token} из БД для active user-токенов."""
    from modules.vk_token_router import get_active_parse_tokens

    rows = [
        _vk_token_row("VALSTAN", "db_tok_valstan", validation_status="valid"),
        _vk_token_row("VITA", "db_tok_vita", validation_status="valid"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows])
    out = await get_active_parse_tokens(session)
    assert out == {"VALSTAN": "db_tok_valstan", "VITA": "db_tok_vita"}


@pytest.mark.asyncio
async def test_active_parse_tokens_value_from_db_not_env():
    """Значение токена — из БД, даже если в env лежит другое (рассинхрон)."""
    from modules.vk_token_router import get_active_parse_tokens

    rows = [_vk_token_row("VALSTAN", "fresh_db_token", validation_status="valid")]
    session = _make_session_with_rows(rows_by_query=[rows])
    with patch.dict(os.environ, {"VK_TOKEN_VALSTAN": "stale_env_token"}, clear=False):
        out = await get_active_parse_tokens(session)
    assert out == {"VALSTAN": "fresh_db_token"}


@pytest.mark.asyncio
async def test_active_parse_tokens_skips_invalid():
    """validation_status='invalid' — токен в парсинг не берётся."""
    from modules.vk_token_router import get_active_parse_tokens

    rows = [
        _vk_token_row("VALSTAN", "tok_valstan", validation_status="invalid"),
        _vk_token_row("VITA", "tok_vita", validation_status="valid"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows])
    out = await get_active_parse_tokens(session)
    assert out == {"VITA": "tok_vita"}


@pytest.mark.asyncio
async def test_active_parse_tokens_keeps_unknown():
    """validation_status='unknown'/None — годится (свежедобавленный токен)."""
    from modules.vk_token_router import get_active_parse_tokens

    rows = [
        _vk_token_row("VALSTAN", "tok_valstan", validation_status="unknown"),
        _vk_token_row("VITA", "tok_vita", validation_status=None),
    ]
    session = _make_session_with_rows(rows_by_query=[rows])
    out = await get_active_parse_tokens(session)
    assert out == {"VALSTAN": "tok_valstan", "VITA": "tok_vita"}


@pytest.mark.asyncio
async def test_active_parse_tokens_skips_inactive():
    """is_active=False — исключён."""
    from modules.vk_token_router import get_active_parse_tokens

    rows = [
        _vk_token_row("VALSTAN", "tok_valstan", is_active=False, validation_status="valid"),
        _vk_token_row("VITA", "tok_vita", validation_status="valid"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows])
    out = await get_active_parse_tokens(session)
    assert out == {"VITA": "tok_vita"}


@pytest.mark.asyncio
async def test_active_parse_tokens_skips_cooldown():
    """disabled_until > now() — на cooldown, исключён."""
    from modules.vk_token_router import get_active_parse_tokens

    future = datetime.utcnow() + timedelta(hours=2)
    rows = [
        _vk_token_row("VALSTAN", "tok_valstan", disabled_until=future, validation_status="valid"),
        _vk_token_row("VITA", "tok_vita", validation_status="valid"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows])
    out = await get_active_parse_tokens(session)
    assert out == {"VITA": "tok_vita"}


@pytest.mark.asyncio
async def test_active_parse_tokens_skips_empty_token():
    """Пустой token в БД — исключён (плейсхолдер-строки)."""
    from modules.vk_token_router import get_active_parse_tokens

    rows = [
        _vk_token_row("ELIS", "", validation_status="unknown"),
        _vk_token_row("VITA", "tok_vita", validation_status="valid"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows])
    out = await get_active_parse_tokens(session)
    assert out == {"VITA": "tok_vita"}


# ---------------------------------------------------------------------------
# Каскад публикации 2026-07-25: community → МАМА → VALSTAN.
# VITA не публикует никогда (hard deny), даже когда все остальные мертвы.
# Обоснование порядка — docs/VK_TOKEN_ROADMAP.md.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_community_write_cascade_order():
    """COMMUNITY_WRITE: community-токен → whitelist (МАМА) → резерв (VALSTAN)."""
    rows_active = [
        _vk_token_row("VITA", "tok_vita"),  # нарочно первым в БД — порядок задаёт каскад
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("MAMA", "tok_mama"),
    ]
    comm_rows = [_vk_token_row("COMM_777", "tok_comm", community_id=777)]
    session = _make_session_with_rows(rows_by_query=[rows_active, comm_rows])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_VITA": "tok_vita",
            "VK_TOKEN_MAMA": "tok_mama",
            "VK_PUBLISH_TOKEN_NAMES": "MAMA",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.COMMUNITY_WRITE, group_id=-777)

    assert [(c.name, c.source) for c in out] == [
        ("COMM_777", "community"),
        ("MAMA", "user"),
        ("VALSTAN", "user"),
    ]


@pytest.mark.asyncio
async def test_pick_user_write_reserve_last():
    """USER_WRITE: whitelist (МАМА) сначала, резерв (VALSTAN) строго последним."""
    rows_active = [
        _vk_token_row("VITA", "tok_vita"),
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("MAMA", "tok_mama"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "tok_v",
            "VK_TOKEN_VITA": "tok_vita",
            "VK_TOKEN_MAMA": "tok_mama",
            "VK_PUBLISH_TOKEN_NAMES": "MAMA",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        out = await TokenPolicy(session).pick(TokenOp.USER_WRITE)

    # Vita в списке БД есть, но она под hard deny — в кандидаты не попадает.
    assert [c.name for c in out] == ["MAMA", "VALSTAN"]


@pytest.mark.asyncio
async def test_pick_db_token_value_wins_over_env():
    """Единый источник 2026-07-12: значение токена берётся из БД, не из env."""
    rows_active = [_vk_token_row("VALSTAN", "db_tok")]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "stale_env_tok",  # рассинхрон: env отстал от БД
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        out = await TokenPolicy(session).pick(TokenOp.READ)

    assert [(c.name, c.token) for c in out] == [("VALSTAN", "db_tok")]


# ---------------------------------------------------------------------------
# pick_healthy_read_token: probe + self-heal (инцидент 2026-07-12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_healthy_read_token_skips_dead_and_cooldowns_it():
    """Мёртвый первый токен (error 5) → cooldown + отдаётся следующий живой."""
    import modules.vk_token_router as vtr

    rows_active = [
        _vk_token_row("VALSTAN", "dead_tok"),
        _vk_token_row("VITA", "live_tok"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])

    probed = []

    def _fake_probe(token):
        probed.append(token)
        return 5 if token == "dead_tok" else None

    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "dead_tok",
            "VK_TOKEN_VITA": "live_tok",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        with patch.object(vtr, "_probe_token_sync", _fake_probe):
            cand = await vtr.pick_healthy_read_token(session)

    assert cand is not None
    assert cand.name == "VITA"
    assert probed == ["dead_tok", "live_tok"]


@pytest.mark.asyncio
async def test_pick_healthy_read_token_none_when_all_dead():
    import modules.vk_token_router as vtr

    rows_active = [_vk_token_row("VALSTAN", "dead1")]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "dead1",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        with patch.object(vtr, "_probe_token_sync", lambda t: 5):
            cand = await vtr.pick_healthy_read_token(session)
    assert cand is None


@pytest.mark.asyncio
async def test_pick_healthy_read_token_network_glitch_skips_without_disable():
    """Сетевой сбой probe (-1) — токен пропускается, но report_error не зовётся."""
    import modules.vk_token_router as vtr

    rows_active = [
        _vk_token_row("VALSTAN", "glitchy"),
        _vk_token_row("VITA", "live_tok"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
            "VK_TOKEN_VALSTAN": "glitchy",
            "VK_TOKEN_VITA": "live_tok",
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        report_calls = []
        with (
            patch.object(vtr, "_probe_token_sync", lambda t: -1 if t == "glitchy" else None),
            patch.object(
                TokenPolicy,
                "report_error",
                AsyncMock(side_effect=lambda *a: report_calls.append(a)),
            ),
        ):
            cand = await vtr.pick_healthy_read_token(session)
    assert cand is not None and cand.name == "VITA"
    assert report_calls == []  # сетевой сбой ≠ вина токена


# ---------------------------------------------------------------------------
# Карусель чтения 2026-07-12: last_used ASC + штамп при выборе
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_read_orders_by_last_used_oldest_first():
    """READ-кандидаты идут каруселью: давно не использованные первыми, NULL в голову."""
    rows_active = [
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("VITA", "tok_vita"),
        _vk_token_row("MAMA", "tok_m"),
    ]
    rows_active[0].last_used = datetime(2026, 7, 12, 10, 0)  # использован недавно
    rows_active[1].last_used = datetime(2026, 7, 12, 9, 0)  # раньше
    rows_active[2].last_used = None  # никогда → первым
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        out = await TokenPolicy(session).pick(TokenOp.READ)
    assert [c.name for c in out] == ["MAMA", "VITA", "VALSTAN"]


@pytest.mark.asyncio
async def test_pick_healthy_read_token_stamps_rotation():
    """Успешный выбор штампует last_used (report_success) — замыкает карусель."""
    import modules.vk_token_router as vtr

    rows_active = [_vk_token_row("VITA", "live_tok")]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "REDIS_URL": os.environ["REDIS_URL"],
        },
        clear=True,
    ):
        import importlib

        import config.runtime as rt

        importlib.reload(rt)
        success_calls = []
        with (
            patch.object(vtr, "_probe_token_sync", lambda t: None),
            patch.object(
                TokenPolicy,
                "report_success",
                AsyncMock(side_effect=lambda name: success_calls.append(name)),
            ),
        ):
            cand = await vtr.pick_healthy_read_token(session)
    assert cand is not None and cand.name == "VITA"
    assert success_calls == ["VITA"]


# ---------------------------------------------------------------------------
# Балансировка READ по фактическому расходу запросов (заказ владельца 2026-07-25)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_read_orders_by_calls_today():
    """READ: первым идёт токен, потративший сегодня меньше запросов.

    Раньше порядок задавал только ``last_used``, но READ-токен выбирается один
    на волну парсинга — волны разного веса давали одинаковый штамп. Теперь
    балансируется расход: VITA сегодня сожгла больше всех и уходит в хвост,
    хотя по ``last_used`` была бы первой.
    """
    old = datetime.utcnow() - timedelta(hours=5)
    recent = datetime.utcnow()
    rows_active = [
        _vk_token_row("VITA", "tok_vita"),
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("MAMA", "tok_mama"),
    ]
    rows_active[0].last_used = old  # давно не использовалась → была бы первой
    rows_active[1].last_used = recent
    rows_active[2].last_used = recent

    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch(
        "modules.vk_monitor.token_usage.get_calls_today",
        return_value={"VITA": 900, "VALSTAN": 120, "MAMA": 40},
    ):
        out = await TokenPolicy(session).pick(TokenOp.READ)

    assert [c.name for c in out] == ["MAMA", "VALSTAN", "VITA"]


@pytest.mark.asyncio
async def test_pick_read_falls_back_to_last_used_without_usage():
    """Учёт расхода недоступен (Redis лёг) → прежний порядок по last_used."""
    older = datetime.utcnow() - timedelta(hours=9)
    newer = datetime.utcnow()
    rows_active = [
        _vk_token_row("MAMA", "tok_mama"),
        _vk_token_row("VITA", "tok_vita"),
    ]
    rows_active[0].last_used = newer
    rows_active[1].last_used = older

    session = _make_session_with_rows(rows_by_query=[rows_active])
    with patch(
        "modules.vk_monitor.token_usage.get_calls_today",
        side_effect=RuntimeError("redis down"),
    ):
        out = await TokenPolicy(session).pick(TokenOp.READ)

    assert [c.name for c in out] == ["VITA", "MAMA"]


@pytest.mark.asyncio
async def test_pick_registers_token_names_for_usage_accounting():
    """pick() объявляет соответствие «строка токена → имя» учёту расхода.

    Без этого ``VKClient`` не знает, чей запрос считать, и весь расход уезжает
    в ``UNKNOWN:<отпечаток>``.
    """
    from modules.vk_monitor import token_usage

    token_usage.reset_for_tests()
    rows_active = [_vk_token_row("MAMA", "tok_mama")]
    session = _make_session_with_rows(rows_by_query=[rows_active])

    await TokenPolicy(session).pick(TokenOp.READ)

    assert token_usage.resolve_token_name("tok_mama") == "MAMA"
    token_usage.reset_for_tests()


@pytest.mark.asyncio
async def test_load_community_tokens_registers_names_for_usage():
    """Публикация ходит legacy-картой мимо pick() — имена всё равно нужны.

    Без регистрации здесь весь расход ``wall.post`` уезжал в
    ``UNKNOWN:<отпечаток>``: издатель строит клиент прямо из этой карты.
    """
    from modules.vk_monitor import token_usage
    from modules.vk_token_router import load_community_tokens

    token_usage.reset_for_tests()
    comm_rows = [_vk_token_row("COMM_777", "tok_comm", community_id=777)]
    session = _make_session_with_rows(rows_by_query=[comm_rows])

    out = await load_community_tokens(session)

    assert out == {777: "tok_comm"}
    assert token_usage.resolve_token_name("tok_comm") == "COMM_777"
    token_usage.reset_for_tests()


# ---------------------------------------------------------------------------
# Этап 3 (2026-09-05): личность отправителя и фильтр по способностям
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_dm_is_exactly_the_account_or_nothing(monkeypatch):
    """USER_DM — ровно названный аккаунт, без каскада; VITA (deny-list) — никогда."""
    import modules.vk_token_router as router

    rows_active = [
        _vk_token_row("VALSTAN", "tok_valstan"),
        _vk_token_row("MAMA", "tok_mama"),
        _vk_token_row("VITA", "tok_vita"),
    ]
    session = _make_session_with_rows([rows_active])
    monkeypatch.setattr(router, "_capabilities_matrix_safe", lambda: None)
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value={"VITA"}),
        patch("config.runtime.get_publish_token_names", return_value=["VALSTAN"]),
        patch("config.runtime.get_reserve_publish_token_names", return_value=[]),
    ):
        policy = TokenPolicy(session)
        mama = await policy.pick(TokenOp.USER_DM, account="mama")
        assert [c.name for c in mama] == ["MAMA"] and mama[0].source == "user"
        assert await policy.pick(TokenOp.USER_DM, account="VITA") == []
        assert await policy.pick(TokenOp.USER_DM, account="NOBODY") == []
        assert await policy.pick(TokenOp.USER_DM) == []
        # Явный отказ права в снапшоте — аккаунт не годится даже по прямому запросу.
        router._capabilities_cache.update({"at": None, "matrix": None})
        monkeypatch.setattr(
            router,
            "_capabilities_matrix_safe",
            lambda: {"MAMA": {"messages.getConversations": "err15"}},
        )
        assert await TokenPolicy(session).pick(TokenOp.USER_DM, account="MAMA") == []


@pytest.mark.asyncio
async def test_community_dm_cascade_and_capability_filter(monkeypatch):
    """COMMUNITY_DM: community-токен первым, затем каскад; без права messages — мимо."""
    import modules.vk_token_router as router

    rows_active = [_vk_token_row("VALSTAN", "tok_valstan"), _vk_token_row("MAMA", "tok_mama")]
    rows_comm = [_vk_token_row("COMM_100", "tok_c100", community_id=100)]
    session = _make_session_with_rows([rows_active, rows_comm])
    matrix = {
        "VALSTAN": {"messages.getConversations": "err15"},  # scope messages нет
        "MAMA": {"messages.getConversations": "ok"},
        # COMM_100 в снапшоте нет — неизвестное не фильтруем
    }
    monkeypatch.setattr(router, "_capabilities_matrix_safe", lambda: matrix)
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value={"VITA"}),
        patch("config.runtime.get_publish_token_names", return_value=["VALSTAN", "MAMA"]),
        patch("config.runtime.get_reserve_publish_token_names", return_value=[]),
    ):
        policy = TokenPolicy(session)
        out = await policy.pick(TokenOp.COMMUNITY_DM, group_id=-100)
        assert [c.name for c in out] == ["COMM_100", "MAMA"]
        assert out[0].source == "community" and out[0].community_id == 100


@pytest.mark.asyncio
async def test_read_is_never_filtered_by_snapshot(monkeypatch):
    """READ не фильтруется: проба «wall.get(чужое)» бьёт в одну донорскую стену."""
    import modules.vk_token_router as router

    rows_active = [_vk_token_row("A", "ta"), _vk_token_row("B", "tb")]
    session = _make_session_with_rows([rows_active])
    monkeypatch.setattr(
        router, "_capabilities_matrix_safe", lambda: {"A": {"wall.get(чужое)": "err15"}}
    )
    monkeypatch.setattr(router, "_calls_today_safe", lambda: {})
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value=set()),
        patch("config.runtime.get_publish_token_names", return_value=[]),
        patch("config.runtime.get_reserve_publish_token_names", return_value=[]),
    ):
        out = await TokenPolicy(session).pick(TokenOp.READ)
        assert sorted(c.name for c in out) == ["A", "B"]


@pytest.mark.asyncio
async def test_community_dm_without_snapshot_has_no_user_tail(monkeypatch):
    """Без снапшота хвост из user-токенов в DM не подмешивается (allow-list)."""
    import modules.vk_token_router as router

    rows_active = [_vk_token_row("MAMA", "tok_mama")]
    rows_comm = [_vk_token_row("COMM_100", "tok_c100", community_id=100)]
    session = _make_session_with_rows([rows_active, rows_comm])
    monkeypatch.setattr(router, "_capabilities_matrix_safe", lambda: None)
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value=set()),
        patch("config.runtime.get_publish_token_names", return_value=["MAMA"]),
        patch("config.runtime.get_reserve_publish_token_names", return_value=[]),
    ):
        out = await TokenPolicy(session).pick(TokenOp.COMMUNITY_DM, group_id=100)
        assert [c.name for c in out] == ["COMM_100"]


def test_capability_filter_env_kill_switch(monkeypatch):
    import modules.vk_token_router as router

    monkeypatch.setenv("VK_CAPABILITY_FILTER", "0")
    assert router._capabilities_matrix_safe() is None


# ---------------------------------------------------------------------------
# token_for_account(): конкретный аккаунт, без каскада (P214)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_for_account_returns_named_account():
    """Запросили конкретный аккаунт — получили именно его токен."""
    rows_active = [
        _vk_token_row("VALSTAN", "tok_v"),
        _vk_token_row("MAMA", "tok_mama"),
    ]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value=set()),
    ):
        candidate = await TokenPolicy(session).token_for_account("mama")
    assert candidate is not None
    assert candidate.name == "MAMA"
    assert candidate.token == "tok_mama"
    assert candidate.source == "user"


@pytest.mark.asyncio
async def test_token_for_account_unknown_account_is_none():
    """Аккаунта нет — токена нет: выдумывать нечего."""
    session = _make_session_with_rows(rows_by_query=[[]])
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value=set()),
    ):
        assert await TokenPolicy(session).token_for_account("VALSTAN") is None
        assert await TokenPolicy(session).token_for_account("") is None


@pytest.mark.asyncio
async def test_token_for_account_respects_hard_deny_list():
    """Hard deny-list действует и здесь: запрещённому аккаунту нельзя и заливать."""
    rows_active = [_vk_token_row("VITA", "tok_vita")]
    session = _make_session_with_rows(rows_by_query=[rows_active])
    with (
        patch("config.runtime.VK_TOKENS", {}),
        patch("config.runtime.get_never_publish_token_names", return_value={"VITA"}),
    ):
        assert await TokenPolicy(session).token_for_account("VITA") is None
