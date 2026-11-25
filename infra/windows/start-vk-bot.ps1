# VK-бот Сарафана (Long Poll сообщества) на Windows — замена systemd-юнита
# `setka-vk-bot`.
#
# На проде это демон, который держит Long Poll и слушает входящие события
# сообщества. Обычный процесс, без systemd-специфики, — переносится как есть.
#
# ВАЖНО: этот юнит работает с токеном сообщества САРАФАН. Токен привязан к
# адресу, с которого идут запросы: переезд на другую машину может упереться
# в привязку и лимиты по IP. Это проверяется ДО переезда, а не после
# (см. docs/MIGRATION_TO_WINDOWS.md, §«Риски»).

param(
    [string]$EnvFile = "",
    [switch]$Foreground
)

. "$PSScriptRoot\common.ps1"

$null = Initialize-StandEnvironment -EnvFile $EnvFile

$proc = Start-StandProcess -Name "setka-vk-bot" -LogFileName "vk-bot.log" -Foreground:$Foreground -Arguments @(
    "scripts\vk_bot_daemon.py"
)

Write-Host "[setka-vk-bot] pid=$($proc.Id)"
