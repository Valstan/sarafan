# Регистрация задач планировщика Windows — замена systemd-юнитов Сарафана.
#
# На проде unit-файл systemd даёт три вещи: автостарт вместе с машиной
# (`WantedBy=multi-user.target`), перезапуск при падении (`Restart=always`)
# и «не убивать при остановке» (`KillMode`). Планировщик задач Windows
# умеет всё то же — и это ближайший аналог, потому что он **переживает
# закрытие окна**: процесс живёт в сессии пользователя, а не в окне.
#
# Отличия, которые надо знать заранее:
#
# * **Запуск — по входу в систему.** С триггером «при входе» задачи
#   стартуют, когда владелец залогинился. Пока никто не вошёл — стенд не
#   работает. Альтернатива «при старте системы» требует записи пароля
#   учётной записи в задаче, а это ровно то, чего канон не хочет.
# * **Уровень запуска — `Interactive`:** процесс идёт в сессии пользователя
#   и therefore имеет сеть (S4U — без сети, а VK и DeepSeek без сети не
#   работают).
# * **Перезапуск:** `RestartCount` + `RestartInterval` — аналог
#   `Restart=always`/`RestartSec=10`. Лимит снят (`RestartCount 999`),
#   потому что Windows всё равно считает его конечным.
# * **Время выполнения — ноль** (`ExecutionTimeLimit = 0`): иначе Windows
#   убьёт «зависшую» задачу через 72 часа. Это ровно тот случай, когда
#   система молча перестаёт публиковать.
#
# Применение:
#   powershell -ExecutionPolicy Bypass -File infra\windows\register-scheduled-tasks.ps1
#   powershell -ExecutionPolicy Bypass -File infra\windows\register-scheduled-tasks.ps1 -List
#   powershell -ExecutionPolicy Bypass -File infra\windows\register-scheduled-tasks.ps1 -Unregister

param(
    [switch]$Unregister,
    [switch]$List,
    [string]$EnvFile = ""
)

. "$PSScriptRoot\common.ps1"

$repoRoot = Get-StandRepoRoot

# Юнит прода → скрипт запуска на Windows. Имена задач совпадают с именами
# юнитов: так проще сверять «что чем заменено».
$units = @(
    @{ Name = "setka"; Script = "start-web.ps1"; Description = "SETKA FastAPI (веб-интерфейс)" },
    @{ Name = "setka-celery-worker"; Script = "start-worker.ps1"; Description = "SETKA Celery Worker (очередь celery)" },
    @{ Name = "setka-celery-worker-wave"; Script = "start-wave-worker.ps1"; Description = "SETKA Celery Worker (очереди wave,digest)" },
    @{ Name = "setka-celery-beat"; Script = "start-beat.ps1"; Description = "SETKA Celery Beat (расписание)" },
    @{ Name = "setka-vk-bot"; Script = "start-vk-bot.ps1"; Description = "SETKA VK bot (Bots Long Poll)" },
    @{ Name = "setka-radar-intake"; Script = "start-radar-intake.ps1"; Description = "SETKA radar intake (VK Long Poll + Telegram getUpdates)" }
)

if ($List) {
    Get-ScheduledTask -TaskName "setka*" -ErrorAction SilentlyContinue |
        Select-Object TaskName, State | Format-Table -AutoSize
    exit 0
}

if ($Unregister) {
    foreach ($unit in $units) {
        if (Get-ScheduledTask -TaskName $unit.Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $unit.Name -Confirm:$false
            Write-Host "снята задача: $($unit.Name)"
        }
    }
    exit 0
}

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    Write-Error "модуль ScheduledTasks недоступен (нужна Windows 8+/Server 2012+)"
    exit 2
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
foreach ($unit in $units) {
    $scriptPath = Join-Path $PSScriptRoot $unit.Script
    if (-not (Test-Path $scriptPath)) {
        Write-Error "нет скрипта запуска: $scriptPath"
        exit 2
    }

    $envArg = ""
    if ($EnvFile) { $envArg = "-EnvFile `"$EnvFile`"" }

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -Foreground $envArg" `
        -WorkingDirectory $repoRoot

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId

    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $unit.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $unit.Description `
        -Force | Out-Null

    Write-Host "зарегистрирована задача: $($unit.Name) — $($unit.Description)"
}

Write-Host ""
Write-Host "Проверка: Get-ScheduledTask -TaskName 'setka*'"
Write-Host "Запуск сейчас: Start-ScheduledTask -TaskName 'setka'"
