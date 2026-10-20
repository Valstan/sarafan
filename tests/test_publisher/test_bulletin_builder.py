"""BulletinBuilder behaviour (empty header, group_names lookup)."""

from modules.publisher.bulletin_builder import BulletinBuilder


def test_post_starts_with_the_news_text_itself():
    """Ни шапки, ни маркера — пост начинается тем, ради чего его читают.

    Маркер ✍ снят 22.09 вместе с шапками (решение владельца: «чистые посты, как
    в оригинале»). Первый экран — официально названный сигнал выдачи ВК, и
    занимать его служебным символом нечем.
    """
    posts = [
        {
            "owner_id": -100,
            "id": 1,
            "text": "Текст новости",
            "likes": {"count": 0},
            "comments": {"count": 0},
            "reposts": {"count": 0},
        }
    ]
    b = BulletinBuilder(header="", hashtags=["тест"], local_hashtag="#тест", max_text_length=4096)
    r = b.build_bulletin(posts, group_names={"100": "Группа тест"})
    assert not r.text.startswith("📰")
    assert "✍" not in r.text
    assert r.text.startswith("Текст новости")
    # Подпись источника — название без ссылки (решение владельца 20.10).
    assert "Источник: Группа тест" in r.text
    assert "vk.com" not in r.text
    assert "#тест" in r.text


def test_explicit_empty_string_not_replaced_by_default_header():
    b = BulletinBuilder(header="")
    assert b.header == ""


def test_empty_header_without_hashtags_has_no_footer_tags():
    posts = [
        {
            "owner_id": -100,
            "id": 2,
            "text": "Текст без хештегов",
            "likes": {"count": 0},
            "comments": {"count": 0},
            "reposts": {"count": 0},
        }
    ]
    b = BulletinBuilder(header="", hashtags=[], local_hashtag="", max_text_length=4096)
    r = b.build_bulletin(posts, group_names={"100": "Группа тест"})
    assert "#тест" not in r.text
    assert "#" not in r.text


def _stub_post(post_id: int, text: str, *, owner_id: int = -100) -> dict:
    return {
        "owner_id": owner_id,
        "id": post_id,
        "text": text,
        "likes": {"count": 0},
        "comments": {"count": 0},
        "reposts": {"count": 0},
    }


def test_all_posts_empty_text_yields_empty_bulletin():
    """Whitespace-only text → empty BulletinResult, no header/hashtag leak."""
    posts = [_stub_post(1, "   "), _stub_post(2, ""), _stub_post(3, "\n\n")]
    b = BulletinBuilder(
        header="Физическое развитие:",
        hashtags=["спортМалмыж"],
        local_hashtag="#малмыж",
        max_text_length=4096,
    )
    r = b.build_bulletin(posts, group_names={"100": "Группа"})
    assert r.text == ""
    assert r.post_count == 0
    assert r.posts_included == []
    assert r.attachments_list == []
    # The reported regression: header + hashtags must NOT slip through alone
    assert "Физическое развитие" not in r.text
    assert "#спортМалмыж" not in r.text


def test_no_posts_fit_yields_empty_bulletin():
    """All candidate posts individually exceed max_text_length → empty bulletin, not header-only."""
    long = "Очень длинный текст. " * 200  # ~4000 chars × 3 posts > 4096 each + header
    posts = [_stub_post(i, long) for i in range(1, 4)]
    b = BulletinBuilder(
        header="📰 Заголовок",
        hashtags=["новости"],
        local_hashtag="#локал",
        max_text_length=200,  # tight cap — no post can possibly fit
    )
    r = b.build_bulletin(posts, group_names={"100": "Группа"})
    assert r.text == ""
    assert r.post_count == 0
    assert "Заголовок" not in r.text
    assert "#новости" not in r.text


