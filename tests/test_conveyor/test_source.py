"""Тесты отбора постов для сайта — вход конвейера (D-015).

Держим ровно те свойства, поломка которых стоит денег или репутации сайта:
неопубликованное не уходит, обработанное не переплачивается, префильтр тем
работает, медиа доезжают (без них сайт получит новость без картинок).
"""

from __future__ import annotations

import pytest

from database.models_extended import ConveyorDelivery
from modules.conveyor import source
from tests.test_conveyor.conftest import SITE, seed_audit, seed_pair, seed_run


@pytest.mark.asyncio
async def test_picks_published_posts_with_text_and_media(db_session):
    await seed_pair(db_session, lip="1_10", media=[{"type": "photo", "url": "https://cdn/1.jpg"}])
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in out] == ["1_10"]
    assert out[0]["text"] == "текст поста"
    assert out[0]["media"] == [{"type": "photo", "url": "https://cdn/1.jpg"}]
    assert out[0]["theme"] == "novost"


@pytest.mark.asyncio
async def test_unpublished_bulletin_is_not_a_source(db_session):
    """Сводка-черновик — это ещё не решение человека, брать её нельзя."""
    await seed_pair(db_session, lip="1_10", published=False)
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_post_without_audit_row_is_skipped(db_session):
    """Нет строки аудита — нет ни текста, ни медиа; отдавать LLM нечего."""
    await seed_run(db_session, lips=["1_10"])
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_empty_text_is_skipped(db_session):
    await seed_pair(db_session, lip="1_10", text="   ")
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_skip_themes_prefilter(db_session):
    """Дешёвый префильтр до LLM: реклама и соседи на районный портал не идут."""
    await seed_pair(db_session, lip="1_10", theme="reklama")
    await seed_pair(db_session, lip="1_20", theme="neighbors")
    await seed_pair(db_session, lip="1_30", theme="kultura")
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in out] == ["1_30"]


@pytest.mark.asyncio
async def test_other_region_is_not_taken(db_session):
    await seed_run(db_session, lips=["2_10"], region_code="vp")
    await seed_audit(db_session, lip="2_10", region="vp")
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_window_excludes_old(db_session):
    await seed_pair(db_session, lip="1_10", days_ago=30)
    assert await source.fetch_pending_for_site(db_session, SITE, days=3) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["delivered", "rejected", "held", "failed", "selected"])
async def test_any_existing_delivery_row_excludes_post(db_session, status):
    """Пост, по которому решение уже принято, не возвращается ни в каком статусе.

    Особенно rejected/held: иначе каждый прогон переспрашивал бы LLM про то, что
    уже отклонено, и счёт за токены рос бы на ровном месте.
    """
    await seed_pair(db_session, lip="1_10")
    db_session.add(ConveyorDelivery(site="vmalmyzhe", lip="1_10", status=status))
    await db_session.commit()
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_delivery_of_another_site_does_not_block(db_session):
    """Журнал ведётся по (site, lip) — доставка на ДК не закрывает пост для портала."""
    await seed_pair(db_session, lip="1_10")
    db_session.add(ConveyorDelivery(site="kalinino", lip="1_10", status="delivered"))
    await db_session.commit()
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in out] == ["1_10"]


@pytest.mark.asyncio
async def test_dedup_across_bulletins(db_session):
    """Один пост в двух сводках — одна запись на выходе."""
    await seed_run(db_session, lips=["1_10"], theme="novost")
    await seed_run(db_session, lips=["1_10"], theme="kultura", days_ago=1)
    await seed_audit(db_session, lip="1_10")
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in out] == ["1_10"]


@pytest.mark.asyncio
async def test_limit_is_respected(db_session):
    for i in range(5):
        await seed_pair(db_session, lip=f"1_{i}")
    out = await source.fetch_pending_for_site(db_session, SITE, limit=2)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_site_without_region_yields_nothing(db_session):
    """Полуописанный сайт молчит, а не сыплет данными чужого региона."""
    await seed_pair(db_session, lip="1_10")
    broken = dict(SITE, source_region="")
    assert await source.fetch_pending_for_site(db_session, broken) == []


# ───────── повтор несбывшейся классификации (завал 402, 2026-10-06) ─────────
#
# Строка failed БЕЗ вердикта — «классификация не состоялась», а не решение:
# она возвращается в отбор после паузы, пока не исчерпан лимит попыток.
# С вердиктом, rejected, held — решения, их ведёт человек и retry_failed.


