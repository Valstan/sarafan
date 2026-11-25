# Второй воркер Сарафана на Windows — замена systemd-юнита
# `setka-celery-worker-wave` (очереди `wave` и `digest`).
#
# На проде второй слот нужен ровно там, где задания длинные (волна, сводка),
# иначе первый слот уходит в них и расписание ждёт своей очереди
# (ADR-0020, ADR-0022). На локальной машине раскладка та же: отдельный
# процесс, отдельные очереди.

param(
    [string]$EnvFile = "",
    [string]$Queues = "wave,digest",
    [string]$Hostname = "wave@%h",
    [string]$Pool = "solo",
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

$proc = Start-StandProcess -Name "setka-wave-worker" -LogFileName "celery-wave.log" -Foreground:$Foreground -Arguments @(
    "-m", "celery", "-A", "celery_app", "worker",
    "--loglevel=info",
    "--queues=$Queues",
    "--concurrency=1",
    "--pool=$Pool",
    "--hostname=$Hostname"
)

Write-Host "[setka-wave-worker] pid=$($proc.Id) очереди: $Queues пул: $Pool"