def test_at_least_one_post_fits_produces_normal_bulletin():
    """Sanity: when at least one post fits, header/hashtag/body are all present."""
    posts = [_stub_post(1, "Короткий валидный текст")]
    b = BulletinBuilder(
        header="Заголовок:",
        hashtags=["тег"],
        local_hashtag="#локал",
        max_text_length=4096,
    )
    r = b.build_bulletin(posts, group_names={"100": "Группа"})
    assert r.post_count == 1
    assert "Заголовок" in r.text
    assert "Короткий валидный текст" in r.text
    assert "#тег" in r.text


# ───────── формат поста 20.10: заголовок, имя источника, теги ─────────


def test_headline_goes_above_the_text():
    post = _stub_post(1, "Текст новости про субботник.")
    post["headline"] = "Субботник собрал сто человек"
    b = BulletinBuilder(header="", hashtags=[], local_hashtag="")
    r = b.build_bulletin([post], group_names={"100": "Верхнедеревенский клуб"})

    assert r.text.startswith("Субботник собрал сто человек\n\nТекст новости про субботник.")
    assert "Источник: Верхнедеревенский клуб" in r.text


def test_no_headline_no_blank_line():
    """Без заголовка пост начинается текстом — пустая строка сверху не нужна."""
    post = _stub_post(1, "Текст новости.")
    b = BulletinBuilder(header="", hashtags=[], local_hashtag="")
    r = b.build_bulletin([post], group_names={"100": "Группа"})

    assert r.text.startswith("Текст новости.")


def test_unknown_source_leaves_no_empty_attribution():
    """Имени нет — подписи нет вообще, а не «Источник: » с пустотой."""
    post = _stub_post(1, "Текст новости.")
    b = BulletinBuilder(header="", hashtags=[], local_hashtag="")
    r = b.build_bulletin([post], group_names={})

    assert "Источник" not in r.text
    assert not r.text.rstrip().endswith("\n")


def test_topical_hashtags_follow_theme_and_are_capped():
    post = _stub_post(1, "Текст новости.")
    b = BulletinBuilder(
        header="",
        hashtags=["новости", "малмыжский_район"],
        local_hashtag="#Малмыж",
        topical_hashtags=["#субботник", "#благоустройство"],
    )
    r = b.build_bulletin([post], group_names={"100": "Группа"})

    tags_line = r.text.rstrip().splitlines()[-1]
    assert (
        tags_line.split()
        == [
            "#новости",
            "#малмыжский_район",
            "#субботник",
            "#благоустройство",
            "#Малмыж",
        ][: BulletinBuilder.MAX_HASHTAGS]
    )


def test_duplicate_and_case_variants_are_deduplicated():
    """«#малмыж #Малмыж» — ровно та недоделанная подпись, которую забраковали."""
    post = _stub_post(1, "Текст новости.")
    b = BulletinBuilder(header="", hashtags=["Малмыж"], local_hashtag="#малмыж")
    r = b.build_bulletin([post], group_names={"100": "Группа"})

    tags_line = r.text.rstrip().splitlines()[-1]
    assert len(tags_line.split()) == 1


# ───────── сортировка по рейтингу (звено 5, шаг 2) ─────────


def _p(pid, views, likes, comments=0, reposts=0):
    return {
        "owner_id": -100,
        "id": pid,
        "text": f"post {pid}",
        "views": {"count": views} if views is not None else None,
        "likes": {"count": likes},
        "comments": {"count": comments},
        "reposts": {"count": reposts},
    }


def test_sort_follows_post_rating_with_configured_alpha(monkeypatch):
    """alpha=0 = чистое вовлечение: 100 лайков при 10k просмотров обгоняют
    12 лайков при 20 просмотрах (при 0.5 было бы наоборот — гейт ниже)."""
    monkeypatch.setenv("RATING_VIEWS_ALPHA", "0")
    b = BulletinBuilder(header="")
    out = b._sort_by_popularity([_p(1, 20, 12), _p(2, 10_000, 100)])
    assert [p["id"] for p in out] == [2, 1]


