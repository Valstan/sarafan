"""Ручной режим конвейера: вердикты человека вместо DeepSeek + сверка с журналом.

Режим существует не для удобства, а потому что движок лежит с 22.09 (P177), а
конвейер на четырёх сайтах стоял бы. Проверяем ровно три вещи, на которых он
может разойтись с обычным прогоном: модель не зовётся, гейты вердикта те же,
и журнал говорит, КТО принял решение.

Сверка (``reconcile``) — отдельный сюжет: она ловит класс отказа, который уже
случался на живых данных (скрипт рапортовал 201 с ``remote_id``, а строка
осталась ``selected``), и который прогон, завершившийся кодом 0, скрывал.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from modules.conveyor import runner, source
from tests.test_conveyor.conftest import SITE, seed_pair

LONG_TEXT = (
    "В Малмыже отремонтировали участок дороги по улице Ленина. Работы шли две недели, "
    "подрядчик уложил новое покрытие и обновил разметку у школы номер один."
)
OTHER_TEXT = (
    "Библиотека Малмыжа объявила запись в кружок краеведения. Занятия начнутся "
    "в октябре, ведёт их сотрудник музея, записаться можно по телефону."
)


def _accept(text: str = LONG_TEXT, **over):
    v = {"action": "accept", "section": "novosti", "title": "Заголовок", "text": text}
    v.update(over)
    return v


@pytest.fixture
def wired(monkeypatch):
    """Подменяет обе внешние границы и **считает вызовы модели**."""
    calls = {"classify": [], "deliver": []}

    def fake_classify(post, *, sections, rules="", api_key=None):
        calls["classify"].append(post["lip"])
        return {"ok": True, "verdict": {**_accept(), "section_known": True}}

    def fake_deliver(site, key, body, **kw):
        calls["deliver"].append(body["vkPostId"])
        return {"ok": True, "status": 201, "attempts": 1, "remote_id": "r1"}

    monkeypatch.setattr(runner.classify_mod, "classify", fake_classify)
    monkeypatch.setattr(runner.delivery_mod, "deliver", fake_deliver)
    return calls


async def _status(db_session, lip):
    from sqlalchemy import select

    from database.models_extended import ConveyorDelivery

    row = (
        await db_session.execute(select(ConveyorDelivery).where(ConveyorDelivery.lip == lip))
    ).scalar_one_or_none()
    return getattr(row, "status", None), getattr(row, "reason", None)


@pytest.mark.asyncio
async def test_manual_verdict_delivers_without_calling_the_model(db_session, wired, monkeypatch):
    """Главное обещание ручного режима: DeepSeek не зовётся вообще."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(
        db_session, SITE, verdicts={"1_10": _accept()}, collect_results=True, sleep=None
    )
    assert stats["delivered"] == 1 and stats["manual"] is True
    assert wired["classify"] == [] and len(wired["deliver"]) == 1
    status, reason = await _status(db_session, "1_10")
    assert status == "delivered" and reason is None
    assert stats["results"][0]["remote_id"] == "r1"


@pytest.mark.asyncio
async def test_manual_reject_is_marked_as_human_decision(db_session, wired, monkeypatch):
    """Отказ человека и отказ модели — разные строки в журнале.

    Смешав их, разбор «почему новости нет на сайте» через месяц будет врать о
    том, кто принял решение.
    """
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(
        db_session,
        SITE,
        verdicts={"1_10": {"action": "reject", "reason": "не для сайта"}},
        sleep=None,
    )
    assert stats["rejected"] == 1 and wired["deliver"] == []
    status, reason = await _status(db_session, "1_10")
    assert status == "rejected"
    assert reason.startswith("manual_reject")
    assert "не для сайта" in reason


@pytest.mark.asyncio
async def test_manual_verdict_passes_through_the_same_gates(db_session, wired, monkeypatch):
    """Ручной путь не дыра: пустой заголовок ловится так же, как у модели.

    Иначе «вердикты глазами» станут путём, минующим сторож на выдумку. Статус при
    этом ``failed``, а не ``rejected``: опечатка оператора — это ввод, который
    надо повторить, и закрывать по ней пост навсегда нельзя.
    """
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(
        db_session,
        SITE,
        verdicts={"1_10": _accept(title="")},
        sleep=None,
    )
    assert stats["rejected"] == 0 and stats["failed"] == 1
    assert wired["deliver"] == []
    status, reason = await _status(db_session, "1_10")
    assert status == "failed" and reason == "manual_no_title"


