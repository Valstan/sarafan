# Планировщик Celery на Windows — замена systemd-юнита `setka-celery-beat`.
#
# На проде beat пишет файл расписания в каталог рядом с приложением
# (`--schedule=...`). На Windows путь к файлу расписания тоже задаётся
# явно: иначе beat пишет его в текущий каталог, и при перезапуске из
# планировщика задач «рабочий каталог» может оказаться другим — расписание
# молча потерялось бы.

param(
    [string]$EnvFile = "",
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

$schedule = Join-Path $script:LogDir "celerybeat-schedule"
$pidfile = Join-Path $script:LogDir "celery-beat.pid"

$proc = Start-StandProcess -Name "setka-beat" -LogFileName "celery-beat.log" -Foreground:$Foreground -Arguments @(
    "-m", "celery", "-A", "celery_app", "beat",
    "--loglevel=info",
    "--pidfile=$pidfile",
    "--schedule=$schedule"
)

Write-Host "[setka-beat] pid=$($proc.Id) расписание: $schedule"