def test_alpha_05_reproduces_old_post_popularity_order(monkeypatch):
    """Гейт «формула вырождается в нынешнюю»: при 0.5 порядок старый."""
    monkeypatch.setenv("RATING_VIEWS_ALPHA", "0.5")
    b = BulletinBuilder(header="")
    out = b._sort_by_popularity([_p(1, 20, 12), _p(2, 10_000, 100)])
    assert [p["id"] for p in out] == [1, 2]


def test_post_without_views_goes_to_tail_not_top(monkeypatch):
    """Раньше отсутствующие views считались нулём, делитель схлопывался в 1,
    и пост без единого просмотра обгонял районный хит."""
    monkeypatch.setenv("RATING_VIEWS_ALPHA", "0.25")
    b = BulletinBuilder(header="")
    no_views = {
        "owner_id": -100,
        "id": 3,
        "text": "post 3",
        "likes": {"count": 50},
        "comments": {"count": 0},
        "reposts": {"count": 0},
    }
    out = b._sort_by_popularity([no_views, _p(1, 1000, 5)])
    assert [p["id"] for p in out] == [1, 3]


# ───────── свежее впереди устаревшего (заказ владельца 2026-08-30) ─────────


def _aged(pid, hours, views):
    import time

    return {
        "owner_id": -100,
        "id": pid,
        "text": f"post {pid}",
        "date": int(time.time() - hours * 3600),
        "views": {"count": views},
        "likes": {"count": views},
        "comments": {"count": 0},
        "reposts": {"count": 0},
    }


def test_fresh_post_outranks_an_older_hit(monkeypatch):
    """«Свежак должен первым выходить» — дословное требование владельца.

    Раньше порядок задавал только рейтинг, и вчерашний хит с тысячей лайков
    обгонял сегодняшнюю новость. Теперь возраст — старший ключ сортировки.
    """
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "24")
    from modules.publisher.bulletin_builder import BulletinBuilder

    builder = BulletinBuilder()
    out = builder._sort_by_popularity([_aged(1, 40, 1000), _aged(2, 2, 5)])
    assert [p["id"] for p in out] == [2, 1]


def test_within_the_same_freshness_group_rating_still_decides(monkeypatch):
    # Для волны без устаревших постов поведение не меняется вовсе.
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "24")
    from modules.publisher.bulletin_builder import BulletinBuilder

    builder = BulletinBuilder()
    out = builder._sort_by_popularity([_aged(1, 2, 5), _aged(2, 3, 500)])
    assert [p["id"] for p in out] == [2, 1]


def test_zero_threshold_disables_the_freshness_split(monkeypatch):
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "0")
    from modules.publisher.bulletin_builder import BulletinBuilder

    builder = BulletinBuilder()
    out = builder._sort_by_popularity([_aged(1, 40, 1000), _aged(2, 2, 5)])
    assert [p["id"] for p in out] == [1, 2]


def test_post_without_date_is_treated_as_fresh(monkeypatch):
    # До сборщика такой пост доходит только в обход возрастного фильтра — это
    # дефект данных, и наказывать за него местом в сводке незачем.
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "24")
    from modules.publisher.bulletin_builder import BulletinBuilder

    stale = _aged(1, 40, 1000)
    undated = _aged(2, 2, 5)
    undated.pop("date")
    builder = BulletinBuilder()
    out = builder._sort_by_popularity([stale, undated])
    assert [p["id"] for p in out] == [2, 1]


