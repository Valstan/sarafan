# Ежедневный бэкап локальной базы Сарафана (Windows).
#
# Зачем свой скрипт. На боевом боксе штатный `scripts/backup_database.sh` падал
# на разборе `DATABASE_URL` (экранированные кавычки внутри одинарных) и
# оставлял пустые файлы — рабочего ежедневного бэкапа не было с октября
# (P242). После переезда локальная машина — единственное место, где лежит
# боевая база, поэтому бэкап здесь обязателен, а не опционален.
#
# Что делает:
# 1. берёт DATABASE_URL из env-файла стенда (значение не печатается);
# 2. снимает дамп `pg_dump -Fc` в каталог ВНЕ репозитория (данные не должны
#    жить рядом с кодом: `git add -A` в /close_session их подхватит);
# 3. проверяет размер: пустой файл бэкапом не считается (ровно та ошибка,
#    из-за которой бокс молчал полгода);
# 4. оставляет последние 7 дампов.
#
# Запуск — задача планировщика `sarafan-db-backup` (ежедневно в 03:00),
# регистрируется вместе с юнитами стенда.

param(
    [string]$EnvFile = "",
    [string]$BackupDir = "",
    [int]$Keep = 7,
    [int]$MinBytes = 1048576
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\common.ps1"

$repoRoot = Get-StandRepoRoot
if (-not $EnvFile) {
    $EnvFile = Get-StandEnvFile
}
if (-not $EnvFile) {
    Write-Error "env-файл стенда не найден (config\local-stand.env)"
    exit 2
}
if (-not $BackupDir) {
    # Вне репозитория: бэкапы — это данные, а не код.
    $BackupDir = Join-Path $env:LOCALAPPDATA "sarafan-backups"
}
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

$logFile = Join-Path $repoRoot "logs\backup.log"
function Write-BackupLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $logFile -Encoding UTF8 -Value $line
    Write-Host $line
}

# --- разбор DATABASE_URL (значение не печатается) ---
$url = $null
foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
    if ($line -match '^DATABASE_URL=(.*)$') { $url = $matches[1].Trim() }
}
if (-not $url) {
    Write-BackupLog "ОШИБКА: в env-файле нет DATABASE_URL"
    exit 2
}
$parsed = [System.Uri]$url.Replace("postgresql+asyncpg://", "postgresql://")
$dbHost = $parsed.Host
$dbPort = $parsed.Port
$dbUser = $parsed.UserInfo.Split(":")[0]
$dbPass = $parsed.UserInfo.Split(":")[1]
$dbName = $parsed.AbsolutePath.TrimStart("/")

$pgBin = Join-Path $repoRoot "downloads\pgsql\bin"
$pgDump = Join-Path $pgBin "pg_dump.exe"
if (-not (Test-Path $pgDump)) {
    Write-BackupLog "ОШИБКА: не найден pg_dump.exe в $pgBin"
    exit 2
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dumpFile = Join-Path $BackupDir "sarafan_backup_$stamp.dump"
$tmpFile = "$dumpFile.part"

$env:PGPASSWORD = $dbPass
Write-BackupLog "дамп: $dbName@$dbHost`:$dbPort -> $dumpFile"
# Во временный файл и только потом переносим: перенаправление создаёт целевой
# файл ДО pg_dump, и упавший дамп остался бы «бэкапом» нулевого размера.
& $pgDump -h $dbHost -p $dbPort -U $dbUser -d $dbName --format=custom --no-owner --no-privileges --file="$tmpFile"
if ($LASTEXITCODE -ne 0) {
    Write-BackupLog "ОШИБКА: pg_dump вернул $LASTEXITCODE — дамп не засчитан"
    Remove-Item $tmpFile -ErrorAction SilentlyContinue
    exit 1
}

$size = (Get-Item $tmpFile).Length
if ($size -lt $MinBytes) {
    Write-BackupLog "ОШИБКА: дамп в $size байт (мало) — бэкап НЕ засчитан, прошлый не тронут"
    Remove-Item $tmpFile -ErrorAction SilentlyContinue
    exit 1
}
Move-Item $tmpFile $dumpFile -Force
Write-BackupLog "ГОТОВО: $([Math]::Round($size / 1MB, 1)) МБ"

# --- ротация: последние N дампов ---
$all = Get-ChildItem $BackupDir -Filter "sarafan_backup_*.dump" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending
if ($all.Count -gt $Keep) {
    foreach ($old in $all[$Keep..($all.Count - 1)]) {
        Write-BackupLog "удаляю старый дамп: $($old.Name)"
        Remove-Item $old.FullName -ErrorAction SilentlyContinue
    }
}
Write-BackupLog "осталось дампов: $(@(Get-ChildItem $BackupDir -Filter 'sarafan_backup_*.dump' -ErrorAction SilentlyContinue).Count)"
