"""Нейро-фильтр ДО первой публикации (заказ владельца 2026-08-19).

Измеренная дыра: волна сама парсит посты и сама же их публикует, а
классификация шла отдельной таской раз в 3 часа по накопленному аудиту.
За 14 дней — ноль вердиктов раньше сбора и 10 743 позже, то есть первая
публикация всегда проходила без нейро-фильтра. Отсев спама при этом —
главная задача движка: за ретрансляцию чужой рекламы ВК банит админа паблика.
"""

from __future__ import annotations

import pytest

from database.models_extended import CollectedPostAudit
from modules.classifier import prepublish, service
from modules.classifier.schema import ClassifierVerdict


@pytest.fixture(autouse=True)
def _enable_gate(monkeypatch):
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_ENABLED", "1")
    # Тесты ниже проверяют, что происходит ПОСЛЕ вызова модели, а их пачки —
    # в один-два поста. С 25.10 у гейта есть порог «на пачку меньше трёх
    # модель не зовём», поэтому здесь он опущен до 1: иначе эти тесты молча
    # перестали бы проверять блокировку. Сам порог проверяют отдельные тесты
    # в конце файла — они снимают переменную и работают с дефолтом.
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", "1")
    monkeypatch.setattr(prepublish, "_lips_with_verdict", _no_known_verdicts, raising=False)


async def _no_known_verdicts(session, lips, region_code):
    return set()


def _vk_post(owner_id: int, post_id: int, text: str) -> dict:
    return {"owner_id": owner_id, "id": post_id, "text": text}


