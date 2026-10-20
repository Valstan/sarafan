"""Подпись источника: имя без ссылки (решение владельца 20.10)."""

from utils.post_utils import extract_source_attribution


def test_extract_source_attribution_is_name_only():
    post = {"owner_id": -123456, "id": 789}
    s = extract_source_attribution(post, "Новости района")
    assert s == "Источник: Новости района"
    assert "vk.com" not in s


def test_extract_source_attribution_keeps_pipe_as_plain_text():
    """Вертикальная черта больше не ломает разметку: это обычный текст."""
    post = {"owner_id": -1, "id": 2}
    s = extract_source_attribution(post, "A|B")
    assert s == "Источник: A|B"


def test_extract_source_attribution_without_name_is_empty():
    """Не знаем, чей пост, — подпись не ставим вовсе.

    «Источник: Источник» — хуже, чем отсутствие подписи: он выглядит как
    потерянные данные, а не как их отсутствие.
    """
    post = {"owner_id": 100, "id": 5}
    assert extract_source_attribution(post, "") == ""
    assert extract_source_attribution(post, "   ") == ""


def test_post_counts_unwraps_both_vk_shapes():
    """ВК отдаёт счётчики то словарём, то голым числом — разбор один на проект.

    Две копии разошлись бы молча: модель судила бы по одним числам, а отбор
    ранжировал по другим.
    """
    from utils.post_utils import post_counts

    counts = post_counts(
        {"views": {"count": 120}, "likes": 4, "comments": {"count": 2}, "reposts": 0}
    )

    assert counts == {"views": 120, "likes": 4, "comments": 2, "reposts": 0}


def test_post_counts_keeps_none_apart_from_zero():
    """«ВК ничего не прислал» и «ноль просмотров» — разные сведения.

    Ноль вместо None отправил бы пост без измерений в рейтинг как непопулярный,
    вместо того чтобы честно уехать в хвост.
    """
    from utils.post_utils import post_counts

    counts = post_counts({"likes": 0})

    assert counts["views"] is None
    assert counts["likes"] == 0
    assert counts["reposts"] is None


def test_post_counts_survives_garbage():
    from utils.post_utils import post_counts

    assert post_counts({"views": "много", "likes": None})["views"] is None


def test_post_lip_prefers_the_ready_value_and_falls_back_to_ids():
    """lip считается ТОЙ ЖЕ формулой, что у парсера и аудита сбора.

    Своя формула где-либо была бы тихой поломкой: сравнивались бы ключи,
    которых нет в БД, и совпадений не находилось бы никогда.
    """
    from utils.post_utils import lip_of_post, post_lip

    assert post_lip({"lip": "готовый_1"}) == "готовый_1"
    assert post_lip({"owner_id": -123, "id": 456}) == lip_of_post(-123, 456)
    assert post_lip({"text": "нет идентификаторов"}) == ""
