# Общая часть скриптов локального стенда Сарафана (Windows).
#
# Точка входа — dot-source:  . "$PSScriptRoot\common.ps1"
#
# Что здесь решается:
# * корень репозитория ищется от расположения скрипта (скрипт запускают
#   откуда угодно);
# * путь к venv и к env-файлу — в одном месте, а не в каждом скрипте;
# * env-файл попадает в процесс через SETKA_ENV_FILES — на Windows нет
#   systemd'овского EnvironmentFile=, его читает сам процесс
#   (infra/windows/local_env.py);
# * логи пишутся в logs/ рядом с репозиторием (как на проде — отдельными
#   файлами на каждый юнит).
#
# Два режима запуска:
# * **фоновый** (по умолчанию) — скрипт возвращает pid и сразу выходит;
# * **-Foreground** — скрипт живёт вместе с процессом и выходит с его кодом.
#   Этот режим нужен планировщику задач Windows: он supervising'ит процесс
#   скрипта, а значит — и процесс приложения. Упал python → упал скрипт →
#   задача завершилась → планировщик перезапустил её (аналог `Restart=always`).

Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:VenvPython = Join-Path $script:RepoRoot "venv\Scripts\python.exe"
$script:LogDir = Join-Path $script:RepoRoot "logs"

function Get-StandRepoRoot { return $script:RepoRoot }

function Get-StandEnvFile {
    param([string]$EnvFile)
    if ($EnvFile) { return (Resolve-Path $EnvFile).Path }
    $default = Join-Path $script:RepoRoot "config\local-stand.env"
    if (Test-Path $default) { return $default }
    return $null
}

function Test-StandPrerequisites {
    if (-not (Test-Path $script:VenvPython)) {
        Write-Error "venv не найден: $($script:VenvPython). Создай его: py -3.12 -m venv venv"
        exit 2
    }
    if (-not (Test-Path (Join-Path $script:RepoRoot "main.py"))) {
        Write-Error "корень репозитория не найден: $($script:RepoRoot)"
        exit 2
    }
}

function Initialize-StandEnvironment {
    param([string]$EnvFile)
    Test-StandPrerequisites
    if (-not (Test-Path $script:LogDir)) {
        New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
    }
    $resolved = Get-StandEnvFile -EnvFile $EnvFile
    if ($resolved) {
        # Разделитель путей на Windows — «;» (os.pathsep).
        $env:SETKA_ENV_FILES = $resolved
    }
    # Каталог mmap-файлов метрик — относительный путь разрешается от
    # рабочего каталога процесса, поэтому задаём его явно от корня репо.
    if (-not $env:PROMETHEUS_MULTIPROC_DIR) {
        $env:PROMETHEUS_MULTIPROC_DIR = Join-Path $script:LogDir "prom_multiproc"
    }
    if (-not (Test-Path $env:PROMETHEUS_MULTIPROC_DIR)) {
        New-Item -ItemType Directory -Force -Path $env:PROMETHEUS_MULTIPROC_DIR | Out-Null
    }
    return $resolved
}

function Test-StandWebHealth {
    param([int]$Port = 8000, [int]$TimeoutSec = 5)
    # Отвечает ли живой экземпляр веба на порту.
    #
    # Нужно супервизору: остановка задачи убивает обёртку, но не сам python —
    # процесс-сирота остаётся держать порт. Следующая попытка старта падает с
    # «address already in use», задача уходит в бесконечный цикл ошибок, а
    # страница при этом жива. Проверка здоровья различает «порт занят живым
    # экземпляром» (ничего не делаем) и «порт свободен» (поднимаем свой).
    #
    # ⚠️ Докстринг здесь — комментарием, а не строкой: в PowerShell одиночная
    # строка в теле функции уходит в поток вывода, и функция вернула бы
    # «[текст, False]». Массив в булевом контексте истинен — супервизор решил бы,
    # что порт занят, и никогда не поднял бы экземпляр. Это случилось
    # 26.11.2026: молчащий супервизор, пустой порт и ноль публикаций.
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health/" `
            -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
}

function Start-StandProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$LogFileName = "",
        [switch]$Foreground
    )
    # Оператор ?? есть только в PowerShell 7+, а на машине владельца — 5.1.
    if (-not $LogFileName) { $LogFileName = "$Name.log" }
    $stdout = Join-Path $script:LogDir $LogFileName
    $stderr = Join-Path $script:LogDir "$Name.err.log"
    Write-Host "[$Name] старт: $($Arguments -join ' ')"
    Write-Host "[$Name] логи: $stdout"

    if ($Foreground) {
        # Ждём процесс и уходим с его кодом возврата — планировщик задач
        # видит завершение и перезапускает задачу.
        $proc = Start-Process -FilePath $script:VenvPython -ArgumentList $Arguments `
            -WorkingDirectory $script:RepoRoot -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        exit $proc.ExitCode
    }

    $proc = Start-Process -FilePath $script:VenvPython -ArgumentList $Arguments `
        -WorkingDirectory $script:RepoRoot -NoNewWindow -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    return $proc
}
