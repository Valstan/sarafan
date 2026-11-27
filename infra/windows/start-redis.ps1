# Redis локального стенда — замена ручного запуска.
#
# Заказ владельца 2026-11-27: «сайт должен работать после перезагрузки
# компьютера без входа в систему». Redis — брокер celery и общий кеш, и до
# 27.11 он был просто процессом, запущенным вручную: после перезагрузки
# (без входа владельца) его не было, а вместе с ним не работали ни воркеры,
# ни ограничения движка, ни rate-limiter.
#
# Службой Windows он не стал: на каталог с бинарниками доступ есть только у
# владельца (ACL: PC79\valstan), а служба идёт под LocalSystem и получает
# «Access is denied». Поэтому Redis — та же задача планировщика, что и
# остальные безлогинные юниты: «при старте системы», вход S4U (без пароля
# в задаче). PostgreSQL — настоящая служба Windows, он поднимается сам.
#
# Параметры — ровно те, с которыми Redis работал вручную: порт 6379, только
# localhost, без персистентности (в Redis лежат очереди и кеши, а не данные).
#
# Применение:
#   powershell -ExecutionPolicy Bypass -File infra\windows\start-redis.ps1
#   powershell -ExecutionPolicy Bypass -File infra\windows\start-redis.ps1 -Foreground

param(
    [int]$Port = 6379,
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDir = Join-Path $repoRoot "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

# Ищем сборку Redis в downloads: версия в папке меняется, жёстко её не пишем.
$redisDir = Get-ChildItem -Path (Join-Path $repoRoot "downloads") -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "Redis-*" } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $redisDir) {
    Write-Error "каталог сборки Redis не найден в downloads\"
    exit 2
}
$serviceExe = Join-Path $redisDir "RedisService.exe"
$redisExe = Join-Path $redisDir "redis-server.exe"
if (-not (Test-Path $serviceExe)) {
    Write-Error "RedisService.exe не найден: $serviceExe"
    exit 2
}

$stdout = Join-Path $logDir "setka-redis.log"
$stderr = Join-Path $logDir "setka-redis.err.log"

# Путь данных: каталог сборки. Персистентности нет (save "" / appendonly no),
# поэтому он по сути не используется — но redis-server требует его валидным.
#
# ⚠️ Запускаем через RedisService.exe run --foreground, а НЕ напрямую
# redis-server.exe: сборка msys2 форкается (родитель выходит, остаётся
# сирота), и задача планировщика на этом теряет управление — она считает
# юнит упавшим и перезапускает его раз в минуту, а новый redis падает с
# «Address already in use». Обёртка держит процесс в переднем плане, и задача
# живёт ровно столько, сколько живёт redis.
$args = @(
    "run", "--foreground",
    "-c", (Join-Path $PSScriptRoot "redis-stand.conf"),
    "--port", "$Port",
    "--dir", $redisDir
)

Write-Host "[setka-redis] старт: $serviceExe $($args -join ' ')"
Write-Host "[setka-redis] логи: $stdout"

if ($Foreground) {
    # Режим планировщика: процесс живёт вместе с задачей, и когда он падает,
    # задача завершается — планировщик перезапускает её (RestartCount 999).
    $proc = Start-Process -FilePath $serviceExe -ArgumentList $args -WorkingDirectory $redisDir `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    exit $proc.ExitCode
}

$proc = Start-Process -FilePath $serviceExe -ArgumentList $args -WorkingDirectory $redisDir `
    -NoNewWindow -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
Write-Host "[setka-redis] pid=$($proc.Id) redis://127.0.0.1:$Port"
