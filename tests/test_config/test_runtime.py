"""
Unit tests for config/runtime.py

Tests environment variable parsing, token collection, and configuration loading.
These tests do NOT require a real database or external services.
"""

import os
from unittest.mock import patch

import pytest


class TestCollectPrefixedTokens:
    """Tests for _collect_prefixed_tokens function."""

    def test_collects_vk_tokens(self):
        """Should collect all VK_TOKEN_* env vars."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "VK_TOKEN_VALSTAN": "token_val",
                "VK_TOKEN_VITA": "token_vita",
                "VK_TOKEN_OLGA": "token_olga",
                "OTHER_VAR": "ignored",
            },
            clear=True,
        ):
            # Force reimport to pick up new env
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            result = config.runtime._collect_prefixed_tokens("VK_TOKEN_")
            assert result == {
                "VALSTAN": "token_val",
                "VITA": "token_vita",
                "OLGA": "token_olga",
            }

    def test_skips_empty_values(self):
        """Should skip env vars with empty values."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "VK_TOKEN_VALSTAN": "token_val",
                "VK_TOKEN_EMPTY": "",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            result = config.runtime._collect_prefixed_tokens("VK_TOKEN_")
            assert "EMPTY" not in result
            assert result["VALSTAN"] == "token_val"

    def test_returns_empty_dict_when_no_matches(self):
        """Should return empty dict when no matching env vars."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            result = config.runtime._collect_prefixed_tokens("VK_TOKEN_")
            assert result == {}


class TestParseRedisUrl:
    """Tests for _parse_redis_url function."""

    def test_standard_redis_url(self):
        """Should parse standard redis:// URL."""
        from config.runtime import _parse_redis_url

        result = _parse_redis_url("redis://localhost:6379/0")
        assert result["host"] == "localhost"
        assert result["port"] == 6379
        assert result["db"] == 0
        assert result["ssl"] is False

    def test_custom_port_and_db(self):
        """Should parse custom port and db number."""
        from config.runtime import _parse_redis_url

        result = _parse_redis_url("redis://redis.example.com:6380/5")
        assert result["host"] == "redis.example.com"
        assert result["port"] == 6380
        assert result["db"] == 5

    def test_ssl_redis_url(self):
        """Should detect SSL from rediss:// scheme."""
        from config.runtime import _parse_redis_url

        result = _parse_redis_url("rediss://localhost:6379/0")
        assert result["ssl"] is True


class TestParseDatabaseUrl:
    """Tests for _parse_database_url function."""

    def test_postgresql_asyncpg_url(self):
        """Should parse postgresql+asyncpg:// URL."""
        from config.runtime import _parse_database_url

        result = _parse_database_url("postgresql+asyncpg://user:pass@localhost:5432/setka")
        assert result["user"] == "user"
        assert result["password"] == "pass"
        assert result["host"] == "localhost"
        assert result["port"] == 5432
        assert result["database"] == "setka"

    def test_default_port(self):
        """Should use default port 5432 when not specified."""
        from config.runtime import _parse_database_url

        result = _parse_database_url("postgresql+asyncpg://user:pass@localhost/setka")
        assert result["port"] == 5432


class TestGetenv:
    """Tests for _getenv helper."""

    def test_returns_value(self):
        """Should return env var value."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "MY_VAR": "hello",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            assert config.runtime._getenv("MY_VAR") == "hello"

    def test_returns_default_when_missing(self):
        """Should return default when env var is not set."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            assert config.runtime._getenv("MISSING", "fallback") == "fallback"

    def test_returns_none_when_missing_no_default(self):
        """Should return None when env var is not set and no default."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            assert config.runtime._getenv("MISSING") is None

    def test_returns_default_when_empty_string(self):
        """Should return default when env var is empty string."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "MY_VAR": "",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            assert config.runtime._getenv("MY_VAR", "fallback") == "fallback"


class TestRequire:
    """Tests for _require helper."""

    def test_returns_value(self):
        """Should return env var value."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "REQUIRED": "yes",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            assert config.runtime._require("REQUIRED") == "yes"

    def test_raises_when_missing(self):
        """Should raise RuntimeError when env var is not set."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            with pytest.raises(RuntimeError, match="MISSING_REQUIRED"):
                config.runtime._require("MISSING_REQUIRED")


class TestLoadJsonEnv:
    """Tests for _load_json_env helper."""

    def test_parses_valid_json(self):
        """Should parse valid JSON env var."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "MY_JSON": '{"key": "value"}',
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            result = config.runtime._load_json_env("MY_JSON", {})
            assert result == {"key": "value"}

    def test_returns_default_when_missing(self):
        """Should return default when env var is not set."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            assert config.runtime._load_json_env("MISSING", {"default": True}) == {"default": True}

    def test_raises_on_invalid_json(self):
        """Should raise RuntimeError on invalid JSON."""
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                "REDIS_URL": "redis://localhost:6379/0",
                "BAD_JSON": "not json at all",
            },
            clear=True,
        ):
            import importlib

            import config.runtime

            importlib.reload(config.runtime)
            with pytest.raises(RuntimeError, match="Invalid JSON"):
                config.runtime._load_json_env("BAD_JSON", {})


class TestRefreshTokenGlobals:
    """Поздние секреты: они могут приехать ПОСЛЕ импорта модуля."""

    def test_picks_up_telegram_token_added_later(self, monkeypatch):
        """Секрет из комнаты, доехавший после импорта, должен попасть в словарь."""
        import config.runtime as runtime

        # Словари подменяем копиями: тест не должен оставлять токены в глобальном
        # состоянии на остальную сессию.
        monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", dict(runtime.TELEGRAM_TOKENS))
        monkeypatch.setattr(runtime, "VK_TOKENS", dict(runtime.VK_TOKENS))
        monkeypatch.setenv("TELEGRAM_TOKEN_LATEBOT", "999:xyz")

        added = runtime.refresh_token_globals()

        assert runtime.TELEGRAM_TOKENS.get("LATEBOT") == "999:xyz"
        assert added["TELEGRAM_TOKEN_"] >= 1

    def test_picks_up_vk_token_added_later(self, monkeypatch):
        import config.runtime as runtime

        monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", dict(runtime.TELEGRAM_TOKENS))
        monkeypatch.setattr(runtime, "VK_TOKENS", dict(runtime.VK_TOKENS))
        monkeypatch.setenv("VK_TOKEN_LATE", "vk1.late")

        added = runtime.refresh_token_globals()

        assert runtime.VK_TOKENS.get("LATE") == "vk1.late"
        assert added["VK_TOKEN_"] >= 1

    def test_keeps_local_value_stronger(self, monkeypatch):
        """Приоритет локального значения (свойство 3 спеки) не нарушается."""
        import config.runtime as runtime

        monkeypatch.setattr(runtime, "TELEGRAM_TOKENS", dict(runtime.TELEGRAM_TOKENS))
        monkeypatch.setattr(runtime, "VK_TOKENS", dict(runtime.VK_TOKENS))
        monkeypatch.setenv("TELEGRAM_TOKEN_LATEBOT", "из-комнаты")

        runtime.TELEGRAM_TOKENS["LATEBOT"] = "локальное"
        runtime.refresh_token_globals()

        assert runtime.TELEGRAM_TOKENS["LATEBOT"] == "локальное"