async def _seed_delivery(
    session, *, lip, status, reason="http_402", verdict=None, attempts=0, days_ago=0
):
    from datetime import datetime, timedelta

    row = ConveyorDelivery(
        site="vmalmyzhe",
        lip=lip,
        status=status,
        reason=reason,
        verdict=verdict,
        attempts=attempts,
        updated_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_failed_without_verdict_returns_after_backoff(db_session):
    """Упавшая две недели назад классификация (402) подбирается снова."""
    await seed_pair(db_session, lip="1_10")
    await _seed_delivery(db_session, lip="1_10", status="failed", attempts=2, days_ago=14)
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in out] == ["1_10"]


@pytest.mark.asyncio
async def test_failed_without_verdict_rests_during_backoff(db_session):
    """Пауза между попытками растёт: только что упавшее не долбится каждый прогон."""
    await seed_pair(db_session, lip="1_10")
    await _seed_delivery(db_session, lip="1_10", status="failed", attempts=2, days_ago=0)
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_failed_without_verdict_buried_after_max_attempts(db_session):
    """Лимит попыток — предохранитель от вечного перебора ядовитой строки."""
    await seed_pair(db_session, lip="1_10")
    await _seed_delivery(
        db_session,
        lip="1_10",
        status="failed",
        attempts=source.CLASSIFY_RETRY_MAX,
        days_ago=30,
    )
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_failed_with_verdict_stays_buried(db_session):
    """Классификация состоялась, упала доставка — это вотчина retry_failed."""
    await seed_pair(db_session, lip="1_10")
    await _seed_delivery(
        db_session,
        lip="1_10",
        status="failed",
        verdict={"action": "accept"},
        days_ago=14,
    )
    assert await source.fetch_pending_for_site(db_session, SITE) == []


@pytest.mark.asyncio
async def test_orphan_selected_returns_after_a_day(db_session):
    """Selected без движения сутки — сирота убитого воркера, а не «в работе»."""
    await seed_pair(db_session, lip="1_10")
    await _seed_delivery(db_session, lip="1_10", status="selected", days_ago=2)
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert [p["lip"] for p in out] == ["1_10"]


@pytest.mark.asyncio
async def test_fresh_selected_is_not_stolen(db_session):
    """Свежий selected — идущий прямо сейчас прогон, его не перехватываем."""
    await seed_pair(db_session, lip="1_10")
    await _seed_delivery(db_session, lip="1_10", status="selected", days_ago=0)
    assert await source.fetch_pending_for_site(db_session, SITE) == []


# ───────── record_selection ─────────


@pytest.mark.asyncio
async def test_record_selection_is_idempotent(db_session):
    assert await source.record_selection(db_session, site="vmalmyzhe", lips=["1_10", "1_20"]) == 2
    await db_session.commit()
    assert await source.record_selection(db_session, site="vmalmyzhe", lips=["1_10", "1_30"]) == 1
    await db_session.commit()
    counts = await source.site_status_counts(db_session, site="vmalmyzhe")
    assert counts == {"selected": 3}


@pytest.mark.asyncio
async def test_record_selection_ignores_blanks(db_session):
    assert await source.record_selection(db_session, site="vmalmyzhe", lips=["", "  ", None]) == 0


@pytest.mark.asyncio
async def test_selection_then_fetch_excludes(db_session):
    """Отобранное в прошлом прогоне не приходит снова — цикл сходится."""
    await seed_pair(db_session, lip="1_10")
    first = await source.fetch_pending_for_site(db_session, SITE)
    await source.record_selection(db_session, site="vmalmyzhe", lips=[p["lip"] for p in first])
    await db_session.commit()
    assert await source.fetch_pending_for_site(db_session, SITE) == []


# ───────── summarize_for_prompt ─────────


def test_summarize_mentions_theme_and_media_kinds():
    out = source.summarize_for_prompt(
        {"text": "новость", "theme": "kultura", "media": [{"type": "photo"}, {"type": "doc"}]}
    )
    assert "kultura" in out and "photo" in out and "doc" in out and out.endswith("новость")


def test_summarize_without_text_is_none():
    assert source.summarize_for_prompt({"text": "  ", "theme": "novost"}) is None


