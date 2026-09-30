"""Tests for modules.secrets_bootstrap — vault recovery client (scenario A).

Покрывают обязательные свойства спеки vault-client.md: no-op в норме (ноль
сетевых вызовов), allowlist вместо «всё, что пришло» (мутационная приёмка:
NODE_OPTIONS/LD_PRELOAD не пролезают), bootstrap-конфиг вне allowlist, локальное
значение сильнее, best-effort (нет токена / vault недоступен → WARNING, не падаем),
логирование неучтённых ключей только по именам.
"""

from __future__ import annotations

import logging

import pytest

from modules import secrets_bootstrap as sb

# Адрес хранилища в коде больше не зашит (это инфра-деталь, AGENTS.md), поэтому
# тесты задают его через окружение — ровно так, как это делает прод. Хост
# ``vault.example`` зарезервирован стандартом и ничего не резолвит.
_VAULT_URL = "https://vault.example/api/secrets"


def _bootstrap_env(env):
    """Дописать адрес хранилища в то же окружение, которое клиент будет менять.

    Мутируем на месте — как на проде, где это ``os.environ``: тесты проверяют
    содержимое того же словаря, который передали.
    """
    env.setdefault("SECRETS_VAULT_URL", _VAULT_URL)
    return env


def _bootstrap(env, **kw):
    """``bootstrap_secrets`` с адресом в окружении — как на проде."""
    return sb.bootstrap_secrets(env=_bootstrap_env(env), **kw)


def test_no_token_means_no_network(monkeypatch):
    """Без ``SECRETS_TOKEN`` в vault не ходим ни в каком режиме.

    Раньше этот случай отсекался раньше — проверкой целостности локального env
    (``local-env-intact``). С режимом «комната как источник» (владелец 2026-08-09)
    целый env больше не повод молчать, и единственный барьер здесь — отсутствие
    токена. Сетевых вызовов по-прежнему ноль, что тест и держит.
    """
    calls = []

    def _fake_fetch(*a, **k):  # pragma: no cover — не должен вызываться
        calls.append(a)
        return {}

    env = {"DATABASE_URL": "postgresql+asyncpg://u:p@h/db", "REDIS_URL": "redis://h:1/0"}
    monkeypatch.setattr(sb, "_fetch_secrets", _fake_fetch)

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["reason"] == "no-token"
    assert res["recovered"] == 0
    assert calls == []


def test_missing_vault_address_is_reported_not_silently_skipped(monkeypatch):
    """Нет ``SECRETS_VAULT_URL`` — приложение продолжает старт, но причина названа.

    Раньше адрес был зашит в код, и такая ошибка была невозможна; теперь, когда
    он приходит из окружения, «нет адреса» обязано звучать в логе, иначе старт
    упадёт позже и совсем по другому поводу — на REQUIRED-секретах.
    """
    calls = []

    def _fake_fetch(*a, **k):  # pragma: no cover — не должен вызываться
        calls.append(a)
        return {}

    monkeypatch.setattr(sb, "_fetch_secrets", _fake_fetch)
    env = {"SECRETS_TOKEN": "tok"}
    res = sb.bootstrap_secrets(env=env)
    assert res["reason"] == "no-url"
    assert res["recovered"] == 0
    assert calls == []


def test_vault_address_taken_from_env(monkeypatch):
    """Адрес берётся из окружения — значение в подставленном URL приходит из него."""
    seen = {}

    def _fake_fetch(token, url):
        seen["url"] = url
        return {}

    monkeypatch.setattr(sb, "_fetch_secrets", _fake_fetch)
    env = {
        "SECRETS_TOKEN": "tok",
        "SECRETS_VAULT_URL": "https://vault.example/api/secrets",
    }
    sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert seen["url"] == "https://vault.example/api/secrets"


