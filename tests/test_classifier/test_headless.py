"""Headless-классификация на DeepSeek — замена облачной рутине (D-024).

Эти вердикты гейтят публикацию (``CLASSIFIER_ENFORCE_ENABLED=1`` на проде,
``enforce.fetch_blocked_lips`` блокирует ``delete``/``hold``). Поэтому тесты
здесь стерегут не «модель ответила», а **чего модуль не должен делать**:
записать вердикт на пост, которого не было в пачке; вынести суждение о посте
без текста; склеить посты разных регионов.
"""

from __future__ import annotations

import json

import pytest

from modules.classifier import headless

POSTS = [
    {
        "lip": "1_10",
        "region_code": "mi",
        "text": "Ремонт дороги на Ленина начался",
        "theme": "novost",
    },
    {
        "lip": "1_11",
        "region_code": "mi",
        "text": "Школа откроется в понедельник",
        "theme": "novost",
    },
    {"lip": "2_20", "region_code": "ur", "text": "Ярмарка в субботу", "theme": "afisha"},
]


def _chat(monkeypatch, payload, *, model="deepseek-chat"):
    """Подменить вызов модели; вернуть список захваченных запросов."""
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        body = payload(len(calls)) if callable(payload) else payload
        if isinstance(body, dict) and body.get("__fail__"):
            return {"ok": False, "reason": body["__fail__"]}
        return {
            "ok": True,
            "content": json.dumps(body, ensure_ascii=False),
            "model": model,
            "usage": {"total_tokens": 100},
        }

    monkeypatch.setattr(headless, "chat", fake_chat)
    return calls


def _verdict(lip, action="publish", theme="Новости", **kw):
    out = {"lip": lip, "theme": theme, "action": action, "confidence": 80, "reasoning": "ок"}
    out.update(kw)
    return out


# ───────── нарезка ─────────


def test_timeout_budget_reaches_every_chunk(monkeypatch):
    """Короткий потолок пути публикации обязан дойти до каждого чанка.

    Иначе волна снова ждёт по общему таймауту движка, и правка «не держать
    слот» остаётся на бумаге.
    """
    calls = _chat(
        monkeypatch,
        lambda n: {"verdicts": [{"lip": "1_10", "action": "publish"}]},
    )

    headless.classify_posts(POSTS, postulates="правила", chunk_size=1, timeout=17.0)

    assert calls, "модель должны были позвать"
    assert {c.get("timeout") for c in calls} == {17.0}


def test_chunks_never_mix_regions():
    """Склейка — суждение «это одно событие»; посты разных районов склеивать
    нельзя, а одного района нужно видеть вместе."""
    chunks = headless.chunk_posts(POSTS, size=10)
    assert [sorted({p["region_code"] for p in c}) for c in chunks] == [["mi"], ["ur"]]


def test_chunk_size_splits_within_region():
    posts = [{"lip": f"1_{i}", "region_code": "mi", "text": "x"} for i in range(5)]
    assert [len(c) for c in headless.chunk_posts(posts, size=2)] == [2, 2, 1]


def test_has_text_accepts_both_field_names():
    assert headless.has_text({"text": "a"})
    assert headless.has_text({"post_text": "a"})
    assert not headless.has_text({"text": "   "})
    assert not headless.has_text({})


# ───────── разбор ответа ─────────


def test_unknown_lip_is_dropped():
    """Вердикт на пост, которого не было в пачке, — либо путаница ключей, либо
    выдумка. Записать его значит заблокировать чужую публикацию."""
    payload = {"verdicts": [_verdict("1_10"), _verdict("999_999", action="delete")]}
    verdicts, problems = headless.parse_chunk_response(payload, POSTS[:2])
    assert [v.lip for v in verdicts] == ["1_10"]
    assert any("незнакомый lip" in p for p in problems)


def test_duplicate_verdict_is_dropped():
    payload = {"verdicts": [_verdict("1_10"), _verdict("1_10", action="delete")]}
    verdicts, problems = headless.parse_chunk_response(payload, POSTS[:2])
    assert len(verdicts) == 1
    assert verdicts[0].normalized_action() == "publish"
    assert any("дубль" in p for p in problems)


