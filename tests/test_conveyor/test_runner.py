"""Тесты прогона конвейера — склейка трёх звеньев.

Ни одного живого вызова: и классификация, и доставка подменяются. Проверяем
маршрутизацию исходов по журналу — потому что журнал и есть ответ на вопрос
«почему этой новости нет на сайте», и ошибка в нём делает конвейер немым.
"""

from __future__ import annotations

import pytest

from modules.conveyor import runner, source
from tests.test_conveyor.conftest import SITE, seed_pair

LONG_TEXT = (
    "В Малмыже отремонтировали участок дороги по улице Ленина. Работы шли две недели, "
    "подрядчик уложил новое покрытие и обновил разметку у школы номер один."
)

# Второй пост там, где тест про ДВА независимых поста. Раньше оба сеялись одним
# и тем же ``LONG_TEXT``, и это перестало быть безобидным, когда отбор научился
# снимать дубли до LLM: партия из двух копий одной новости схлопывалась в одну —
# ровно так, как и задумано. Текст обязан быть про другое событие, иначе тест
# проверяет не то, что написано в его имени.
OTHER_TEXT = (
    "Библиотека Малмыжа объявила запись в кружок краеведения. Занятия начнутся "
    "в октябре, ведёт их сотрудник музея, записаться можно по телефону."
)


def _accept(**over):
    v = {"action": "accept", "section": "novosti", "title": "Заголовок", "text": LONG_TEXT}
    v.update(over)
    return {"ok": True, "verdict": {**v, "section_known": True}, "usage": {"total_tokens": 100}}


@pytest.fixture
def wired(monkeypatch):
    """Подменяет обе внешние границы: LLM и HTTP-доставку."""

    calls = {"classify": [], "deliver": []}

    def fake_classify(post, *, sections, rules="", api_key=None):
        calls["classify"].append(post["lip"])
        calls["rules"] = rules
        return calls.get("classify_result", _accept())

    def fake_deliver(site, key, body, **kw):
        calls["deliver"].append(body["vkPostId"])
        return calls.get(
            "deliver_result", {"ok": True, "status": 201, "attempts": 1, "remote_id": "r1"}
        )

    monkeypatch.setattr(runner.classify_mod, "classify", fake_classify)
    monkeypatch.setattr(runner.delivery_mod, "deliver", fake_deliver)
    return calls


async def _journal(db_session, lip):
    from sqlalchemy import select

    from database.models_extended import ConveyorDelivery

    return (
        await db_session.execute(select(ConveyorDelivery).where(ConveyorDelivery.lip == lip))
    ).scalar_one()


@pytest.mark.asyncio
async def test_happy_path_marks_delivered(db_session, wired, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE, sleep=None)
    assert stats["selected"] == 1 and stats["delivered"] == 1
    row = await _journal(db_session, "1_10")
    assert row.status == "delivered" and row.remote_id == "r1" and row.http_status == 201
    assert row.verdict["title"] == "Заголовок"


@pytest.mark.asyncio
async def test_rules_reach_the_model(db_session, wired, monkeypatch):
    """Правила сайта доезжают до классификации — иначе файл правил декоративен."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await runner.run_site(db_session, SITE, rules="правило сайта", sleep=None)
    assert wired["rules"] == "правило сайта"


@pytest.mark.asyncio
async def test_dry_run_touches_nothing(db_session, wired):
    """Сухой прогон показывает, что улетит, до того как улетит."""
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE, dry_run=True)
    assert stats["selected"] == 1 and stats["delivered"] == 0
    assert stats["preview"][0]["lip"] == "1_10"
    assert wired["classify"] == [] and wired["deliver"] == []
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {}


@pytest.mark.asyncio
async def test_llm_reject_closes_post(db_session, wired, monkeypatch):
    """Отказ модели — решение: пост закрывается и в следующий прогон не вернётся."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["classify_result"] = {"ok": False, "reason": "llm_reject:реклама"}
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["rejected"] == 1 and wired["deliver"] == []
    row = await _journal(db_session, "1_10")
    assert row.status == "rejected" and "реклама" in row.reason
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_llm_network_failure_is_failed_not_rejected(db_session, wired, monkeypatch):
    """Сбой связи — не решение модели, и путать их нельзя: разные статусы разбора."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["classify_result"] = {"ok": False, "reason": "network"}
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["failed"] == 1
    row = await _journal(db_session, "1_10")
    assert row.status == "failed" and row.reason == "network"


@pytest.mark.asyncio
async def test_classify_failure_bumps_attempts_for_backoff(db_session, wired, monkeypatch):
    """Несбывшаяся классификация считает попытки: без счётчика пауза не на чем держать."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["classify_result"] = {"ok": False, "reason": "http_402"}
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await runner.run_site(db_session, SITE)
    row = await _journal(db_session, "1_10")
    assert row.status == "failed" and row.attempts == 1 and row.verdict is None