def test_gate_is_off_by_default(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_PREPUBLISH_ENABLED", raising=False)
    assert prepublish.prepublish_enabled() is False


def test_lip_matches_the_format_used_by_parser_and_audit():
    """Формат тот же, что у ``lip_of_post``: ``abs(owner_id)_id``, знак теряется.

    Своя формула здесь была бы тихой поломкой — гейт сравнивал бы ключи,
    которых нет в БД, и блокировал бы ноль постов, ничем этого не выдав.
    """
    assert prepublish.post_lip(_vk_post(-123, 456, "t")) == "123_456"
    from utils.post_utils import lip_of_post

    assert prepublish.post_lip(_vk_post(-123, 456, "t")) == lip_of_post(-123, 456)


def test_lip_of_broken_post_is_empty_not_an_exception():
    assert prepublish.post_lip({"text": "нет идентификаторов"}) == ""


def test_items_carry_the_wave_region():
    """Регион в батче — тот, от чьего лица судит модель: гео-правила
    («чужой район → delete») без него дают противоположный вердикт."""
    items = prepublish.to_classifier_items([_vk_post(-1, 2, "новость")], region_code="mi")
    assert items == [
        {
            "lip": "1_2",
            "region_code": "mi",
            "text": "новость",
            # В ссылке знак owner_id нужен, в отличие от lip.
            "url": "https://vk.com/wall-1_2",
            "media": [],
            # Метрики и дата — в промпт (решение владельца 2026-09-22): модель
            # решает срочность и важность. Прочерк-``None`` вместо нуля здесь
            # значим: у поста без счётчиков ВК ничего не прислал.
            "date": None,
            "views": None,
            "likes": None,
            "comments": None,
            "reposts": None,
        }
    ]


def test_items_carry_vk_counters_in_both_shapes():
    """ВК отдаёт счётчики то словарём, то голым числом — в промпт должны
    доехать числа, а не структура."""
    post = _vk_post(-1, 2, "новость")
    post.update({"views": {"count": 120}, "likes": 4, "date": 1716200000})

    item = prepublish.to_classifier_items([post], region_code="mi")[0]

    assert item["views"] == 120
    assert item["likes"] == 4
    assert item["date"] == 1716200000


@pytest.mark.asyncio
async def test_spam_is_blocked_before_publication(db_session, monkeypatch):
    """Главный сценарий: движок сказал delete — пост не должен уйти в печать."""
    posts = [_vk_post(-1, 1, "заработок на дому, пишите в личку"), _vk_post(-1, 2, "открыли мост")]

    monkeypatch.setattr(
        "modules.classifier.rules.render_effective_postulates",
        _fake_postulates,
    )
    monkeypatch.setattr(
        "modules.classifier.headless.classify_posts",
        lambda items, **kw: {
            "verdicts": [
                ClassifierVerdict(lip="1_1", theme="мусор", action="delete", region_code="mi"),
                ClassifierVerdict(lip="1_2", theme="новости", action="publish", region_code="mi"),
            ],
            "failures": [],
            "problems": [],
            "chunks": 1,
            "tokens": 900,
        },
    )
    for lip in ("1_1", "1_2"):
        db_session.add(
            CollectedPostAudit(
                lip=lip,
                region_code="mi",
                post_text="t",
                post_url="u",
                has_media=False,
                decision="kept",
            )
        )
    await db_session.commit()

    blocked = await prepublish.blocked_lips_before_publish(db_session, posts, region_code="mi")
    assert blocked == {"1_1"}

    # Вердикты записаны — оператор увидит их в ленте, а следующие волны
    # переиспользуют, не тратя токены заново.
    stats = await service.agree_rate_stats(db_session)
    assert stats["classified_by_engine"] == {"headless": 2}


@pytest.mark.asyncio
async def test_hold_is_blocked_too(db_session, monkeypatch):
    """`hold` — «нужен человек», а не «можно печатать»: до решения оператора
    пост в сводку не идёт (та же семантика, что у enforce)."""
    monkeypatch.setattr("modules.classifier.rules.render_effective_postulates", _fake_postulates)
    monkeypatch.setattr(
        "modules.classifier.headless.classify_posts",
        lambda items, **kw: {
            "verdicts": [
                ClassifierVerdict(lip="1_3", theme="новости", action="hold", region_code="mi")
            ],
            "failures": [],
            "problems": [],
            "chunks": 1,
            "tokens": 100,
        },
    )
    db_session.add(
        CollectedPostAudit(
            lip="1_3",
            region_code="mi",
            post_text="t",
            post_url="u",
            has_media=False,
            decision="kept",
        )
    )
    await db_session.commit()

    blocked = await prepublish.blocked_lips_before_publish(
        db_session, [_vk_post(-1, 3, "непонятно что")], region_code="mi"
    )
    assert blocked == {"1_3"}


@pytest.mark.asyncio
async def test_engine_failure_is_fail_open_and_loud(db_session, monkeypatch, caplog):
    """Отказ движка не останавливает волну — но и не молчит.

    Fail-closed остановил бы сводки всех районов на любом сбое DeepSeek, а
    инцидент 2026-08-19 показал, что сбой может длиться сутками. Цена
    МОЛЧАЛИВОГО fail-open — те же трое суток спама, поэтому здесь ERROR.
    """
    import logging

    monkeypatch.setattr("modules.classifier.rules.render_effective_postulates", _fake_postulates)
    monkeypatch.setattr(
        "modules.classifier.headless.classify_posts",
        lambda items, **kw: {
            "verdicts": [],
            "failures": ["no_api_key"],
            "problems": [],
            "chunks": 1,
            "tokens": 0,
        },
    )
    with caplog.at_level(logging.ERROR, logger="modules.classifier.prepublish"):
        blocked = await prepublish.blocked_lips_before_publish(
            db_session, [_vk_post(-1, 4, "текст")], region_code="mi"
        )
    assert blocked == set()
    assert "no_api_key" in caplog.text


@pytest.mark.asyncio
async def test_crash_inside_gate_does_not_break_the_wave(db_session, monkeypatch):
    """Классификатор — усилитель, не единая точка отказа."""
    monkeypatch.setattr("modules.classifier.rules.render_effective_postulates", _boom)
    blocked = await prepublish.blocked_lips_before_publish(
        db_session, [_vk_post(-1, 5, "текст")], region_code="mi"
    )
    assert blocked == set()


@pytest.mark.asyncio
async def test_disabled_gate_costs_nothing(db_session, monkeypatch):
    """Выключенный гейт не должен даже смотреть в сторону модели."""
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_ENABLED", "0")
    monkeypatch.setattr("modules.classifier.headless.classify_posts", _boom_sync)
    blocked = await prepublish.blocked_lips_before_publish(
        db_session, [_vk_post(-1, 6, "текст")], region_code="mi"
    )
    assert blocked == set()


async def _fake_postulates(session):
    return "постулаты"


async def _boom(session):
    raise RuntimeError("рухнуло")


def _boom_sync(*a, **k):  # pragma: no cover — не должен вызываться
    raise AssertionError("модель не должна вызываться при выключенном гейте")


@pytest.mark.asyncio
async def test_gate_reports_the_all_known_outcome(db_session, monkeypatch, caplog):
    """«Всё уже размечено» — законный исход, но он обязан быть слышен.

    Без строки в логе этот путь неотличим от «гейт не отработал вовсе», а
    именно такую немоту мы чинили в самом классификаторе в тот же день.
    """
    import logging

    async def _all_known(session, lips, region_code):
        return set(lips)

    monkeypatch.setattr(prepublish, "_lips_with_verdict", _all_known)
    monkeypatch.setattr("modules.classifier.headless.classify_posts", _boom_sync)

    with caplog.at_level(logging.INFO, logger="modules.classifier.prepublish"):
        blocked = await prepublish.blocked_lips_before_publish(
            db_session, [_vk_post(-1, 7, "текст")], region_code="mi"
        )
    assert blocked == set()
    assert "новых=0" in caplog.text


@pytest.mark.asyncio
async def test_all_candidates_without_text_is_not_a_warning(db_session, monkeypatch, caplog):
    """Пост без текста движок пропускает намеренно — это норма, не тревога.

    Предупреждение, срабатывающее на штатном исходе, обучает не смотреть на
    предупреждения. Ровно так сегодня уже потеряли трое суток.
    """
    import logging

    monkeypatch.setattr("modules.classifier.rules.render_effective_postulates", _fake_postulates)
    monkeypatch.setattr(
        "modules.classifier.headless.classify_posts",
        lambda items, **kw: {
            "verdicts": [],
            "failures": [],
            "problems": [],
            "chunks": 0,
            "tokens": 0,
            "skipped_no_text": len(items),
        },
    )
    with caplog.at_level(logging.INFO, logger="modules.classifier.prepublish"):
        await prepublish.blocked_lips_before_publish(
            db_session, [_vk_post(-1, 8, "")], region_code="mi"
        )
    assert "все без текста" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_silent_engine_with_text_still_warns(db_session, monkeypatch, caplog):
    """А вот молчание движка на постах С текстом — по-прежнему тревога."""
    import logging

    monkeypatch.setattr("modules.classifier.rules.render_effective_postulates", _fake_postulates)
    monkeypatch.setattr(
        "modules.classifier.headless.classify_posts",
        lambda items, **kw: {
            "verdicts": [],
            "failures": [],
            "problems": [],
            "chunks": 1,
            "tokens": 0,
            "skipped_no_text": 0,
        },
    )
    with caplog.at_level(logging.INFO, logger="modules.classifier.prepublish"):
        await prepublish.blocked_lips_before_publish(
            db_session, [_vk_post(-1, 9, "настоящий текст")], region_code="mi"
        )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "движок молчит" in caplog.text


# --- порог «звать модель только на пачку» (25.10) --------------------------- #


def _count_calls(monkeypatch):
    """Подменить классификатор счётчиком: важно ЧИСЛО вызовов, не вердикты."""
    calls = []

    def fake(items, **kw):
        calls.append(list(items))
        return {"verdicts": [], "failures": [], "problems": [], "chunks": 1, "tokens": 10}

    monkeypatch.setattr("modules.classifier.headless.classify_posts", fake)
    monkeypatch.setattr("modules.classifier.rules.render_effective_postulates", _fake_postulates)
    return calls


@pytest.mark.asyncio
async def test_small_wave_does_not_call_the_model(db_session, monkeypatch, caplog):
    """Два кандидата при дефолтном пороге 3 — вызова нет.

    Замер 25.10: 946 вызовов из 1319 были ради ОДНОГО кандидата, и каждый
    оплачивал постулаты целиком. В выпуск при этом попадает не больше трёх
    постов, так что вызов на пачку из одного — деньги за воздух.
    """
    import logging

    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", "3")
    calls = _count_calls(monkeypatch)

    with caplog.at_level(logging.INFO, logger="modules.classifier.prepublish"):
        blocked = await prepublish.blocked_lips_before_publish(
            db_session,
            [_vk_post(-1, 11, "раз"), _vk_post(-1, 12, "два")],
            region_code="mi",
        )

    assert blocked == set(), "fail-open: без вердикта волна идёт по фильтрам"
    assert calls == [], "модель не должна была вызываться"
    assert "меньше" in caplog.text and "порога 3" in caplog.text


@pytest.mark.asyncio
async def test_threshold_is_about_fresh_candidates_only(db_session, monkeypatch):
    """Порог считается по НОВЫМ постам: уже размеченные не должны его добивать."""
    monkeypatch.delenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", raising=False)

    async def _known_first(session, lips, region_code):
        return {"1_21"}

    monkeypatch.setattr(prepublish, "_lips_with_verdict", _known_first)
    calls = _count_calls(monkeypatch)

    blocked = await prepublish.blocked_lips_before_publish(
        db_session,
        [_vk_post(-1, 21, "уже размечен"), _vk_post(-1, 22, "новый")],
        region_code="mi",
    )

    assert blocked == set()
    assert calls == [], "свежих один — ниже порога, хотя кандидатов два"


@pytest.mark.asyncio
async def test_wave_at_threshold_calls_the_model(db_session, monkeypatch):
    """Ровно столько свежих кандидатов, сколько в пороге, — вызов есть.

    Порог задаётся здесь явно, а не берётся дефолтом: тест про механику
    «кандидатов >= порога → зовём», и смена продуктового дефолта не должна
    его переписывать.
    """
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", "3")
    calls = _count_calls(monkeypatch)

    await prepublish.blocked_lips_before_publish(
        db_session,
        [_vk_post(-1, 31, "а"), _vk_post(-1, 32, "б"), _vk_post(-1, 33, "в")],
        region_code="mi",
    )

    assert len(calls) == 1
    assert len(calls[0]) == 3


def test_min_candidates_env_bounds(monkeypatch):
    from config.classifier import get_prepublish_min_candidates

    monkeypatch.delenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", raising=False)
    assert get_prepublish_min_candidates() == 15
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", "0")
    assert get_prepublish_min_candidates() == 1, "0 вернул бы прежнее поведение молча"
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", "999")
    assert get_prepublish_min_candidates() == 40
    monkeypatch.setenv("CLASSIFIER_PREPUBLISH_MIN_CANDIDATES", "не число")
    assert get_prepublish_min_candidates() == 15
