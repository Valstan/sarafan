"""Сторож белого списка sudo для воркера.

Класс отказа, который ловится только здесь: код воркера вызывает новую
привилегированную команду, локальные тесты и CI зелёные (на машине
разработчика sudo вообще не нужен), а на боксе вызов падает с
``sudo: a password is required``. Ошибка выглядит как «сломался прод» и ищется
в коде, хотя лежит в одном файле sudoers.

Канон — ``infra/sudoers/setka-worker``; правило описано в
``infra/sudoers/README.md`` и ``docs/OPERATIONS.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "infra" / "sudoers" / "setka-worker"
OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"

# Каталоги, чей код исполняется на боксе под пользователем воркера. Скрипты
# (scripts/) сюда не входят намеренно: их запускает человек, у него sudo с
# паролем.
RUNTIME_DIRS = ("modules", "tasks", "web", "config")

# Команда, которой движок меняет выход ВПН (NEURAL_EGRESS_ROTATE_CMD).
EGRESS_ROTATE_SCRIPT = "/etc/neural/egress.d/10-nextgenvpn.sh"


def _entries() -> list[str]:
    """Команды из белого списка: без комментариев, с раскрытыми переносами."""
    text = ALLOWLIST.read_text(encoding="utf-8")
    text = re.sub(r"\\\n\s*", " ", text)
    commands: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"^\S+\s+ALL=\(root\)\s+NOPASSWD:\s*(.+)$", line)
        assert match, f"строка не в формате sudoers: {raw!r}"
        for part in match.group(1).split(","):
            command = part.strip()
            if command:
                commands.append(command)
    return commands


def _sudo_calls() -> list[tuple[str, tuple[str, ...]]]:
    """``sudo -n <бинарник> <арг> <арг>`` из рантайм-кода: (файл, аргументы)."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    pattern = re.compile(r'\[\s*"sudo"\s*,\s*"-n"\s*,((?:\s*"[^"]+"\s*,?)+)\]')
    arg_re = re.compile(r'"([^"]+)"')
    for directory in RUNTIME_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            source = path.read_text(encoding="utf-8", errors="replace")
            for match in pattern.finditer(source):
                args = tuple(arg_re.findall(match.group(1)))
                if args:
                    calls.append((str(path.relative_to(REPO_ROOT)), args))
    return calls


@pytest.fixture(scope="module")
def commands() -> list[str]:
    assert ALLOWLIST.is_file(), f"нет белого списка sudo: {ALLOWLIST}"
    return _entries()


# Команды чтения состояния: звёздочка здесь обязательна (см. ниже) и безопасна —
# `status`/`is-active` состояние не меняют, `--vacuum` живёт в journalctl.
READONLY_UNIT_WILDCARD = re.compile(r"^/(?:usr/)?bin/systemctl (?:status|is-active) \*$")


def test_allowlist_has_no_blanket_root(commands: list[str]) -> None:
    """Белый список не должен снова стать «всё можно»."""
    assert commands, "белый список пуст"
    for command in commands:
        assert command != "ALL", "NOPASSWD: ALL — ровно та дыра, которую закрывали"
        if "*" in command:
            assert READONLY_UNIT_WILDCARD.match(command), f"wildcard в белом списке: {command}"
        assert command.startswith("/"), f"не абсолютный путь: {command}"


def test_allowlist_covers_egress_rotation(commands: list[str]) -> None:
    """Смена выхода ВПН — главная причина, по которой воркеру нужен root."""
    assert EGRESS_ROTATE_SCRIPT in commands


def test_allowlist_covers_runtime_sudo_calls(commands: list[str]) -> None:
    """Каждый `sudo -n ...` в рантайм-коде есть в белом списке.

    Добавил в код привилегированный вызов — добавь строку в
    ``infra/sudoers/setka-worker`` тем же PR, иначе на боксе будет
    «a password is required».
    """
    missing: list[str] = []
    for origin, args in _sudo_calls():
        binaries = [f"/usr/bin/{args[0]}", f"/bin/{args[0]}"]
        wanted = [f"{binary} {' '.join(args[1:])}".strip() for binary in binaries]
        if not any(
            command == want or command.startswith(want + " ")
            for want in wanted
            for command in commands
        ):
            missing.append(f"{origin}: sudo -n {' '.join(args)}")
    assert not missing, "нет в sudo-белом списке: " + "; ".join(missing)


def test_allowlist_covers_worker_units(commands: list[str]) -> None:
    """Рабочие юниты, которые релиз и диагностика перезапускают без человека."""
    for unit in (
        "setka",
        "setka-celery-worker",
        "setka-celery-beat",
        "setka-vk-bot",
        "opencode-web",
    ):
        for binary in ("/usr/bin/systemctl", "/bin/systemctl"):
            assert f"{binary} restart {unit}" in commands, f"нет restart {unit}"
            assert f"{binary} stop {unit}" in commands, f"нет stop {unit}"


def test_operations_doc_reminds_about_allowlist() -> None:
    """Правило должно быть там, куда разработчик смотрит при правке воркера."""
    text = OPERATIONS.read_text(encoding="utf-8")
    assert "infra/sudoers/setka-worker" in text
    assert "белый список" in text or "белом списке" in text
