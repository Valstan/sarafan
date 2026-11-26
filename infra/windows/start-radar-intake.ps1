# Приём радара на Windows — замена systemd-юнита `setka-radar-intake`.
#
# На проде это демон приёма: VK Bots Long Poll + Telegram getUpdates. Он же
# держит живыми сессии радара и доставку.
#
# Судьба самого радара — отдельное решение владельца
# (docs/RADAR_FATE.md). Пока решение «оставляем в Сарафане», юнит переносится
# как есть; если решение «выносим в отдельный проект» — этот скрипт и
# `modules/radar/` уезжают вместе с ним, и юнит на этой машине снимается.

param(
    [string]$EnvFile = "",
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

$proc = Start-StandProcess -Name "setka-radar-intake" -LogFileName "radar-intake.log" -Foreground:$Foreground -Arguments @(
    "scripts\radar_intake_daemon.py"
)

Write-Host "[setka-radar-intake] pid=$($proc.Id)"
