# Веб Сарафана на Windows — замена systemd-юнита `setka`.
#
# На проде: uvicorn слушает 127.0.0.1:8000, за ним — nginx с доменом.
# На локальной машине nginx не нужен: интерфейс открывается прямо по
# http://127.0.0.1:8000 — и слушать что-то кроме 127.0.0.1 нельзя
# (машина владельца в домашней сети, а не боевой сервер).

param(
    [string]$EnvFile = "",
    [int]$Port = 8000,
    # Имя НЕ $Host: в PowerShell $Host — автоматическая переменная,
    # переопределить её нельзя.
    [string]$BindAddress = "127.0.0.1",
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

if ($Foreground) {
    # Режим планировщика: скрипт не «ждёт один процесс», а **супервизит**:
    # держит ровно один живой экземпляр и сам поднимает новый, если тот упал.
    #
    # Почему не «просто ждать процесса»: остановка задачи убивает обёртку
    # (powershell), но не python — тот остаётся сиротой и держит порт. Следующая
    # попытка старта падает с «address already in use», задача уходит в цикл
    # ошибок, а страница при этом жива и никто не понимает, что происходит.
    # Супервизор различает три состояния: порт занят живым экземпляром (не
    # плодим второй), порт свободен (поднимаем свой), экземпляр умер (поднимаем).
    #
    # Свой лог — обязателен: окно скрыто, и без файла не видно ни слова о том,
    # что супервизор делает. Молчащий супервизор — это ровно тот класс отказа,
    # о котором предупреждает канон: «процесс жив, а что он делает — неизвестно».
    $supervisorLog = Join-Path (Join-Path $PSScriptRoot "..\..") "logs\setka-web-supervisor.log"

    function Write-SupervisorLog {
        param([string]$Message)
        try {
            Add-Content -Path $supervisorLog -Encoding UTF8 -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
        }
        catch {
            # Лог не пишется — молча продолжаем: супервизор важнее журнала.
        }
    }

    Write-SupervisorLog "супервизор запущен, порт $Port"
    while ($true) {
        $healthy = Test-StandWebHealth -Port $Port
        if (-not $healthy) {
            Write-SupervisorLog "экземпляра нет — поднимаю"
            try {
                $new = Start-StandProcess -Name "setka-web" -LogFileName "setka-web.log" -Arguments @(
                    "-m", "uvicorn", "main:app",
                    "--host", $BindAddress,
                    "--port", "$Port",
                    "--workers", "1"
                )
                Write-SupervisorLog "поднят экземпляр, pid $($new.Id)"
            }
            catch {
                Write-SupervisorLog "не поднялся: $($_.Exception.GetType().Name): $($_.Exception.Message)"
            }
        }
        else {
            Write-SupervisorLog "экземпляр отвечает — не плодю второй"
        }
        Start-Sleep -Seconds 30
    }
}

$proc = Start-StandProcess -Name "setka-web" -LogFileName "setka-web.log" -Foreground:$Foreground -Arguments @(
    "-m", "uvicorn", "main:app",
    "--host", $BindAddress,
    "--port", "$Port",
    "--workers", "1"
)

Write-Host "[setka-web] pid=$($proc.Id) http://${BindAddress}:$Port"