def test_missing_verdicts_are_reported():
    payload = {"verdicts": [_verdict("1_10")]}
    _, problems = headless.parse_chunk_response(payload, POSTS[:2])
    assert any("без вердикта" in p for p in problems)


def test_merge_targets_outside_batch_are_stripped():
    payload = {"verdicts": [_verdict("1_10", merge_with=["1_11", "8_88", "1_10"])]}
    verdicts, _ = headless.parse_chunk_response(payload, POSTS[:2])
    assert verdicts[0].merge_with == ["1_11"]


def test_snapshot_comes_from_our_posts_not_model_echo():
    """Текст/url/регион подставляем свои: они у нас есть, а просить модель
    переписать текст — платить за него второй раз и получить шанс на искажение."""
    payload = {"verdicts": [_verdict("1_10", text="ВЫДУМАННЫЙ ТЕКСТ", region_code="xx")]}
    verdicts, _ = headless.parse_chunk_response(payload, POSTS[:1])
    assert verdicts[0].text == POSTS[0]["text"]
    assert verdicts[0].region_code == "mi"


def test_response_without_verdicts_list_is_a_problem():
    verdicts, problems = headless.parse_chunk_response({"oops": 1}, POSTS[:1])
    assert verdicts == []
    assert problems


# ───────── прогон ─────────


def test_textless_posts_are_skipped_not_judged(monkeypatch):
    """Текстовая модель не видит вложений. Выдуманный вердикт на такой пост не
    просто бесполезен — он ЗАБЛОКИРУЕТ публикацию (enforce режет delete/hold).
    Пропуск оставляет пост публикующимся по обычным фильтрам."""
    posts = POSTS + [{"lip": "3_30", "region_code": "mi", "text": "", "has_media": True}]
    calls = _chat(monkeypatch, {"verdicts": [_verdict("1_10"), _verdict("1_11"), _verdict("2_20")]})
    run = headless.classify_posts(posts, postulates="ПРАВИЛА")
    assert run["skipped_no_text"] == 1
    assert "3_30" not in "".join(c["user"] for c in calls)
    assert all(v.lip != "3_30" for v in run["verdicts"])


def test_postulates_go_to_system_message_for_prompt_cache(monkeypatch):
    """Постулаты одинаковы для всех чанков прогона — стабильный системный
    префикс кэшируется провайдером; в пользовательской части он оплачивался бы
    заново на каждом чанке."""
    calls = _chat(monkeypatch, {"verdicts": []})
    headless.classify_posts(POSTS, postulates="ПОСТУЛАТЫ-МАРКЕР", chunk_size=1)
    assert len(calls) == 3
    assert all("ПОСТУЛАТЫ-МАРКЕР" in c["system"] for c in calls)
    assert all("ПОСТУЛАТЫ-МАРКЕР" not in c["user"] for c in calls)
    # системный префикс байт-в-байт одинаков — иначе кэш не сработает
    assert len({c["system"] for c in calls}) == 1


def test_json_mode_is_requested(monkeypatch):
    calls = _chat(monkeypatch, {"verdicts": []})
    headless.classify_posts(POSTS[:1], postulates="x")
    assert calls[0]["json_object"] is True


def test_failed_chunk_does_not_cancel_the_rest(monkeypatch):
    """Прогон стоит денег: отказ одного чанка не должен обнулять остальные."""

    def payload(n):
        return {"__fail__": "http_429"} if n == 1 else {"verdicts": [_verdict("2_20")]}

    _chat(monkeypatch, payload)
    run = headless.classify_posts(POSTS, postulates="x", chunk_size=10)
    assert run["failures"] == ["http_429"]
    assert [v.lip for v in run["verdicts"]] == ["2_20"]