def test_recovers_missing_required_from_vault(monkeypatch):
    """Сценарий A: нет DATABASE_URL/REDIS_URL → берём из vault."""
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "SETKA_WEB_SECRET": "svc",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["reason"] == "recovered"
    assert res["recovered"] == 3
    assert env["DATABASE_URL"].startswith("postgresql+asyncpg")
    assert env["REDIS_URL"].startswith("redis://")
    assert env["SETKA_WEB_SECRET"] == "svc"


def test_foreign_keys_ignored_by_allowlist(monkeypatch):
    """Свойство 2 (мутационная приёмка): чужой ключ из vault не проезжает.

    RCE-сценарий образца trener — NODE_OPTIONS / LD_PRELOAD: даже если они
    лежат в комнате, клиент их НЕ пишет в окружение.
    """
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "NODE_OPTIONS": "--require /proc/self/environ",
            "LD_PRELOAD": "/tmp/evil.so",
            "PYTHONPATH": "/tmp/evil",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["recovered"] == 2  # только DATABASE_URL + REDIS_URL
    assert sorted(res["ignored"]) == ["LD_PRELOAD", "NODE_OPTIONS", "PYTHONPATH"]
    assert "NODE_OPTIONS" not in env
    assert "LD_PRELOAD" not in env
    assert "PYTHONPATH" not in env


def test_bootstrap_config_never_accepted_from_vault(monkeypatch):
    """Свойство 6: SECRETS_TOKEN / SECRETS_VAULT_URL принципиально вне allowlist."""
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "SECRETS_TOKEN": "stolen-token",
            "SECRETS_VAULT_URL": "https://evil.example/api/secrets",
            "SECRETS_MANAGER_URL": "https://evil.example/manage",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["recovered"] == 2
    assert sorted(res["ignored"]) == ["SECRETS_MANAGER_URL", "SECRETS_TOKEN", "SECRETS_VAULT_URL"]
    assert env["SECRETS_TOKEN"] == "tok"  # оригинал не перетёрся
    # Адрес хранилища задаётся окружением, поэтому «не перетёрся» тут означает
    # «остался локальным», а не «отсутствует». Подмена адреса из ответа vault —
    # ровно тот самый вектор, ради которого bootstrap-конфиг вне allowlist.
    assert env["SECRETS_VAULT_URL"] == _VAULT_URL


def test_local_value_wins_over_vault(monkeypatch):
    """Свойство 3: systemd дал ключ → vault не перетирает локальное."""
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        # REDIS_URL отсутствует — его надо восстановить
    }
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://vault:p@h/db",
            "REDIS_URL": "redis://h:1/0",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["recovered"] == 1  # только REDIS_URL
    assert env["DATABASE_URL"] == "postgresql+asyncpg://local:p@h/db"


def test_no_token_returns_no_token(monkeypatch):
    """Свойство 4: нет SECRETS_TOKEN → best-effort no-op (не падаем)."""
    env = {}  # и REQUIRED нет, и токена нет
    monkeypatch.setattr(sb, "_fetch_secrets", lambda *a, **k: pytest.fail("не должен вызываться"))

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["reason"] == "no-token"
    assert res["recovered"] == 0


def test_vault_unreachable_is_best_effort(monkeypatch):
    """Свойство 4: vault недоступен → fetch-failed, старт НЕ валим."""
    env = {"SECRETS_TOKEN": "tok"}

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(sb, "_fetch_secrets", _boom)
    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["reason"] == "fetch-failed"
    assert res["recovered"] == 0


def test_prefixes_accepted():
    """Множественные токены (VK_TOKEN_* / TELEGRAM_TOKEN_*) проходят по префиксу."""
    assert sb._accepted("VK_TOKEN_VALSTAN")
    assert sb._accepted("VK_TOKEN_MAMA")
    assert sb._accepted("TELEGRAM_TOKEN_AFONYA")
    assert sb._accepted("DATABASE_URL")
    assert not sb._accepted("FOO_BAR")
    assert not sb._accepted("NODE_OPTIONS")
    assert not sb._accepted("SECRETS_TOKEN")