@pytest.mark.asyncio
async def test_post_without_verdict_is_not_sent_to_the_model(db_session, wired, monkeypatch):
    """Нет вердикта — не сбой: ни строки в журнале, ни ухода в LLM.

    Тишина при лежащем движке иначе читалась бы как «отбор пуст». И — важнее —
    такой пост обязан остаться в отборе: отбор отсекает любой ``lip`` со строкой
    журнала, и заведённая ``selected``-строка выбросила бы его навсегда (проверено
    ниже отдельным тестом).
    """
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE, verdicts={"2_20": _accept()}, sleep=None)
    assert stats["skipped"] == 1 and stats["without_verdict"] == ["1_10"]
    assert stats["delivered"] == 0 and wired["classify"] == [] and wired["deliver"] == []
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {}
    assert await source.fetch_pending_for_site(db_session, SITE)


@pytest.mark.asyncio
async def test_post_without_verdict_stays_selectable(db_session, wired, monkeypatch):
    """Кандидат без вердикта остаётся кандидатом следующего прогона.

    Это и есть разница между ручным режимом и модельным: там вердикт есть у
    каждого, и ``selected``-строка означает «обрабатывается прямо сейчас».
    Здесь строка была бы приговором — отбор больше никогда этот lip не возьмёт.
    """
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=OTHER_TEXT)

    first = await runner.run_site(db_session, SITE, verdicts={"1_10": _accept()}, sleep=None)
    assert first["skipped"] == 1 and first["without_verdict"] == ["2_20"]
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {"delivered": 1}

    # Второй прогон без вердиктов для 2_20 всё равно его видит.
    pending = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in pending] == ["2_20"]

    second = await runner.run_site(
        db_session, SITE, verdicts={"2_20": _accept(OTHER_TEXT)}, sleep=None
    )
    assert second["delivered"] == 1 and second["without_verdict"] == []
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {"delivered": 2}


