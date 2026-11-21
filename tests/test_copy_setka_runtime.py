"""Тесты конфигурации сетевого хаба copy/setka (без VK/БД)."""


def test_copy_setka_source_group_id_parses(monkeypatch):
    monkeypatch.setenv("COPY_SETKA_SOURCE_GROUP_ID", "-123456")
    from config.runtime import get_copy_setka_source_owner_id

    assert get_copy_setka_source_owner_id() == -123456


def test_copy_setka_default_source_is_copy_by_setka(monkeypatch):
    """Без env — группа vk.com/copy_by_setka (-167381590)."""
    monkeypatch.delenv("COPY_SETKA_SOURCE_GROUP_ID", raising=False)
    from config.runtime import get_copy_setka_source_owner_id

    assert get_copy_setka_source_owner_id() == -167381590


def test_copy_setka_use_repost_default(monkeypatch):
    monkeypatch.delenv("COPY_SETKA_USE_REPOST", raising=False)
    from config.runtime import copy_setka_use_repost

    assert copy_setka_use_repost() is True


def test_copy_setka_target_codes(monkeypatch):
    monkeypatch.setenv("COPY_SETKA_TARGET_REGION_CODES", " Ur , pizhanka ")
    from config.runtime import get_copy_setka_target_region_codes

    assert get_copy_setka_target_region_codes() == {"ur", "pizhanka"}


def test_copy_setka_disabled(monkeypatch):
    monkeypatch.setenv("COPY_SETKA_DISABLED", "1")
    from config.runtime import copy_setka_disabled

    assert copy_setka_disabled() is True


# ---------------------------------------------------------------------------
# Перебор parse-токенов при чтении частной стены-источника (фикс 2026-06-07)
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, items):
        self._items = list(items)

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _FakeScalars(self._items)


class _FakeSession:
    """Отдаёт заранее заготовленные результаты на каждый execute() по очереди."""

    def __init__(self, results):
        self._results = list(results)
        self.commits = 0

    async def execute(self, _query):
        return _FakeResult(self._results.pop(0))

    async def commit(self):
        self.commits += 1

    def add(self, _obj):
        pass

    async def refresh(self, _obj):
        pass


class _WorkTable:
    def __init__(self):
        self.region_code = "copy"
        self.theme = "setka"
        self.lip = []
        self.hash = []


class _Region:
    def __init__(self, code, vk_group_id, *, id=None, parent_region_id=None):
        self.code = code
        self.vk_group_id = vk_group_id
        self.is_active = True
        self.id = id
        self.parent_region_id = parent_region_id


def _patch_copy_setka_deps(stack, *, parse_tokens, wall_by_token, publisher):
    """Заглушить внешние зависимости execute_copy_setka_network.

    ``wall_by_token`` — dict {token_value: [posts]} (что вернёт get_wall_posts).
    """
    from contextlib import contextmanager
    from unittest.mock import patch

    import config.runtime as rt

    @contextmanager
    def _p(target, **kw):
        with patch(target, **kw) as m:
            yield m

    # config.runtime
    stack.enter_context(_p("config.runtime.copy_setka_disabled", return_value=False))
    stack.enter_context(
        _p("config.runtime.get_copy_setka_source_owner_id", return_value=-167381590)
    )
    stack.enter_context(_p("config.runtime.get_copy_setka_max_post_age_hours", return_value=48.0))
    stack.enter_context(_p("config.runtime.get_copy_setka_repost_message", return_value=""))
    stack.enter_context(_p("config.runtime.get_copy_setka_target_region_codes", return_value=None))
    stack.enter_context(_p("config.runtime.get_copy_setka_post_interval_seconds", return_value=0.0))
    stack.enter_context(_p("config.runtime.get_copy_setka_max_reposts_per_tick", return_value=8))
    stack.enter_context(_p("config.runtime.get_copy_setka_freeze_minutes", return_value=120))
    assert rt  # keep import used

    async def _fake_get_tokens(_session):
        return dict(parse_tokens)

    stack.enter_context(
        _p("modules.vk_token_router.get_active_parse_tokens", side_effect=_fake_get_tokens)
    )

    def _fake_vkclient(token):
        from unittest.mock import MagicMock

        m = MagicMock()
        m.get_wall_posts.return_value = list(wall_by_token.get(token, []))
        return m

    stack.enter_context(_p("modules.vk_monitor.vk_client.VKClient", side_effect=_fake_vkclient))

    async def _create_with_policy(*_a, **_k):
        return publisher

    stack.enter_context(
        _p(
            "modules.publisher.vk_publisher_extended.VKPublisher.create_with_policy",
            side_effect=_create_with_policy,
        )
    )

    # Region / WorkTable НЕ патчим: реальные SQLAlchemy-модели нужны для
    # построения select(...).where(...). _FakeSession всё равно игнорирует
    # запрос и отдаёт подставные инстансы (_WorkTable / _Region) из canned-
    # результатов.

    stack.enter_context(_p("utils.post_utils.lip_of_post", side_effect=lambda o, p: f"{o}_{p}"))
    stack.enter_context(_p("utils.post_utils.clear_copy_history", side_effect=lambda post: post))
    stack.enter_context(_p("utils.vk_attachments.extract_vk_attachments", return_value={}))
    stack.enter_context(_p("utils.vk_attachments.build_attachments_list", return_value=[]))

    # стабильное "сейчас" для проверки возраста поста
    stack.enter_context(_p("modules.copy_setka_network.time.time", return_value=1_000_000))


