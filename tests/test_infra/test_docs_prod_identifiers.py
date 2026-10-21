"""Сторож идентификаторов прода в живых памятках.

Класс отказа: памятка называет юнит или путь, которых на боксе нет, и это не
видно никому — документ выглядит нормальным, гейты его не читают. Ломается ровно
в момент, когда по нему действуют: `systemctl restart sarafan-celery-worker` →
`Unit not found`, а проверка «я на том боксе» (`test -f ~/САРАФАН/main.py`)
краснеет на правильной машине, и агент ищет другой хост.

Правда о боксе (сверено 21.10): корень деплоя — `~/SETKA`, юниты — `setka`,
`setka-celery-worker`, `setka-celery-beat`, `setka-vk-bot`. Слова `sarafan`
как SSH-алиаса это не касается: алиас остаётся `sarafan`.

Исторические записи не проверяются намеренно (`docs/SESSION_HANDOFF.md`,
`docs/PENDING_FOLLOWUPS.md`, `docs/pending/`, `docs/adr/`, `mailbox/`, логи
операций): их не переписывают, они описывают то, что было.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Памятки, по которым выполняют команды прямо сейчас.
LIVE_DOCS = [REPO_ROOT / "README.md", REPO_ROOT / "AGENTS.md"]
LIVE_DOCS += sorted((REPO_ROOT / "docs").glob("*.md"))
LIVE_DOCS += sorted((REPO_ROOT / ".claude" / "commands").glob("*.md"))

# Записи о том, что было: не переписываются (канон AGENTS.md).
HISTORICAL = {"SESSION_HANDOFF.md", "PENDING_FOLLOWUPS.md"}

STALE = (
    "sarafan-celery",
    "sarafan-vk-bot",
    "~/САРАФАН",
    "САРАФАН/main.py",
)


def test_live_docs_use_real_prod_identifiers() -> None:
    offenders: list[str] = []
    for path in LIVE_DOCS:
        if path.name in HISTORICAL:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in STALE:
            if needle in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    assert not offenders, (
        "живые памятки называют несуществующие юниты/пути прода: "
        + "; ".join(offenders)
        + ". На боксе корень — ~/SETKA, юниты — setka*."
    )


def test_live_docs_exist() -> None:
    """Сторож не должен молча позеленеть, если памятки переехали."""
    assert len(LIVE_DOCS) >= 10, f"найдено всего {len(LIVE_DOCS)} живых памяток"
