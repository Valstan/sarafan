"""Пересборка текста новости своими словами перед публикацией (P205, 20.10).

Проверяем не «модель написала красиво», а контракт конвейера: переписанный
текст доезжает до публикации, отказ движка не ломает волну, а платим мы только
за посты, которые реально уходят в ленту.
"""

from modules.publisher import rewriter


def _post(text: str = "x" * 500, **extra):
    post = {"id": 1, "owner_id": -100, "text": text, "views": {"count": 10}}
    post.update(extra)
    return post


def _ok(content: str, tokens: int = 100, model: str = "opencode/m"):
    return {
        "ok": True,
        "content": content,
        "usage": {"total_tokens": tokens},
        "model": model,
    }


def _long(text: str = "Пересказ новости района.") -> str:
    """Годный по форме пересказ: в пределах 300..1400 знаков."""
    return (text + " ") + "Подробности уточняются у администрации района. " * 8


def test_disabled_keeps_source_and_calls_nothing(monkeypatch):
    monkeypatch.setenv("POST_REWRITE_ENABLED", "0")
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(_long()))

    post = _post()
    stats = rewriter.rewrite_posts([post], region="mi", theme="новости")

    assert stats.enabled is False
    assert calls == []
    assert post["text"] == "x" * 500
    assert "text_original" not in post


def test_rewrite_passes_publish_timeout_budget(monkeypatch):
    """Волна даёт движку короткий потолок, а не общий (120 с).

    Мерило — аргумент, который уходит в движок: под нагрузкой free-тира именно
    он ограничивает простой единственного слота воркера.
    """
    monkeypatch.delenv("POST_REWRITE_ENABLED", raising=False)
    monkeypatch.setenv("OPENCODE_PUBLISH_TIMEOUT", "17")
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(_long()))

    rewriter.rewrite_posts([_post()], region="mi", theme="новости")

    assert calls and calls[0]["timeout"] == 17.0


def test_rewrite_replaces_text_and_keeps_original(monkeypatch):
    monkeypatch.delenv("POST_REWRITE_ENABLED", raising=False)
    monkeypatch.setattr(
        rewriter, "_chat_call", lambda **kw: _ok(_long("Глава района открыл школу."), 250)
    )

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi", theme="новости")

    assert stats.rewritten == 1
    assert stats.tokens == 250
    assert post["text_original"] == source
    assert post["text"].startswith("Глава района открыл школу.")
    assert "х" * 100 not in post["text"]


def test_engine_failure_keeps_source(monkeypatch):
    monkeypatch.setattr(
        rewriter,
        "_chat_call",
        lambda **kw: {"ok": False, "reason": "http_402", "usage": None},
    )

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.failed == 1
    assert stats.rewritten == 0
    assert post["text"] == source
    assert "http_402" in stats.reasons


def test_too_short_answer_is_rejected(monkeypatch):
    """Обрубок в три слова — не пересказ новости; публикуем источник."""
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok("Коротко."))

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.failed == 1
    assert post["text"] == source


def test_too_long_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok("я" * 5000))

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.failed == 1
    assert post["text"] == source


def test_short_source_is_not_rewritten(monkeypatch, caplog):
    """Объявление из двух строк пересказывать нечего — токены не тратим."""
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(_long()))

    post = _post("Продам мотоблок, 8900, звоните.")
    with caplog.at_level("INFO", logger="modules.publisher.rewriter"):
        stats = rewriter.rewrite_posts([post], region="mi")

    assert calls == []
    assert stats.skipped_short == 1
    assert stats.attempted == 0
    # Волна, где всё короткое, не должна молчать: иначе «пересказ не сработал»
    # не отличить от «пересказ не включался».
    assert any("коротких=1" in rec.getMessage() for rec in caplog.records)


def test_limit_caps_paid_calls(monkeypatch):
    """Платим только за верхушку порядка публикации."""
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(_long()))

    posts = [_post() for _ in range(5)]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi", limit=2)

    assert len(calls) == 2
    assert stats.rewritten == 2


