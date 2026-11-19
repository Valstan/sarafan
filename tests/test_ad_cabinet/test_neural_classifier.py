"""Нейросетевой судья рекламных заявок — границы вмешательства и отказоустойчивость.

Держим три вещи:

- **судья вмешивается только в серую зону** (признак есть, порога не хватает);
  уверенные решения алгоритма он не трогает, а посты без признаков до него не
  доходят — иначе цена вопроса взлетает на два порядка;
- **отказ движка — не отказ детекта**: не ответил движок → решает алгоритм,
  заявка не теряется из-за чужого сбоя;
- **неразобранный или неуверенный ответ игнорируется**, а не трактуется как
  «не реклама».
"""

from __future__ import annotations

import pytest

from modules.ad_cabinet import classifier as ad_classifier
from modules.ad_cabinet import neural_classifier as neural


class TestMode:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("AD_NEURAL_MODE", raising=False)
        assert neural.mode() == neural.MODE_OFF
        assert neural.enabled() is False

    def test_unknown_mode_falls_back_to_off(self, monkeypatch):
        monkeypatch.setenv("AD_NEURAL_MODE", "что-то")
        assert neural.mode() == neural.MODE_OFF
        assert neural.enabled() is False

    def test_assist_is_enabled(self, monkeypatch):
        monkeypatch.setenv("AD_NEURAL_MODE", "assist")
        assert neural.enabled() is True


class TestJsonExtraction:
    def test_plain_json(self):
        payload = neural._extract_json('{"is_ad": true, "confidence": 90, "reason": "да"}')
        assert payload["is_ad"] is True

    def test_markdown_wrapped_json(self):
        raw = '```json\n{"is_ad": false, "confidence": 80, "reason": "нет"}\n```'
        payload = neural._extract_json(raw)
        assert payload["is_ad"] is False

    def test_garbage_is_none(self):
        assert neural._extract_json("извините, я не понял") is None
        assert neural._extract_json("") is None


class TestJudge:
    async def test_engine_failure_returns_none(self, monkeypatch):
        """Нет вердикта — значит «решает алгоритм», а не «рекламы нет»."""
        from modules.classifier import headless

        monkeypatch.setattr(
            headless, "chat", lambda **kw: {"ok": False, "reason": "pool_dead"}
        )
        assert await neural.judge("хочу разместить рекламу") is None

    async def test_unparsable_answer_returns_none(self, monkeypatch):
        from modules.classifier import headless

        monkeypatch.setattr(headless, "chat", lambda **kw: {"ok": True, "content": "мусор"})
        assert await neural.judge("хочу разместить рекламу") is None

    async def test_low_confidence_returns_none(self, monkeypatch):
        from modules.classifier import headless

        monkeypatch.setattr(
            headless,
            "chat",
            lambda **kw: {
                "ok": True,
                "content": '{"is_ad": true, "confidence": 20, "reason": "может быть"}',
            },
        )
        assert await neural.judge("хочу разместить рекламу") is None

    async def test_missing_is_ad_returns_none(self, monkeypatch):
        from modules.classifier import headless

        monkeypatch.setattr(
            headless,
            "chat",
            lambda **kw: {"ok": True, "content": '{"confidence": 90, "reason": "да"}'},
        )
        assert await neural.judge("хочу разместить рекламу") is None

    async def test_confident_answer_is_parsed(self, monkeypatch):
        from modules.classifier import headless

        seen = {}

        def fake_chat(**kw):
            seen.update(kw)
            return {
                "ok": True,
                "content": '{"is_ad": true, "confidence": 92, "reason": "просит прайс"}',
            }

        monkeypatch.setattr(headless, "chat", fake_chat)
        verdict = await neural.judge("хочу разместить рекламу, сколько стоит?")
        assert verdict["is_ad"] is True
        assert verdict["confidence"] == 92
        assert "прайс" in verdict["reason"]
        # Текст уходит вложением, задание — своё (не классификаторское).
        assert seen["task"] == neural.AD_TASK_TEXT
        assert seen["user"] == "хочу разместить рекламу, сколько стоит?"