async def test_copy_setka_skips_nonmember_token_and_uses_member(monkeypatch):
    """Первый токен не видит частную стену (error 15 → []), берётся второй."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from modules.copy_setka_network import execute_copy_setka_network

    fresh_post = {"owner_id": -167381590, "id": 555, "date": 999_990, "text": "новость"}
    publisher = AsyncMock()
    publisher.publish_bulletin.return_value = {"success": True, "url": "https://vk.com/wall-1_1"}

    session = _FakeSession([[_WorkTable()], [_Region("ur", -168170215)]])

    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            # порядок важен: НЕ-участник первым (имитируем недетерминированный
            # порядок из get_active_parse_tokens без ORDER BY)
            parse_tokens={"VITA": "TOK_NONMEMBER", "VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [fresh_post]},  # TOK_NONMEMBER → []
            publisher=publisher,
        )
        out = await execute_copy_setka_network(session)

    assert out["success"] is True
    assert out["posts_published"] == 1
    assert out["mode"] == "wall.post copy"
    publisher.publish_bulletin.assert_awaited_once()


async def test_copy_setka_reports_when_no_token_can_read(monkeypatch):
    """Если ни один токен не читает стену — корректный no-op, без падения."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from modules.copy_setka_network import execute_copy_setka_network

    publisher = AsyncMock()
    session = _FakeSession([[_WorkTable()], [_Region("ur", -168170215)]])

    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VITA": "TOK_A", "VALSTAN": "TOK_B"},
            wall_by_token={},  # оба токена → []
            publisher=publisher,
        )
        out = await execute_copy_setka_network(session)

    assert out["success"] is True
    assert out["posts_published"] == 0
    assert out["message"] == "no posts on source wall"
    publisher.publish_bulletin.assert_not_called()


def _bulletin_side_effect(captcha_gids):
    """Фабрика side_effect для publish_bulletin: капча на заданных gid."""

    def _se(*, group_id, text=None, attachments=None):
        if group_id in captcha_gids:
            return {"success": False, "error": "Captcha needed"}
        return {"success": True, "url": f"https://vk.com/wall{group_id}_1"}

    return _se


