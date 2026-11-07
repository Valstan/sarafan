"""Тесты LLM-стадии конвейера (DeepSeek).

Живых вызовов нет — весь HTTP подменяется. Тесты держат то, что решает судьбу
текста на публичном сайте: запрет на дописывание фактов, отказ вместо падения,
и то, что неизвестная рубрика не отбрасывает пост.
"""

from __future__ import annotations

import json

import pytest

from modules.conveyor import classify

SECTIONS = ("novosti", "afisha", "zhkh")
POST = {
    "lip": "1_10",
    "text": "В Малмыже отремонтировали улицу Ленина, работы шли две недели.",
    "theme": "novost",
    "media": [{"type": "photo", "url": "https://cdn/1.jpg"}],
}


def _api(status, payload):
    """Подмена HTTP-вызова фиксированным ответом."""

    # `label` появился вместе с учётом префикс-кэша (мандат brain 2026-08-30):
    # конвейер помечает свои вызовы, чтобы его доля кэша не смешивалась с чужой.
    def _fake(body, *, api_key, base_url, timeout, label="deepseek"):
        _fake.body = body
        _fake.label = label
        return status, payload

    return _fake


def _completion(obj):
    return {
        "choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}],
        "usage": {"total_tokens": 321},
    }


# ───────── промпт ─────────


class TestPrompt:
    def test_system_prompt_forbids_inventing_facts(self):
        """Правило «фактов не добавлять» — из мандата; проверяем, что оно в промпте."""
        assert "НЕ добавляй фактов" in classify.SYSTEM_PROMPT

    def test_user_prompt_lists_sections_and_context(self):
        out = classify.build_user_prompt(POST, sections=SECTIONS)
        assert "novosti, afisha, zhkh" in out
        assert "novost" in out and "photo" in out
        assert "улицу Ленина" in out

    def test_site_rules_are_included_when_given(self):
        out = classify.build_user_prompt(POST, sections=SECTIONS, rules="Не берём соболезнования.")
        assert "Не берём соболезнования." in out

    def test_long_post_is_truncated(self):
        out = classify.build_user_prompt({"text": "я" * 9000}, sections=SECTIONS, max_chars=100)
        body = out.split("Текст поста:\n", 1)[1]  # хвост, а не вся строка: в шапке своя кириллица
        assert len(body) == 100

    # ── префикс-кэш (R29, мандат brain 2026-08-30) ──
    # Сторож на порядок частей: перестановка строк не меняет ни ответов модели,
    # ни логов — только счёт. Без теста единственный сигнал — упавшая доля кэша
    # на проде через сутки.

    RULES = "Не берём соболезнования.\nАфишу — только с датой.\n" * 20

    def test_site_stable_prefix_is_identical_across_posts(self):
        """Два поста одного сайта с разной темой и вложениями → один префикс."""
        a = classify.build_user_prompt(
            {**POST, "theme": "novost", "media": [{"type": "photo"}]},
            sections=SECTIONS,
            rules=self.RULES,
        )
        b = classify.build_user_prompt(
            {**POST, "theme": "afisha", "media": [{"type": "video"}], "text": "другой текст"},
            sections=SECTIONS,
            rules=self.RULES,
        )
        assert classify.stable_prefix(a) == classify.stable_prefix(b)
        # Префикс — это не пустая шапка: правила сайта внутри него, а не после.
        assert "Не берём соболезнования." in classify.stable_prefix(a)

    def test_variable_parts_come_after_rules(self):
        """Тема, вложения и текст стоят ПОСЛЕ правил — иначе правила платятся заново."""
        out = classify.build_user_prompt(POST, sections=SECTIONS, rules=self.RULES)
        rules_at = out.index("Правила этого сайта:")
        assert rules_at < out.index("Тема сводки района:")
        assert rules_at < out.index("Вложения поста:")
        assert rules_at < out.index("Текст поста:")


# ───────── разбор ответа ─────────


class TestExtractJson:
    def test_plain_json(self):
        assert classify._extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fenced_json(self):
        """Обёртка в ```json — частая привычка моделей, ронять из-за неё оплаченный вызов жалко."""
        assert classify._extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_chatter_around(self):
        assert classify._extract_json('Вот результат: {"a": 1} — готово') == {"a": 1}

    @pytest.mark.parametrize("bad", ["", "   ", "не json вовсе", "[1,2,3]", "{битый"])
    def test_unparseable_is_none(self, bad):
        assert classify._extract_json(bad) is None


class TestParseVerdict:
    def test_accept_is_parsed(self):
        v, err = classify.parse_verdict(
            {"action": "accept", "section": "novosti", "title": "З", "text": "Т"},
            POST,
            sections=SECTIONS,
        )
        assert err is None and v["section"] == "novosti" and v["section_known"] is True

    def test_reject_carries_reason(self):
        v, err = classify.parse_verdict(
            {"action": "reject", "reason": "реклама"}, POST, sections=SECTIONS
        )
        assert v is None and err.startswith("llm_reject:") and "реклама" in err

    @pytest.mark.parametrize("action", ["", "maybe", "ACCEPTED_MAYBE"])
    def test_unknown_action_is_refused(self, action):
        v, err = classify.parse_verdict({"action": action}, POST, sections=SECTIONS)
        assert v is None and err == "llm_bad_action"

    def test_missing_title_is_refused(self):
        v, err = classify.parse_verdict({"action": "accept", "text": "Т"}, POST, sections=SECTIONS)
        assert v is None and err == "llm_no_title"

    def test_missing_text_is_refused(self):
        v, err = classify.parse_verdict({"action": "accept", "title": "З"}, POST, sections=SECTIONS)
        assert v is None and err == "llm_no_text"

    def test_unknown_section_passes_but_is_flagged(self):
        """Неизвестный slug — не отказ: приёмник его тоже не отвергает, а директива
        запрещает молча подгонять поток под черновой список рубрик."""
        v, err = classify.parse_verdict(
            {"action": "accept", "section": "sport", "title": "З", "text": "Т"},
            POST,
            sections=SECTIONS,
        )
        assert err is None and v["section"] == "sport" and v["section_known"] is False


