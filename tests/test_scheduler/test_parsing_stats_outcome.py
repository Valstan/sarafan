"""Исход строки `ParsingStats` по результату волны (P175).

Инвариант: ноль «не мерили» отличается от нуля «померили и там пусто».
Ранние выходы конвейера возвращают dict без ключа `stats` — по нему пишется
`success=False` с причиной из `error`/`message`, а не `success=True` с нулями.
"""

from tasks.parsing_scheduler_tasks import resolve_stats_outcome


def test_early_exit_without_stats_is_failure_with_message():
    # Каскад «нет детей»: success=True, но замера не было.
    ok, err, stats = resolve_stats_outcome(
        {
            "success": True,
            "message": "no active children for region kirov_obl",
            "posts_published": 0,
            "bulletins_count": 0,
            "stats": {},
        }
    )
    assert ok is False
    assert err == "no active children for region kirov_obl"
    assert stats == {}


def test_missing_stats_key_is_failure_with_fallback_message():
    ok, err, stats = resolve_stats_outcome({"success": True})
    assert ok is False
    assert "no stats" in (err or "")
    assert stats == {}


def test_failure_without_stats_keeps_error_text():
    # «Нет токенов»: раньше error терялся (поле оставалось NULL).
    ok, err, stats = resolve_stats_outcome(
        {
            "success": False,
            "error": "No active VK READ tokens (all in cooldown?)",
        }
    )
    assert ok is False
    assert err == "No active VK READ tokens (all in cooldown?)"
    assert stats == {}


def test_measured_success_unchanged():
    payload = {"total_groups_checked": 5, "total_posts_scanned": 60}
    ok, err, stats = resolve_stats_outcome({"success": True, "stats": payload})
    assert ok is True
    assert err is None
    assert stats is payload


def test_measured_empty_is_still_success():
    # Честно пустой замер: мерили, нашли ноль — success=True сохраняется.
    payload = {"total_groups_checked": 3, "total_posts_scanned": 0}
    ok, err, stats = resolve_stats_outcome({"success": True, "stats": payload})
    assert ok is True
    assert err is None


def test_measured_failure_propagates_error():
    payload = {"total_groups_checked": 2, "total_posts_scanned": 10}
    ok, err, stats = resolve_stats_outcome({"success": False, "error": "boom", "stats": payload})
    assert ok is False
    assert err == "boom"
    assert stats is payload


def test_measured_failure_without_error_text():
    payload = {"total_groups_checked": 1}
    ok, err, _ = resolve_stats_outcome({"success": False, "stats": payload})
    assert ok is False
    assert err is None


def test_foreign_stats_keys_are_kept_as_measured():
    # Кругозор меряет своими ключами (sources/targets/...) — поведение не меняем:
    # раз stats непуст, исход и success остаются авторскими.
    payload = {"sources": 0, "targets": 0, "items": 0, "published": 0}
    ok, err, stats = resolve_stats_outcome(
        {"success": True, "message": "no krugozor sources", "stats": payload}
    )
    assert ok is True
    assert err is None
    assert stats is payload