def test_summarize_truncates_long_text():
    out = source.summarize_for_prompt({"text": "я" * 5000, "theme": "novost"}, max_chars=100)
    body = out.split("\n", 1)[1]  # считаем хвост, а не всю строку: в шапке своя кириллица
    assert len(body) == 100


# ───────── source_owner_ids ─────────


@pytest.mark.asyncio
async def test_owner_ids_prefilter_keeps_only_named_publics(db_session):
    """Тематический сайт (Казанская) берёт посты РЦКД, а не весь район."""
    await seed_pair(db_session, lip="217788511_10")
    await seed_pair(db_session, lip="111_20")
    site = dict(SITE, key="kazanskaya", source_owner_ids=(-217788511,))
    out = await source.fetch_pending_for_site(db_session, site)
    assert [p["lip"] for p in out] == ["217788511_10"]


@pytest.mark.asyncio
async def test_owner_ids_empty_means_whole_region(db_session):
    await seed_pair(db_session, lip="217788511_10")
    await seed_pair(db_session, lip="111_20")
    out = await source.fetch_pending_for_site(db_session, dict(SITE, source_owner_ids=()))
    assert sorted(p["lip"] for p in out) == ["111_20", "217788511_10"]


def test_only_owners_matches_by_abs_value():
    lt = {"217788511_1": "k", "111_2": "n", "2177885110_3": "n"}
    assert source._only_owners(lt, (217788511,)) == {"217788511_1": "k"}
    assert source._only_owners(lt, ("-217788511",)) == {"217788511_1": "k"}


# ───────── дедуп «одна новость от разных пабликов» (recommend brain 2026-09-14) ─────────

# Одно событие глазами двух пабликов: школа и районная газета. Тексты разные
# дословно, событие одно — ровно тот класс, который портал ловил у себя по
# заголовку и просил снимать раньше.
DUP_A = (
    "В лицее Малмыжа стартовал месячник безопасности дорожного движения. "
    "Первоклассников посвятили в пешеходы, инспектор ГИБДД рассказал о правилах."
)
DUP_B = (
    "В лицее стартовал месячник безопасности дорожного движения: первоклассников "
    "посвятили в пешеходы, а инспектор ГИБДД рассказал школьникам о правилах."
)
DIFFERENT = (
    "Библиотека Малмыжа объявила запись в кружок краеведения. Занятия начнутся "
    "в октябре, ведёт их сотрудник музея, записаться можно по телефону."
)


