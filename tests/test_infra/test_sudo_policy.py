"""Сторож политики sudo на боксе: разработчику — полный, движку — никакого.

Граница изоляции проходит по пользователю, а не по перечню команд:

* ``valstan`` (воркер, релиз, агент-разработчик) — ``NOPASSWD: ALL``;
* ``opencode`` (нейро-движок ``opencode-web``) — sudo нет вообще, юнит держит
  ``NoNewPrivileges=yes``.

Класс отказа, который ловится только здесь. Песочницу легко перепутать с
запретом: белый список команд для ``valstan`` выглядел защитой движка, но
ограничивал разработчика, а движок как раз и не имел прав. Тест держит предмет
защиты — пользователя движка — и не даёт вернуть ограничение разработчику,
которое каждую новую привилегированную команду превращало в правку sudoers.

Канон — ``infra/sudoers/valstan-dev`` и ``infra/neural/opencode-web.service``;
политика описана в ``infra/sudoers/README.md`` и ``docs/OPERATIONS.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SUDOERS_DIR = REPO_ROOT / "infra" / "sudoers"
DEVELOPER_RULE = SUDOERS_DIR / "valstan-dev"
ENGINE_UNIT = REPO_ROOT / "infra" / "neural" / "opencode-web.service"
OPERATIONS = REPO_ROOT / "docs" / "OPERATIONS.md"

# Пользователь прод-нейросети. Ему sudo не положен ни в каком виде.
ENGINE_USER = "opencode"

DEVELOPER_LINE = "valstan ALL=(ALL) NOPASSWD: ALL"


def _sudoers_files() -> list[Path]:
    return sorted(path for path in SUDOERS_DIR.iterdir() if path.is_file() and path.suffix != ".md")


def _rules(path: Path) -> list[str]:
    """Строки-правила файла sudoers: без комментариев, с раскрытыми переносами."""
    text = re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8"))
    rules: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            rules.append(" ".join(line.split()))
    return rules


def test_developer_has_full_sudo() -> None:
    """Разработчик не ограничен перечнем команд — это и есть решение 21.10."""
    assert DEVELOPER_RULE.is_file(), f"нет {DEVELOPER_RULE}"
    assert DEVELOPER_LINE in _rules(DEVELOPER_RULE), (
        "правило разработчика должно быть ровно "
        f"{DEVELOPER_LINE!r}: ограничение адресовано не тому пользователю"
    )


def test_no_command_allowlist_for_developer() -> None:
    """Регрессия к белому списку команд: перечень запрещённых команд не вернулся.

    Любой файл в ``infra/sudoers/`` кроме правила разработчика — это снова
    список разрешённых команд, то есть ограничение, которое не защищает движок.
    """
    unexpected = [path.name for path in _sudoers_files() if path.name != DEVELOPER_RULE.name]
    assert not unexpected, (
        "в infra/sudoers/ появились файлы помимо правила разработчика: "
        f"{unexpected}. Ограничение на valstan не защищает прод-нейросеть — "
        "её песочница держится пользователем opencode и NoNewPrivileges"
    )


def test_engine_user_has_no_sudo() -> None:
    """Ни одна строка sudoers не выдаёт права пользователю движка."""
    leaks: list[str] = []
    for path in _sudoers_files():
        for rule in _rules(path):
            if re.search(rf"(^|\W){re.escape(ENGINE_USER)}(\W|$)", rule):
                leaks.append(f"{path.name}: {rule}")
    assert not leaks, "движку выдали sudo: " + "; ".join(leaks)


def test_engine_unit_stays_sealed() -> None:
    """Юнит движка: отдельный пользователь и запрет поднимать привилегии."""
    text = ENGINE_UNIT.read_text(encoding="utf-8")
    assert re.search(
        rf"^User={re.escape(ENGINE_USER)}$", text, re.MULTILINE
    ), "движок должен работать под отдельным пользователем opencode"
    assert re.search(
        r"^NoNewPrivileges=yes$", text, re.MULTILINE
    ), "NoNewPrivileges=yes — это и есть песочница движка; без неё граница исчезает"
    assert "sudo" not in text, "движок не должен запускаться через sudo"


def test_operations_doc_states_the_policy() -> None:
    """Политика должна быть там, куда смотрит разработчик перед правкой."""
    text = OPERATIONS.read_text(encoding="utf-8")
    assert "infra/sudoers/valstan-dev" in text, "в OPERATIONS.md нет канона правила sudo"
    assert "NoNewPrivileges" in text, "в OPERATIONS.md не описана песочница движка"
    assert "setka-worker" not in text, "OPERATIONS.md всё ещё ссылается на снятый белый список"
