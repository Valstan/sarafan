"""Гейт автозапуска локального стенда без входа в систему.

Заказ владельца 2026-11-27: «сайт должен работать после перезагрузки
компьютера без входа в систему». Для этого задачи планировщика должны
подниматься при старте ОС и работать не в интерактивной сессии.

Здесь зафиксировано то, что при невнимательной правке тихо ломает эту
гарантию:

1. **Юниты, нужные живому сайту, помечены `NoLogin`.** Веб, туннель и Redis:
   без них публичная витрина не отвечает. Воркеры и beat — остаются на
   «при входе в систему» (им нужна интерактивная сессия для CLI-движка).

2. **Для `NoLogin` — триггер «при старте системы» и вход S4U.** S4U не
   сохраняет пароль владельца в задаче (в отличие от «Выполнять независимо
   от регистрации» с паролем) и не требует интерактивной сессии.

3. **Остановка юнита снимает и СУПЕРВИЗОР.** Задача планировщика не убивает
   свой дочерний powershell, и тот продолжает поднимать новые экземпляры
   поверх остановленного — шторм экземпляров (P255).

4. **Redis запускается через `RedisService.exe run --foreground`.** Сборка
   msys2 форкается: запущенный напрямую `redis-server.exe` оставляет сироту,
   задача теряет управление и перезапускается раз в минуту, а новый redis
   падает с «Address already in use».
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTER = REPO_ROOT / "infra" / "windows" / "register-scheduled-tasks.ps1"
STOP_SCRIPT = REPO_ROOT / "infra" / "windows" / "stop-unit.ps1"
START_REDIS = REPO_ROOT / "infra" / "windows" / "start-redis.ps1"

#: Юниты, без которых публичный сайт не живёт: веб отдаёт страницу, туннель
#: выводит её в интернет, Redis — общий кеш и брокер.
NO_LOGIN_UNITS = ("setka", "setka-public-tunnel", "setka-redis")

#: Юниты, которым нужна интерактивная сессия (CLI-движок MiniMax и прочее).
LOGIN_UNITS = (
    "setka-celery-worker",
    "setka-celery-worker-wave",
    "setka-celery-beat",
    "setka-vk-bot",
    "setka-radar-intake",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _unit_block(name: str) -> str:
    """Блок объявления юнита в реестре задач."""
    text = _text(REGISTER)
    m = re.search(r'@\{\s*Name\s*=\s*"' + re.escape(name) + r'"[^}]*\}', text, re.S)
    assert m, f"юнит {name} не объявлен в register-scheduled-tasks.ps1"
    return m.group(0)


class TestNoLoginUnits:
    def test_units_needed_by_the_site_are_marked_nologin(self) -> None:
        for name in NO_LOGIN_UNITS:
            assert "NoLogin" in _unit_block(name), (
                f"{name}: без NoLogin юнит поднимется только по входу в систему, "
                "и публичный сайт не ответит после перезагрузки без входа"
            )

    def test_worker_units_stay_on_logon(self) -> None:
        """Воркеры и beat остаются на входе: им нужна интерактивная сессия."""
        for name in LOGIN_UNITS:
            assert "NoLogin" not in _unit_block(name), (
                f"{name}: перевод на S4U без отдельного решения владельца — "
                "CLI-движок и консольные инструменты ждут интерактивную сессию"
            )

    def test_nologin_branch_uses_boot_trigger_and_s4u(self) -> None:
        text = _text(REGISTER)
        assert (
            "New-ScheduledTaskTrigger -AtStartup" in text
        ), "для NoLogin-юнитов нужен триггер «при старте системы»"
        assert "-LogonType S4U" in text, (
            "для NoLogin-юнитов нужен вход S4U: без пароля в задаче и без " "интерактивной сессии"
        )

    def test_flag_lookup_survives_strict_mode(self) -> None:
        """Отсутствующий ключ хеша под StrictMode роняет строку.

        `$unit.NoLogin` на юните без этого ключа бросает
        PropertyNotFoundStrict, и юнит наследует триггер ПРЕДЫДУЩЕГО — тихо
        и неверно. Поэтому ключи читаются через ContainsKey.
        """
        text = _text(REGISTER)
        # Каждое чтение флага обязано быть на одной строке с ContainsKey:
        # без него обращение к отсутствующему ключу хеша под StrictMode роняет
        # строку, и юнит наследует триггер ПРЕДЫДУЩЕГО.
        for flag in ("NoLogin", "NoEnv"):
            lines = [
                ln
                for ln in text.splitlines()
                if f"$unit.{flag}" in ln and not ln.strip().startswith("#")
            ]
            assert lines, f"register-scheduled-tasks.ps1: флаг {flag} не читается"
            for line in lines:
                assert 'ContainsKey("%s")' % flag in line, (
                    f"register-scheduled-tasks.ps1: чтение $unit.{flag} без "
                    "ContainsKey на той же строке — на юните без ключа строка "
                    "падает под StrictMode, и юнит наследует чужие настройки"
                )
        assert 'ContainsKey("NoLogin")' in text
        assert 'ContainsKey("NoEnv")' in text


class TestStopUnit:
    def test_supervisor_is_covered_by_the_signature(self) -> None:
        """Остановка должна снимать и супервизор (powershell), и процесс.

        Задача планировщика не убивает свой дочерний powershell — а тот
        продолжает поднимать экземпляры поверх остановленного (P255: 11
        веб-инстансов за вечер).
        """
        text = _text(STOP_SCRIPT)
        assert "powershell.exe" in text, (
            "stop-unit.ps1: в выборку процессов нет powershell.exe — "
            "супервизор выживет и продолжит плодить экземпляры"
        )
        for unit in ("setka", "setka-public-tunnel", "setka-redis"):
            block = re.search(r'"' + re.escape(unit) + r'"\s*=\s*"([^"]+)"', text)
            assert block, f"юнит {unit} не найден в stop-unit.ps1"
            assert ".ps1" in block.group(1), (
                f"stop-unit.ps1: сигнатура юнита {unit} не ловит его супервизор "
                "(в ней должен быть скрипт запуска)"
            )

    def test_redis_pattern_covers_the_service_wrapper(self) -> None:
        text = _text(STOP_SCRIPT)
        assert "RedisService.exe" in text, (
            "stop-unit.ps1: сигнатура Redis должна ловить и обёртку "
            "RedisService.exe, иначе она остаётся жить после остановки юнита"
        )


class TestRedisRunner:
    def test_redis_is_started_through_the_service_wrapper(self) -> None:
        """Сборка msys2 форкается — прямой запуск redis-server.exe не годится."""
        text = _text(START_REDIS)
        assert "RedisService.exe" in text, (
            "start-redis.ps1: Redis надо запускать через RedisService.exe "
            "run --foreground — напрямую redis-server.exe форкается, и задача "
            "теряет управление (перезапуск раз в минуту + «Address already in use»)"
        )
        assert "--foreground" in text
        assert "-FilePath $serviceExe" in text, (
            "start-redis.ps1: запускаться должен RedisService.exe ($serviceExe), "
            "а не redis-server.exe"
        )
