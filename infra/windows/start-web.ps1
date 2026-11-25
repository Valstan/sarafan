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

$proc = Start-StandProcess -Name "setka-web" -LogFileName "setka-web.log" -Foreground:$Foreground -Arguments @(
    "-m", "uvicorn", "main:app",
    "--host", $BindAddress,
    "--port", "$Port",
    "--workers", "1"
)

Write-Host "[setka-web] pid=$($proc.Id) http://${BindAddress}:$Port"
