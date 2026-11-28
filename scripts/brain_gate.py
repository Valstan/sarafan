#!/usr/bin/env python3
"""Гейт системы мозга проекта: линза не должна разойтись с кодом, реестр — с ролями.

**Зачем.** Система мозга проекта (`docs/PROJECT_LENS.md`, `docs/EXPERTS.md`,
`docs/INTAKE.md`) — это документы, которые живут дольше кода, который они
описывают. Документ про карту имеет один неприятный класс отказа: он
**выглядит живым, пока в нём написано «как было»**. Никто не падает, тесты
зелёные, а новая сессия читает линзу, верит ей и идёт в каталог, которого
уже нет.

Тот же отказ у реестра экспертов: роль в шаблоне разбора описана, а в реестре
её нет (или наоборот) — совет превращается в монолог, и это видно только
по факту.

Гейт ловит ровно эти молчаливые расхождения:

  1. **Пути из документов системы не существуют.** Линза, реестр и порядок
     приёма ссылаются на файлы и каталоги проекта; каждый такой путь должен
     быть на диске. Переименовал каталог — гейт сразу сказал.
  2. **Пропала роль.** В реестре обязаны быть девять ролей, и у каждой — четыре
     поля: что делает, что запускает, что отдаёт дальше, проверяемый выход.
  3. **Потерялись маркеры приёма.** В реестре обязаны быть маршрут простой
     задачи («координатор → программист») и потолок совета.
  4. **Выкинули правило обновления из правил проекта.** В `AGENTS.md` обязаны
     быть сама фраза правила и ссылки на все три документа системы.

    python scripts/brain_gate.py            # тихо, exit 0 — расхождений нет
    python scripts/brain_gate.py --verbose  # печатает счёт даже когда всё цело

Коды возврата: 0 — сошлось; 1 — расхождение (гейт сработал); 2 — не смог
посмотреть (файлов нет, каталог не читается). «Не смог посмотреть» ≠ «сошлось».
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
LENS = ROOT / "docs" / "PROJECT_LENS.md"
EXPERTS = ROOT / "docs" / "EXPERTS.md"
INTAKE = ROOT / "docs" / "INTAKE.md"

# Документы, в которых пути проверяются: система мозга целиком.
DOCS: Tuple[Path, ...] = (LENS, EXPERTS, INTAKE)

# Роли совета: (человекочитаемое имя, ключ для поиска в заголовке).
REQUIRED_ROLES: Tuple[Tuple[str, str], ...] = (
    ("Координатор", "координатор"),
    ("Программист", "программист"),
    ("Разведчик", "разведчик"),
    ("Критик", "критик"),
    ("Тестер", "тестер"),
    ("Эксперт по антропогенезу", "антропогенез"),
    ("Шут", "шут"),
    ("Эксперт по эффективности и дублям", "эффективност"),
    ("Эксперт по нужности", "нужност"),
)

# Обязательные поля роли: у каждой роли есть проверяемый выход.
ROLE_FIELDS: Tuple[str, ...] = (
    "Что делает",
    "Что запускает",
    "Что отдаёт дальше",
    "Проверяемый выход",
)

# Маркеры приёма, которые обязаны остаться в реестре.
SIMPLE_ROUTE_RE = re.compile(r"координатор\s*→\s*программист", re.IGNORECASE)
COUNCIL_CAP_RE = re.compile(r"потолок\s+совета[^\n]*?(\d+)", re.IGNORECASE)
UPDATE_RULE_RE = re.compile(r"изменился\s+проект", re.IGNORECASE)

# Токен в обратных кавычках, похожий на путь в репозитории.
TOKEN_RE = re.compile(r"`([^`\n]+)`")
# Допустимый набор символов пути: пробелов, «<», «:» и прочего в пути нет.
PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")
# Расширения, по которым путь считается файлом, даже если в нём нет «/».
FILE_EXTS = (
    ".md",
    ".py",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".txt",
    ".ini",
    ".cfg",
    ".sh",
    ".ps1",
    ".sql",
    ".html",
    ".css",
    ".js",
    ".env",
)


def looks_like_path(token: str) -> bool:
    """Похож ли токен на путь внутри репозитория."""
    if not token or not PATH_CHARS_RE.match(token):
        return False
    if token.startswith("..") or token.startswith("/"):
        return False  # вне репозитория — не наша зона
    if "*" in token or "?" in token:
        return False  # glob намеренно не проверяем
    return "/" in token or token.endswith(FILE_EXTS)


def extract_paths(text: str) -> List[str]:
    """Все токены в обратных кавычках, похожие на пути в репозитории."""
    out: List[str] = []
    for token in TOKEN_RE.findall(text):
        token = token.strip()
        if looks_like_path(token) and token not in out:
            out.append(token)
    return out


def check_paths(text: str, root: Path, label: str = "") -> List[str]:
    """Пути, которых нет на диске."""
    problems: List[str] = []
    suffix = f" (в {label})" if label else ""
    for rel in extract_paths(text):
        if not (root / rel).exists():
            problems.append(f"путь не существует: {rel}{suffix}")
    return problems


def sections_by_heading(text: str) -> Dict[str, str]:
    """Разбить документ на секции по заголовкам ``###``/``##``."""
    out: Dict[str, str] = {}
    current: str | None = None
    buffer: List[str] = []
    for line in text.split("\n"):
        m = re.match(r"^#{2,4}\s+(.*)$", line)
        if m:
            if current is not None:
                out[current] = "\n".join(buffer)
            current = m.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        out[current] = "\n".join(buffer)
    return out


