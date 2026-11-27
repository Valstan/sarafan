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
#
# Флаг `NoLogin = $true` — юнит должен подниматься БЕЗ входа пользователя в
# систему (заказ владельца 2026-11-27: «сайт должен работать после
# перезагрузки компьютера без входа в систему»). Для таких юнитов триггер —
# «при старте системы», а вход — S4U (без сохранения пароля учётной записи в
# задаче; право «Log on as a batch job» у группы Users на машине есть).
#
# Почему не все юниты такие: воркеры и beat поднимают CLI-движок MiniMax и
# прочие консольные инструменты, которым нужна интерактивная сессия; их
# перевод на S4U — отдельное решение владельца (см. PENDING). Юниты без
# флага работают как раньше: «при входе в систему», в сессии владельца.
$units = @(
    @{ Name = "setka"; Script = "start-web.ps1"; Description = "SETKA FastAPI (веб-интерфейс)"; NoLogin = $true },
    @{ Name = "setka-celery-worker"; Script = "start-worker.ps1"; Description = "SETKA Celery Worker (очередь celery)" },
    @{ Name = "setka-celery-worker-wave"; Script = "start-wave-worker.ps1"; Description = "SETKA Celery Worker (очереди wave,digest)" },
    @{ Name = "setka-celery-beat"; Script = "start-beat.ps1"; Description = "SETKA Celery Beat (расписание)" },
    @{ Name = "setka-vk-bot"; Script = "start-vk-bot.ps1"; Description = "SETKA VK bot (Bots Long Poll)" },
    @{ Name = "setka-radar-intake"; Script = "start-radar-intake.ps1"; Description = "SETKA radar intake (VK Long Poll + Telegram getUpdates)" },
    # Обратный SSH-туннель на боевой VPS: публичная витрина САРАФАНА
    # (валентин.вмалмыже.рф) с данными с этой машины. Env-файл ему не нужен —
    # он запускает ssh, а не приложение (NoEnv). Без входа в систему туннель
    # тоже должен подниматься, иначе публичный сайт ляжет до входа владельца.
    @{ Name = "setka-public-tunnel"; Script = "start-tunnel.ps1"; Description = "Обратный SSH-туннель на VPS: публичная витрина САРАФАНА"; NoEnv = $true; NoLogin = $true },
    # Redis — брокер celery и общий кеш. До 27.11 он был просто процессом,
    # запущенным вручную, и после перезагрузки не появлялся: без него не
    # работают ни воркеры, ни ограничения движка. Службой Windows он не
    # стал: на папку с бинарниками доступ есть только у владельца, а
    # LocalSystem (под которым идёт служба) к ней не допущен — отсюда
    # «Access is denied» при старте. Задача с S4U работает под владельцем и
    # поднимается без входа в систему.
    @{ Name = "setka-redis"; Script = "start-redis.ps1"; Description = "Redis локального стенда (брокер celery и общий кеш)"; NoEnv = $true; NoLogin = $true }
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

    # ⚠️ ContainsKey, а не `$unit.NoEnv`: обращение к ОТСУТСТВУЮЩЕМУ ключу
    # хеша под Set-StrictMode (common.ps1) роняет строку — и переменная
    # $envArg остаётся от ПРЕДЫДУЩЕГО юнита. Так юнит без NoEnv получал
    # чужой env-файл, а юнит с флагом NoEnv — чужой (или никакой).
    $envArg = ""
    if ($EnvFile -and $unit.ContainsKey("NoEnv") -and -not $unit.NoEnv) {
        $envArg = "-EnvFile `"$EnvFile`""
    }

    # Тихий запуск. Задача выполняется в сессии владельца, поэтому любая
    # консольная программа создаёт на экране окно. Скрытие стиля окна его
    # прячет, но не отменяет создания — на быстрых машинах это вспышка.
    # Поэтому действием задачи стоит wscript.exe (приложение без консоли),
    # а он запускает цепочку cmd -> powershell -> python скрыто.
    # Цепочка лежит в .cmd-файле: так в аргументы не тащится экранирование
    # кавычек, а планировщик получает одну короткую команду.
    $launchDir = Join-Path $repoRoot "logs\launch"
    New-Item -ItemType Directory -Force -Path $launchDir | Out-Null
    $cmdFile = Join-Path $launchDir "$($unit.Name).cmd"
    $cmdBody = @"
@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "$scriptPath" -Foreground $envArg
"@
    Set-Content -Path $cmdFile -Value $cmdBody -Encoding ASCII

    $vbs = Join-Path $PSScriptRoot "hidden_run.vbs"
    if (-not (Test-Path $vbs)) {
        Write-Error "нет скрытого лаунчера: $vbs"
        exit 2
    }

    $action = New-ScheduledTaskAction `
        -Execute "$env:SystemRoot\System32\wscript.exe" `
        -Argument "//nologo `"$vbs`" `"$cmdFile`"" `
        -WorkingDirectory $repoRoot

    # Триггер и вход — по флагу NoLogin:
    #   * обычный юнит — «при входе в систему», LogonType Interactive:
    #     процесс живёт в сессии владельца (интерактивная среда нужна
    #     консольным инструментам движка);
    #   * юнит с NoLogin — «при старте системы», LogonType S4U: поднимается
    #     без входа владельца и БЕЗ сохранения его пароля в задаче
    #     (S4U = «Log on as a batch job», право у группы Users есть).
    #
    # ⚠️ ContainsKey — по той же причине, что и в NoEnv выше: отсутствующий
    # ключ под StrictMode роняет строку, и юнит наследует триггер предыдущего.
    $noLogin = $unit.ContainsKey("NoLogin") -and [bool]$unit.NoLogin
    if ($noLogin) {
        $trigger = New-ScheduledTaskTrigger -AtStartup
        # Пауза после старта системы: сеть и PostgreSQL к этому моменту ещё
        # поднимаются, а S4U-задача стартует сразу. Сама пауза не страшна:
        # у каждого такого юнита есть перезапуск, а туннель вдобавок
        # переподключается сам.
        $trigger.Delay = "PT1M"
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Limited
    }
    else {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    }

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

# --- Задача ежедневного бэкапа: НЕ по входу, а по часу (03:00). ---
# На боевом боксе это делал cron, который с октября падал на разборе
# DATABASE_URL и оставлял пустые файлы (P242) — то есть рабочего бэкапа
# боевой базы не было полгода. После переезда локальная машина — единственное
# место, где лежит боевая база, поэтому бэкап здесь обязателен.
$backupScript = Join-Path $PSScriptRoot "backup_db.ps1"
if (-not (Test-Path $backupScript)) {
    Write-Error "нет скрипта бэкапа: $backupScript"
    exit 2
}
$launchDir = Join-Path $repoRoot "logs\launch"
New-Item -ItemType Directory -Force -Path $launchDir | Out-Null
$backupCmd = Join-Path $launchDir "sarafan-db-backup.cmd"
$backupBody = @"
@echo off
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "$backupScript"
"@
Set-Content -Path $backupCmd -Value $backupBody -Encoding ASCII

$backupAction = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\wscript.exe" `
    -Argument "//nologo `"$vbs`" `"$backupCmd`"" `
    -WorkingDirectory $repoRoot
$backupTrigger = New-ScheduledTaskTrigger -Daily -At 03:00
$backupPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$backupSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "sarafan-db-backup" -Action $backupAction -Trigger $backupTrigger `
    -Principal $backupPrincipal -Settings $backupSettings `
    -Description "SETKA: ежедневный дамп локальной боевой базы (03:00)" -Force | Out-Null
Write-Host "зарегистрирована задача: sarafan-db-backup — ежедневный дамп базы (03:00)"

Write-Host ""
Write-Host "Проверка: Get-ScheduledTask -TaskName 'setka*'"
Write-Host "Запуск сейчас: Start-ScheduledTask -TaskName 'setka'"
