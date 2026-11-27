# Воркер очереди `celery` на Windows — замена systemd-юнита `setka-celery-worker`.
#
# На проде юнит ограничен по памяти (MemoryHigh/MemoryMax): бокс 1,5 ГБ без
# свопа, и второй воркер без потолка выбивал бы случайную жертву. На
# локальной машине потолок задаёт сам Windows (см. register-scheduled-tasks.ps1),
# а в самом Celery ограничение остаётся прежним — concurrency=1.
#
# ⚠️ Пул процессов на Windows — только `threads`. Prefork (billiard) на Windows
# не работает: дочерний процесс падает с `PermissionError: [WinError 5]
# Отказано в доступе` на обращении к общему счётчику пула
# (`billiard/pool.py::_ensure_messages_consumed`), воркер при этом
# рапортует «ready» и бесконечно перезапускает детей. Проверено на этой
# машине 26.11.2026.
#
# ⚠️ И `solo` больше нельзя (P253). При пуле `solo` ETA-задачу (у нас это веер
# волны и веер сводки — у обеих `countdown=`) исполняет **поток таймера
# celery**, а обычные задачи — главный поток; оба зовут общий персистентный
# event-loop (`utils/celery_asyncio`), и он оказывается занят другим
# потоком. Живой замер 27.11 06:47: держатель — поток `Timer-1`, и из-за
# отказа падали лента хаба и мама волны (а мама — это «волна не пошла
# вообще»). Пул `threads` (обычные потоки, `concurrent.futures`) исполняет
# тела задач в потоках пула, а поток таймера только раздаёт ETA-задачи:
# коллизии не бывает. A/B-проба на стенде 27.11: `solo` — 16 коллизий и 24
# отказа из 246 задач, `threads` — 0 и 0.

param(
    [string]$EnvFile = "",
    [string]$Queues = "celery",
    [string]$Hostname = "celery@%h",
    # Пул воркера: `threads`, а не `solo` — см. комментарий внизу файла.
    [string]$Pool = "threads",
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
