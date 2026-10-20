"""Тесты хедлайнера (modules/publisher/headliner.py)."""

from modules.publisher.headliner import (
    MAX_LEN,
    MIN_LEN,
    MIN_POOL,
    build_headliner,
    headliner_enabled,
    pick_headliner,
)


def _post(pid=1, text="х" * 200, views=100, likes=5, comments=1, reposts=0, **extra):
    return {
        "id": pid,
        "owner_id": -111,
        "text": text,
        "views": {"count": views},
        "likes": {"count": likes},
        "comments": {"count": comments},
        "reposts": {"count": reposts},
        **extra,
    }


def test_enabled_by_default_and_opt_out():
    assert headliner_enabled(None)
    assert headliner_enabled({})
    assert headliner_enabled({"headliner": True})
    assert not headliner_enabled({"headliner": False})


def test_small_pool_no_headliner():
    posts = [_post(pid=i) for i in range(MIN_POOL - 1)]
    assert pick_headliner(posts) is None


def test_picks_highest_rated_in_range():
    weak = _post(pid=1, likes=0, views=500)
    strong = _post(pid=2, likes=50, comments=10, views=500)
    other = _post(pid=3, likes=1, views=500)
    assert pick_headliner([weak, strong, other])["id"] == 2


def test_skips_out_of_range_text():
    """Простыня и обрывок не годятся, даже если рейтинг у них выше."""
    longread = _post(pid=1, text="х" * (MAX_LEN + 1), likes=999)
    stub = _post(pid=2, text="х" * (MIN_LEN - 1), likes=999)
    normal = _post(pid=3, likes=1)
    assert pick_headliner([longread, stub, normal])["id"] == 3


def test_unmeasured_views_not_headliner():
    """Пост без просмотров (rating=None) не может стать хедлайнером."""
    unmeasured = _post(pid=1, views=None, likes=999)
    unmeasured["views"] = None
    normal = _post(pid=2, likes=1)
    third = _post(pid=3, likes=0)
    assert pick_headliner([unmeasured, normal, third])["id"] == 2


def test_all_out_of_range_returns_none():
    posts = [_post(pid=i, text="х" * 10) for i in range(5)]
    assert pick_headliner(posts) is None


def test_build_headliner_text_and_attachments():
    post = _post(
        text="Важная новость района.",
        attachments=[{"type": "photo", "photo": {"owner_id": -111, "id": 9, "access_key": "k"}}],
    )
    text, atts = build_headliner(post, group_name="Родник", local_hashtag="#опарино")
    assert text.startswith("Важная новость района.")
    assert "Источник: Родник" in text
    assert "vk.com" not in text
    assert "#опарино" in text
    assert "Новости" not in text.split("\n")[0]  # без шапки-заголовка сводки
    assert atts and atts[0].startswith("photo-111_9")


def test_build_headliner_puts_the_headline_first():
    """Завлекающий заголовок — первая строка поста (заказ владельца 20.10)."""
    post = _post(text="Субботник собрал больше ста человек.", headline="Субботник на кладбище")
    text, _ = build_headliner(
        post,
        group_name="Верхнедеревенский сельский клуб",
        local_hashtag="#Малмыж",
        theme_tags=["культура", "малмыжский_район"],
    )
    lines = text.splitlines()
    assert lines[0] == "Субботник на кладбище"
    assert lines[1] == ""
    assert "Субботник собрал больше ста человек." in text
    assert "#культура" in text and "#малмыжский_район" in text and "#Малмыж" in text
    assert "Источник: Верхнедеревенский сельский клуб" in text
    assert text.rstrip().splitlines()[-1].startswith("#")


def test_build_headliner_caps_hashtags():
    post = _post(text="Текст.", topic_tag="#субботник")
    text, _ = build_headliner(
        post,
        theme_tags=["новости", "район", "лишний1", "лишний2"],
        local_hashtag="#Малмыж",
    )
    tags_line = text.rstrip().splitlines()[-1]
    assert len(tags_line.split()) <= 5


def test_build_headliner_hide_attribution():
    post = _post(
        text="Текст новости для проверки атрибуции и её отключения тут.", hide_attribution=True
    )
    text, _ = build_headliner(post, group_name="Родник", local_hashtag="")
    assert "Родник" not in text and "vk.com" not in text


def _rated(post_id, *, likes, views=100, text_len=300):
    """Пост с предсказуемым рейтингом.

    Формула делит отклик на просмотры (``post_rating``), поэтому «больше
    просмотров» само по себе рейтинг НЕ поднимает — разводим посты лайками при
    равных просмотрах.
    """
    return {
        "owner_id": -post_id,
        "id": post_id,
        "text": "о" * text_len,
        "views": {"count": views},
        "likes": {"count": likes},
        "comments": {"count": 0},
        "reposts": {"count": 0},
    }


def test_without_exclusions_the_headliner_picks_the_top_rated():
    posts = [_rated(1, likes=10), _rated(2, likes=100), _rated(3, likes=1)]

    assert pick_headliner(posts)["id"] == 2


def test_urgent_post_is_not_stolen_by_the_headliner():
    """Хедлайнер ВЫНИМАЕТ пост из пула сводки.

    При потолке в одну новость забрать срочное значило бы оставить сводку либо
    пустой, либо с другой новостью вместо аварии. Срочное место — в сводке.

    Взят ровно тот пост, который без исключения хедлайнер бы и выбрал (тест
    выше это фиксирует), иначе проверка прошла бы сама собой.
    """
    posts = [_rated(1, likes=10), _rated(2, likes=100), _rated(3, likes=1)]

    picked = pick_headliner(posts, exclude_lips={"2_2"})

    assert picked is not None, "исключение срочного не должно отменять хедлайнер целиком"
    assert picked["id"] == 1, "должен быть взят следующий по рейтингу, а не срочный"