@pytest.mark.asyncio
async def test_invariant_holds_garbage_before_delivery(db_session, wired, monkeypatch):
    """Мусор задерживается ДО отправки и остаётся видимым человеку."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["classify_result"] = _accept(text="Здравствуйте, {author_name}! " + LONG_TEXT)
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["held"] == 1 and wired["deliver"] == []
    row = await _journal(db_session, "1_10")
    assert row.status == "held" and row.reason == "unresolved_placeholder"
    assert row.verdict is not None  # вердикт сохранён — есть что показать человеку


@pytest.mark.asyncio
async def test_delivery_failure_is_recorded_with_attempts(db_session, wired, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["deliver_result"] = {"ok": False, "status": 503, "attempts": 3, "reason": "http_503"}
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["failed"] == 1
    row = await _journal(db_session, "1_10")
    assert row.status == "failed" and row.attempts == 3 and row.http_status == 503


@pytest.mark.asyncio
async def test_one_bad_post_does_not_stop_the_run(db_session, wired, monkeypatch):
    """Падение на одном посте не рушит прогон — остальные доезжают."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")

    def boom(post, *, sections, rules="", api_key=None):
        if post["lip"] == "1_10":
            raise RuntimeError("модель икнула")
        return _accept()

    monkeypatch.setattr(runner.classify_mod, "classify", boom)
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="1_20", text=OTHER_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["selected"] == 2 and stats["delivered"] == 1 and stats["failed"] == 1
    assert (await _journal(db_session, "1_10")).reason == "classify_crashed"