class TestClassifyIntegration:
    async def test_off_mode_keeps_algorithmic_result(self, monkeypatch):
        """В режиме off ответ — ровно алгоритмический, движок не зовётся.

        «Размещу рекламу» — одно слово о размещении без контакта: алгоритм даёт
        score 2, порог 3, то есть это СЕРАЯ ЗОНА. Решать её должен судья, а в
        режиме off решает алгоритм — то есть не решает вовсе.
        """
        monkeypatch.setenv("AD_NEURAL_MODE", "off")
        called = []

        async def fake_judge(text, **kw):
            called.append(text)
            return {"is_ad": True, "confidence": 95, "reason": "заявка"}

        monkeypatch.setattr(neural, "judge", fake_judge)
        post = {"text": "Размещу рекламу у вас"}
        is_ad, score, reasons = await ad_classifier.classify(post)
        assert not called, "в режиме off нейросеть не должна звать движок"
        assert is_ad is False
        assert score == 2  # серая зона, алгоритм не уверен

    async def test_assist_mode_turns_grey_zone_into_request(self, monkeypatch):
        """Тот же пост, но с судьёй: «да» — и это заявка."""
        monkeypatch.setenv("AD_NEURAL_MODE", "assist")

        async def fake_judge(text, **kw):
            return {"is_ad": True, "confidence": 95, "reason": "просит разместить рекламу"}

        monkeypatch.setattr(neural, "judge", fake_judge)
        post = {"text": "Размещу рекламу у вас"}
        is_ad, score, reasons = await ad_classifier.classify(post)
        assert is_ad is True
        assert score >= ad_classifier.SCORE_THRESHOLD
        assert any("нейросеть" in r for r in reasons)

    async def test_grey_zone_goes_to_neural(self, monkeypatch):
        """Одно слово «реклама» без контакта — серая зона: решает нейросеть."""
        monkeypatch.setenv("AD_NEURAL_MODE", "assist")
        seen = []

        async def fake_judge(text, **kw):
            seen.append(text)
            return {"is_ad": True, "confidence": 90, "reason": "просит разместить рекламу"}

        monkeypatch.setattr(neural, "judge", fake_judge)
        post = {"text": "Хочу рекламу в вашей группе"}
        is_ad, score, reasons = await ad_classifier.classify(post)
        assert seen, "серая зона обязана дойти до нейросети"
        assert is_ad is True
        assert any("нейросеть" in r for r in reasons)

    async def test_neural_says_no_leaves_algorithmic_answer(self, monkeypatch):
        monkeypatch.setenv("AD_NEURAL_MODE", "assist")

        async def fake_judge(text, **kw):
            return {"is_ad": False, "confidence": 95, "reason": "это обычное объявление"}

        monkeypatch.setattr(neural, "judge", fake_judge)
        post = {"text": "Хочу рекламу в вашей группе"}
        is_ad, score, reasons = await ad_classifier.classify(post)
        assert is_ad is False

    async def test_engine_failure_keeps_algorithmic_answer(self, monkeypatch):
        """Движок молчит — решает алгоритм, заявка не теряется."""
        monkeypatch.setenv("AD_NEURAL_MODE", "assist")

        async def fake_judge(text, **kw):
            return None

        monkeypatch.setattr(neural, "judge", fake_judge)
        post = {"text": "Хочу рекламу в вашей группе"}
        is_ad, score, reasons = await ad_classifier.classify(post)
        assert is_ad is False  # алгоритм тоже не уверен — серая зона остаётся серой

    async def test_confident_algo_flag_is_not_rejudged(self, monkeypatch):
        """Уверенное «реклама» от алгоритма нейросеть не пересматривает:
        заявка не должна «исчезнуть» из-за чужого мнения."""
        monkeypatch.setenv("AD_NEURAL_MODE", "assist")
        seen = []

        async def fake_judge(text, **kw):
            seen.append(text)
            return {"is_ad": False, "confidence": 99, "reason": "нет"}

        monkeypatch.setattr(neural, "judge", fake_judge)
        # Контакт + слово о размещении = два признака = уверенная заявка.
        post = {"text": "Размещу рекламу, пишите 89123456789"}
        is_ad, score, reasons = await ad_classifier.classify(post)
        assert not seen, "уверенную алгоритмичную заявку пересматривать нельзя"
        assert is_ad is True
