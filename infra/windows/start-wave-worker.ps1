# Второй воркер Сарафана на Windows — замена systemd-юнита
# `setka-celery-worker-wave` (очереди `wave` и `digest`).
#
# На проде второй слот нужен ровно там, где задания длинные (волна, сводка),
# иначе первый слот уходит в них и расписание ждёт своей очереди
# (ADR-0020, ADR-0022). На локальной машине раскладка та же: отдельный
# процесс, отдельные очереди.
#
# ⚠️ **Пул `threads`, а не `solo` (P253).** При `solo` ETA-задачу (веер волны
# и веер сводки — у обеих `countdown=`) исполняет **поток таймера celery**, а
# обычные задачи — главный поток; оба зовут общий персистентный event-loop
# (`utils/celery_asyncio`), и он оказывается занят другим потоком. Живой
# замер 27.11 06:47: держатель — поток `Timer-1`, из-за отказа падали лента
# хаба и мама волны (мама — значит «волна не пошла вообще»: 26.11 так умерли
# 5 мам-задач). Prefork на Windows не работает вовсе (`WinError 5`, проверено
# 26.11), а `threads` (обычные потоки, `concurrent.futures`) исполняет тела
# задач в потоках пула — коллизии не бывает. A/B-проба на стенде 27.11:
# `solo` — 16 коллизий и 24 отказа из 246 задач, `threads` — 0 и 0.
#
# ⚠️ **Два слота.** На проде очередь `wave` обслуживают оба воркера
# (`--queues=celery,wave,digest` и `--queues=wave,digest`) — то есть у
# публикационного слота там ДВА места (ADR-0022). На стенде очередь `wave`
# забирает только этот юнит, поэтому два слота берём concurrency'ем пула.
# Замер 27.11: задача региона — 24.7 с в среднем, мама волны — 0.2 с; веер
# сводки — 1–3 мин на регион. С одним слотом сводка забирает слот на часы и
# волны уходят в «кладбище» по сроку (P233, P234) — это ровно то, что
# случилось 26.11.

param(
    [string]$EnvFile = "",
    [string]$Queues = "wave,digest",
    [string]$Hostname = "wave@%h",
    # Пул воркера: `threads`, а не `solo` — см. комментарий внизу файла.
    [string]$Pool = "threads",
    # Два слота публикации: на проде очередь `wave` обслуживают оба воркера
    # (ADR-0022), на стенде — один юнит, поэтому слоты берём concurrency'ем.
    [int]$Concurrency = 2,
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

$proc = Start-StandProcess -Name "setka-wave-worker" -LogFileName "celery-wave.log" -Foreground:$Foreground -Arguments @(
    "-m", "celery", "-A", "celery_app", "worker",
    "--loglevel=info",
    "--queues=$Queues",
    "--concurrency=$Concurrency",
    "--pool=$Pool",
    "--hostname=$Hostname"
)

Write-Host "[setka-wave-worker] pid=$($proc.Id) очереди: $Queues пул: $Pool"