class TestSplitNearDuplicates:
    def test_same_event_from_two_publics_collapses(self):
        posts = [{"lip": "1_10", "text": DUP_A}, {"lip": "2_20", "text": DUP_B}]
        kept, dropped = source.split_near_duplicates(posts)
        assert len(kept) == 1 and len(dropped) == 1
        assert dropped[0]["dup_of"] == kept[0]["lip"]

    def test_longer_text_wins_regardless_of_order(self):
        """Порядок партии — ``collected_at desc``, про качество он не говорит
        ничего. Победитель обязан определяться содержимым, иначе результат
        зависит от того, чей парсер отработал первым."""
        short = {"lip": "1_10", "text": DUP_A}
        long = {"lip": "2_20", "text": DUP_B + " Мероприятие продолжится до конца месяца."}
        kept_a, _ = source.split_near_duplicates([short, long])
        kept_b, _ = source.split_near_duplicates([long, short])
        assert kept_a[0]["lip"] == "2_20" and kept_b[0]["lip"] == "2_20"

    def test_different_news_survive_both(self):
        posts = [{"lip": "1_10", "text": DUP_A}, {"lip": "2_20", "text": DIFFERENT}]
        kept, dropped = source.split_near_duplicates(posts)
        assert len(kept) == 2 and dropped == []

    def test_duplicate_of_already_delivered_is_dropped(self):
        """Вчера уехало от газеты, сегодня приезжает от школы — платить второй раз незачем."""
        recent = [("9_99", source.dup_signature(DUP_A))]
        kept, dropped = source.split_near_duplicates(
            [{"lip": "1_10", "text": DUP_B}], recent=recent
        )
        assert kept == [] and dropped[0]["dup_of"] == "9_99"

    def test_threshold_is_above_the_measured_noise_floor(self):
        """Замер на 315 живых доставках: ближайшая пара-НЕ-дубль лежит на 0.32.
        Порог ниже неё срезал бы настоящие новости, и навсегда — строка журнала
        закрывает пост от следующих прогонов."""
        assert source.DUP_THRESHOLD > 0.32

    def test_lead_signal_catches_what_full_text_misses(self):
        """Два сигнала, а не один: одинаковое начало при разном хвосте даёт по
        полному тексту меньше порога, по лиду — больше. Это измеренный случай
        (одинаковые заголовки «Педагоги … соревнованиях»: 0.48 против 0.62)."""
        # Общий лид длиннее окна лида, дальше тексты расходятся совсем.
        head = (
            "Педагоги Малмыжского района участвуют в областных туристских соревнованиях "
            "учителей-организаторов туристско-краеведческой работы. Сборная команда "
            "выехала в областной центр в четверг утром, соревнования продлятся три дня "
            "и завершатся в воскресенье подведением итогов на общем построении команд района. "
        )
        assert len(head) > source.DUP_LEAD_CHARS
        tail_a = (
            "Программа включает контрольный туристский маршрут, спортивное ориентирование, "
            "конкурс краеведческих находок, вечернюю игровую эстафету и защиту проектов. "
            "Судейская коллегия оценивает скорость прохождения этапов, точность отметок "
            "на контрольных пунктах, качество снаряжения и слаженность действий группы. "
            "Отдельная номинация посвящена методическим разработкам педагогов-краеведов. "
        )
        tail_b = (
            "Организаторы благодарят спонсоров, предоставивших призы лучшим участникам, "
            "а также водителей автобусов, доставивших делегации со всех уголков региона. "
            "Фотографии выложат в группе профсоюза работников образования после закрытия. "
            "Победители получат путёвки на всероссийский слёт, который пройдёт весной. "
            "Заявки на следующий сезон принимают до пятнадцатого декабря текущего года. "
        )
        a = {"lip": "1_10", "text": head + tail_a}
        b = {"lip": "2_20", "text": head + tail_b}
        sig_a, sig_b = source.dup_signature(a["text"]), source.dup_signature(b["text"])
        from modules.deduplication.fingerprints import jaccard_similarity

        assert jaccard_similarity(sig_a[0], sig_b[0]) < source.DUP_THRESHOLD
        assert source.dup_similarity(sig_a, sig_b) >= source.DUP_THRESHOLD
        assert len(source.split_near_duplicates([a, b])[0]) == 1


@pytest.mark.asyncio
async def test_recent_signatures_take_delivered_only(db_session):
    """``rejected`` в окно не входит: новая копия заслуживает своего вердикта,
    а не унаследованного чужого."""
    await seed_audit(db_session, lip="1_10", text=DUP_A)
    await seed_audit(db_session, lip="2_20", text=DIFFERENT)
    db_session.add(ConveyorDelivery(site="vmalmyzhe", lip="1_10", status="delivered"))
    db_session.add(ConveyorDelivery(site="vmalmyzhe", lip="2_20", status="rejected"))
    await db_session.commit()
    got = await source.fetch_recent_signatures(db_session, site="vmalmyzhe")
    assert [lip for lip, _ in got] == ["1_10"]


@pytest.mark.asyncio
async def test_published_at_reaches_the_caller(db_session):
    """Дата поста в ВК — то, по чему сортируется лента портала (D-091)."""
    from datetime import datetime

    when = datetime(2026, 9, 14, 8, 30, 0)
    await seed_pair(db_session, lip="1_10", published_at=when)
    out = await source.fetch_pending_for_site(db_session, SITE)
    assert out[0]["published_at"] == when
    snaps = await source.fetch_audit_snapshots(db_session, lips=["1_10"])
    assert snaps["1_10"]["published_at"] == when


# ───────── ловля по словам во всём потоке района (заказ владельца 2026-09-14) ─────────

FAIR_TEXT = (
    "В эти выходные в Малмыже пройдёт ярмарка «Казанская»: торговые ряды на площади, "
    "выступления коллективов и детская программа. Начало в десять утра."
)
OFF_TOPIC = (
    "Библиотека Малмыжа объявила запись в кружок краеведения. Занятия начнутся "
    "в октябре, ведёт их сотрудник музея, записаться можно по телефону."
)