def test_ignored_logged_by_names_only(monkeypatch, caplog):
    """Свойство 7: чужие ключи логируются ПО ИМЕНАМ, без значений."""
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "MYSTERY": "super-secret-value-12345",
        },
    )

    with caplog.at_level(logging.WARNING, logger="modules.secrets_bootstrap"):
        sb.bootstrap_secrets(env=_bootstrap_env(env))

    assert "MYSTERY" in caplog.text
    assert "super-secret-value-12345" not in caplog.text


def test_recovered_logs_count(monkeypatch, caplog):
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
        },
    )

    # WARNING (не INFO) — хук выполняется до logging.basicConfig, должен пройти
    # даже через lastResort-хендлер root-логгера.
    with caplog.at_level(logging.WARNING, logger="modules.secrets_bootstrap"):
        sb.bootstrap_secrets(env=_bootstrap_env(env))

    assert "восстановлено секретов из vault: 2" in caplog.text


def test_site_ingest_keys_accepted_by_suffix(monkeypatch):
    """Ключи доставки конвейера приходят как <SITE>_INGEST_KEY (имя от КАРМАНа).

    Суффиксное правило узкое: пускает ключи сайтов и не пускает ни bootstrap-имена,
    ни посторонние вроде NODE_OPTIONS, лежащего в той же комнате.
    """
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "VMALMYZHE_INGEST_KEY": "site-key-1",
            "CDK_KALININO_INGEST_KEY": "site-key-2",
            "NODE_OPTIONS": "--require /proc/self/environ",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert env["VMALMYZHE_INGEST_KEY"] == "site-key-1"
    assert env["CDK_KALININO_INGEST_KEY"] == "site-key-2"
    assert res["ignored"] == ["NODE_OPTIONS"]


def test_gateway_key_name_is_not_smuggled_in_by_suffix_rule(monkeypatch):
    """GATEWAY_KEY_* — ключи ВХОДЯЩИХ в наш VK-шлюз, из vault они не принимаются.

    Проверяем, что суффиксное правило не открыло им дверь: имя, начинающееся с
    GATEWAY_KEY_, не оканчивается на _INGEST_KEY и остаётся за бортом.
    """
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "GATEWAY_KEY_VMALMYZHE": "wrong-direction",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["ignored"] == ["GATEWAY_KEY_VMALMYZHE"]
    assert "GATEWAY_KEY_VMALMYZHE" not in env


def test_room_is_a_source_not_only_a_backup(monkeypatch):
    """Секрет, положенный ТОЛЬКО в комнату, доезжает до процесса (владелец 2026-08-09).

    До этой правки модуль выходил на `local-env-intact`, если DATABASE_URL и
    REDIS_URL целы, и в vault не ходил вовсе — DEEPSEEK_API_KEY лежал в комнате,
    а os.environ его не видел.
    """
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        "REDIS_URL": "redis://h:1/0",
    }
    monkeypatch.setattr(
        sb, "_fetch_secrets", lambda token, url: {"DEEPSEEK_API_KEY": "sk-from-room"}
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert env["DEEPSEEK_API_KEY"] == "sk-from-room"
    assert res["recovered"] == 1 and res["reason"] == "pulled"


def test_pull_always_can_be_switched_off(monkeypatch):
    """VAULT_PULL_ALWAYS=0 возвращает старое поведение — ноль сетевых вызовов."""
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        "REDIS_URL": "redis://h:1/0",
        "VAULT_PULL_ALWAYS": "0",
    }

    def _boom(token, url):  # pragma: no cover — вызов означал бы провал теста
        raise AssertionError("в vault ходить не должны")

    monkeypatch.setattr(sb, "_fetch_secrets", _boom)
    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["reason"] == "local-env-intact" and res["recovered"] == 0