def test_unparseable_answer_is_reported_not_raised(monkeypatch):
    def fake_chat(**_kw):
        return {"ok": True, "content": "я не смог", "model": "deepseek-chat", "usage": None}

    monkeypatch.setattr(headless, "chat", fake_chat)
    run = headless.classify_posts(POSTS[:1], postulates="x")
    assert run["failures"] == ["llm_unparseable"]
    assert run["verdicts"] == []


def test_truncated_chunk_is_its_own_failure_and_still_counted(monkeypatch):
    """Упор в max_tokens — отдельная причина, не llm_unparseable (G334), а
    оплаченный обрезанный вызов остаётся в учёте токенов прогона."""

    def fake_chat(**_kw):
        return {
            "ok": False,
            "reason": "truncated",
            "detail": "max_tokens=220",
            "usage": {"total_tokens": 7700},
        }

    monkeypatch.setattr(headless, "chat", fake_chat)
    run = headless.classify_posts(POSTS[:1], postulates="x")
    assert run["failures"] == ["truncated"]
    assert run["tokens"] == 7700, "расход обрезанного вызова выпал из учёта"


def test_model_name_is_stamped_on_verdicts(monkeypatch):
    _chat(monkeypatch, {"verdicts": [_verdict("1_10")]}, model="deepseek-chat")
    run = headless.classify_posts(POSTS[:1], postulates="x")
    assert run["verdicts"][0].model == "deepseek-chat"


def test_json_fenced_answer_is_unwrapped(monkeypatch):
    def fake_chat(**_kw):
        body = json.dumps({"verdicts": [_verdict("1_10")]}, ensure_ascii=False)
        return {"ok": True, "content": f"```json\n{body}\n```", "model": "m", "usage": None}

    monkeypatch.setattr(headless, "chat", fake_chat)
    run = headless.classify_posts(POSTS[:1], postulates="x")
    assert [v.lip for v in run["verdicts"]] == ["1_10"]


def test_tokens_are_summed_across_chunks(monkeypatch):
    _chat(monkeypatch, {"verdicts": []})
    run = headless.classify_posts(POSTS, postulates="x", chunk_size=1)
    assert run["tokens"] == 300


def test_actions_histogram(monkeypatch):
    _chat(
        monkeypatch,
        {"verdicts": [_verdict("1_10", "publish"), _verdict("1_11", "delete")]},
    )
    run = headless.classify_posts(POSTS[:2], postulates="x")
    assert headless.actions_histogram(run["verdicts"]) == {"publish": 1, "delete": 1}


@pytest.mark.parametrize("bad", ["", "publish!", "PUBLISH"])
def test_action_is_normalised_or_falls_back_to_hold(bad):
    payload = {"verdicts": [_verdict("1_10", action=bad)]}
    verdicts, _ = headless.parse_chunk_response(payload, POSTS[:1])
    assert verdicts[0].normalized_action() in ("publish", "hold")


def test_user_prompt_carries_metrics_and_age():
    """Модель решает срочность и важность — значит обязана видеть отклик и возраст.

    До 2026-09-22 в промпт уходил только текст, а рейтинг считался отдельно и
    после; два сигнала о посте нигде не встречались.
    """
    import time

    from modules.classifier.headless import build_user_prompt

    prompt = build_user_prompt(
        [
            {
                "lip": "1_1",
                "region_code": "mi",
                "text": "отключение воды",
                "views": 120,
                "likes": 4,
                "reposts": 1,
                "comments": 2,
                "date": time.time() - 3 * 3600,
            }
        ]
    )

    assert "просмотры 120" in prompt
    assert "лайки 4" in prompt
    assert "репосты 1" in prompt
    assert "комментарии 2" in prompt
    assert "возраст: 3.0 ч" in prompt


def test_user_prompt_prints_a_dash_where_vk_sent_nothing():
    """Прочерк, а не ноль: «не измерено» и «ноль просмотров» — разные сведения,
    и свежий пост нельзя показывать модели как непопулярный."""
    from modules.classifier.headless import build_user_prompt

    prompt = build_user_prompt([{"lip": "1_1", "region_code": "mi", "text": "т"}])

    assert "просмотры —" in prompt
    assert "возраст: —" in prompt


