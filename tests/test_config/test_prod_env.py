"""Тесты чтения env-файлов приложения (modules/prod_env.py).

Модуль нужен операционным скриптам: файлы приложения на боксе — ``600
root:root``, а скрипт работает от root. Проверяем разбор формата и главное
правило — **уже заданное переменное не перебивается** (systemd сильнее файла).
"""

from __future__ import annotations

import os

from modules.prod_env import load_env_files, parse_env_file


def test_parse_skips_comments_and_blank_lines():
    pairs = parse_env_file("# комментарий\n" "\n" "   \n" "KEY=value\n" "# KEY=commented\n")
    assert pairs == [("KEY", "value")]


def test_parse_strips_quotes_and_keeps_equals_inside_value():
    pairs = parse_env_file("A=\"value with = sign\"\nB='quoted'\n")
    assert pairs == [("A", "value with = sign"), ("B", "quoted")]


def test_parse_ignores_line_without_equals():
    assert parse_env_file("GARBAGE\nKEY=1\n") == [("KEY", "1")]


def test_load_env_files_sets_new_keys(tmp_path, monkeypatch):
    env_file = tmp_path / "setka.env"
    env_file.write_text("NEW_KEY=значение\nOTHER=2\n", encoding="utf-8")
    monkeypatch.delenv("NEW_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)

    assert load_env_files([str(env_file)]) == 2
    assert os.environ["NEW_KEY"] == "значение"
    assert os.environ["OTHER"] == "2"


def test_load_env_files_does_not_override_existing(tmp_path, monkeypatch):
    """Systemd-окружение сильнее файла — значение из окружения остаётся."""
    env_file = tmp_path / "setka.env"
    env_file.write_text("MAX_TOKEN=из-файла\n", encoding="utf-8")
    monkeypatch.setenv("MAX_TOKEN", "из-systemd")

    assert load_env_files([str(env_file)]) == 0
    assert os.environ["MAX_TOKEN"] == "из-systemd"


def test_load_env_files_can_override_explicitly(tmp_path, monkeypatch):
    env_file = tmp_path / "setka.env"
    env_file.write_text("MAX_TOKEN=из-файла\n", encoding="utf-8")
    monkeypatch.setenv("MAX_TOKEN", "из-systemd")

    assert load_env_files([str(env_file)], override=True) == 1
    assert os.environ["MAX_TOKEN"] == "из-файла"


def test_load_env_files_missing_file_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv("MAX_TOKEN", raising=False)
    assert load_env_files([str(tmp_path / "нет.env")]) == 0