def test_local_env_still_wins_in_pull_mode(monkeypatch):
    """Свойство 3 не ослабло: комната-источник не перетирает то, что дал systemd."""
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        "REDIS_URL": "redis://h:1/0",
        "GROQ_API_KEY": "local-wins",
    }
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {"GROQ_API_KEY": "from-room", "DEEPSEEK_API_KEY": "new"},
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert env["GROQ_API_KEY"] == "local-wins"
    assert env["DEEPSEEK_API_KEY"] == "new"
    assert res["recovered"] == 1


def test_allowlist_still_applies_in_pull_mode(monkeypatch):
    """«Источник» не значит «доверяем всему»: NODE_OPTIONS из комнаты не проедет."""
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        "REDIS_URL": "redis://h:1/0",
    }
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "NODE_OPTIONS": "--require /proc/self/environ",
            "DEEPSEEK_API_KEY": "k",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["ignored"] == ["NODE_OPTIONS"]
    assert "NODE_OPTIONS" not in env


def test_unreachable_vault_does_not_break_start(monkeypatch):
    """Best-effort сохраняется: недоступный vault при целом env — не отказ старта."""
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        "REDIS_URL": "redis://h:1/0",
    }

    def _fail(token, url):
        raise OSError("vault недоступен")

    monkeypatch.setattr(sb, "_fetch_secrets", _fail)
    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert res["reason"] == "fetch-failed" and res["recovered"] == 0


def test_quiet_when_pull_adds_nothing(monkeypatch, caplog):
    """Нечего добавить — молчим: иначе «восстановлено: 0» на каждом старте станет шумом."""
    env = {
        "SECRETS_TOKEN": "tok",
        "DATABASE_URL": "postgresql+asyncpg://local:p@h/db",
        "REDIS_URL": "redis://h:1/0",
    }
    monkeypatch.setattr(sb, "_fetch_secrets", lambda token, url: {})

    with caplog.at_level(logging.WARNING, logger="modules.secrets_bootstrap"):
        sb.bootstrap_secrets(env=_bootstrap_env(env))

    assert "восстановлено секретов" not in caplog.text


# ─── Инцидент 2026-08-19: один таймаут на старте = трое суток без ключа ──────


def test_real_fetch_retries_transient_failure(monkeypatch):
    """Ретраи живут в ``_fetch_secrets`` — проверяем саму функцию."""
    calls = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"secrets": {"DEEPSEEK_API_KEY": "sk-ok"}}'

    def _urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("timed out")
        return _Resp()

    monkeypatch.setattr(sb, "_FETCH_BACKOFF_SEC", (0.0, 0.0))
    monkeypatch.setattr(sb.urllib.request, "urlopen", _urlopen)

    assert sb._fetch_secrets("tok", "https://vault.example/api/secrets") == {
        "DEEPSEEK_API_KEY": "sk-ok"
    }
    assert calls["n"] == 3


