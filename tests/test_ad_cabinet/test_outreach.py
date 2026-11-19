"""Рассылка рекламного оффера (Этап 4, 2026-09-05) — настоящий SQL, VK инъектирован.

- аудитория: ЛС → auto, предложка с can_message → auto, без — manual; группы мимо;
  один человек — одна строка (auto важнее manual); окно months_back; стоп-лист,
  уже получившие оффер, клиенты с заказами и архивные — мимо;
- набор идемпотентен, кабинеты и промо-пакет заводятся один раз;
- тик: dry-run кладёт текст и не шлёт; боевой — лимиты в сутки (всего и на
  сообщество), тихие часы, 901 → ручной список, 9 → пауза кампании и стоп тика,
  успех → заявка contacted, троттл между отправками;
- шаблон: явный или последний активный ad_offer, плейсхолдеры цен и ссылок.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from database.models import (
    AdClient,
    AdClientPackage,
    AdOutreachBlacklist,
    AdOutreachCampaign,
    AdOutreachRecipient,
    AdRequest,
    AdScheduledPost,
    MessageTemplate,
)
from modules.ad_cabinet import outreach

NOW = datetime(2026, 9, 5, 9, 0)  # UTC = 12:00 МСК
INFO = -158787639
INFO2 = -168170215


def _req(
    id_, *, origin, person, community=INFO, can_message=None, days_ago=10, group=False, name="Имя"
):
    return AdRequest(
        id=id_,
        origin=origin,
        community_vk_id=community,
        vk_post_id=None if origin == "inbound_dm" else id_ * 10,
        peer_id=person,
        author_vk_id=person if not group else -person,
        author_is_group=group,
        author_name=name,
        can_message=can_message,
        status="new",
        detected_at=NOW - timedelta(days=days_ago),
    )


async def _template(
    session, body="Здравствуйте, {author_name}! Пост — {price_single}, кабинет: {cabinet_link}"
):
    t = MessageTemplate(title="Оффер", body=body, category="ad_offer", is_active=True)
    session.add(t)
    await session.flush()
    return t


class _F:
    def __init__(self, session):
        self.s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, *a):
        return False


# ───────── аудитория ─────────


@pytest.mark.asyncio
async def test_audience_rules(db_session):
    db_session.add_all(
        [
            _req(1, origin="inbound_dm", person=100),
            _req(2, origin="suggested", person=200, can_message=True),
            _req(3, origin="suggested", person=300, can_message=None),
            _req(4, origin="suggested", person=400, can_message=False, group=True),
            _req(5, origin="suggested", person=500, days_ago=250),  # старше 6 месяцев
            _req(6, origin="suggested", person=100, can_message=None, days_ago=1),  # тот же человек
            _req(7, origin="inbound_dm", person=600),
            _req(8, origin="inbound_dm", person=700),
        ]
    )
    db_session.add(AdOutreachBlacklist(vk_user_id=600, reason="просил не писать"))
    ordered = AdClient(id=50, author_vk_id=700, name="Заказчик")
    db_session.add(ordered)
    await db_session.flush()
    db_session.add(
        AdScheduledPost(
            community_vk_id=INFO, text="t", publish_date=NOW, status="published", client_id=50
        )
    )
    await db_session.flush()

    rows = await outreach.build_audience(db_session, months_back=6, now_utc=NOW)
    by = {r["vk_user_id"]: r for r in rows}
    assert set(by) == {100, 200, 300}
    assert by[100]["mode"] == "auto" and by[100]["ad_request_id"] == 1  # auto важнее свежего manual
    assert by[200]["mode"] == "auto" and by[300]["mode"] == "manual"


@pytest.mark.asyncio
async def test_enroll_is_idempotent_and_creates_cabinets_once(db_session):
    db_session.add_all(
        [
            _req(1, origin="inbound_dm", person=100, name="Анна"),
            _req(2, origin="suggested", person=300, can_message=None),
        ]
    )
    existing = AdClient(id=9, author_vk_id=300, name="Уже есть")
    db_session.add(existing)
    db_session.add(
        AdClientPackage(client_id=9, kind="prepaid", posts_total=5, price=1500, paid_at=NOW)
    )
    camp = AdOutreachCampaign(title="Пилот", months_back=6)
    db_session.add(camp)
    await db_session.flush()

    st = await outreach.enroll_campaign(db_session, camp, now_utc=NOW)
    assert st["added"] == 2 and st["auto"] == 1 and st["manual"] == 1
    assert st["clients_created"] == 1 and st["promos_created"] == 1  # у 300 пакет уже был
    again = await outreach.enroll_campaign(db_session, camp, now_utc=NOW)
    assert again["added"] == 0 and again["existing"] == 2 and again["clients_created"] == 0

    rec = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    assert {r.vk_user_id: r.status for r in rec} == {100: "pending", 300: "manual"}
    anna = (
        await db_session.execute(select(AdClient).where(AdClient.author_vk_id == 100))
    ).scalar_one()
    promo = (
        await db_session.execute(
            select(AdClientPackage).where(AdClientPackage.client_id == anna.id)
        )
    ).scalar_one()
    assert promo.kind == "free_promo" and promo.posts_total == 3 and promo.paid_at is not None
    assert all(r.client_id for r in rec)


# ───────── текст ─────────


def test_render_offer_placeholders():
    body = (
        "Привет, {author_name}! {price_single}; {promo_posts} поста бесплатно; "
        "{cabinet_link}; {bot_link}"
    )
    out = outreach.render_offer(body, author_name="Анна")
    assert (
        "Анна" in out
        and "350 ₽" in out
        and "3 поста" in out
        and "сарафан.вмалмыже.рф/cabinet" in out
    )
    assert "{" not in out


# ───────── тик ─────────


async def _seed_running(session, *, dry_run, n=3, community=INFO, total_daily=150, per_comm=30):
    for i in range(n):
        session.add(_req(i + 1, origin="inbound_dm", person=100 + i, community=community))
    await _template(session)
    camp = AdOutreachCampaign(
        title="К",
        months_back=6,
        status="running",
        dry_run=dry_run,
        total_daily=total_daily,
        per_community_daily=per_comm,
        quiet_start=21,
        quiet_end=9,
    )
    session.add(camp)
    await session.flush()
    await outreach.enroll_campaign(session, camp, now_utc=NOW)
    await session.commit()
    return camp


@pytest.mark.asyncio
async def test_dry_run_renders_and_sends_nothing(db_session):
    camp = await _seed_running(db_session, dry_run=True, n=2)
    calls = []

    async def send(*a):
        calls.append(a)
        return {"success": True}

    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0
    )
    assert out["dry_run"] == 2 and out["sent"] == 0 and calls == []
    rows = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    assert all(r.status == "dry_run" and "350 ₽" in r.body for r in rows)
    await db_session.refresh(camp)
    assert camp.status == "running"  # сухой прогон кампанию не закрывает


@pytest.mark.asyncio
async def test_live_send_marks_request_and_respects_caps(db_session):
    camp = await _seed_running(db_session, dry_run=False, n=3, total_daily=2)
    sent = []

    async def send(cid, peer, text, att, rid=None):
        sent.append((cid, peer, rid))
        return {"success": True, "message_id": 77}

    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0
    )
    assert out["sent"] == 2 and len(sent) == 2 and sent[0][0] == INFO
    assert sent[0][2] is not None  # стабильный random_id = id адресата
    rows = (
        (await db_session.execute(select(AdOutreachRecipient).order_by(AdOutreachRecipient.id)))
        .scalars()
        .all()
    )
    assert [r.status for r in rows] == ["sent", "sent", "pending"]
    ar = await db_session.get(AdRequest, 1)
    assert ar.status == "contacted" and ar.vk_message_id == 77 and ar.via == "outreach"
    # Лимит на сутки исчерпан — следующий тик ничего не шлёт.
    out2 = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW + timedelta(minutes=5), interval=0
    )
    assert out2["sent"] == 0 and out2["skipped"] == "daily-cap"
    # Новые МСК-сутки — лимит обнулился.
    out3 = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW + timedelta(days=1), interval=0
    )
    assert out3["sent"] == 1
    await db_session.refresh(camp)
    assert camp.status == "done"


@pytest.mark.asyncio
async def test_manual_send_counts_toward_daily_limit(db_session):
    """Ручная отправка (владелец написал сам через кабинет) занимает лимит.

    Предикат один — ``SENT_STATUSES`` = ('sent', 'done_manual'). Раньше счётчик
    лимита считал литералом только 'sent', и ручные отправки шли **сверх**
    лимита 30/сообщество и 150/сутки (P019, разбор 19.11).
    """
    camp = await _seed_running(db_session, dry_run=False, n=0)
    rows = [
        # Отправлено автоматически сегодня — занимает лимит.
        AdOutreachRecipient(
            campaign_id=camp.id,
            vk_user_id=901,
            community_vk_id=INFO,
            status="sent",
            sent_at=NOW,
        ),
        # Владелец написал сам сегодня — тоже занимает лимит.
        AdOutreachRecipient(
            campaign_id=camp.id,
            vk_user_id=902,
            community_vk_id=INFO,
            status="done_manual",
            sent_at=NOW,
        ),
        # Взято параллельным тиком — тоже занимает лимит.
        AdOutreachRecipient(
            campaign_id=camp.id,
            vk_user_id=903,
            community_vk_id=INFO,
            status="claimed",
            claimed_at=NOW,
        ),
        # Ручная отправка ВЧЕРА — сегодняшний лимит не занимает.
        AdOutreachRecipient(
            campaign_id=camp.id,
            vk_user_id=904,
            community_vk_id=INFO,
            status="done_manual",
            sent_at=NOW - timedelta(days=1),
        ),
        # Другое сообщество — свой счёт.
        AdOutreachRecipient(
            campaign_id=camp.id,
            vk_user_id=905,
            community_vk_id=INFO2,
            status="done_manual",
            sent_at=NOW,
        ),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.flush()

    counts = await outreach._sent_today(db_session, NOW - timedelta(hours=12))
    assert counts[INFO] == 3  # sent + done_manual + claimed
    assert counts[INFO2] == 1  # ручная отправка в другом сообществе
    assert counts[None] == 4  # всего за сутки


@pytest.mark.asyncio
async def test_per_community_cap_and_quiet_hours(db_session):
    camp = await _seed_running(db_session, dry_run=False, n=2, per_comm=1)
    db_session.add(_req(9, origin="inbound_dm", person=900, community=INFO2))
    await db_session.flush()
    await outreach.enroll_campaign(db_session, camp, now_utc=NOW)
    await db_session.commit()

    async def send(cid, peer, text, att, rid=None):
        return {"success": True, "message_id": 1}

    quiet = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=datetime(2026, 9, 5, 19, 30), interval=0
    )  # 22:30 МСК
    assert quiet["sent"] == 0 and quiet["skipped"] == "quiet-hours"
    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0
    )
    assert out["sent"] == 2  # по одному на сообщество
    rows = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    assert sorted(r.status for r in rows) == ["pending", "sent", "sent"]


@pytest.mark.asyncio
async def test_not_allowed_goes_manual_and_flood_stops_tick(db_session, monkeypatch):
    camp = await _seed_running(db_session, dry_run=False, n=3)
    answers = iter(
        [
            {"success": False, "error_code": 901, "error": "not allowed", "allowed": False},
            {
                "success": False,
                "error_code": 9,
                "error": "Flood control",
                "paused_until": (NOW + timedelta(hours=24)).isoformat(),
            },
            {"success": True, "message_id": 5},
        ]
    )
    calls = []

    async def send(cid, peer, text, att, rid=None):
        calls.append(peer)
        return next(answers)

    alerts = []
    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0, alert=alerts.append
    )
    assert out["manual"] == 1 and out["stopped"] is True and out["sent"] == 0
    assert calls == [100, 101]  # третий не тронут — тик остановлен
    rows = (
        (await db_session.execute(select(AdOutreachRecipient).order_by(AdOutreachRecipient.id)))
        .scalars()
        .all()
    )
    assert [(r.mode, r.status) for r in rows] == [
        ("manual", "manual"),
        ("auto", "pending"),
        ("auto", "pending"),
    ]
    ar = await db_session.get(AdRequest, 1)
    assert ar.can_message is False  # 901 → инбокс тоже знает
    await db_session.refresh(camp)
    assert camp.status == "running" and camp.paused_until is None
    assert "VK 9" in camp.paused_reason and len(alerts) == 1
    # Канал сообщества на паузе (dm_channel) — его адресаты пропускаются, VK не трогаем.
    from modules.ad_cabinet import dm_channel

    monkeypatch.setattr(dm_channel, "paused_until", lambda cid, **kw: NOW + timedelta(hours=24))
    again = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW + timedelta(hours=1), interval=0
    )
    assert again["sent"] == 0 and calls == [100, 101]
    manual = await outreach.manual_list(db_session, camp.id)
    assert len(manual) == 1 and manual[0]["deeplink"].endswith("sel=100")
    assert "350 ₽" in manual[0]["body"]


@pytest.mark.asyncio
async def test_audience_skips_non_ad_dialogs_and_signed_group_posts(db_session):
    """route=notifications (не реклама из ЛС) — мимо; подписанный пост от группы — по peer_id."""
    db_session.add(_req(1, origin="inbound_dm", person=100))
    ar2 = _req(2, origin="inbound_dm", person=200)
    ar2.route = "notifications"
    db_session.add(ar2)
    signed = _req(3, origin="suggested", person=300, can_message=True)
    signed.author_vk_id = -158787639  # пост от имени группы, подписант — 300
    db_session.add(signed)
    await db_session.flush()
    rows = await outreach.build_audience(db_session, months_back=6, now_utc=NOW)
    assert {r["vk_user_id"] for r in rows} == {100, 300}
    assert next(r for r in rows if r["vk_user_id"] == 300)["mode"] == "auto"


@pytest.mark.asyncio
async def test_daily_caps_are_global_across_campaigns_and_per_tick(db_session):
    await _seed_running(db_session, dry_run=False, n=2, total_daily=3)
    camp2 = AdOutreachCampaign(
        title="Вторая", months_back=6, status="running", dry_run=False, total_daily=3
    )
    db_session.add(camp2)
    db_session.add(_req(9, origin="inbound_dm", person=900, community=INFO2))
    await db_session.flush()
    await outreach.enroll_campaign(db_session, camp2, now_utc=NOW)
    await db_session.commit()

    async def send(cid, peer, text, att, rid=None):
        return {"success": True, "message_id": 1}

    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0, per_tick=1
    )
    assert out["sent"] == 1 and out["skipped"] == "tick-cap"  # лимит на тик держит
    out2 = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0, per_tick=10
    )
    assert out2["sent"] == 2  # 3 в сутки на ВСЕ кампании: 1 + 2


@pytest.mark.asyncio
async def test_stale_claimed_rows_are_reclaimed(db_session):
    camp = await _seed_running(db_session, dry_run=False, n=1)
    r = (await db_session.execute(select(AdOutreachRecipient))).scalar_one()
    r.status = "claimed"
    r.claimed_at = NOW - timedelta(minutes=30)
    await db_session.commit()

    async def send(cid, peer, text, att, rid=None):
        return {"success": True, "message_id": 1}

    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0
    )
    await db_session.refresh(r)
    assert r.status == "failed" and "зависло" in r.error and out["sent"] == 0
    await db_session.refresh(camp)
    assert camp.status == "done"


@pytest.mark.asyncio
async def test_failed_send_retries_then_fails(db_session):
    camp = await _seed_running(db_session, dry_run=False, n=1)

    async def send(cid, peer, text, att, rid=None):
        return {"success": False, "error_code": 10, "error": "internal"}

    for i in range(outreach.MAX_ATTEMPTS):
        await outreach.run_outreach_tick(
            session_factory=_F(db_session),
            send=send,
            now_utc=NOW + timedelta(minutes=i),
            interval=0,
        )
    r = (await db_session.execute(select(AdOutreachRecipient))).scalar_one()
    assert r.status == "failed" and r.attempts == outreach.MAX_ATTEMPTS
    await db_session.refresh(camp)
    assert camp.status == "done"


def test_quiet_hours_and_day_start():
    assert outreach.in_quiet_hours(datetime(2026, 9, 5, 22, 0), 21, 9)
    assert outreach.in_quiet_hours(datetime(2026, 9, 5, 3, 0), 21, 9)
    assert not outreach.in_quiet_hours(datetime(2026, 9, 5, 12, 0), 21, 9)
    assert not outreach.in_quiet_hours(datetime(2026, 9, 5, 22, 0), 9, 9)
    assert outreach.msk_day_start_utc(datetime(2026, 9, 5, 0, 30)) == datetime(2026, 9, 4, 21, 0)


def test_routes_and_task_registered():
    import main
    from tasks.celery_app import app

    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert (
        "/api/ad-outreach/campaigns" in paths
        and "/api/ad-outreach/campaigns/{campaign_id}/manual" in paths
    )
    assert "tasks.celery_app.dispatch_ad_outreach" in app.tasks
    assert (
        app.conf.beat_schedule["ad-outreach-dispatch"]["task"]
        == "tasks.celery_app.dispatch_ad_outreach"
    )


@pytest.mark.asyncio
async def test_person_in_live_campaign_is_not_enrolled_twice(db_session):
    """Один человек — в одной живой кампании: pending/manual/dry_run в другой кампании исключают."""
    db_session.add(_req(1, origin="inbound_dm", person=100))
    db_session.add(_req(2, origin="suggested", person=300, can_message=None))
    camp1 = AdOutreachCampaign(title="Первая", months_back=6, status="running", dry_run=True)
    db_session.add(camp1)
    await db_session.flush()
    await outreach.enroll_campaign(db_session, camp1, now_utc=NOW)
    camp2 = AdOutreachCampaign(title="Вторая", months_back=6)
    db_session.add(camp2)
    await db_session.flush()
    st = await outreach.enroll_campaign(db_session, camp2, now_utc=NOW)
    assert st["added"] == 0  # оба уже заняты первой кампанией


@pytest.mark.asyncio
async def test_inbound_window_uses_last_activity(db_session):
    """Старый диалог, переоткрытый новым сообщением (updated_at), снова в окне."""
    old = _req(1, origin="inbound_dm", person=100, days_ago=400)
    old.updated_at = NOW - timedelta(days=2)
    db_session.add(old)
    stale = _req(2, origin="inbound_dm", person=200, days_ago=400)
    stale.updated_at = NOW - timedelta(days=300)
    db_session.add(stale)
    await db_session.flush()
    rows = await outreach.build_audience(db_session, months_back=6, now_utc=NOW)
    assert {r["vk_user_id"] for r in rows} == {100}


def test_interleave_by_community_round_robin():
    def mk(i, c):
        return AdOutreachRecipient(id=i, campaign_id=1, vk_user_id=i, community_vk_id=c)

    rows = [mk(1, -1), mk(2, -1), mk(3, -1), mk(4, -2), mk(5, -3)]
    out = outreach._interleave_by_community(rows)
    assert [r.community_vk_id for r in out] == [-1, -2, -3, -1, -1]


@pytest.mark.asyncio
async def test_dry_run_and_channel_pause_do_not_burn_attempts(db_session):
    camp = await _seed_running(db_session, dry_run=True, n=1)
    await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=None, now_utc=NOW, interval=0
    )
    r = (await db_session.execute(select(AdOutreachRecipient))).scalar_one()
    assert r.status == "dry_run" and r.attempts == 0
    await db_session.refresh(camp)
    assert camp.status == "running"


@pytest.mark.asyncio
async def test_enroll_links_request_to_cabinet_and_skips_promo_for_paid_clients(db_session):
    db_session.add(_req(1, origin="inbound_dm", person=100, name="Анна"))
    db_session.add(_req(2, origin="suggested", person=300, can_message=None))
    paid = AdClient(id=9, author_vk_id=300, name="Платный")
    db_session.add(paid)
    db_session.add(
        AdClientPackage(
            client_id=9, kind="prepaid", posts_total=5, price=1500, paid_at=NOW, is_active=True
        )
    )
    camp = AdOutreachCampaign(title="К", months_back=6)
    db_session.add(camp)
    await db_session.flush()
    st = await outreach.enroll_campaign(db_session, camp, now_utc=NOW)
    assert st["promos_created"] == 1  # только Анне; у платного есть активный пакет
    ar = await db_session.get(AdRequest, 1)
    anna = (
        await db_session.execute(select(AdClient).where(AdClient.author_vk_id == 100))
    ).scalar_one()
    assert ar.client_id == anna.id


@pytest.mark.asyncio
async def test_900_goes_to_blacklist_and_synthetic_901_keeps_request(db_session):
    """900 → стоп-лист и skipped; синтетический 901 (нет токена) не трогает can_message заявки."""
    camp = await _seed_running(db_session, dry_run=False, n=2)
    answers = iter(
        [
            {"success": False, "error_code": 900, "error": "blacklisted", "allowed": False},
            {
                "success": False,
                "error_code": 901,
                "error": "нет community-токена",
                "no_token": True,
            },
        ]
    )

    async def send(cid, peer, text, att, rid=None):
        return next(answers)

    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0
    )
    assert out.get("blacklisted") == 1 and out["manual"] == 1
    rows = (
        (await db_session.execute(select(AdOutreachRecipient).order_by(AdOutreachRecipient.id)))
        .scalars()
        .all()
    )
    assert rows[0].status == "skipped" and rows[0].error_code == 900
    assert await db_session.get(AdOutreachBlacklist, 100) is not None
    assert rows[1].status == "manual"
    ar1 = await db_session.get(AdRequest, 1)
    ar2 = await db_session.get(AdRequest, 2)
    assert ar1.can_message is False and ar2.can_message is None
    manual = await outreach.manual_list(db_session, camp.id)
    assert [m["vk_user_id"] for m in manual] == [101]


@pytest.mark.asyncio
async def test_captcha_text_without_code_pauses_channel(db_session, monkeypatch):
    await _seed_running(db_session, dry_run=False, n=2)
    from modules.ad_cabinet import dm_channel

    noted = []
    monkeypatch.setattr(
        dm_channel, "note_error", lambda cid, code, **kw: noted.append((cid, code)) or None
    )
    monkeypatch.setattr(dm_channel, "paused_until", lambda cid, **kw: None)

    async def send(cid, peer, text, att, rid=None):
        return {"success": False, "error_code": 0, "error": "Captcha needed"}

    out = await outreach.run_outreach_tick(
        session_factory=_F(db_session), send=send, now_utc=NOW, interval=0
    )
    assert out["stopped"] is True and noted == [(INFO, 14)]


@pytest.mark.asyncio
async def test_audience_respects_operator_skipped(db_session):
    db_session.add(_req(1, origin="inbound_dm", person=100))
    skipped = _req(2, origin="inbound_dm", person=200)
    skipped.status = "skipped"
    db_session.add(skipped)
    await db_session.flush()
    rows = await outreach.build_audience(db_session, months_back=6, now_utc=NOW)
    assert {r["vk_user_id"] for r in rows} == {100}


# ───────── ручная ветка запоминает отправленный текст (найдено 2026-09-14) ─────────


@pytest.mark.asyncio
async def test_manual_list_stores_the_text_it_hands_out(db_session):
    """Авто-ветка писала ``body`` с самого начала, ручная — нигде.

    Замер на проде 14.09: 16 из 16 записей ``sent/auto`` с текстом, и 0 из 42
    ручных (28 ``done_manual`` + 14 ждущих). На вопрос «что мы ему писали»
    ответа не было ни у нас, ни в CRM.
    """
    camp = await _seed_running(db_session, dry_run=False, n=1)
    rows = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    for r in rows:
        r.mode, r.status, r.body = "manual", "manual", None
    await db_session.commit()

    manual = await outreach.manual_list(db_session, camp.id)
    assert manual and manual[0]["body"]

    stored = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    assert stored[0].body == manual[0]["body"]


@pytest.mark.asyncio
async def test_manual_list_does_not_overwrite_an_earlier_text(db_session):
    """Первым победил текст, который реально ушёл: шаблон с тех пор мог смениться,
    и перерисовать карточку по сегодняшнему — значит записать не тот предмет."""
    camp = await _seed_running(db_session, dry_run=False, n=1)
    rows = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    for r in rows:
        r.mode, r.status, r.body = "manual", "manual", "то, что ушло на самом деле"
    await db_session.commit()

    manual = await outreach.manual_list(db_session, camp.id)
    assert manual[0]["body"] == "то, что ушло на самом деле"
    kept = (await db_session.execute(select(AdOutreachRecipient))).scalars().all()
    assert kept[0].body == "то, что ушло на самом деле"