def check_roles(text: str) -> List[str]:
    """Все ли роли на месте и у каждой ли заполнены четыре поля."""
    problems: List[str] = []
    sections = sections_by_heading(text)
    by_key: Dict[str, str] = {}
    for heading, body in sections.items():
        low = heading.lower()
        for _name, key in REQUIRED_ROLES:
            if key in low and key not in by_key:
                by_key[key] = body
    for name, key in REQUIRED_ROLES:
        if key not in by_key:
            problems.append(f"в реестре нет роли: {name}")
            continue
        body = by_key[key]
        for field in ROLE_FIELDS:
            m = re.search(r"^- \*\*" + re.escape(field) + r":\*\*\s*(.+)$", body, re.MULTILINE)
            if not m:
                problems.append(f"роль «{name}» без поля «{field}»")
            elif len(m.group(1).strip()) < 3:
                problems.append(f"роль «{name}»: поле «{field}» пустое")
    return problems


def check_markers(experts_text: str) -> List[str]:
    """Маркеры приёма: маршрут простой задачи и потолок совета."""
    problems: List[str] = []
    if not SIMPLE_ROUTE_RE.search(experts_text):
        problems.append("в реестре нет маршрута простой задачи «координатор → программист»")
    cap = COUNCIL_CAP_RE.search(experts_text)
    if not cap:
        problems.append("в реестре нет потолка совета («потолок совета — N»)")
    elif not 1 <= int(cap.group(1)) <= 4:
        problems.append(f"потолок совета вне разумных границ: {cap.group(1)}")
    return problems


def check_agents(text: str) -> List[str]:
    """Правило обновления и ссылки на систему мозга — в правилах проекта."""
    problems: List[str] = []
    if not UPDATE_RULE_RE.search(text):
        problems.append("в AGENTS.md нет правила «изменился проект — изменилась линза»")
    for doc in ("docs/PROJECT_LENS.md", "docs/EXPERTS.md", "docs/INTAKE.md"):
        if doc not in text:
            problems.append(f"в AGENTS.md нет ссылки на {doc}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="гейт системы мозга проекта")
    ap.add_argument("--verbose", action="store_true", help="печатает счёт и при успехе")
    args = ap.parse_args()

    required = (AGENTS, LENS, EXPERTS, INTAKE)
    for path in required:
        if not path.is_file():
            print(f"brain-gate: не нашёл {path.relative_to(ROOT)}", file=sys.stderr)
            return 2

    problems: List[str] = []
    try:
        agents_text = AGENTS.read_text(encoding="utf-8")
        experts_text = EXPERTS.read_text(encoding="utf-8")
        problems.extend(check_agents(agents_text))
        problems.extend(check_roles(experts_text))
        problems.extend(check_markers(experts_text))
        checked_paths = 0
        for doc in DOCS:
            text = doc.read_text(encoding="utf-8")
            checked_paths += len(extract_paths(text))
            problems.extend(check_paths(text, ROOT, str(doc.relative_to(ROOT))))
    except OSError as exc:
        print(
            f"brain-gate: не смог прочитать документы ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 2

    if problems:
        print("brain-gate: система мозга разошлась с проектом", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 1

    if args.verbose:
        print(
            f"brain-gate: ролей {len(REQUIRED_ROLES)}, путей проверено {checked_paths}, "
            "правило обновления на месте"
        )
    return 0


if __name__ == "__main__":
    # Гейт обязан падать громко: молчаливый exit 0 здесь означал бы «сошлось» там,
    # где на деле не удалось посмотреть (G375 — сеть безопасности должна различать
    # «нечего сказать» и «не смог сказать»).
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"brain-gate: не смог отработать ({type(exc).__name__}: {exc})", file=sys.stderr)
        sys.exit(2)