class TestSourceFilter:
    def test_own_public_passes_without_keywords(self):
        """Свой паблик отдаёт всё: слова к нему не применяются."""
        assert source.passes_source_filter(
            "217788511_10", OFF_TOPIC, owner_ids=(217788511,), keywords=("сабантуй",)
        )

    def test_foreign_public_passes_on_keyword(self):
        assert source.passes_source_filter(
            "999_10", FAIR_TEXT, owner_ids=(217788511,), keywords=("ярмарк",)
        )

    def test_foreign_public_without_keyword_is_dropped(self):
        assert not source.passes_source_filter(
            "999_10", OFF_TOPIC, owner_ids=(217788511,), keywords=("ярмарк",)
        )

    def test_no_restrictions_means_everything(self):
        assert source.passes_source_filter("999_10", OFF_TOPIC, owner_ids=(), keywords=())

    def test_substring_catches_word_forms(self):
        """«ярмарк» ловит склонения и не зависит от регистра — морфологии у нас нет."""
        for word in ("на ярмарке", "ЯРМАРКА", "ярмарки не будет"):
            assert source.matches_keywords(word, ("ярмарк",))

    def test_stem_alternation_needs_its_own_entry(self):
        """Граница подстрочного подхода, найденная тестом: в «ярмарочный» идёт
        чередование к→ч, и корень «ярмарк» его НЕ ловит. Поэтому в конфиге сайта
        стоят оба корня — это не опечатка."""
        assert not source.matches_keywords("ярмарочный день", ("ярмарк",))
        assert source.matches_keywords("ярмарочный день", ("ярмароч",))

    def test_empty_keywords_never_match(self):
        assert not source.matches_keywords(FAIR_TEXT, ())


@pytest.mark.asyncio
async def test_keywords_widen_source_beyond_named_publics(db_session):
    """Профильный пост ЧУЖОГО паблика доезжает, непрофильный — нет.

    Замер 14.09 показал, зачем это: у тематического сайта свои паблики сезонные
    (стена «Сабантуя» — 153 поста за два года, все в мае–августе), и без ловли
    по словам сайт выглядел бы мёртвым десять месяцев в году.
    """
    await seed_pair(db_session, lip="217788511_10", text=OFF_TOPIC)  # свой паблик
    await seed_pair(db_session, lip="999_20", text=FAIR_TEXT)  # чужой, но про ярмарку
    await seed_pair(db_session, lip="888_30", text=OFF_TOPIC)  # чужой и не про то
    site = dict(
        SITE,
        key="kazanskaya",
        source_owner_ids=(217788511,),
        source_keywords=("ярмарк", "сабантуй"),
    )
    out = await source.fetch_pending_for_site(db_session, site)
    assert sorted(p["lip"] for p in out) == ["217788511_10", "999_20"]


@pytest.mark.asyncio
async def test_keywords_alone_filter_every_public_equally(db_session):
    """Разводка по темам (уточнение владельца 2026-09-14): без ``source_owner_ids``
    профильное слово решает у ВСЕХ источников одинаково — и у своего паблика тоже.

    Прежде лента ДК отдавалась сайту ярмарки целиком, и его афиша шла в модель
    только затем, чтобы получить отказ по одному вызову за пост."""
    await seed_pair(db_session, lip="217788511_10", text=OFF_TOPIC)  # свой паблик, не по теме
    await seed_pair(db_session, lip="217788511_11", text=FAIR_TEXT)  # свой паблик, по теме
    await seed_pair(db_session, lip="999_20", text=FAIR_TEXT)  # чужой, по теме
    site = dict(SITE, key="kazanskaya", source_owner_ids=(), source_keywords=("ярмарк",))
    out = await source.fetch_pending_for_site(db_session, site)
    assert sorted(p["lip"] for p in out) == ["217788511_11", "999_20"]


@pytest.mark.asyncio
async def test_without_keywords_behaviour_is_unchanged(db_session):
    """У сайта без ловли (портал) отбор работает ровно как раньше."""
    await seed_pair(db_session, lip="217788511_10", text=OFF_TOPIC)
    await seed_pair(db_session, lip="999_20", text=FAIR_TEXT)
    site = dict(SITE, key="kazanskaya", source_owner_ids=(217788511,))
    out = await source.fetch_pending_for_site(db_session, site)
    assert [p["lip"] for p in out] == ["217788511_10"]