def test_system_prompt_states_what_urgent_means_and_what_it_is_not():
    """Срочность двигает пост вперёд рейтинга, поэтому её границы — в промпте.

    Без явного «криминал и война срочными НЕ считаются» модель охотно пометит
    громкое как срочное, и лента района превратится в сводку происшествий.
    """
    from modules.classifier.headless import TASK_PROMPT

    assert "urgent" in TASK_PROMPT
    assert "отключения" in TASK_PROMPT
    assert "НЕ считаются" in TASK_PROMPT
    assert "importance" in TASK_PROMPT


# --- размер пачки и деление при обрыве JSON (25.10) ------------------------- #


def _many(n: int, region: str = "mi"):
    return [
        {"lip": f"{region}_{i}", "region_code": region, "text": f"пост {i}", "theme": "novost"}
        for i in range(n)
    ]


def _posts_in(user_prompt: str):
    """lip'ы, которые реально уехали в промпт (по строкам ``lip:``)."""
    return [
        line.split("lip: ", 1)[1].strip()
        for line in user_prompt.splitlines()
        if line.startswith("lip: ")
    ]


def test_chunk_default_is_forty(monkeypatch):
    """Дефолт 40: размер чанка — прямой множитель расхода квоты (ceil(n / chunk))."""
    from config.classifier import get_headless_chunk_size

    monkeypatch.delenv("CLASSIFIER_HEADLESS_CHUNK", raising=False)
    assert get_headless_chunk_size() == 40
    assert headless.DEFAULT_CHUNK_SIZE == 40


def test_unparseable_chunk_is_split_in_half_and_retried(monkeypatch):
    """Обрыв JSON на большой пачке не теряет её целиком: делим пополам.

    Вызов уже оплачен, и без деления вердикты пропадали бы по всем постам
    пачки сразу — то есть цена большого чанка падала бы на редком отказе
    целиком, а не частично.
    """
    posts = _many(20)
    calls = []

    def fake_chat(**kw):
        calls.append(kw)
        lips = _posts_in(kw["user"])
        if len(lips) > 10:
            return {"ok": False, "reason": "llm_unparseable", "usage": {"total_tokens": 5000}}
        body = json.dumps({"verdicts": [_verdict(lip) for lip in lips]}, ensure_ascii=False)
        return {"ok": True, "content": body, "usage": {"total_tokens": 100}}

    monkeypatch.setattr(headless, "chat", fake_chat)
    run = headless.classify_posts(posts, postulates="x", chunk_size=20)

    assert len(calls) == 3, "одна большая пачка + две половины"
    assert run["failures"] == []
    assert sorted(v.lip for v in run["verdicts"]) == sorted(p["lip"] for p in posts)
    # Оплаченный неудачный вызов остаётся в учёте расхода.
    assert run["tokens"] == 5000 + 100 + 100


def test_network_failure_is_not_split(monkeypatch):
    """Квота и сеть делением не лечатся — лишние вызовы только жгут квоту."""
    posts = _many(20)
    calls = []

    def fake_chat(**kw):
        calls.append(kw)
        return {"ok": False, "reason": "quota_exhausted", "usage": None}

    monkeypatch.setattr(headless, "chat", fake_chat)
    run = headless.classify_posts(posts, postulates="x", chunk_size=20)

    assert len(calls) == 1
    assert run["failures"] == ["quota_exhausted"]


def test_split_retry_happens_only_once(monkeypatch):
    """Половины тоже не разобрались — пишем два отказа, третьего деления нет."""
    posts = _many(20)
    calls = []

    def fake_chat(**kw):
        calls.append(kw)
        return {"ok": False, "reason": "llm_unparseable", "usage": None}

    monkeypatch.setattr(headless, "chat", fake_chat)
    run = headless.classify_posts(posts, postulates="x", chunk_size=20)

    assert len(calls) == 3
    assert run["failures"] == ["llm_unparseable", "llm_unparseable"]
    assert run["verdicts"] == []
