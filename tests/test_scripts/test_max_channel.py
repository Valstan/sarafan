"""Тесты скрипта MAX-канала (`scripts/max_channel.py`).

Сеть не трогаем: проверяются чистые части — тело подписки, разбор кодов двери
и проверка «задана ли переменная окружения» без печати значения.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "max_channel.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("max_channel", _SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_subscription_payload_has_url_types_and_secret(mod):
    """Тело подписки: url + update_types + secret."""
    payload = json.loads(
        mod.subscription_payload("https://x.example/api/max/webhook", "sekret").decode()
    )
    assert payload["url"] == "https://x.example/api/max/webhook"
    assert payload["update_types"] == mod.UPDATE_TYPES
    assert payload["secret"] == "sekret"


def test_subscription_asks_for_message_created(mod):
    """``bot_started`` платформа шлёт один раз — нужен второй источник чата.

    Без ``message_created`` чат не выучится, если владелец открыл бота до
    подписки: событие ``bot_started`` тогда просто потерялось.
    """
    assert "message_created" in mod.UPDATE_TYPES


def test_subscription_payload_is_bytes(mod):
    """Подписка уходит POST-ом — тело должно быть байтами."""
    assert isinstance(mod.subscription_payload("https://x/y", "s"), bytes)


def test_explain_code_200_is_alive(mod):
    assert "живой" in mod.explain_webhook_code(200)


def test_explain_code_503_means_secret_not_set(mod):
    """503 — секрет на сервере не задан (не «маршрута нет»)."""
    text = mod.explain_webhook_code(503)
    assert "503" in text
    assert "секрет" in text.lower()


def test_explain_code_401_means_secret_mismatch(mod):
    assert "401" in mod.explain_webhook_code(401)


def test_explain_code_404_means_stale_web_service(mod):
    """404 — веб поднят на старом коде: маршрута ещё нет."""
    assert "рестарт" in mod.explain_webhook_code(404)


def test_has_reports_presence_without_value(mod, monkeypatch):
    """Проверка env говорит только «есть / нет» — значение не уходит наружу."""
    monkeypatch.setenv("MAX_TOKEN", "секретное-значение")
    assert mod._has("MAX_TOKEN") is True
    monkeypatch.delenv("MAX_TOKEN", raising=False)
    assert mod._has("MAX_TOKEN") is False
    monkeypatch.setenv("MAX_TOKEN", "   ")
    assert mod._has("MAX_TOKEN") is False


def test_secret_fits_max_accepts_plain_token(mod):
    """То, что MAX принимает: латиница, цифры, дефис, подчёркивание."""
    assert mod.secret_fits_max("a1b2c3d4e5f6") is True
    assert mod.secret_fits_max("my-secret_0123456789") is True


def test_secret_fits_max_rejects_phrase_with_spaces(mod):
    """Фраза с пробелами — ровно то, что MAX отверг кодом 400 на подписке."""
    assert mod.secret_fits_max("мой секретный код") is False
    assert mod.secret_fits_max("короткий") is False  # кириллица — тоже не годится
    assert mod.secret_fits_max("код") is False  # короче пяти символов


def test_describe_secret_fit_says_what_is_wrong(mod):
    """Владельцу нужен человеческий ответ, а не pattern от API."""
    text = mod.describe_secret_fit("мой секрет")
    assert "НЕТ" in text
    assert "пробел" in text.lower()
    assert "мой" not in text  # значение секрета не печатается
    assert "да" in mod.describe_secret_fit("a1b2c3d4")


def test_subscribe_refuses_secret_that_max_would_reject(mod, monkeypatch):
    """Негодный секрет — не поход в API, а понятный отказ."""
    monkeypatch.setenv("MAX_TOKEN", "token")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "мой секрет")

    def _no_network(*a, **k):  # pragma: no cover — сеть трогаться не должна
        raise AssertionError("к API обращаться нельзя")

    monkeypatch.setattr(mod, "_http", _no_network)
    assert mod.subscribe("https://x.example/api/max/webhook") == 1


def test_check_flags_secret_format(mod, monkeypatch, capsys):
    """Проверка готовности сама говорит, что секрет MAX не примет."""
    monkeypatch.setenv("MAX_TOKEN", "token")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "мой секрет")
    monkeypatch.setattr(mod, "_learned_chat_id", lambda: "chat-1")
    monkeypatch.setattr(mod, "_http", lambda *a, **k: (200, b""))

    assert mod.check("https://x.example/api/max/webhook") == 1
    out = capsys.readouterr().out
    assert "формату MAX: НЕТ" in out
    assert "секрет подписки не по формату MAX" in out


def test_load_env_file_reads_pairs_without_breaking_systemd(mod, tmp_path, monkeypatch):
    """env-файл читается в окружение; уже заданное systemd не перебивается."""
    env_file = tmp_path / "setka.env"
    env_file.write_text(
        "# комментарий\n"
        "\n"
        "MAX_WEBHOOK_SECRET=секрет\n"
        'AD_NEURAL_MODE="assist"\n'
        "ЗНАЧЕНИЕ_С_ПРОБЕЛАМИ = с пробелами\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MAX_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("AD_NEURAL_MODE", raising=False)
    monkeypatch.delenv("ЗНАЧЕНИЕ_С_ПРОБЕЛАМИ", raising=False)
    monkeypatch.setenv("MAX_TOKEN", "из-systemd")

    assert mod.load_env_file(str(env_file)) == 3
    assert os.environ["MAX_WEBHOOK_SECRET"] == "секрет"
    assert os.environ["AD_NEURAL_MODE"] == "assist"
    assert os.environ["ЗНАЧЕНИЕ_С_ПРОБЕЛАМИ"] == "с пробелами"
    # systemd сильнее файла: значение не перебито
    assert os.environ["MAX_TOKEN"] == "из-systemd"


def test_load_env_file_missing_file_is_not_fatal(mod, tmp_path, monkeypatch):
    """Отсутствующий env-файл — не повод падать: скрипт живёт дальше."""
    monkeypatch.delenv("MAX_TOKEN", raising=False)
    assert mod.load_env_file(str(tmp_path / "нет-такого.env")) == 0
