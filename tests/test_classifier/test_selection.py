"""Отбор по меткам + политика деградации (звено 5, D-024).

Здесь решается, что уйдёт в ленты 26 районов, поэтому тесты стерегут именно
политику владельца дословно: пропустить ОДНУ волну, на второй опубликовать
алгоритмами, пропущенную НЕ досылать, и в обоих случаях сообщить.
"""

from __future__ import annotations

from modules import telegram_http
from modules.classifier import selection


class FakeRedis:
    """Минимальный Redis: get/setex/delete. Ошибки — отдельными подклассами."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class BrokenRedis(FakeRedis):
    def get(self, key):
        raise RuntimeError("redis down")


def _decide(publish_lips, redis_client, *, region="mi", theme="novost"):
    return selection.decide_mode(
        publish_lips=publish_lips,
        region_code=region,
        theme=theme,
        redis_client=redis_client,
    )


# ───────── норма ─────────


def test_verdicts_present_means_selection_by_labels():
    mode, alert = _decide({"1_1"}, FakeRedis())
    assert mode == selection.MODE_VERDICTS
    assert alert is False


def test_successful_wave_clears_the_skip_mark():
    """Один сбой не должен вести к алгоритмам через неделю: успешная волна
    сбрасывает счётчик."""
    r = FakeRedis()
    _decide(set(), r)  # пропуск, отметка поставлена
    assert r.store
    _decide({"1_1"}, r)  # успех
    assert r.store == {}


# ───────── политика деградации ─────────


def test_first_silent_wave_is_skipped_not_published():
    mode, alert = _decide(set(), FakeRedis())
    assert mode == selection.MODE_SKIP_WAVE
    assert alert is True


def test_second_silent_wave_falls_back_to_algorithms():
    r = FakeRedis()
    assert _decide(set(), r)[0] == selection.MODE_SKIP_WAVE
    assert _decide(set(), r)[0] == selection.MODE_FALLBACK


def test_policy_repeats_and_does_not_stick_in_fallback():
    """После отката отметка снимается: следующий отказ снова начинается с
    пропуска волны, а не остаётся в режиме алгоритмов навсегда."""
    r = FakeRedis()
    _decide(set(), r)
    _decide(set(), r)  # fallback
    assert _decide(set(), r)[0] == selection.MODE_SKIP_WAVE


def test_waves_are_counted_per_theme():
    """У novost и afisha разные календари; общий счётчик пропускал бы
    публикации там, где всё в порядке."""
    r = FakeRedis()
    assert _decide(set(), r, theme="novost")[0] == selection.MODE_SKIP_WAVE
    assert _decide(set(), r, theme="afisha")[0] == selection.MODE_SKIP_WAVE


def test_waves_are_counted_per_region():
    r = FakeRedis()
    assert _decide(set(), r, region="mi")[0] == selection.MODE_SKIP_WAVE
    assert _decide(set(), r, region="ur")[0] == selection.MODE_SKIP_WAVE


def test_no_redis_skips_the_wave_never_falls_back_silently():
    """Без Redis отметку хранить негде. Молча уйти в алгоритмы значит выпустить
    в ленту то, за что банят аккаунт."""
    mode, alert = _decide(set(), None)
    assert mode == selection.MODE_SKIP_WAVE
    assert alert is True


def test_broken_redis_degrades_to_skip_not_to_fallback():
    mode, _ = _decide(set(), BrokenRedis())
    assert mode == selection.MODE_SKIP_WAVE


# ───────── алёрт ─────────


def test_alert_text_is_system_wide_and_says_what_to_check():
    text = selection.format_alert(mode=selection.MODE_SKIP_WAVE, region_code="mi", theme="novost")
    assert "DeepSeek" in text
    assert "не публикуются" in text
    # Проблема не в этом регионе — регион и тема в телефон не едут.
    assert "mi" not in text and "novost" not in text


def test_fallback_alert_warns_about_spam():
    text = selection.format_alert(mode=selection.MODE_FALLBACK, region_code="mi", theme="novost")
    assert "алгоритм" in text.lower()
    assert "спам" in text.lower()
    assert "НЕ досылаются" in text


def test_alert_needs_telegram_config():
    status = selection.maybe_alert(
        mode=selection.MODE_SKIP_WAVE,
        region_code="mi",
        theme="novost",
        telegram_token="",
        chat_id="",
    )
    assert status == "skipped:no-telegram-config"


def test_alert_cooldown_is_system_wide_not_per_region(monkeypatch):
    """Фильтр встаёт сразу для всей сети; письмо от каждого региона —
    способ научить владельца не читать эти алёрты (заказ 2026-09-28:
    напоминание раз в час, общесистемно)."""
    r = FakeRedis()
    sent = []

    class Resp:
        status_code = 200

    def fake_post(*_a, **kw):
        sent.append(kw)
        return Resp()

    # Патчим шов, через который алёрт реально уходит, а не sys.modules["requests"]:
    # подменённый пакет ломал импорт telegram_http, если тот грузился впервые
    # внутри этого теста (одиночный прогон), и тест зависел от порядка.
    monkeypatch.setattr(telegram_http, "post", fake_post)

    first = selection.maybe_alert(
        mode=selection.MODE_SKIP_WAVE,
        region_code="mi",
        theme="novost",
        telegram_token="t",
        chat_id="42",
        redis_client=r,
    )
    second = selection.maybe_alert(
        mode=selection.MODE_SKIP_WAVE,
        region_code="mi",
        theme="afisha",
        telegram_token="t",
        chat_id="42",
        redis_client=r,
    )
    third = selection.maybe_alert(
        mode=selection.MODE_FALLBACK,
        region_code="ur",
        theme="novost",
        telegram_token="t",
        chat_id="42",
        redis_client=r,
    )
    assert first == "alert-sent"
    assert second == "skipped:cooldown"
    assert third == "skipped:cooldown"
    assert len(sent) == 1


# ───────── apply_wave_selection — врезка в волну (шаг 2) ─────────


import pytest  # noqa: E402


def _post(owner, pid):
    return {"owner_id": owner, "id": pid, "text": f"post {pid}"}


@pytest.mark.asyncio
async def test_gate_off_leaves_wave_untouched(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_SELECTION_ENABLED", raising=False)
    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert out == posts
    assert mode == selection.MODE_DISABLED
    assert removed == 0


@pytest.mark.asyncio
async def test_verdicts_mode_publishes_unjudged_posts_by_fail_open(monkeypatch):
    """Решение владельца 2026-11-02 (п.3): «нет вердикта» ≠ «не публиковать».

    Раньше отбор брал ТОЛЬКО publish, и пост без вердикта молча выпадал из ленты.
    Теперь гейт вычитающий: блокирует осуждённое, остальное идёт по обычным
    фильтрам — иначе лента голодает там, где движок просто не успел.
    """
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def fake_actions(session, lips, region_code):
        # Движок судил только первый пост; остальные — без вердикта.
        return {"100_1": "publish"}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))
    posts = [_post(-100, 1), _post(-100, 2), _post(-100, 3)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert [p["id"] for p in out] == [1, 2, 3]
    assert mode == selection.MODE_VERDICTS
    assert removed == 0


@pytest.mark.asyncio
async def test_verdicts_mode_blocks_only_judged_posts(monkeypatch):
    """Осуждённое движком не публикуется даже при fail-open."""
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def fake_actions(session, lips, region_code):
        return {"100_1": "publish", "100_2": "delete"}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))
    posts = [_post(-100, 1), _post(-100, 2), _post(-100, 3)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert [p["id"] for p in out] == [1, 3]
    assert mode == selection.MODE_VERDICTS
    assert removed == 1


@pytest.mark.asyncio
async def test_verdict_read_failure_keeps_publish_only(monkeypatch):
    """Прочитать вердикты не удалось — непроверенное не выпускаем.

    Это чтение, а не отказ движка: права на fail-open у него нет. Отличать
    «не знаю, кого осудил» от «никого не осудил» обязательно — иначе сбой БД
    выкатил бы в ленту всё подряд.
    """
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def broken_actions(session, lips, region_code):
        return None

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", broken_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))
    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert [p["id"] for p in out] == [1]
    assert mode == selection.MODE_VERDICTS
    assert removed == 1


@pytest.mark.asyncio
async def test_fail_open_switch_returns_previous_behaviour(monkeypatch):
    """Аварийный тормоз: CLASSIFIER_SELECTION_FAIL_OPEN=0 — как было до 02.11."""
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")
    monkeypatch.setenv("CLASSIFIER_SELECTION_FAIL_OPEN", "0")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def fake_actions(session, lips, region_code):
        return {"100_1": "publish"}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))
    posts = [_post(-100, 1), _post(-100, 2)]
    out, _mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert [p["id"] for p in out] == [1]
    assert removed == 1


@pytest.mark.asyncio
async def test_skip_wave_returns_empty_and_alerts(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {}

    alerts = []
    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_SKIP_WAVE, True))
    monkeypatch.setattr(selection, "maybe_alert", lambda **kw: alerts.append(kw) or "alert-sent")
    posts = [_post(-100, 1)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert out == []
    assert mode == selection.MODE_SKIP_WAVE
    assert removed == 1
    assert alerts and alerts[0]["region_code"] == "mi"


# ───────── триггер деградации: волна, а не регион (замер 27.10) ─────────
#
# Пока движок жив, разницы не видно: свежие посты судит `prepublish` той же
# волны. Когда квота мертва, в окне остаются только вчерашние вердикты — регион
# «размечен», кандидаты нет — и волна молча публиковала ноль, не доходя до
# политики деградации. Замер 27.10: 8818 кандидатов → 98 постов.


@pytest.mark.asyncio
async def test_unjudged_wave_triggers_degradation_despite_region_verdicts(monkeypatch):
    """Вчерашний вердикт по ЧУЖОМУ посту не делает кандидатов волны размеченными."""
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        # У района есть publish-вердикт — но по посту, которого в волне нет.
        return {"999_9": "новости"}

    async def fake_judged(session, lips, region_code):
        # Наших кандидатов движок не судил вовсе (квота мертва).
        return set()

    seen = {}

    def fake_decide(**kw):
        seen["publish_lips"] = kw["publish_lips"]
        return selection.MODE_FALLBACK, False

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_judged_lips", fake_judged)
    monkeypatch.setattr(selection, "decide_mode", fake_decide)
    monkeypatch.setattr(selection, "maybe_alert", lambda **kw: "alert-sent")

    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )

    assert seen["publish_lips"] == set(), "в политику уходит пустой набор — «не судил»"
    assert out == posts, "вторая волна публикуется алгоритмами"
    assert mode == selection.MODE_FALLBACK
    assert removed == 0


@pytest.mark.asyncio
async def test_judged_wave_keeps_label_selection(monkeypatch):
    """Если движок судил кандидатов — работает обычный отбор по меткам."""
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def fake_judged(session, lips, region_code):
        # Вердикт есть хотя бы у одного кандидата — значит движок волну видел.
        return {"100_2"}

    async def fake_actions(session, lips, region_code):
        # Движок осудил второго кандидата — тот в ленту не идёт.
        return {"100_1": "publish", "100_2": "delete"}

    seen = {}

    def fake_decide(**kw):
        seen["publish_lips"] = kw["publish_lips"]
        return selection.MODE_VERDICTS, False

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_judged_lips", fake_judged)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", fake_decide)

    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, _removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )

    assert seen["publish_lips"] == {"100_1"}, "регионные вердикты не подменяются"
    assert [p["id"] for p in out] == [1]
    assert mode == selection.MODE_VERDICTS


@pytest.mark.asyncio
async def test_unknown_judgement_is_not_treated_as_silence(monkeypatch):
    """Сбой проверки (None) — не повод публиковать непроверенное алгоритмами."""
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def broken_judged(session, lips, region_code):
        return None  # «проверить не удалось»

    async def broken_actions(session, lips, region_code):
        return None  # действия тоже прочитать не удалось

    seen = {}

    def fake_decide(**kw):
        seen["publish_lips"] = kw["publish_lips"]
        return selection.MODE_VERDICTS, False

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_judged_lips", broken_judged)
    monkeypatch.setattr(selection, "fetch_wave_actions", broken_actions)
    monkeypatch.setattr(selection, "decide_mode", fake_decide)

    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, _removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )

    assert seen["publish_lips"] == {"100_1"}
    assert [p["id"] for p in out] == [1]
    assert mode == selection.MODE_VERDICTS


@pytest.mark.asyncio
async def test_textless_wave_is_not_engine_silence(monkeypatch):
    """Волна из безтекстовых постов — не деградация движка.

    headless такие посты пропускает намеренно (`has_text`), вердикта у них не
    будет никогда. Считать это «движок молчит» значило бы включать алгоритмы на
    волне, где судить было нечего.
    """
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")
    calls = []

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def fake_judged(session, lips, region_code):
        calls.append(lips)
        return set()

    async def fake_actions(session, lips, region_code):
        return {"100_1": "publish"}

    seen = {}

    def fake_decide(**kw):
        seen["publish_lips"] = kw["publish_lips"]
        return selection.MODE_VERDICTS, False

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_judged_lips", fake_judged)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", fake_decide)

    posts = [{"owner_id": -100, "id": 1, "text": "  "}]
    out, mode, _removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )

    assert calls == [], "спрашивать про безтекстовые незачем"
    assert seen["publish_lips"] == {"100_1"}, "ведём себя как раньше"
    assert out == posts
    assert mode == selection.MODE_VERDICTS


@pytest.mark.asyncio
async def test_fetch_wave_judged_lips_reads_any_verdict_but_only_this_region(db_session):
    """«Судил» — это любой вердикт, включая delete; чужой район не в счёт."""
    from database.models_extended import ContentClassification

    db_session.add_all(
        [
            ContentClassification(
                lip="1_1",
                region_code="mi",
                model="t",
                verdict={"action": "delete", "theme": "мусор"},
            ),
            ContentClassification(
                lip="1_2",
                region_code="vp",  # тот же пост, но вердикт соседа
                model="t",
                verdict={"action": "publish", "theme": "новости"},
            ),
        ]
    )
    await db_session.commit()

    got = await selection.fetch_wave_judged_lips(db_session, {"1_1", "1_2", "1_3"}, "mi")

    assert got == {"1_1"}


@pytest.mark.asyncio
async def test_fetch_wave_judged_lips_is_unknown_on_failure(caplog):
    """Сбой чтения — None («неизвестно»), а не пустое множество («не судил»).

    Разница решает, поедет ли в ленту непроверенное: пустое множество включило
    бы алгоритмы, а сбой БД этого права давать не должен.
    """

    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError("БД недоступна")

    with caplog.at_level("WARNING"):
        got = await selection.fetch_wave_judged_lips(_Broken(), {"1_1"}, "mi")

    assert got is None
    assert any("вердиктов волны" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_fetch_wave_actions_reads_actions_and_operator_correction(db_session):
    """Действия читаются по кандидатам волны; правка оператора главнее ИИ."""
    from sqlalchemy import select

    from database.models_extended import ClassificationCorrection, ContentClassification

    db_session.add_all(
        [
            ContentClassification(
                lip="1_1",
                region_code="mi",
                model="t",
                verdict={"action": "delete", "theme": "мусор"},
            ),
            ContentClassification(
                lip="1_2",
                region_code="mi",
                model="t",
                verdict={"action": "publish", "theme": "новости"},
            ),
            ContentClassification(
                lip="1_3",
                region_code="vp",  # соседний район — не в счёт
                model="t",
                verdict={"action": "delete", "theme": "мусор"},
            ),
        ]
    )
    await db_session.commit()

    # Оператор перевёл первый пост в publish — его решение главнее.
    row = (
        await db_session.execute(
            select(ContentClassification.id).where(ContentClassification.lip == "1_1")
        )
    ).scalar_one()
    db_session.add(
        ClassificationCorrection(
            classification_id=row,
            lip="1_1",
            verdict_type="action",
            outcome="correct",
            operator_value="publish",
        )
    )
    await db_session.commit()

    got = await selection.fetch_wave_actions(db_session, {"1_1", "1_2", "1_3"}, "mi")

    assert got == {"1_1": "publish", "1_2": "publish"}


@pytest.mark.asyncio
async def test_fetch_wave_actions_is_unknown_on_failure(caplog):
    """Сбой чтения — None («неизвестно»), а не пустой словарь («никто не осуждён»).

    Пустой словарь включил бы fail-open на посты без вердикта, а сбой БД
    этого права не даёт.
    """

    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError("БД недоступна")

    with caplog.at_level("WARNING"):
        got = await selection.fetch_wave_actions(_Broken(), {"1_1"}, "mi")

    assert got is None
    assert any("действия вердиктов волны" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_fallback_mode_publishes_by_algorithms(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_FALLBACK, True))
    monkeypatch.setattr(selection, "maybe_alert", lambda **kw: "alert-sent")
    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert out == posts
    assert mode == selection.MODE_FALLBACK
    assert removed == 0


@pytest.mark.asyncio
async def test_internal_crash_is_fail_open_and_loud(monkeypatch, caplog):
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def broken_fetch(session, region_code):
        raise RuntimeError("db down")

    monkeypatch.setattr(selection, "fetch_publish_map", broken_fetch)
    posts = [_post(-100, 1)]
    with caplog.at_level("ERROR", logger="modules.classifier.selection"):
        out, mode, removed = await selection.apply_wave_selection(
            None, posts, region_code="mi", theme="novost"
        )
    assert out == posts
    assert mode == "error"
    assert removed == 0
    assert any("fail-open" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_the_wave(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    def broken_alert(**kw):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, True))
    monkeypatch.setattr(selection, "maybe_alert", broken_alert)
    posts = [_post(-100, 1), _post(-100, 2)]
    out, mode, removed = await selection.apply_wave_selection(
        None, posts, region_code="mi", theme="novost"
    )
    assert [p["id"] for p in out] == [1]
    assert mode == selection.MODE_VERDICTS


# ───────── «безтекста» в строке лога — атрибуция пустой волны ─────────
#
# `отобрано=0` само по себе неоднозначно: то ли нейросеть отбраковала всё, то ли
# ей нечего было судить. После решения владельца 2026-11-02 (п.3) пост без
# вердикта ПУБЛИКУЕТСЯ по обычным фильтрам, поэтому убрать его может только
# осуждение. Счётчик `безтекста` показывает, сколько убранных постов движок
# судить не мог — текста нет (`has_text`); `безвердикта` — сколько убрано без
# вердикта вовсе (бывает только при сбое чтения вердиктов).


def _post_no_text(owner, pid):
    return {"owner_id": owner, "id": pid, "text": "   "}


@pytest.mark.asyncio
async def test_log_separates_no_text_from_rejected(monkeypatch, caplog):
    """Безтекстовый без вердикта идёт в ленту; убран — осуждённый."""
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_1": "новости"}

    async def fake_actions(session, lips, region_code):
        return {"100_1": "publish", "100_2": "delete"}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))

    # 1 — publish, 2 — осуждён движком, 3 и 4 — без текста и без вердикта.
    posts = [_post(-100, 1), _post(-100, 2), _post_no_text(-100, 3), _post_no_text(-100, 4)]
    with caplog.at_level("INFO", logger="modules.classifier.selection"):
        out, mode, removed = await selection.apply_wave_selection(
            None, posts, region_code="mi", theme="novost"
        )

    assert [p["id"] for p in out] == [1, 3, 4]
    assert removed == 1
    line = next(r.getMessage() for r in caplog.records if "кандидатов=" in r.getMessage())
    assert "убрано=1" in line
    assert "безтекста=0" in line
    assert "безвердикта=0" in line


@pytest.mark.asyncio
async def test_no_text_counts_only_the_removed(monkeypatch, caplog):
    """Бестекстовый с publish-вердиктом прошёл — в «безтекста» он не попадает.

    Иначе счётчик мерил бы состав волны, а не причину отсева, и в пустых волнах
    выглядел бы одинаково независимо от того, что произошло.
    """
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {"100_3": "новости"}

    async def fake_actions(session, lips, region_code):
        return {"100_3": "publish", "100_1": "delete"}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))

    posts = [_post(-100, 1), _post_no_text(-100, 3)]
    with caplog.at_level("INFO", logger="modules.classifier.selection"):
        out, _mode, removed = await selection.apply_wave_selection(
            None, posts, region_code="mi", theme="novost"
        )

    assert [p["id"] for p in out] == [3]
    assert removed == 1
    line = next(r.getMessage() for r in caplog.records if "кандидатов=" in r.getMessage())
    assert "безтекста=0" in line


@pytest.mark.asyncio
async def test_no_text_post_judged_delete_is_counted_as_no_text(monkeypatch, caplog):
    """Безтекстовый, осуждённый облачной рутиной, убран — и виден в счётчике.

    Текста нет — движок судить не мог, вердикт пришёл от рутины, которая
    смотрела вложения. Убран он по осуждению, и «безтекста=1» это показывает.
    """
    monkeypatch.setenv("CLASSIFIER_SELECTION_ENABLED", "1")

    async def fake_fetch(session, region_code):
        return {}

    async def fake_actions(session, lips, region_code):
        return {"100_3": "delete"}

    monkeypatch.setattr(selection, "fetch_publish_map", fake_fetch)
    monkeypatch.setattr(selection, "fetch_wave_actions", fake_actions)
    monkeypatch.setattr(selection, "decide_mode", lambda **kw: (selection.MODE_VERDICTS, False))

    posts = [_post_no_text(-100, 3)]
    with caplog.at_level("INFO", logger="modules.classifier.selection"):
        out, _mode, removed = await selection.apply_wave_selection(
            None, posts, region_code="mi", theme="novost"
        )

    assert out == []
    assert removed == 1
    line = next(r.getMessage() for r in caplog.records if "кандидатов=" in r.getMessage())
    assert "безтекста=1" in line
    assert "безвердикта=0" in line


@pytest.mark.asyncio
async def test_fetch_urgent_lips_reads_only_this_region_and_only_true(db_session):
    """Срочность читается по флагу вердикта и только в своём районе.

    Регион здесь не формальность: `lip` уникален глобально, и вердикт соседа
    про его аварию не должен двигать ленту нашего района.
    """
    from database.models_extended import ContentClassification
    from modules.classifier.selection import fetch_urgent_lips

    db_session.add_all(
        [
            ContentClassification(
                lip="1_1",
                region_code="mi",
                model="t",
                verdict={"action": "publish", "theme": "происшествия", "urgent": True},
            ),
            ContentClassification(
                lip="1_2",
                region_code="mi",
                model="t",
                verdict={"action": "publish", "theme": "новости", "urgent": False},
            ),
            # Старый вердикт — поля нет вовсе.
            ContentClassification(
                lip="1_3",
                region_code="mi",
                model="t",
                verdict={"action": "publish", "theme": "новости"},
            ),
            # Срочное, но у соседа.
            ContentClassification(
                lip="1_4",
                region_code="vp",
                model="t",
                verdict={"action": "publish", "theme": "происшествия", "urgent": True},
            ),
        ]
    )
    await db_session.commit()

    assert await fetch_urgent_lips(db_session, "mi") == {"1_1"}


@pytest.mark.asyncio
async def test_fetch_urgent_lips_is_fail_open(caplog):
    """Сломанная БД не имеет права ронять волну: срочность — ускоритель.

    Пустое множество означает «порядок публикации прежний, по рейтингу», а не
    «публиковать нечего».
    """
    from modules.classifier.selection import fetch_urgent_lips

    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError("БД недоступна")

    with caplog.at_level("WARNING"):
        out = await fetch_urgent_lips(_Broken(), "mi")

    assert out == set()
    assert any("срочност" in r.getMessage().lower() for r in caplog.records)