def test_exception_inside_single_post_does_not_break_wave(monkeypatch):
    def boom(**kw):
        raise RuntimeError("сеть отвалилась")

    monkeypatch.setattr(rewriter, "_chat_call", boom)

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert stats.failed == 2
    assert posts[0]["text"] == "x" * 500


def test_clean_rewrite_strips_wrappers():
    raw = "```\nТекст: «Новость **района** #тег @group»\n```"
    cleaned = rewriter.clean_rewrite(raw)
    assert cleaned == "Новость района"


def test_clean_rewrite_unwraps_markdown_links():
    cleaned = rewriter.clean_rewrite("Открыли школу [подробнее](https://vk.com/x) вчера")
    assert cleaned == "Открыли школу подробнее вчера"


def test_clean_rewrite_drops_repeated_paragraph():
    """Живой прогон 20.10: модель дописывала абзац-пересказ первого."""
    first = "Дети и родители группы «Карамельки» участвовали в конкурсе поделок."
    raw = f"{first}\n\nРодители проявили выдумку и фантазию.\n\n{first}"
    cleaned = rewriter.clean_rewrite(raw)
    assert cleaned.count("Карамельки") == 1
    assert "Родители проявили выдумку" in cleaned


def test_headline_and_tag_are_taken_from_the_contract(monkeypatch):
    """Ответ по контракту: заголовок и тег становятся полями поста."""
    body = _long("Субботник собрал больше ста человек.")
    raw = f"ЗАГОЛОВОК: Субботник на Аджимском кладбище\nТЕГ: субботник\nТЕКСТ:\n{body}"
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(raw))

    post = _post()
    stats = rewriter.rewrite_posts([post], region="mi", theme="kultura")

    assert stats.rewritten == 1
    assert stats.headlines == 1
    assert post["headline"] == "Субботник на Аджимском кладбище"
    assert post["topic_tag"] == "#субботник"
    # Метки контракта не должны попасть в текст поста.
    assert "ЗАГОЛОВОК" not in post["text"]
    assert "ТЕКСТ:" not in post["text"]
    assert post["text"].startswith("Субботник собрал больше ста человек.")


def test_answer_without_headline_still_publishes(monkeypatch):
    """Модель не дала заголовок — пост выходит как раньше, без него."""
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(_long()))

    post = _post()
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.rewritten == 1
    assert stats.headlines == 0
    assert "headline" not in post
    assert "topic_tag" not in post


def test_headline_is_cut_by_word_not_by_letter():
    """Обрубок вида «Глава района открыл шко» владелец назвал придиркой 20.10."""
    raw = "Глава района торжественно открыл новую школу в селе Большая Китя"
    headline = rewriter.clean_headline(raw)
    assert len(headline) <= rewriter.MAX_HEADLINE_CHARS
    assert not headline.endswith("Кит")
    assert raw.startswith(headline)


def test_headline_drops_links_tags_and_emoji():
    raw = "🎉 Открыли школу: [подробнее](https://vk.com/x) #новости @admin"
    headline = rewriter.clean_headline(raw)
    assert "http" not in headline
    assert "#" not in headline
    assert "@" not in headline
    assert headline.startswith("Открыли школу")


def test_generic_topic_tag_is_rejected():
    """«#новости» дублирует тематический тег и раздувает подпись."""
    assert rewriter.clean_topic_tag("новости") == ""
    assert rewriter.clean_topic_tag("культура") == ""
    assert rewriter.clean_topic_tag("субботник") == "#субботник"
    assert rewriter.clean_topic_tag("#ремонт дорог") == "#ремонт_дорог"
    assert rewriter.clean_topic_tag("!!!!") == ""


def test_padding_against_short_source_is_rejected(monkeypatch):
    """Пересказ длиннее источника на треть — это вода, а не пересказ."""
    source = "Дети участвовали в конкурсе поделок. " * 12  # ~430 знаков
    padded = (source * 2)[: int(len(source) * 1.4)]
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(padded))

    post = _post(source)
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.failed == 1
    assert post["text"] == source


def test_prompt_carries_region_and_theme():
    prompt = rewriter.build_prompt("текст", region="Малмыж", theme="культура")
    assert "Малмыж" in prompt
    assert "культура" in prompt
    assert "текст" in prompt
