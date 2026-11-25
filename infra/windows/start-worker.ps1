# Воркер очереди `celery` на Windows — замена systemd-юнита `setka-celery-worker`.
#
# На проде юнит ограничен по памяти (MemoryHigh/MemoryMax): бокс 1,5 ГБ без
# свопа, и второй воркер без потолка выбивал бы случайную жертву. На
# локальной машине потолок задаёт сам Windows (см. register-scheduled-tasks.ps1),
# а в самом Celery ограничение остаётся прежним — concurrency=1.
#
# ⚠️ Пул процессов на Windows — только `solo`. Prefork (billiard) на Windows
# не работает: дочерний процесс падает с `PermissionError: [WinError 5]
# Отказано в доступе` на обращении к общему счётчику пула
# (`billiard/pool.py::_ensure_messages_consumed`), воркер при этом
# рапортует «ready» и бесконечно перезапускает детей. Проверено на этой
# машине 26.11.2026. Для concurrency=1 solo даёт то же самое, что и prefork
# с одним ребёнком, — но без процесса-ребёнка.

param(
    [string]$EnvFile = "",
    [string]$Queues = "celery",
    [string]$Hostname = "celery@%h",
    # Пул процессов на Windows: prefork ( billiard ) там не работает —
    # см. комментарий внизу. Solo — единственный вариант, который здесь живёт.
    [string]$Pool = "solo",
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

$proc = Start-StandProcess -Name "setka-worker" -LogFileName "celery-worker.log" -Foreground:$Foreground -Arguments @(
    "-m", "celery", "-A", "celery_app", "worker",
    "--loglevel=info",
    "--queues=$Queues",
    "--concurrency=1",
    "--pool=$Pool",
    "--hostname=$Hostname"
)

Write-Host "[setka-worker] pid=$($proc.Id) очереди: $Queues пул: $Pool"
