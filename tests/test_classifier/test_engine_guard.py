"""Предохранитель движка и его рубеж в ``headless.chat``.

Зачем тесты именно такие: предохранитель — механизм деградации, а у деградации
нет симптома, пока она не сработает. Поэтому проверяется не «функция вернула
что-то», а три исхода по отдельности: открылся под лимитом, не открылся на
негодном ответе модели, закрылся сам.

Живой повод (21.10.2026): 371 отказ http_429 и 156 http_403 за 48 часов;
пересказ одного поста занимал 30–120 с, потому что цепочка трёх моделей с
двумя попытками ждала заведомо неудачные ответы в единственном слоте воркера.
"""

from __future__ import annotations

import pytest

from modules import engine_guard
from modules.classifier import headless


class _FakeTime:
    """Управляемое время: ``monotonic`` двигаем руками, без sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    engine_guard.reset()
    monkeypatch.delenv("ENGINE_BREAKER_ENABLED", raising=False)
    monkeypatch.delenv("ENGINE_BREAKER_FAILS", raising=False)
    monkeypatch.delenv("ENGINE_BREAKER_COOLDOWN", raising=False)
    yield
    engine_guard.reset()


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(engine_guard, "time", fake)
    return fake


def _fail(reason: str, times: int) -> None:
    for _ in range(times):
        engine_guard.note_result(False, reason)


def test_opens_after_threshold_and_skips_calls(clock):
    _fail("http_429", 2)
    assert engine_guard.allow() is True, "двух отказов мало для открытия"

    _fail("http_429", 1)
    assert engine_guard.allow() is False, "третий отказ подряд обязан открыть предохранитель"

    snap = engine_guard.snapshot()
    assert snap["open"] is True
    assert snap["opens"] == 1
    assert snap["last_reason"] == "http_429"
    assert snap["skipped"] >= 1


def test_success_closes_early(clock, monkeypatch):
    monkeypatch.setenv("ENGINE_BREAKER_COOLDOWN", "300")
    _fail("http_429", 3)
    assert engine_guard.allow() is False

    engine_guard.note_result(True)
    assert engine_guard.allow() is True, "движок ответил — ждать остаток паузы незачем"


def test_unusable_answer_does_not_open_breaker(clock):
    # Модель ответила, но ответ не по форме: это не лимит capacity.
    _fail("llm_unparseable", 10)
    _fail("empty_response", 10)
    assert engine_guard.allow() is True


def test_breaker_closes_when_cooldown_expires(clock, monkeypatch):
    monkeypatch.setenv("ENGINE_BREAKER_COOLDOWN", "300")
    _fail("http_429", 3)
    assert engine_guard.allow() is False

    clock.advance(299)
    assert engine_guard.allow() is False

    clock.advance(2)
    assert engine_guard.allow() is True
    assert engine_guard.snapshot()["failures_in_row"] == 0, "после паузы счёт начинается заново"


def test_zero_cooldown_disables_opening(clock, monkeypatch):
    monkeypatch.setenv("ENGINE_BREAKER_COOLDOWN", "0")
    _fail("http_429", 5)
    assert engine_guard.allow() is True


def test_env_switch_off(clock, monkeypatch):
    monkeypatch.setenv("ENGINE_BREAKER_ENABLED", "0")
    _fail("http_429", 10)
    assert engine_guard.allow() is True
    assert engine_guard.snapshot()["enabled"] is False


def test_threshold_is_configurable(clock, monkeypatch):
    monkeypatch.setenv("ENGINE_BREAKER_FAILS", "1")
    _fail("timeout", 1)
    assert engine_guard.allow() is False


@pytest.fixture
def engine(monkeypatch):
    """Подменить движок на управляемый и запомнить, звали ли его."""
    calls = []

    def fake_engine(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "content": "{}", "model": "fake"}

    import modules.opencode_engine as opencode_engine

    monkeypatch.setattr(headless, "engine_enabled", lambda: True)
    monkeypatch.setattr(opencode_engine, "chat", fake_engine)
    return calls


def test_chat_does_not_touch_engine_while_breaker_is_open(engine):
    _fail("http_429", 3)

    result = headless.chat(user="три поста", system="постулаты")

    assert result == {"ok": False, "reason": "breaker_open"}
    assert engine == [], "открытый предохранитель обязан снимать вызов до сети"


def test_chat_reports_throttle_and_opens_breaker(engine, monkeypatch):
    monkeypatch.setattr(headless, "engine_enabled", lambda: True)

    import modules.opencode_engine as opencode_engine

    def limited(**kwargs):
        return {"ok": False, "reason": "http_429", "detail": "Rate limit exceeded"}

    monkeypatch.setattr(opencode_engine, "chat", limited)

    for _ in range(3):
        result = headless.chat(user="пост")
        assert result["ok"] is False

    assert engine_guard.allow() is False


def test_chat_success_resets_counter(engine, monkeypatch):
    _fail("http_429", 2)
    headless.chat(user="пост")
    assert engine_guard.snapshot()["failures_in_row"] == 0


def test_quota_opens_breaker_immediately_with_long_fuse(clock, monkeypatch):
    """Квота аккаунта — свой класс: один отказ открывает, пауза длинная.

    Замер 24.10: квота держится сутками и сменой выхода не снимается. Порог в
    три отказа здесь вреден — второй и третий отказ стоят запросов, а сообщают
    то же самое.
    """
    monkeypatch.setenv("ENGINE_BREAKER_COOLDOWN", "300")
    monkeypatch.setenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")

    engine_guard.note_result(False, "quota_exhausted")
    assert engine_guard.allow() is False, "квота обязана закрывать вызовы сразу"

    snap = engine_guard.snapshot()
    assert snap["last_reason"] == "quota_exhausted"
    assert snap["quota_cooldown"] == 1800.0

    # Обычная пятиминутная пауза квоту не отпускает.
    clock.advance(600)
    assert engine_guard.allow() is False

    clock.advance(1300)
    assert engine_guard.allow() is True


def test_headless_passes_quota_flag_to_breaker(engine, clock, monkeypatch):
    """Флаг ``quota`` из движка становится отдельной причиной предохранителя."""
    monkeypatch.setenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")
    monkeypatch.setattr(
        headless,
        "engine_enabled",
        lambda: True,
    )
    import modules.opencode_engine as opencode_engine

    monkeypatch.setattr(
        opencode_engine,
        "chat",
        lambda **kw: {"ok": False, "reason": "engine_all_failed", "quota": True},
    )

    result = headless.chat(user="u")

    assert result["ok"] is False
    assert engine_guard.allow() is False
    assert engine_guard.snapshot()["last_reason"] == "quota_exhausted"


# --- 29.10: смена выхода ВПН снимает предохранитель, поставленный на старый адрес ---


def test_rotation_lifts_quota_breaker(clock, monkeypatch):
    """Живой счёт 27.10: 7 из 8 прогонов умерли ``breaker_open`` при 17 поворотах.

    Предохранитель ставился на прежний адрес и про поворот не знал — остаток
    прогона короткозамыкался уже после того, как выход был другой.
    """
    monkeypatch.setenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")
    engine_guard.note_result(False, "quota_exhausted")
    assert engine_guard.allow() is False

    engine_guard.rotation_happened("http_429: Free usage limit reached")

    assert engine_guard.allow() is True, "выход другой — держать прежнюю паузу незачем"
    assert engine_guard.snapshot()["rotations"] == 1


def test_rotation_does_not_fake_a_success(clock, monkeypatch):
    """Снятие паузы — не выдача успеха за успех: новый адрес тоже проверяется."""
    monkeypatch.setenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")
    engine_guard.note_result(False, "quota_exhausted")
    engine_guard.rotation_happened("http_429")

    engine_guard.note_result(False, "quota_exhausted")

    assert engine_guard.allow() is False, "второй 429 обязан открыть предохранитель снова"


def test_rotation_without_open_breaker_changes_nothing(clock):
    engine_guard.rotation_happened("gate")

    snap = engine_guard.snapshot()
    assert snap["rotations"] == 0
    assert snap["open"] is False


def test_rotate_egress_lifts_breaker_on_success(clock, monkeypatch, tmp_path):
    """Проводка: успешный поворот выхода сам снимает предохранитель."""
    from modules import opencode_engine as engine

    class _Proc:
        returncode = 0
        stdout = "egress: KZ -> DE\n"
        stderr = ""

    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_CMD", "rotate-egress --next")
    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_COOLDOWN", "600")
    monkeypatch.setenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")
    monkeypatch.setattr(engine.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **kw: _Proc())

    engine_guard.note_result(False, "quota_exhausted")
    assert engine_guard.allow() is False

    assert engine._rotate_egress("http_429: Free usage limit reached") is True

    assert engine_guard.allow() is True
    assert engine_guard.snapshot()["rotations"] == 1


def test_rotate_egress_keeps_breaker_on_failure(clock, monkeypatch, tmp_path):
    """Неудачный поворот паузу не снимает: ждать честно, а не гадать."""
    from modules import opencode_engine as engine

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "no free server"

    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_CMD", "rotate-egress --next")
    monkeypatch.setenv("NEURAL_EGRESS_ROTATE_COOLDOWN", "600")
    monkeypatch.setenv("ENGINE_BREAKER_QUOTA_COOLDOWN", "1800")
    monkeypatch.setattr(engine.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **kw: _Proc())

    engine_guard.note_result(False, "quota_exhausted")

    assert engine._rotate_egress("http_429") is False
    assert engine_guard.allow() is False
    assert engine_guard.snapshot()["rotations"] == 0
