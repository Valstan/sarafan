"""Поломки еженедельного recheck'а, найденные аудитом 23.11.

Три независимые ошибки, каждая из которых молча портила отчётность:

1. **Цена прогона была пустой.** ``ai_tokens_kwargs`` читала ключ ``usage`` в
   результате категоризатора — а там его нет (он разобран на месте вызова).
   Итог: замер 23.11 показал ``tokens_prompt: 0, tokens_measured: False`` по
   всем 81 региону, при 2,29 млн реально потраченных prompt-токенов. Вопрос
   «сколько это стоит» (P001) висел не из-за отсутствия данных, а из-за
   неверного чтения.
2. **Telegram-отчёт не уходил.** При 81 регионе сообщение длиннее 4096
   символов, а Telegram такой отвергает с кодом 400 — владелец не получил
   ничего, в логе осталась одна строка WARNING.
3. **Прогон занимал воркер на полсети.** На MiniMax (вызов ~10 с против ~1,7 с
   у Дипсика) обход всех регионов растягивается на ~2 часа и наезжает на
   утреннюю волну — поэтому обход разложен по ночным срезам.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from modules.discovery import health_check as hc
from tasks import discovery_tasks as dt

# ───────── цена прогона ─────────


def test_ai_tokens_kwargs_reads_categorizer_fields():
    """Счётчики лежат в полях результата категоризатора, а не в ``usage``."""
    kwargs = hc.ai_tokens_kwargs(
        {"tokens_prompt": 840, "tokens_completion": 53, "engine": "minimax"}
    )
    assert kwargs["tokens_prompt"] == 840
    assert kwargs["tokens_completion"] == 53
    assert kwargs["tokens_known"] is True
    assert kwargs["engine"] == "minimax"


def test_ai_tokens_kwargs_marks_unmeasured_call():
    """Нет счётчиков — это «нечем померить», а не «стоит ноль»."""
    kwargs = hc.ai_tokens_kwargs({"tokens_prompt": None, "tokens_completion": None})
    assert kwargs["tokens_known"] is False
    assert kwargs["tokens_prompt"] == 0


def test_ai_tokens_kwargs_does_not_read_raw_usage():
    """Ключа ``usage`` в результате категоризатора нет — читать его нельзя.

    Именно это давало пустой отчёт о цене на всех 81 регионах.
    """
    kwargs = hc.ai_tokens_kwargs({"usage": {"prompt_tokens": 958, "completion_tokens": 49}})
    assert kwargs["tokens_known"] is False


# ───────── разбивка отчёта Telegram ─────────


def test_split_telegram_text_chunks_long_report():
    """Отчёт на 81 регион не влезает в одно сообщение — режем на части.

    Строки — как в живом отчёте: имя региона и четыре счётчика.
    """
    lines = [
        "  • <b>selo-%02d-dlya-oblasta</b> — 💀 dead: 1, 😴 dormant: 4, 🔀 changed_category: 12" % i
        for i in range(81)
    ]
    text = "<b>Discovery recheck</b>\n" + "\n".join(lines)
    assert len(text) > dt.TELEGRAM_MAX_CHARS
    parts = dt._split_telegram_text(text)
    assert len(parts) > 1
    assert all(len(part) <= dt.TELEGRAM_MAX_CHARS for part in parts)
    # Ни одна строка не потеряна: режем по границам строк.
    joined = "\n".join(parts)
    assert joined.count("selo-") == 81


def test_split_telegram_text_keeps_short_report_intact():
    parts = dt._split_telegram_text("<b>Discovery recheck</b>\n  • <b>mi</b> — dead: 1")
    assert len(parts) == 1
    assert "mi" in parts[0]


# ───────── ночные срезы ─────────


class _FakeSession:
    def __init__(self, plan):
        self._plan = list(plan)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        step = self._plan.pop(0)
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = step["value"]
        result.scalars.return_value = scalars
        return result

    async def commit(self):
        pass


def _regions(count: int):
    out = []
    for i in range(count):
        region = MagicMock()
        region.id = i + 1
        region.code = "r%02d" % i
        out.append(region)
    return out


@pytest.mark.asyncio
async def test_recheck_slices_check_only_one_slice_of_regions():
    """Срез = 1/7 регионов за ночь; каждый регион всё так же ждёт своей недели."""
    session = _FakeSession([{"value": _regions(7)}])
    checked: list[int] = []

    async def fake_region(rid, **kwargs):
        checked.append(rid)
        return {"success": True, "region": "r", "total": 1, "active": 1}

    with (
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "recheck_communities_for_region_async", new=fake_region),
        patch.object(dt, "_maybe_send_recheck_telegram_alert"),
    ):
        out = await dt.recheck_all_active_regions_async(slices=7, slice_index=0)

    assert checked == [1]
    assert out["total_regions"] == 7
    assert out["regions_checked"] == 1
    assert out["slices"] == 7
    assert out["slice_index"] == 0


@pytest.mark.asyncio
async def test_recheck_without_slices_checks_everything():
    """По умолчанию (slices=1) поведение прежнее — обход всех регионов."""
    session = _FakeSession([{"value": _regions(3)}])
    checked: list[int] = []

    async def fake_region(rid, **kwargs):
        checked.append(rid)
        return {"success": True, "region": "r", "total": 1, "active": 1}

    with (
        patch.object(dt, "AsyncSessionLocal", return_value=session),
        patch.object(dt, "recheck_communities_for_region_async", new=fake_region),
        patch.object(dt, "_maybe_send_recheck_telegram_alert"),
    ):
        out = await dt.recheck_all_active_regions_async()

    assert checked == [1, 2, 3]
    assert out["regions_checked"] == 3


def test_recheck_slices_env_override(monkeypatch):
    monkeypatch.setenv(dt.ENV_RECHECK_SLICES, "3")
    assert dt._recheck_slices() == 3
    monkeypatch.setenv(dt.ENV_RECHECK_SLICES, "many")
    assert dt._recheck_slices() == dt.DEFAULT_RECHECK_SLICES
    monkeypatch.delenv(dt.ENV_RECHECK_SLICES, raising=False)
    assert dt._recheck_slices() == dt.DEFAULT_RECHECK_SLICES


# ───────── разбивка по движкам ─────────


def test_merge_engines_across_regions():
    reports = [
        {"engines": {"minimax": 10, "deepseek": 1}},
        {"engines": {"minimax": 5}},
    ]
    assert dt._merge_engines(reports) == {"minimax": 15, "deepseek": 1}


def test_tokens_of_health_counts_engines():
    health = dt.CommunityHealth(
        community_id=1,
        vk_id=10,
        status="active",
        last_post_at=None,
        posts_sampled=3,
        suggested_category=None,
        error_code=None,
        reasoning=None,
        tokens_prompt=800,
        tokens_completion=50,
        tokens_known=True,
        engine="minimax",
    )
    got = dt._tokens_of_health([health])
    assert got["tokens_prompt"] == 800
    assert got["engines"] == {"minimax": 1}
