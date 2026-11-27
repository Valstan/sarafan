# Остановка и перезапуск юнита локального стенда — Сарафан (Windows).
#
# Зачем этот скрипт (объяснение из живого замера 26.11):
#
#   **Остановка задачи планировщика НЕ останавливает приложение.**
#   Задача поднимает цепочку wscript → cmd → powershell → python, и
#   `Stop-ScheduledTask` снимает только верхушку (wscript/cmd/powershell).
#   Сам python переживает остановку и остаётся сиротой: он продолжает держать
#   порт 8000 (веб) или забирать задачи из очереди (воркер). Следующий
#   `Start-ScheduledTask` поднимает **второй** экземпляр рядом с первым.
#
#   Живой замер 26.11: после рестарта в 22:40 на каждую очередь висело по два
#   воркера (09:58 и 22:40), а правки #1297/#1306 в бою не работали — работали
#   старые процессы, поднятые до мержей. У веба к 20:36 накопилось 11
#   экземпляров (P255).
#
#   Поэтому порядок жёсткий: **снять задачу → снять процессы → поднять задачу**.
#   Пропустить второй шаг — значит не перезапустить, а размножить.
#
# Что делает:
#   1. Stop-ScheduledTask по имени задачи (если задача есть).
#   2. Гасит ВСЕ процессы этого юнита — по сигнатуре в командной строке, а не
#      по PID: PID меняется, а сигнатура одна на юнит. Останавливает и
#      «настоящий» python, и его venv-обёртку (она переживает остановку
#      главного процесса).
#   3. Проверяет, что процессов не осталось. Не осталось — иначе падает с
#      ненулевым кодом возврата.
#   4. С -Start поднимает задачу заново и ждёт появления процессов.
#
# Применение:
#   powershell -ExecutionPolicy Bypass -File infra\windows\stop-unit.ps1 -TaskName setka-celery-worker
#   powershell -ExecutionPolicy Bypass -File infra\windows\stop-unit.ps1 -TaskName setka -Start
#   powershell -ExecutionPolicy Bypass -File infra\windows\stop-unit.ps1 -List
#
# ⚠️ Шаг 2 гасит процессы БОЕВОГО контура. На стенде владельца это боевые
# воркеры и веб: публикация на паузе, пока юнит не поднят заново. Отсюда
# -WhatIfOnly: он показывает, что будет снято, ничего не трогая.