async def test_copy_setka_partial_then_retries_tail_next_tick(monkeypatch):
    """Регион в капче → пост помечается pending, добирается на следующем тике."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from modules.copy_setka_network import execute_copy_setka_network

    post = {"owner_id": -167381590, "id": 555, "date": 999_990, "text": "новость"}
    wt = _WorkTable()  # общий между прогонами — несёт pending в .hash
    UR, MI = -168170215, -158787639

    # --- Тик 1: MI ловит капчу ---
    pub1 = AsyncMock()
    pub1.publish_bulletin.side_effect = _bulletin_side_effect({MI})
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=pub1,
        )
        out1 = await execute_copy_setka_network(
            _FakeSession([[wt], [_Region("ur", UR), _Region("mi", MI)]])
        )

    assert out1["complete"] is False
    assert out1["posts_published"] == 1  # ur доставлен
    assert out1["missing"] == ["mi"]
    assert wt.lip == []  # пост ещё НЕ помечен разосланным
    assert isinstance(wt.hash, dict) and wt.hash["done"] == ["ur"] and wt.hash["tries"] == 1

    # --- Тик 2: капчи нет, добираем MI ---
    pub2 = AsyncMock()
    pub2.publish_bulletin.side_effect = _bulletin_side_effect(set())
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=pub2,
        )
        out2 = await execute_copy_setka_network(
            _FakeSession([[wt], [_Region("ur", UR), _Region("mi", MI)]])
        )

    assert out2["complete"] is True
    assert out2["posts_published"] == 1  # только MI (UR уже был)
    assert out2["targets"] == 1  # слали только недоставленному
    # Заморозка переживает завершение разлёта (21.11).
    assert isinstance(wt.hash, dict) and wt.hash.get("frozen_until", 0) > 0
    assert wt.lip == ["-167381590_555"]  # пост помечен полностью разосланным
    # UR не дёргали повторно на тике 2
    assert pub2.publish_bulletin.await_count == 1


async def test_copy_setka_spreads_regions_over_ticks(monkeypatch):
    """Веер разнесён по тикам: за тик — не больше потолка регионов.

    Без потолка один пост уходил во все активные регионы разом — 25–102
    публикации в одну минуту (замер 15.11). Хвост не теряется: его добирает
    следующий тик, и это НЕ считается неудачной попыткой.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    from modules.copy_setka_network import execute_copy_setka_network

    post = {"owner_id": -167381590, "id": 777, "date": 999_990, "text": "новость"}
    wt = _WorkTable()
    UR, MI, OR = -168170215, -158787639, -158787640

    pub = AsyncMock()
    pub.publish_bulletin.side_effect = _bulletin_side_effect(set())
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=pub,
        )
        stack.enter_context(
            patch("config.runtime.get_copy_setka_max_regions_per_tick", return_value=2)
        )
        out = await execute_copy_setka_network(
            _FakeSession([[wt], [_Region("ur", UR), _Region("mi", MI), _Region("or", OR)]])
        )

    assert out["posts_published"] == 2  # потолок, а не все три
    assert out["complete"] is False
    assert len(out["missing"]) == 1
    # Отложенное — не сбой: счётчик попыток не растёт.
    assert isinstance(wt.hash, dict)
    assert wt.hash["tries"] == 0
    assert len(wt.hash["done"]) == 2
    assert wt.lip == []  # пост ещё не помечен разосланным


async def test_copy_setka_gives_up_after_max_tries(monkeypatch):
    """Если хвост не добирается за PENDING_MAX_TRIES — пост закрывается, не застревает."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from modules.copy_setka_network import PENDING_MAX_TRIES, execute_copy_setka_network

    post = {"owner_id": -167381590, "id": 999, "date": 999_990, "text": "новость"}
    wt = _WorkTable()
    wt.hash = {"lip": "-167381590_999", "done": [], "tries": PENDING_MAX_TRIES - 1}
    UR, MI = -168170215, -158787639

    pub = AsyncMock()
    pub.publish_bulletin.side_effect = _bulletin_side_effect({UR, MI})  # обе в капче
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=pub,
        )
        out = await execute_copy_setka_network(
            _FakeSession([[wt], [_Region("ur", UR), _Region("mi", MI)]])
        )

    assert out["complete"] is False
    assert out["posts_published"] == 0
    assert wt.hash == []  # backstop сработал — pending снят
    assert wt.lip == ["-167381590_999"]  # пост закрыт, новые не блокируются


# ---------------------------------------------------------------------------
# Разлёт по всей сети + заморозка новых постов (заказ владельца 21.11)
# ---------------------------------------------------------------------------


def _scope_tree():
    """kirov_obl(mi, vp) + tatarstan_obl(elabuga) — как в проде."""
    return [
        _Region("kirov_obl", -1001, id=1, parent_region_id=None),
        _Region("mi", -1002, id=2, parent_region_id=1),
        _Region("vp", -1003, id=3, parent_region_id=1),
        _Region("tatarstan_obl", -1004, id=4, parent_region_id=None),
        _Region("elabuga", -1005, id=5, parent_region_id=4),
    ]


async def test_copy_setka_post_goes_to_whole_network_whatever_the_words(monkeypatch):
    """Развилки по областям больше нет: любое слово в тексте — вся сеть.

    Заказ владельца 21.11: «без разницы, пишут в посте казань или киров или ещё
    что-то, всё равно публикация разносится по всем входящим в сеть САРАФАН
    нашим группам». Замер за день до заказа: 22 развеера подряд, все со
    `scope='all'` — фильтр и не работал.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from modules.copy_setka_network import execute_copy_setka_network

    post = {"owner_id": -167381590, "id": 555, "date": 999_990, "text": "новость Киров"}
    publisher = AsyncMock()
    publisher.publish_bulletin.return_value = {"success": True, "url": "https://vk.com/wall-1_1"}

    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=publisher,
        )
        out = await execute_copy_setka_network(_FakeSession([[_WorkTable()], _scope_tree()]))

    assert out["success"] is True
    assert out["scope"] == "all"
    assert out["posts_published"] == 5
    sent_gids = {c.kwargs["group_id"] for c in publisher.publish_bulletin.await_args_list}
    assert sent_gids == {-1001, -1002, -1003, -1004, -1005}


