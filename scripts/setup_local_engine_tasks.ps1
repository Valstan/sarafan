#
# **Что это.** Локальный движок на ПК владельца состоит из двух частей, и обе
# должны подниматься сами после входа в систему:
#
# 1. ``opencode serve --service --port 4097`` — сервер движка (free-тир моделей,
#    пароль он пишет сам в ``~/.config/opencode/service.json``);
# 2. демон моста (``scripts/local_engine_daemon.py``) — тянет батч по SSH, считает
#    локально и отдаёт вердикты. По умолчачанию — **в тени**: вердикты не пишутся
#    в корпус, пока качество локальной модели не измерено.
#
# **Запуск (один раз, от имени владельца):**
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_local_engine_tasks.ps1
#
# Задачи: «SETKA Local Engine - opencode serve» и «SETKA Local Engine - daemon».
# Обе — с перезапуском при падении и без ограничения по времени.
#
# Примечание: файл сохранён в UTF-8 **с BOM** — Windows PowerShell 5.1 читает
# .ps1 без BOM как ANSI, и кириллица в комментариях ломает разбор файла.
#

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$opencodeCmd = Join-Path $env:APPDATA 'npm\opencode.cmd'
$python = Join-Path $repo 'venv\Scripts\python.exe'
$port = 4097

if (-not (Test-Path $opencodeCmd)) {
    throw "не найден opencode: $opencodeCmd"
}
if (-not (Test-Path $python)) {
    throw "не найден python проекта: $python"
}

$commonSettings = @{
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable = $true
    ExecutionTimeLimit = ([TimeSpan]::Zero)
    RestartCount = 5
    RestartInterval = (New-TimeSpan -Minutes 1)
}

# ── 1. Сервер движка ──
$actionServe = New-ScheduledTaskAction -Execute $opencodeCmd -Argument ('serve --service --port {0}' -f $port)
$triggerServe = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settingsServe = New-ScheduledTaskSettingsSet @commonSettings
Register-ScheduledTask -TaskName 'SETKA Local Engine - opencode serve' -Action $actionServe -Trigger $triggerServe -Settings $settingsServe -Description 'Локальный движок классификатора: opencode serve на 127.0.0.1:4097 (free-тир моделей).' -Force | Out-Null

# ── 2. Демон моста ──
# Режим боевой: вердикты пишутся в корпус (source=local), облако забирает то,
# что локальный движок не успел. Режим тени — добавь `--shadow`, если правку
# корпуса трогать нельзя.
#
# `--regions-per-chunk 2`: free-тир-модель на большом чанке (8 регионов,
# до 40 постов) уходит в длинное рассуждение и не отвечает (замер 10.11: чанк
# из 23 постов молчал 11 минут). По два региона на вызов — быстро и стабильно.
# `--model-timeout 100`: зависшая модель в цепочке съедает свой потолок и не
# блокирует следующую.
$daemonScript = Join-Path $repo 'scripts\local_engine_daemon.py'
$daemonArgs = '"{0}" --engine opencode --model-url http://127.0.0.1:{1} --regions-per-chunk 2 --model-timeout 100 --interval 300' -f $daemonScript, $port
$actionDaemon = New-ScheduledTaskAction -Execute $python -Argument $daemonArgs -WorkingDirectory $repo
$triggerDaemon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$triggerDaemon.Delay = 'PT1M'
$settingsDaemon = New-ScheduledTaskSettingsSet @commonSettings
Register-ScheduledTask -TaskName 'SETKA Local Engine - daemon' -Action $actionDaemon -Trigger $triggerDaemon -Settings $settingsDaemon -Description 'Демон моста Pull: тянет батч по SSH, считает локально, отдаёт вердикты в корпус (source=local).' -Force | Out-Null

foreach ($name in @('SETKA Local Engine - opencode serve', 'SETKA Local Engine - daemon')) {
    $t = Get-ScheduledTask -TaskName $name
    Write-Output ('{0}: state={1}' -f $name, $t.State)
    Write-Output ('   execute: {0}' -f $t.Actions[0].Execute)
    Write-Output ('   args:    {0}' -f $t.Actions[0].Arguments)
}