param(
    # Имя задачи планировщика: setka, setka-celery-worker,
    # setka-celery-worker-wave, setka-celery-beat, setka-vk-bot,
    # setka-radar-intake, setka-public-tunnel. Пусто — все юниты стенда.
    [string]$TaskName = "",
    # Поднять задачу заново после остановки.
    [switch]$Start,
    # Показать, что будет снято, ничего не меняя.
    [switch]$WhatIfOnly,
    # Сколько секунд ждать появления процессов после старта.
    [int]$StartTimeoutSec = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Сигнатуры юнитов ────────────────────────────────────────────────────────
#
# Ключ — имя задачи планировщика, значение — регулярка по командной строке
# процесса. Порядок в регулярке важен: у воркеров очереди различаются
# значением --queues, и «celery» матчится и в «wave,digest» не должен.
$UnitPatterns = [ordered]@{
    "setka"                  = "uvicorn\s+main:app"
    "setka-celery-worker"    = "celery_app\s+worker.*--queues=celery(\s|$)"
    "setka-celery-worker-wave" = "celery_app\s+worker.*--queues=wave,digest(\s|$)"
    "setka-celery-beat"      = "celery_app\s+beat(\s|$)"
    "setka-vk-bot"           = "vk_bot_daemon\.py"
    "setka-radar-intake"     = "radar_intake_daemon\.py"
    # Обратный SSH-туннель на VPS (публичная витрина). Это не python, а ssh —
    # поэтому в выборку процессов добавлен ssh.exe (см. Get-UnitProcess).
    "setka-public-tunnel"    = "ssh.*-R\s+127\.0\.0\.1:\d+:127\.0\.0\.1:\d+"
}

function Get-UnitProcess {
    param([string]$Pattern)
    # python.exe — юниты приложения; ssh.exe — обратный туннель на VPS.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ssh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Pattern }
}

function Stop-UnitProcesses {
    param([string]$Task, [string]$Pattern)
    $procs = @(Get-UnitProcess -Pattern $Pattern)
    if ($procs.Count -eq 0) {
        Write-Host "[$Task] процессов не было — снимать нечего"
        return
    }
    foreach ($proc in $procs) {
        Write-Host ("[{0}] снимаю pid={1} start={2}" -f $Task, $proc.ProcessId, $proc.CreationDate)
        if ($WhatIfOnly) { continue }
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($WhatIfOnly) {
        Write-Host "[$Task] dry-run: процессов снято не было бы: $($procs.Count)"
        return
    }
    Start-Sleep -Seconds 2
    $left = @(Get-UnitProcess -Pattern $Pattern)
    if ($left.Count -gt 0) {
        foreach ($proc in $left) {
            Write-Host ("[{0}] ВНИМАНИЕ: pid={1} выжил после Stop-Process" -f $Task, $proc.ProcessId)
            if (-not $WhatIfOnly) {
                Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
        Start-Sleep -Seconds 2
        $left = @(Get-UnitProcess -Pattern $Pattern)
    }
    if ($left.Count -gt 0) {
        Write-Error "[$Task] процессы не сняты: $($left.ProcessId -join ', ')"
        exit 1
    }
    Write-Host "[$Task] снято процессов: $($procs.Count)"
}

if ($TaskName -eq "-List") {
    Write-Host "Юниты стенда и их сигнатуры:"
    foreach ($key in $UnitPatterns.Keys) {
        Write-Host ("  {0,-26} {1}" -f $key, $UnitPatterns[$key])
    }
    exit 0
}

$targets = if ($TaskName) { @($TaskName) } else { @($UnitPatterns.Keys) }

foreach ($unit in $targets) {
    if (-not $UnitPatterns.Contains($unit)) {
        Write-Error "неизвестный юнит: $unit (см. -List)"
        exit 2
    }
    $pattern = $UnitPatterns[$unit]
    Write-Host "=== $unit ==="

    if (Get-ScheduledTask -TaskName $unit -ErrorAction SilentlyContinue) {
        if ($WhatIfOnly) {
            Write-Host "[$unit] было бы: Stop-ScheduledTask"
        }
        else {
            Stop-ScheduledTask -TaskName $unit -ErrorAction SilentlyContinue
            Write-Host "[$unit] задача остановлена"
        }
    }
    else {
        Write-Host "[$unit] задачи планировщика нет — снимаю процессы напрямую"
    }

    Stop-UnitProcesses -Task $unit -Pattern $pattern

    if ($Start) {
        if ($WhatIfOnly) {
            Write-Host "[$unit] было бы: Start-ScheduledTask"
            continue
        }
        Start-ScheduledTask -TaskName $unit
        Write-Host "[$unit] задача запущена, жду процессы…"
        $deadline = (Get-Date).AddSeconds($StartTimeoutSec)
        $seen = @()
        while ((Get-Date) -lt $deadline) {
            $seen = @(Get-UnitProcess -Pattern $pattern)
            if ($seen.Count -gt 0) { break }
            Start-Sleep -Seconds 2
        }
        if ($seen.Count -eq 0) {
            Write-Error "[$unit] процессы не появились за $StartTimeoutSec с — смотри лог юнита"
            exit 1
        }
        foreach ($proc in $seen) {
            Write-Host ("[{0}] живой pid={1} start={2}" -f $unit, $proc.ProcessId, $proc.CreationDate)
        }
    }
}

Write-Host "готово"