@pytest.mark.asyncio
async def test_results_are_collected_only_when_asked(db_session, wired, monkeypatch):
    """Построчный результат — для сверки; автоматическому прогону он не нужен."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    plain = await runner.run_site(db_session, SITE, verdicts={"1_10": _accept()}, sleep=None)
    assert "results" not in plain


@pytest.mark.asyncio
async def test_reconcile_finds_delivered_but_selected(db_session, wired, monkeypatch):
    """Класс отказа, который уже случался: 201 с remote_id, а строка `selected`."""
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(
        db_session, SITE, verdicts={"1_10": _accept()}, collect_results=True, sleep=None
    )
    # Возвращаем строку в исходное состояние — как если бы update_delivery
    # не нашёл её (observed на живых данных 30.09, причина не найдена).
    from sqlalchemy import update

    from database.models_extended import ConveyorDelivery

    await db_session.execute(
        update(ConveyorDelivery).where(ConveyorDelivery.lip == "1_10").values(status="selected")
    )
    await db_session.commit()

    script = _load_script()
    bad = await script.reconcile(db_session, "vmalmyzhe", stats["results"])
    assert len(bad) == 1
    assert bad[0]["lip"] == "1_10"
    assert bad[0]["reported"] == "delivered" and bad[0]["journal"] == "selected"
    assert bad[0]["remote_id"] == "r1"


@pytest.mark.asyncio
async def test_reconcile_is_quiet_when_journal_agrees(db_session, wired, monkeypatch):
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(
        db_session, SITE, verdicts={"1_10": _accept()}, collect_results=True, sleep=None
    )
    script = _load_script()
    assert await script.reconcile(db_session, "vmalmyzhe", stats["results"]) == []


@pytest.mark.asyncio
async def test_plan_shows_what_would_fly_and_what_would_not(db_session, monkeypatch, tmp_path):
    """``--check``: план, который прошёл бы на доставке, должен проходить и тут."""
    script = _load_script()
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=OTHER_TEXT)

    payload = {
        "site": "vmalmyzhe",
        "candidates": [],
        "verdicts": {"1_10": _accept(), "2_20": {"action": "accept", "title": "", "text": "x"}},
    }
    file_name = tmp_path / "plan.json"
    file_name.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = await script.plan(
        db_session,
        SITE,
        script.load_verdicts(str(file_name)),
        file_name=str(file_name),
    )
    by_lip = {r["lip"]: r["verdict"] for r in report["rows"]}
    assert by_lip["1_10"] == "уедет"
    assert by_lip["2_20"] == "отказ"


@pytest.mark.asyncio
async def test_plan_flags_verdict_for_a_post_the_selection_did_not_take(
    db_session, monkeypatch, tmp_path
):
    """Вердикт в лишний lip не применяется — и это обязано быть сказано."""
    script = _load_script()
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    file_name = tmp_path / "plan.json"
    file_name.write_text(json.dumps({"verdicts": {"9_90": _accept()}}), encoding="utf-8")
    report = await script.plan(
        db_session, SITE, script.load_verdicts(str(file_name)), file_name=str(file_name)
    )
    assert report["unknown_lips"] == ["9_90"]


@pytest.mark.asyncio
async def test_dry_full_preview_carries_the_text(db_session):
    """Выгрузка кандидатов бессмысленна без текста: вердикт по lip не написать."""
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    stats = await runner.run_site(db_session, SITE, dry_run=True, dry_full=True)
    preview = stats["preview"][0]
    assert preview["text"] == LONG_TEXT and preview["url"].endswith("1_10")


@pytest.mark.asyncio
async def test_inflated_manual_text_is_caught_by_the_shared_gate(db_session, wired, monkeypatch):
    """Сторож на дописанные факты работает и на вердикте человека.

    Исходник — длиннее порога (400 символов): на коротком тексте рост в полтора
    раза не отличить от добавленного заголовка, и проверка там сознательно
    молчит (тест на это — в ``test_classify.py``).
    """
    long_text = LONG_TEXT * 4
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=long_text)
    stats = await runner.run_site(
        db_session,
        SITE,
        verdicts={"1_10": _accept(text=long_text * 3)},
        collect_results=True,
        sleep=None,
    )
    assert stats["failed"] == 1 and wired["deliver"] == []
    _, reason = await _status(db_session, "1_10")
    assert reason == "manual_text_inflated"
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {"failed": 1}


@pytest.mark.asyncio
async def test_emit_mode_writes_candidates_with_text(db_session, monkeypatch, tmp_path):
    """``--emit`` на живом отборе: файл с текстом, а не пустая заглушка.

    Режимы CLI проверяются именно здесь, а не только разбором чистых функций:
    ветка ``--emit`` оставалась непокрытой и держала баг «свободная переменная
    ``runner_mod``», который виден только при запуске скрипта целиком. Тест,
    не заходящий в ветку, не отличит «работает» от «никогда не запускалось``.
    """
    script = _load_script()
    _patch_session(monkeypatch, db_session)
    published = datetime(2026, 9, 29, 18, 30)
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT, published_at=published)
    out = tmp_path / "cand.json"

    assert await script._amain(_args(emit=str(out)), _real_site(), "emit", str(out)) == 0

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdicts"] == {}
    assert [c["lip"] for c in data["candidates"]] == ["1_10"]
    assert data["candidates"][0]["text"] == LONG_TEXT
    # Дата из БД приходит объектом datetime: без приведения к ISO файл не пишется.
    # На тестах с published_at=None баз был не виден, на живых данных он есть всегда.
    assert data["candidates"][0]["published_at"] == published.isoformat()
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {}


@pytest.mark.asyncio
async def test_check_mode_end_to_end(db_session, monkeypatch, tmp_path, capsys):
    """``--check`` на настоящем отборе: 1 — пока вердикт не на все посты."""
    script = _load_script()
    _patch_session(monkeypatch, db_session)
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=OTHER_TEXT)
    site = _real_site()

    file_name = tmp_path / "plan.json"
    file_name.write_text(
        json.dumps({"verdicts": {"1_10": _accept()}}, ensure_ascii=False), encoding="utf-8"
    )
    rc = await script._amain(_args(check=str(file_name)), site, "check", str(file_name))
    assert rc == 1
    assert "нет вердикта" in capsys.readouterr().out

    file_name.write_text(
        json.dumps(
            {"verdicts": {"1_10": _accept(), "2_20": _accept(text=OTHER_TEXT)}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc = await script._amain(_args(check=str(file_name)), site, "check", str(file_name))
    assert rc == 0
    assert await source.site_status_counts(db_session, site="vmalmyzhe") == {}


@pytest.mark.asyncio
async def test_check_is_ready_when_every_post_is_decided(db_session, monkeypatch, tmp_path, capsys):
    """Отказ — тоже решение: план, где часть постов отклонена, готов к доставке.

    Пока ``--check`` считал отказ «нерешённым», любой план с отказами (а они
    будут почти всегда) возвращал код 1 — и гейт переставал быть сигналом.
    """
    script = _load_script()
    _patch_session(monkeypatch, db_session)
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    await seed_pair(db_session, lip="2_20", text=OTHER_TEXT)
    file_name = tmp_path / "plan.json"
    file_name.write_text(
        json.dumps(
            {
                "verdicts": {
                    "1_10": _accept(),
                    "2_20": {"action": "reject", "reason": "не для сайта"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc = await script._amain(_args(check=str(file_name)), _real_site(), "check", str(file_name))
    assert rc == 0
    assert "решено: 2 из 2" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_deliver_mode_end_to_end(db_session, wired, monkeypatch, tmp_path, capsys):
    """``--deliver`` на живом пути: отправлено, журнал сошёлся, код 0."""
    script = _load_script()
    _patch_session(monkeypatch, db_session)
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)
    file_name = tmp_path / "plan.json"
    file_name.write_text(
        json.dumps({"verdicts": {"1_10": _accept()}}, ensure_ascii=False), encoding="utf-8"
    )
    rc = await script._amain(_args(deliver=str(file_name)), _real_site(), "deliver", str(file_name))
    assert rc == 0
    out = capsys.readouterr().out
    assert "delivered=1" in out and "РАСХОЖДЕНИЕ" not in out
    status, _ = await _status(db_session, "1_10")
    assert status == "delivered"


@pytest.mark.asyncio
async def test_deliver_mode_rc_2_when_journal_disagrees(
    db_session, wired, monkeypatch, tmp_path, capsys
):
    """Код 2 — прогон состоялся, но журнал разошёлся: молчать здесь нельзя.

    Именно этот случай отчёт скрипта показывал зелёным 30.09, пока строка
    оставалась ``selected``.
    """
    script = _load_script()
    _patch_session(monkeypatch, db_session)
    monkeypatch.setenv("VMALMYZHE_INGEST_KEY", "k")
    await seed_pair(db_session, lip="1_10", text=LONG_TEXT)

    real_update = source.update_delivery
    calls = {"n": 0}

    async def flaky(session, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # строка не найдена — как на живых данных
        return await real_update(session, **kw)

    monkeypatch.setattr(source, "update_delivery", flaky)
    file_name = tmp_path / "plan.json"
    file_name.write_text(
        json.dumps({"verdicts": {"1_10": _accept()}}, ensure_ascii=False), encoding="utf-8"
    )
    rc = await script._amain(_args(deliver=str(file_name)), _real_site(), "deliver", str(file_name))
    assert rc == 2
    out = capsys.readouterr().out
    assert "РАСХОЖДЕНИЕ С ЖУРНАЛОМ" in out and "журнал=selected" in out


def _args(**over):
    """Пространство имён как его собирает argparse (разбор проверен отдельно)."""
    import argparse

    base = {
        "site": "vmalmyzhe",
        "emit": None,
        "check": None,
        "deliver": None,
        "days": None,
        "limit": None,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _real_site():
    """Настоящий сайт из конфига, а не тестовый словарь: проверяем и путь конфигурации."""
    from config.content_conveyor import get_site

    return get_site("vmalmyzhe")


def _patch_session(monkeypatch, session):
    """Подменить фабрику сессий CLI на уже открытую тестовую сессию."""
    from database import connection

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(connection, "AsyncSessionLocal", lambda: _Ctx())


def _load_script():
    import importlib.util
    import sys as _sys
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "scripts" / "conveyor_manual_deliver.py"
    spec = importlib.util.spec_from_file_location("conveyor_manual_deliver", path)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["conveyor_manual_deliver"] = mod
    spec.loader.exec_module(mod)
    return mod