class TestNoInventedFacts:
    def test_inflated_rewrite_is_refused(self):
        """Ответ вдвое длиннее исходника — это уже не чистка, а сочинение."""
        src = "факт. " * 100  # 600 символов
        v, err = classify.parse_verdict(
            {"action": "accept", "title": "З", "text": src + "и ещё столько же " * 40},
            {"text": src},
            sections=SECTIONS,
        )
        assert v is None and err == "llm_text_inflated"

    def test_short_post_growth_is_allowed(self):
        """Короткий пост от заголовка и абзацев растёт в долях — наказывать не за что."""
        v, err = classify.parse_verdict(
            {"action": "accept", "title": "З", "text": "а" * 300},
            {"text": "а" * 100},
            sections=SECTIONS,
        )
        assert err is None and v is not None

    def test_shortening_is_fine(self):
        src = "текст. " * 100
        v, err = classify.parse_verdict(
            {"action": "accept", "title": "З", "text": "текст. " * 30},
            {"text": src},
            sections=SECTIONS,
        )
        assert err is None and v is not None


# ───────── вызов ─────────


class TestClassify:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setattr(
            classify,
            "_call_api",
            _api(
                200,
                _completion({"action": "accept", "section": "novosti", "title": "З", "text": "Т"}),
            ),
        )
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert out["ok"] and out["verdict"]["title"] == "З"
        assert out["usage"]["total_tokens"] == 321

    def test_request_shape_is_json_mode_and_low_temperature(self, monkeypatch):
        fake = _api(200, _completion({"action": "accept", "title": "З", "text": "Т"}))
        monkeypatch.setattr(classify, "_call_api", fake)
        classify.classify(POST, sections=SECTIONS, api_key="k")
        assert fake.body["response_format"] == {"type": "json_object"}
        assert fake.body["temperature"] == 0.2
        assert fake.body["messages"][0]["role"] == "system"

    def test_thinking_is_disabled_in_request(self, monkeypatch):
        """Конвейер собирает тело запроса сам — выключатель раздумий нужен и здесь.

        Без него явное имя модели включает «раздумья», они съедают бюджет
        ответа (1200 токенов на пост), JSON обрывается, и пост остаётся без
        классификации: замер 07.11 — 277 обрезанных вызовов за сутки.
        """
        monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
        fake = _api(200, _completion({"action": "accept", "title": "З", "text": "Т"}))
        monkeypatch.setattr(classify, "_call_api", fake)

        classify.classify(POST, sections=SECTIONS, api_key="k")

        assert fake.body["thinking"] == {"type": "disabled"}

    def test_missing_key_is_refusal_not_crash(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        out = classify.classify(POST, sections=SECTIONS)
        assert out == {"ok": False, "reason": "no_api_key"}

    def test_empty_text_short_circuits(self):
        out = classify.classify({"text": "  "}, sections=SECTIONS, api_key="k")
        assert out["reason"] == "empty_text"

    def test_http_error_is_reported(self, monkeypatch):
        monkeypatch.setattr(classify, "_call_api", _api(429, {"error": "rate limited"}))
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert not out["ok"] and out["reason"] == "http_429"

    def test_network_failure_is_reported(self, monkeypatch):
        monkeypatch.setattr(classify, "_call_api", _api(0, {"error": "timeout"}))
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert not out["ok"] and out["reason"] == "network"

    def test_unparseable_answer_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            classify, "_call_api", _api(200, {"choices": [{"message": {"content": "здрасьте"}}]})
        )
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert not out["ok"] and out["reason"] == "llm_unparseable"

    def test_length_cutoff_is_truncated_not_unparseable(self, monkeypatch):
        """Оборванный по max_tokens JSON — отдельная причина (G334): она ляжет в
        ``conveyor_deliveries.reason`` и должна отличаться от мусора в ответе."""
        payload = {
            "choices": [
                {
                    "message": {"content": '{"action": "accept", "title": "З'},
                    "finish_reason": "length",
                }
            ],
            "usage": {"total_tokens": 1200},
        }
        monkeypatch.setattr(classify, "_call_api", _api(200, payload))
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert not out["ok"] and out["reason"] == "truncated"
        assert out["usage"]["total_tokens"] == 1200

    def test_empty_choices_is_reported(self, monkeypatch):
        monkeypatch.setattr(classify, "_call_api", _api(200, {"choices": []}))
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert not out["ok"] and out["reason"] == "llm_unparseable"

    def test_reject_keeps_usage_for_accounting(self, monkeypatch):
        monkeypatch.setattr(
            classify, "_call_api", _api(200, _completion({"action": "reject", "reason": "не наше"}))
        )
        out = classify.classify(POST, sections=SECTIONS, api_key="k")
        assert not out["ok"] and out["usage"]["total_tokens"] == 321


class TestUnknownSectionsFeedback:
    def test_collects_flagged_slugs_once(self):
        results = [
            {"ok": True, "verdict": {"section": "sport", "section_known": False}},
            {"ok": True, "verdict": {"section": "sport", "section_known": False}},
            {"ok": True, "verdict": {"section": "novosti", "section_known": True}},
            {"ok": False, "reason": "network"},
        ]
        assert classify.collect_unknown_sections(results) == ["sport"]

    def test_empty_input(self):
        assert classify.collect_unknown_sections([]) == []