async def test_copy_setka_freezes_new_posts_after_fanout_starts(monkeypatch):
    """После начала разлёта новые посты из хаба 2 часа не берём.

    Заказ владельца 21.11: «после начала развеера нового поста прекратить
    проверку новых постов на 2 часа». Разлёт текущего поста идёт на своих
    тиках, а следующий пост ждёт — иначе разлёт растягивается, а хвост
    очереди в хабе растёт.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    from modules.copy_setka_network import execute_copy_setka_network

    post1 = {"owner_id": -167381590, "id": 555, "date": 999_990, "text": "первый"}
    post2 = {"owner_id": -167381590, "id": 556, "date": 999_991, "text": "второй"}
    wt = _WorkTable()

    pub1 = AsyncMock()
    pub1.publish_bulletin.side_effect = _bulletin_side_effect(set())
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post1]},
            publisher=pub1,
        )
        out1 = await execute_copy_setka_network(_FakeSession([[wt], [_Region("ur", -168170215)]]))

    assert out1["posts_published"] == 1
    # Заморозка переживает завершение разлёта.
    assert isinstance(wt.hash, dict) and wt.hash["frozen_until"] > 0

    pub2 = AsyncMock()
    pub2.publish_bulletin.side_effect = _bulletin_side_effect(set())
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post2]},
            publisher=pub2,
        )
        out2 = await execute_copy_setka_network(_FakeSession([[wt], [_Region("ur", -168170215)]]))

    assert out2["posts_published"] == 0
    assert out2["message"] == "frozen after fan-out"
    pub2.publish_bulletin.assert_not_called()


async def test_copy_setka_repost_path_is_capped_per_account(monkeypatch):
    """Репост-путь ограничен отдельно: весь веер идёт с ОДНОГО аккаунта.

    `wall.repost` ВК принимает только с user-токена (community-токен
    отклоняет кодом 27, замер 21.05), а капча бьёт по аккаунту: 10+ репостов
    за 7 с → «Captcha needed». Поэтому профиль 8 репостов на тик держим, даже
    когда общий потолок веера шире.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    from modules.copy_setka_network import execute_copy_setka_network

    post = {
        "owner_id": -167381590,
        "id": 556,
        "date": 999_990,
        "text": "репост чего-то",
        "copy_history": [{"owner_id": -200, "id": 7}],
    }
    regions = [_Region(f"r{i}", -1000 - i, id=i) for i in range(1, 13)]

    pub = AsyncMock()
    pub.publish_repost.return_value = {"success": True, "url": "https://vk.com/wall-1_1"}
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=pub,
        )
        stack.enter_context(
            patch("config.runtime.get_copy_setka_max_regions_per_tick", return_value=40)
        )
        out = await execute_copy_setka_network(_FakeSession([[_WorkTable()], regions]))

    assert out["mode"] == "wall.repost"
    assert out["posts_published"] == 8, "профиль 8 репостов с аккаунта не тронут"
    assert len(out["missing"]) == 4


async def test_copy_setka_copy_path_is_not_capped_by_the_account_limit(monkeypatch):
    """Копи-путь идёт токеном САМОЙ группы — по аккаунту публикаций одна.

    Капча бьёт по аккаунту, а не по IP: на копи-пути каждый аккаунт публикует
    один раз, поэтому общий потолок веера применяется целиком.
    """
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    from modules.copy_setka_network import execute_copy_setka_network

    post = {"owner_id": -167381590, "id": 557, "date": 999_990, "text": "новость"}
    regions = [_Region(f"r{i}", -1000 - i, id=i) for i in range(1, 13)]

    pub = AsyncMock()
    pub.publish_bulletin.side_effect = _bulletin_side_effect(set())
    with ExitStack() as stack:
        _patch_copy_setka_deps(
            stack,
            parse_tokens={"VALSTAN": "TOK_MEMBER"},
            wall_by_token={"TOK_MEMBER": [post]},
            publisher=pub,
        )
        stack.enter_context(
            patch("config.runtime.get_copy_setka_max_regions_per_tick", return_value=40)
        )
        out = await execute_copy_setka_network(_FakeSession([[_WorkTable()], regions]))

    assert out["mode"] == "wall.post copy"
    assert out["posts_published"] == 12, "копи-путь ограничен только общим потолком"
    assert out["complete"] is True


# Развилки по областям («Киров»/«Казань») удалены 21.11 по заказу владельца:
# пост из хаба разносится по всей сети. Их тесты — впереди, в блоке
# «Разлёт по всей сети + заморозка новых постов».