def test_real_fetch_raises_after_all_attempts(monkeypatch):
    """Исчерпав попытки, пробрасываем последнюю причину — вызывающий её залогирует."""
    monkeypatch.setattr(sb, "_FETCH_BACKOFF_SEC", (0.0, 0.0))

    def _always_fail(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(sb.urllib.request, "urlopen", _always_fail)
    with pytest.raises(TimeoutError):
        sb._fetch_secrets("tok", "https://vault.example/api/secrets")


def test_ensure_secret_pulls_missing_value(monkeypatch):
    """До-тяг в момент использования: ключа нет → сходили в комнату и взяли."""
    monkeypatch.setattr(sb, "_fetch_secrets", lambda t, u: {"DEEPSEEK_API_KEY": "sk-late"})
    sb._last_ensure_at.clear()
    env = {"SECRETS_TOKEN": "tok"}
    assert sb.ensure_secret("DEEPSEEK_API_KEY", env=env) is True
    assert env["DEEPSEEK_API_KEY"] == "sk-late"


def test_ensure_secret_is_noop_when_value_present(monkeypatch):
    """Значение на месте — в vault не ходим вовсе (ноль сетевых вызовов)."""
    calls = []
    monkeypatch.setattr(sb, "_fetch_secrets", lambda t, u: calls.append(1) or {})
    sb._last_ensure_at.clear()
    env = {"SECRETS_TOKEN": "tok", "DEEPSEEK_API_KEY": "sk-already-here"}
    assert sb.ensure_secret("DEEPSEEK_API_KEY", env=env) is True
    assert calls == []


def test_ensure_secret_respects_cooldown(monkeypatch):
    """Второй промах подряд не идёт в сеть: иначе таска долбит vault на каждый чанк."""
    calls = []

    def _fetch(t, u):
        calls.append(1)
        return {}  # в комнате ключа нет

    monkeypatch.setattr(sb, "_fetch_secrets", _fetch)
    sb._last_ensure_at.clear()
    env = {"SECRETS_TOKEN": "tok"}
    assert sb.ensure_secret("DEEPSEEK_API_KEY", env=env) is False
    assert sb.ensure_secret("DEEPSEEK_API_KEY", env=env) is False
    assert len(calls) == 1


def test_ensure_secret_refuses_names_outside_allowlist(monkeypatch):
    """Свойство 2 действует и на до-тяг: комната не может подсунуть чужое имя."""
    monkeypatch.setattr(sb, "_fetch_secrets", lambda t, u: {"NODE_OPTIONS": "--require evil"})
    sb._last_ensure_at.clear()
    env = {"SECRETS_TOKEN": "tok"}
    assert sb.ensure_secret("NODE_OPTIONS", env=env) is False
    assert "NODE_OPTIONS" not in env


def test_ensure_secret_never_pulls_bootstrap_config(monkeypatch):
    """Свойство 6: ``SECRETS_TOKEN``/``SECRETS_VAULT_URL`` не до-тягиваются никогда."""
    monkeypatch.setattr(sb, "_fetch_secrets", lambda t, u: {"SECRETS_VAULT_URL": "https://evil"})
    sb._last_ensure_at.clear()
    env = {"SECRETS_TOKEN": "tok"}
    assert sb.ensure_secret("SECRETS_VAULT_URL", env=env) is False
    assert "SECRETS_VAULT_URL" not in env


def test_site_publish_key_reaches_the_process(monkeypatch):
    """D-091: ``<SITE>_PUBLISH_KEY`` обязан доезжать из комнаты в процесс.

    Регрессия на живом случае 2026-09-14: выдача была принята штатно, а ключ
    остался в комнате — суффикс `_PUBLISH_KEY` не входил в allowlist, и в логе
    это было одной строкой «вне allowlist, проигнорированы». Наружу выглядело
    как «портал не публикует», хотя мандат D-091 был исполнен целиком.
    """
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "VMALMYZHE_PUBLISH_KEY": "publish-key",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert env["VMALMYZHE_PUBLISH_KEY"] == "publish-key"
    assert res["ignored"] == []


def test_publish_suffix_does_not_open_the_door_to_gateway_names(monkeypatch):
    """Правило осталось узким: направление по-прежнему читается из имени."""
    env = {"SECRETS_TOKEN": "tok"}
    monkeypatch.setattr(
        sb,
        "_fetch_secrets",
        lambda token, url: {
            "DATABASE_URL": "postgresql+asyncpg://u:p@h/db",
            "REDIS_URL": "redis://h:1/0",
            "GATEWAY_KEY_VMALMYZHE": "wrong-direction",
            "PUBLISH_KEY": "без имени сайта",
            "SECRETS_TOKEN_PUBLISH_KEY": "bootstrap-имя с хвостом",
        },
    )

    res = sb.bootstrap_secrets(env=_bootstrap_env(env))
    assert "GATEWAY_KEY_VMALMYZHE" in res["ignored"]
    assert "GATEWAY_KEY_VMALMYZHE" not in env
    assert env["SECRETS_TOKEN"] == "tok"  # bootstrap-имя не перетёрто