def test_freshness_threshold_stays_inside_the_candidate_window(monkeypatch):
    """Порог «свежего» обязан быть МЕНЬШЕ окна кандидатов, иначе он ничего не делит.

    Пока окно было 72 часа, порог 24 честно делил кандидатов на две группы.
    С окном в сутки (решение владельца 2026-09-22) порог в те же сутки делает
    признак «свежий» истинным для ВСЕХ допущенных постов: ключ сортировки
    вырождается, и «свежак первым» молча превращается в чистый рейтинг — то
    есть ровно в то, от чего владелец уходил, сокращая окно.

    Тест сторожит соотношение, а не числа: двигать можно оба, схлопывать — нет.
    """
    monkeypatch.delenv("BULLETIN_FRESH_HOURS", raising=False)

    from config.runtime import get_bulletin_fresh_hours
    from modules.vk_monitor.advanced_parser import BULLETIN_MAX_POST_AGE_HOURS

    fresh = get_bulletin_fresh_hours()

    assert fresh > 0, "ноль отключает деление на свежие и несвежие совсем"
    assert fresh < BULLETIN_MAX_POST_AGE_HOURS, (
        f"порог свежести {fresh} ч не меньше окна кандидатов "
        f"{BULLETIN_MAX_POST_AGE_HOURS} ч — деление вырождается"
    )


def _post_with(lip_owner, lip_id, *, views, age_hours, text="новость про район"):
    import time

    return {
        "owner_id": lip_owner,
        "id": lip_id,
        "text": text,
        "views": {"count": views},
        "likes": {"count": 0},
        "comments": {"count": 0},
        "reposts": {"count": 0},
        "date": time.time() - age_hours * 3600,
    }


def test_urgent_post_outranks_a_fresh_district_hit(monkeypatch):
    """Срочное впереди рейтинга — решение владельца 2026-09-22.

    У сообщения об аварии, вывешенного десять минут назад, нет ни просмотров,
    ни лайков: по рейтингу оно проигрывает чему угодно. Ровно поэтому одного
    рейтинга для срочного мало.
    """
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "6")
    hit = _post_with(-1, 1, views=50000, age_hours=1, text="районный хит")
    outage = _post_with(-2, 2, views=3, age_hours=0.2, text="отключение воды до вечера")

    builder = BulletinBuilder(urgent_lips={"2_2"})
    order = builder._sort_by_popularity([hit, outage])

    assert order[0] is outage, "срочное обязано идти первым"


def test_without_the_urgent_set_order_is_exactly_as_before(monkeypatch):
    """Пустое множество (нет вердиктов, движок отказал) не меняет ничего."""
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "6")
    hit = _post_with(-1, 1, views=50000, age_hours=1, text="районный хит")
    outage = _post_with(-2, 2, views=3, age_hours=0.2, text="отключение воды до вечера")

    assert BulletinBuilder()._sort_by_popularity([hit, outage])[0] is hit
    assert BulletinBuilder(urgent_lips=set())._sort_by_popularity([hit, outage])[0] is hit


def test_urgent_beats_freshness_too(monkeypatch):
    """Ступени именно в этом порядке: срочное → свежее → рейтинг."""
    monkeypatch.setenv("BULLETIN_FRESH_HOURS", "6")
    fresh = _post_with(-1, 1, views=100, age_hours=0.5, text="свежая новость")
    stale_urgent = _post_with(-2, 2, views=1, age_hours=20, text="отключение света")

    order = BulletinBuilder(urgent_lips={"2_2"})._sort_by_popularity([fresh, stale_urgent])

    assert order[0] is stale_urgent


def test_order_for_publication_puts_urgent_first_then_rating():
    """Порядок для пересказа совпадает с порядком публикации.

    Пересказ платный, поэтому переписываем верхушку этого порядка: срочное
    вперёд, дальше по рейтингу. Тест держит именно совпадение с сортировкой
    самой сборки — разойтись они не имеют права.
    """
    posts = [
        {"owner_id": -1, "id": 1, "text": "обычный", "views": {"count": 1000}},
        {"owner_id": -1, "id": 2, "text": "срочный", "views": {"count": 0}},
        {"owner_id": -1, "id": 3, "text": "слабый", "views": {"count": 5}},
    ]
    b = BulletinBuilder(header="", urgent_lips={"1_2"})
    order = b.order_for_publication(posts)
    assert [p["id"] for p in order][0] == 2
    # Тот же порядок, что уйдёт в публикацию: сортировка одна и та же.
    built = b.build_bulletin(posts, group_names={})
    assert built.posts_included[0] == "1_2"
