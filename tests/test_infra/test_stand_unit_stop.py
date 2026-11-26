"""Сторож процедуры остановки юнитов локального стенда.

Класс отказа, который здесь ловится: **остановка задачи планировщика не
останавливает приложение.** Задача поднимает цепочку
wscript → cmd → powershell → python, и ``Stop-ScheduledTask`` снимает только
верхушку: python-потомок переживает остановку и остаётся сиротой — держит
порт 8000 (веб) или забирает задачи из очереди (воркер). Следующий
``Start-ScheduledTask`` поднимает **второй** экземпляр рядом с первым.

Живой замер 26.11: после рестарта в 22:40 на каждую очередь висело по два
воркера (09:58 и 22:40), правки #1297/#1306 в бою не работали (работали
старые процессы, поднятые до мержей), а к вечеру у веба накопилось 11
экземпляров (P255).

Поэтому тест держит три вещи:

1. **Каждый зарегистрированный юнит умеет остановиться.** Новый юнит в
   ``register-scheduled-tasks.ps1`` без сигнатуры в ``stop-unit.ps1`` — это
   юнит, который нельзя перезапустить: остановка задачи его не убьёт.
2. **Сигнатуры не пересекаются.** Очереди ``celery`` и ``wave,digest``
   различаются только значением ``--queues``; если регулярка для ``celery``
   начнёт матчить ``wave,digest``, остановка одного воркера убьёт другой.
3. **Проба супервизора не вернулась к 5 секундам.** Именно таймаут 5 с при
   ответе веба в 229 с породил шторм из 11 экземпляров (P255): супервизор
   считал живой веб мёртвым и поднимал новый каждые 30 секунд.

Канон — ``infra/windows/stop-unit.ps1``, ``infra/windows/common.ps1``,
``infra/windows/register-scheduled-tasks.ps1``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTER = REPO_ROOT / "infra" / "windows" / "register-scheduled-tasks.ps1"
STOP_SCRIPT = REPO_ROOT / "infra" / "windows" / "stop-unit.ps1"
COMMON = REPO_ROOT / "infra" / "windows" / "common.ps1"
START_WEB = REPO_ROOT / "infra" / "windows" / "start-web.ps1"

#: Юнит в реестре задач: @{ Name = "setka"; Script = "start-web.ps1"; ... }
UNIT_ENTRY = re.compile(r'@\{\s*Name\s*=\s*"(?P<name>[^"]+)"')

#: Сигнатура в стоп-скрипте: "setka" = "uvicorn\s+main:app"
SIGNATURE = re.compile(r'^\s*"(?P<unit>[^"]+)"\s*=\s*"(?P<pattern>[^"]+)"', re.MULTILINE)

#: Проба супервизора: param([int]$Port = 8000, [int]$TimeoutSec = 5)
HEALTH_TIMEOUT = re.compile(r"function\s+Test-StandWebHealth\s*\{.*?\$TimeoutSec\s*=\s*(\d+)", re.S)

#: Примеры командных строк процессов — как их видит CIM на живой машине.
SAMPLE_COMMAND_LINES: Dict[str, str] = {
    "setka": (
        '"D:\\valstan\\REPO\\sarafan\\venv\\Scripts\\python.exe" -m uvicorn main:app '
        "--host 127.0.0.1 --port 8000 --workers 1"
    ),
    "setka-celery-worker": (
        '"D:\\valstan\\REPO\\sarafan\\venv\\Scripts\\python.exe" -m celery -A celery_app worker '
        "--loglevel=info --queues=celery --concurrency=1 --pool=solo --hostname=celery@PC79"
    ),
    "setka-celery-worker-wave": (
        '"D:\\valstan\\REPO\\sarafan\\venv\\Scripts\\python.exe" -m celery -A celery_app worker '
        "--loglevel=info --queues=wave,digest --concurrency=1 --pool=solo --hostname=wave@PC79"
    ),
    "setka-celery-beat": (
        '"D:\\valstan\\REPO\\sarafan\\venv\\Scripts\\python.exe" -m celery -A celery_app beat '
        "--loglevel=info --pidfile=D:\\valstan\\REPO\\sarafan\\logs\\celery-beat.pid"
    ),
    "setka-vk-bot": (
        '"D:\\valstan\\REPO\\sarafan\\venv\\Scripts\\python.exe" scripts\\vk_bot_daemon.py'
    ),
    "setka-radar-intake": (
        '"D:\\valstan\\REPO\\sarafan\\venv\\Scripts\\python.exe" scripts\\radar_intake_daemon.py'
    ),
}


def _registered_units() -> List[str]:
    text = REGISTER.read_text(encoding="utf-8")
    return [match.group("name") for match in UNIT_ENTRY.finditer(text)]


def _signatures() -> Dict[str, str]:
    """Сигнатуры из блока ``$UnitPatterns = [ordered]@{ … }``."""
    text = STOP_SCRIPT.read_text(encoding="utf-8")
    start = text.index("$UnitPatterns = [ordered]@{")
    end = text.index("}", start)
    block = text[start:end]
    return {match.group("unit"): match.group("pattern") for match in SIGNATURE.finditer(block)}


def test_every_registered_unit_can_be_stopped() -> None:
    """Новый юнит без сигнатуры — юнит, который нельзя остановить."""
    registered = set(_registered_units())
    signatures = set(_signatures())
    missing = sorted(registered - signatures)
    assert not missing, f"юниты без сигнатуры в stop-unit.ps1: {missing}"


def test_no_signature_without_a_registered_unit() -> None:
    """Сигнатура без юнита — мёртвая ветка: её никто не вызовет."""
    registered = set(_registered_units())
    extra = sorted(set(_signatures()) - registered)
    assert not extra, f"сигнатуры без зарегистрированного юнита: {extra}"


def test_signatures_do_not_overlap() -> None:
    """Сигнатура одного юнита не должна ловить процессы другого."""
    signatures = _signatures()
    for unit, command_line in SAMPLE_COMMAND_LINES.items():
        assert unit in signatures, f"нет примера сигнатуры для {unit}"
    for unit, pattern in signatures.items():
        for other_unit, command_line in SAMPLE_COMMAND_LINES.items():
            matched = re.search(pattern, command_line) is not None
            if unit == other_unit:
                assert matched, f"сигнатура {unit} не ловит собственный процесс: {pattern}"
            else:
                assert not matched, (
                    f"сигнатура {unit} ловит процесс {other_unit}: {pattern!r} "
                    f"матчит {command_line!r}"
                )


def test_supervisor_health_timeout_is_not_back_to_five_seconds() -> None:
    """Таймаут 5 с породил шторм из 11 экземпляров (P255) — вернуться нельзя."""
    match = HEALTH_TIMEOUT.search(COMMON.read_text(encoding="utf-8"))
    assert match, "не найден таймаут Test-StandWebHealth"
    timeout = int(match.group(1))
    assert (
        timeout >= 30
    ), f"проба супервизора снова короче ответа веба на тесной машине: {timeout} с"


def test_supervisor_decides_by_port_not_only_by_health() -> None:
    """Сокет живёт дольше ответа: «порт занят» — главный признак, что не плодить."""
    assert "function Test-StandPortListening" in COMMON.read_text(
        encoding="utf-8"
    ), "в common.ps1 нет проверки «порт держит живой процесс»"
    web = START_WEB.read_text(encoding="utf-8")
    assert (
        "Test-StandPortListening" in web
    ), "супервизор веба не смотрит на порт — шторм экземпляров вернётся (P255)"