@pytest.mark.asyncio
async def test_tokens_are_accounted(db_session, wired, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="1_20", text=OTHER_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["tokens"] == 200


@pytest.mark.asyncio
async def test_unknown_sections_are_collected_for_feedback(db_session, wired, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["classify_result"] = {
        "ok": True,
        "verdict": {
            "action": "accept",
            "section": "sport",
            "title": "З",
            "text": LONG_TEXT,
            "section_known": False,
        },
        "usage": {"total_tokens": 10},
    }
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["unknown_sections"] == ["sport"]


@pytest.mark.asyncio
async def test_empty_source_is_a_no_op(db_session, wired):
    stats = await runner.run_site(db_session, SITE)
    assert stats["selected"] == 0 and wired["classify"] == []


@pytest.mark.asyncio
async def test_processed_posts_do_not_come_back(db_session, wired, monkeypatch):
    """Второй прогон подряд не переплачивает за уже обработанное."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await runner.run_site(db_session, SITE)
    wired["classify"].clear()
    stats = await runner.run_site(db_session, SITE)
    assert stats["selected"] == 0 and wired["classify"] == []


# ───────── досылка после сбоя ─────────


@pytest.mark.asyncio
async def test_retry_failed_reuses_saved_verdict(db_session, wired, monkeypatch):
    """Досылка не платит за классификацию второй раз — вердикт уже в журнале."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    wired["deliver_result"] = {"ok": False, "status": 0, "attempts": 3, "reason": "network"}
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await runner.run_site(db_session, SITE)
    assert (await _journal(db_session, "1_10")).status == "failed"

    wired["classify"].clear()
    wired["deliver_result"] = {"ok": True, "status": 201, "attempts": 1, "remote_id": "r9"}
    stats = await runner.retry_failed(db_session, SITE)

    assert stats["retried"] == 1 and stats["delivered"] == 1
    assert wired["classify"] == []  # LLM не звали
    row = await _journal(db_session, "1_10")
    assert row.status == "delivered" and row.remote_id == "r9" and row.reason is None


@pytest.mark.asyncio
async def test_retry_ignores_rejected_and_held(db_session, wired, monkeypatch):
    """rejected и held — решения, а не сбои: досылать их нельзя."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    from database.models_extended import ConveyorDelivery

    db_session.add_all(
        [
            ConveyorDelivery(
                site="vmalmyzhe", lip="1_10", status="rejected", verdict={"title": "x"}
            ),
            ConveyorDelivery(site="vmalmyzhe", lip="1_20", status="held", verdict={"title": "y"}),
        ]
    )
    await db_session.commit()
    stats = await runner.retry_failed(db_session, SITE)
    assert stats["retried"] == 0 and wired["deliver"] == []


@pytest.mark.asyncio
async def test_retry_without_audit_snapshot_is_skipped(db_session, wired, monkeypatch):
    """Нет снапшота — нечем собрать тело; молча пропускаем, а не шлём пустое."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    from database.models_extended import ConveyorDelivery

    db_session.add(
        ConveyorDelivery(site="vmalmyzhe", lip="9_99", status="failed", verdict={"title": "x"})
    )
    await db_session.commit()
    stats = await runner.retry_failed(db_session, SITE)
    assert stats["retried"] == 0 and wired["deliver"] == []


# ───────── дата поста и публикация (D-091) ─────────


@pytest.mark.asyncio
async def test_vk_date_reaches_the_receiver(db_session, wired, monkeypatch):
    """Первые 379 доставок ушли с ``date: null``, и лента портала сортировалась
    по моменту доставки. Дата обязана доезжать — с явным UTC."""
    from datetime import datetime

    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    bodies = []
    monkeypatch.setattr(
        runner.delivery_mod,
        "deliver",
        lambda site, key, body, **kw: bodies.append(body)
        or {"ok": True, "status": 201, "attempts": 1, "remote_id": "r1"},
    )
    await seed_pair(
        db_session, lip="1_10", text=LONG_TEXT, published_at=datetime(2026, 9, 14, 8, 30, 0)
    )
    await runner.run_site(db_session, SITE)
    assert bodies[0]["date"] == "2026-09-14T08:30:00Z"


@pytest.mark.asyncio
async def test_post_without_date_still_delivers(db_session, wired, monkeypatch):
    """Бэклог до миграции 080 даты не имеет. Подставлять «сейчас» нельзя, но и
    ронять доставку не за что: приёмник честно ответит ``date missing``."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    bodies = []
    monkeypatch.setattr(
        runner.delivery_mod,
        "deliver",
        lambda site, key, body, **kw: bodies.append(body)
        or {"ok": True, "status": 201, "attempts": 1, "remote_id": "r1"},
    )
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT, published_at=None)
    stats = await runner.run_site(db_session, SITE)
    assert stats["delivered"] == 1 and "date" not in bodies[0]


@pytest.mark.asyncio
async def test_publish_key_turns_on_publication(db_session, wired, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    monkeypatch.setenv("VMALMYZHE_PUBLISH_KEY", "pub")
    seen = {}
    monkeypatch.setattr(
        runner.delivery_mod,
        "deliver",
        lambda site, key, body, **kw: seen.update(body=body, publish_key=kw.get("publish_key"))
        or {"ok": True, "status": 201, "attempts": 1, "remote_id": "r1"},
    )
    site = dict(SITE, publish_key_env="VMALMYZHE_PUBLISH_KEY")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, site)
    assert stats["publish"] is True
    assert seen["body"]["publish"] is True and seen["publish_key"] == "pub"


@pytest.mark.asyncio
async def test_site_without_publish_key_stays_draft(db_session, wired, monkeypatch):
    """Казанская ключа публикации не выдавала — её поток обязан остаться черновиками."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    monkeypatch.delenv("VMALMYZHE_PUBLISH_KEY", raising=False)
    seen = {}
    monkeypatch.setattr(
        runner.delivery_mod,
        "deliver",
        lambda site, key, body, **kw: seen.update(body=body, publish_key=kw.get("publish_key"))
        or {"ok": True, "status": 201, "attempts": 1, "remote_id": "r1"},
    )
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE)
    assert stats["publish"] is False
    assert "publish" not in seen["body"] and not seen["publish_key"]


# ───────── дедуп до LLM (recommend brain 2026-09-14) ─────────

SAME_EVENT = (
    "В лицее стартовал месячник безопасности дорожного движения: первоклассников "
    "посвятили в пешеходы, а инспектор ГИБДД рассказал школьникам о правилах."
)


@pytest.mark.asyncio
async def test_duplicate_never_reaches_the_model(db_session, wired, monkeypatch):
    """Вся экономия в том, что дубль снимается ДО вызова модели и до того, как
    приёмник пойдёт качать те же фотографии второй раз."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=LONG_TEXT + " Подрядчик сдал работу в срок.")
    stats = await runner.run_site(db_session, SITE)
    assert stats["deduped"] == 1 and stats["selected"] == 1
    assert len(wired["classify"]) == 1 and len(wired["deliver"]) == 1


@pytest.mark.asyncio
async def test_dropped_duplicate_is_journalled_and_does_not_return(db_session, wired, monkeypatch):
    """Без строки журнала следующий прогон подберёт дубль как новый пост и
    заплатит за ту же новость второй раз."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=LONG_TEXT + " Подрядчик сдал работу в срок.")
    await runner.run_site(db_session, SITE)
    loser = "1_10" if len(LONG_TEXT) < len(LONG_TEXT + " Подрядчик сдал работу в срок.") else "2_20"
    row = await _journal(db_session, loser)
    assert row.status == "rejected" and row.reason.startswith("dup:")
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_duplicate_of_earlier_run_is_dropped(db_session, wired, monkeypatch):
    """Вчера уехало от газеты, сегодня приезжает от школы — окно семь дней."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=SAME_EVENT)
    await runner.run_site(db_session, SITE)
    await seed_pair(
        db_session,
        lip="2_20",
        text=SAME_EVENT.replace("В лицее", "В лицее Малмыжа"),
    )
    stats = await runner.run_site(db_session, SITE)
    assert stats["deduped"] == 1 and stats["selected"] == 0
    assert len(wired["classify"]) == 1  # второй прогон модель не звал


@pytest.mark.asyncio
async def test_dry_run_shows_duplicates_without_journalling(db_session, wired):
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=LONG_TEXT + " Подрядчик сдал работу в срок.")
    stats = await runner.run_site(db_session, SITE, dry_run=True)
    assert stats["deduped"] == 1 and len(stats["preview_duplicates"]) == 1
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {}
