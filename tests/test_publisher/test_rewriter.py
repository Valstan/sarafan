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

    Мерило — аргумент, который уходит в движок: под нагрузкой именно он
    ограничивает простой единственного слота воркера.

    Потолок переписывания — свой (``REWRITE_TIMEOUT``), а не общий
    ``OPENCODE_PUBLISH_TIMEOUT``: у переписывания свой движок и свой бюджет.
    """
    monkeypatch.delenv("POST_REWRITE_ENABLED", raising=False)
    monkeypatch.setenv("REWRITE_ENGINE", "deepseek")
    monkeypatch.setenv("REWRITE_TIMEOUT", "17")
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(_long()))

    rewriter.rewrite_posts([_post()], region="mi", theme="новости")

    assert calls and calls[0]["timeout"] == 17.0


def test_rewrite_on_minimax_takes_publish_timeout(monkeypatch):
    """На MiniMax потолок переписывания — **пути публикации движка** (180 с).

    Вызов MiniMax — запуск агента-CLI: он идёт десятки секунд прямо на волне
    (замер 17.11: 45–102 с). Общий 40 с обрывал бы его на главном пути —
    потраченные токены без пересказа. Поэтому у MiniMax потолок берётся из
    ``MINIMAX_PUBLISH_TIMEOUT`` (решение владельца 18.11: переписывание тоже
    ушло на MiniMax).
    """
    from config.opencode import get_publish_timeout_for_engine

    monkeypatch.delenv("POST_REWRITE_ENABLED", raising=False)
    monkeypatch.setenv("REWRITE_ENGINE", "minimax")
    monkeypatch.setenv("MINIMAX_PUBLISH_TIMEOUT", "180")
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(_long()))

    rewriter.rewrite_posts([_post()], region="mi", theme="новости")

    # Потолок MiniMax — из пути публикации **этого** движка, а не из
    # CLASSIFIER_ENGINE: без явного движка здесь был бы 40 с (общий HTTP-потолок
    # Дипсика) — обрыв вызова прямо на волне, потраченные токены без пересказа.
    assert calls and calls[0]["timeout"] == 180.0
    assert get_publish_timeout_for_engine("minimax") == 180.0
    assert get_publish_timeout_for_engine() == 40.0  # без CLASSIFIER_ENGINE — Дипсик


def test_default_rewrite_engine_is_minimax(monkeypatch):
    """Весь нейро-трафик — на MiniMax (решение владельца 18.11).

    Платный Дипсик остаётся страховкой на случай отказа MiniMax, а не штатным
    движком переписывания.
    """
    from config.post_rewrite import get_rewrite_engine

    monkeypatch.delenv("REWRITE_ENGINE", raising=False)
    assert get_rewrite_engine() == "minimax"


def test_rewrite_engine_env_overrides_default(monkeypatch):
    """Откат без деплоя: ``REWRITE_ENGINE=deepseek`` возвращает платный движок."""
    from config.post_rewrite import get_rewrite_engine

    monkeypatch.setenv("REWRITE_ENGINE", "deepseek")
    assert get_rewrite_engine() == "deepseek"


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


def test_truncated_answer_keeps_source(monkeypatch):
    """Модель сказала ``finish=length`` — половину предложения в ленту не пускаем.

    Живой случай 21.10: пересказ пришёл оборванным на «…место завалено грудо»
    (775 знаков). По тексту такой ответ неотличим от нормального, поэтому
    решает причина остановки, а не «красивость».
    """
    answer = dict(_ok(_long("Глава района открыл школу.")))
    answer["finish"] = "length"
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: answer)

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.failed == 1
    assert stats.rewritten == 0
    assert stats.reasons == ["truncated"]
    assert post["text"] == source


def test_answer_cut_mid_word_is_rejected_even_without_finish(monkeypatch):
    """Провайдер причину не сообщил — ловим по форме: проза не кончается буквой."""
    body = _long("Синцовская библиотека проводит диктант.") + " Место завалено грудо"
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(body))

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.failed == 1
    assert stats.reasons == ["truncated"]
    assert post["text"] == source


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
    """Платим только за верхушку порядка публикации — и одним вызовом.

    Вызов переписывания — самая дорогая операция на этом пути (у MiniMax это
    запуск агента-CLI: ~16 000 токенов и десятки секунд), поэтому порция волны
    уходит **одним** вызовом, а не по одному на пост (замер 16.11: 358 вызовов
    в сутки, 85 % всех вызовов MiniMax).
    """
    calls = []
    answer = (
        "=== ПОСТ 1 ===\nЗАГОЛОВОК: Первое\nТЕГ: новости\nТЕКСТ:\n"
        + _long("Первый переписан.")
        + "\n\n=== ПОСТ 2 ===\nЗАГОЛОВОК: Второе\nТЕГ: новости\nТЕКСТ:\n"
        + _long("Второй переписан.")
    )
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(answer))

    posts = [_post() for _ in range(5)]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi", limit=2)

    assert len(calls) == 1
    assert stats.attempted == 2
    assert stats.rewritten == 2
    assert stats.failed == 0


def test_batch_rewrite_splits_blocks_by_marker(monkeypatch):
    """Батч: ответ разбирается по меткам, каждый пост — свой пересказ."""
    answer = (
        "=== ПОСТ 1 ===\n"
        "ЗАГОЛОВОК: Школа открылась\n"
        "ТЕГ: образование\n"
        "ТЕКСТ:\n" + _long("Первый пост переписан.") + "\n\n=== ПОСТ 2 ===\n"
        "ЗАГОЛОВОК: Дорогу перекрыли\n"
        "ТЕГ: дороги\n"
        "ТЕКСТ:\n" + _long("Второй пост переписан.")
    )
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(answer))

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert len(calls) == 1
    assert stats.rewritten == 2
    assert posts[0]["text"].startswith("Первый пост переписан.")
    assert posts[1]["text"].startswith("Второй пост переписан.")
    assert posts[0]["headline"] == "Школа открылась"
    assert posts[1]["topic_tag"] == "#дороги"


def test_batch_rewrite_keeps_post_without_its_block(monkeypatch):
    """Блок потерялся, а страховка выключена — пост уходит с исходным текстом.

    Ответ без меток в батче из двух постов не применяется ни к кому: пересказ
    одного поста не должен уехать в другой (подмена текста в ленте). Добить
    пропущенное может только страховка (см. следующий тест), а когда она
    выключена — остаётся fail-open.
    """
    answer = (
        "=== ПОСТ 1 ===\n"
        "ЗАГОЛОВОК: Школа открылась\n"
        "ТЕГ: образование\n"
        "ТЕКСТ:\n" + _long("Первый пост переписан.")
    )
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(answer))
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: False)

    posts = [_post(), _post()]
    source = posts[1]["text"]
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert len(calls) == 1
    assert stats.rewritten == 1
    assert stats.failed == 1
    assert "no_block" in stats.reasons
    assert posts[1]["text"] == source


def test_batch_rewrite_accepts_json_answer(monkeypatch):
    """Агент ответил JSON-массивом — разбор берёт блоки по номеру поста.

    Живой факт 18.11: на постах с упоминаниями и ссылкой MiniMax вместо меток
    ``=== ПОСТ N ===`` отдаёт JSON, где номер поста указан полем ``post``.
    Разбор меток его не видел, и оба поста батча уходили с исходным текстом
    (отказ ``no_block``).
    """
    answer = (
        "```json\n"
        "[\n"
        '  {"post": 1, "ЗАГОЛОВОК": "В ДК открылся новый зал", "ТЕГ": "мероприятие", '
        '"ТЕКСТ": "%s"},\n'
        '  {"post": 2, "ЗАГОЛОВОК": "Дорогу перекрыли", "ТЕГ": "дороги", "ТЕКСТ": "%s"}\n'
        "]\n"
        "```"
    ) % (_long("Первый пост переписан."), _long("Второй пост переписан."))
    calls = []
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: calls.append(kw) or _ok(answer))

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert len(calls) == 1
    assert stats.rewritten == 2
    assert stats.failed == 0
    assert posts[0]["text"].startswith("Первый пост переписан.")
    assert posts[1]["text"].startswith("Второй пост переписан.")
    assert posts[0]["headline"] == "В ДК открылся новый зал"
    assert posts[1]["topic_tag"] == "#дороги"


def test_json_answer_without_post_number_is_not_applied(monkeypatch):
    """JSON без номера поста применять нельзя: пересказ уехал бы не туда."""
    answer = (
        "```json\n" '[{"ЗАГОЛОВОК": "Без номера", "ТЕГ": "новости", "ТЕКСТ": "%s"}]\n' "```"
    ) % _long("Пересказ без номера.")
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(answer))
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: False)

    posts = [_post(), _post()]
    source = posts[0]["text"]
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert stats.rewritten == 0
    assert stats.failed == 2
    assert posts[0]["text"] == source


def test_json_answer_with_repeated_number_is_not_applied(monkeypatch):
    """Два элемента с одним номером — какой чей, по JSON не понять."""
    answer = ("```json\n" '[{"post": 1, "ТЕКСТ": "%s"}, {"post": 1, "ТЕКСТ": "%s"}]\n' "```") % (
        _long("Первый переписан."),
        _long("Второй переписан."),
    )
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(answer))
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: False)

    posts = [_post(), _post()]
    source = posts[0]["text"]
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert stats.rewritten == 0
    assert stats.failed == 2
    assert posts[0]["text"] == source


def test_raw_json_answer_never_becomes_post_text(monkeypatch):
    """Сырой JSON в ленту не попадает — это хуже, чем исходный текст."""
    answer = '```json\n[{"текст": "без номера и без разбора"}\n```'
    monkeypatch.setattr(rewriter, "_chat_call", lambda **kw: _ok(answer))

    post = _post()
    source = post["text"]
    stats = rewriter.rewrite_posts([post], region="mi")

    assert stats.rewritten == 0
    assert post["text"] == source
    assert "json" not in post["text"].lower()


def test_missing_block_is_topped_up_by_paid_engine(monkeypatch):
    """Блок не пришёл — страховка добирает пост платным Дипсиком.

    Живой факт 18.11: на MiniMax ответ иногда приходил без меток
    ``=== ПОСТ N ===``, и оба поста батча уходили с исходным текстом (отказ
    ``no_block``; на Дипсике такого не было ни разу за 129 вызовов 17.11).
    Решение владельца 17.11: «если Минимакс проморгал или не смог — то отдавать
    пропущенное Дипсику».
    """
    calls = []

    def fake(**kw):
        calls.append(kw)
        if len(calls) == 1:
            # Основной движок ответил, но без меток — блоков нет.
            return _ok(_long("Ответ без меток."))
        return _ok(
            "=== ПОСТ 1 ===\nЗАГОЛОВОК: Первое\nТЕГ: новости\nТЕКСТ:\n"
            + _long("Первый переписан.")
            + "\n\n=== ПОСТ 2 ===\nЗАГОЛОВОК: Второе\nТЕГ: новости\nТЕКСТ:\n"
            + _long("Второй переписан.")
        )

    monkeypatch.setattr(rewriter, "_chat_call", fake)
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: True)

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert len(calls) == 2
    # Страховка — платный Дипсик, и это видно прямо в аргументах вызова.
    assert calls[1]["engine"] == "deepseek"
    assert calls[1]["posts"] == 2
    assert stats.rewritten == 2
    assert stats.failed == 0
    assert posts[0]["text"].startswith("Первый переписан.")
    assert posts[1]["text"].startswith("Второй переписан.")


def test_missing_block_falls_back_to_single_post(monkeypatch):
    """Батч платным движком меток тоже не вернул — добираем по одному.

    Ответ на один пост и есть его блок: разбирать там нечего, поэтому
    одиночный вызов — последний рубеж перед fail-open.
    """
    calls = []

    def fake(**kw):
        calls.append(kw)
        if int(kw.get("posts") or 0) > 1:
            return _ok(_long("Ответ без меток."))
        return _ok("ЗАГОЛОВОК: Один\nТЕГ: новости\nТЕКСТ:\n" + _long("Пост переписан."))

    monkeypatch.setattr(rewriter, "_chat_call", fake)
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: True)

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi")

    # Батч → батч страховкой → два одиночных вызова.
    assert len(calls) == 4
    assert calls[-1]["engine"] == "deepseek"
    assert calls[-1]["posts"] == 1
    assert stats.rewritten == 2
    assert stats.failed == 0


def test_missing_block_when_paid_topup_fails_keeps_source(monkeypatch):
    """Страховка не сработала — пост уходит с исходным текстом, отказ учтён."""
    calls = []

    def fake(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return _ok(_long("Ответ без меток."))
        return {"ok": False, "reason": "no_api_key"}

    monkeypatch.setattr(rewriter, "_chat_call", fake)
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: True)

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    stats = rewriter.rewrite_posts(posts, region="mi")

    assert stats.rewritten == 0
    assert stats.failed == 2
    assert "no_block" in stats.reasons
    assert posts[0]["text"] == "x" * 500


def test_paid_topup_uses_its_own_timeout(monkeypatch):
    """Страховка — Дипсик: её потолок 40 с, а не 180 с MiniMax.

    Лишние 140 с стоили бы волне: слот воркера на боксе один.
    """
    monkeypatch.setenv("REWRITE_TIMEOUT", "40")
    monkeypatch.setenv("MINIMAX_PUBLISH_TIMEOUT", "180")
    calls = []

    def fake(**kw):
        calls.append(kw)
        return _ok(_long("Ответ без меток."))

    monkeypatch.setattr(rewriter, "_chat_call", fake)
    monkeypatch.setattr(rewriter, "paid_topup_enabled", lambda: True)

    posts = [_post(), _post()]
    for i, post in enumerate(posts):
        post["id"] = i + 1
    rewriter.rewrite_posts(posts, region="mi")

    assert calls
    assert all(call["timeout"] == 40.0 for call in calls if call.get("engine") == "deepseek")


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
