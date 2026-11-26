# Восстановление локальной базы Сарафана из дампа боевой (Windows).
#
# Сценарий переезда: боевой конвейер остановлен → снят финальный дамп →
# этот скрипт пересоздаёт локальную базу из дампа → сверяет счётчики.
#
# Пересоздание, а не долив: локальная база — копия, и после переезда она
# становится боевой. Долить поверх копии означало бы оставить в боевой базе
# посты, вердикты и счётчики, которых на проде никогда не было (например,
# строки, записанные локальной сверкой классификатора).
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File infra\windows\restore_db.ps1 `
#     -DumpFile .\downloads\sarafan_migration_final.dump
#
# Опции:
#   -EnvFile      путь к env-файлу стенда (по умолчанию config\local-stand.env)
#   -DumpFile     файл дампа (обязателен)
#   -KeepDatabase 1 — не пересоздавать базу, а долить дамп поверх (осторожно:
#                   в боевой базе останутся лишние строки)
#   -CountersOut  куда положить построчные счётчики (для сверки с продом)

param(
    [string]$EnvFile = "",
    [string]$DumpFile = "",
    [string]$CountersOut = "",
    [switch]$KeepDatabase
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\common.ps1"

if (-not $DumpFile) {
    Write-Error "нужен -DumpFile (файл дампа pg_dump -Fc)"
    exit 2
}
if (-not (Test-Path $DumpFile)) {
    Write-Error "файла дампа нет: $DumpFile"
    exit 2
}

# --- разбор DATABASE_URL (значение не печатается) ---
$envPath = if ($EnvFile) { (Resolve-Path $EnvFile).Path } else { Get-StandEnvFile }
if (-not $envPath) {
    Write-Error "env-файл стенда не найден (config\local-stand.env)"
    exit 2
}
$url = $null
foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
    if ($line -match '^DATABASE_URL=(.*)$') { $url = $matches[1].Trim() }
}
if (-not $url) {
    Write-Error "в env-файле нет DATABASE_URL"
    exit 2
}
$parsed = [System.Uri]$url.Replace("postgresql+asyncpg://", "postgresql://")
$dbHost = $parsed.Host
$dbPort = $parsed.Port
$dbUser = $parsed.UserInfo.Split(":")[0]
$dbPass = $parsed.UserInfo.Split(":")[1]
$dbName = $parsed.AbsolutePath.TrimStart("/")

$pgBin = Join-Path (Get-StandRepoRoot) "downloads\pgsql\bin"
if (-not (Test-Path (Join-Path $pgBin "pg_restore.exe"))) {
    Write-Error "не найден pg_restore.exe в $pgBin"
    exit 2
}
$env:PGPASSWORD = $dbPass

# --- пересоздание базы ---
if (-not $KeepDatabase) {
    Write-Host "[restore] пересоздаю базу $dbName на $dbHost`:$dbPort"
    & (Join-Path $pgBin "psql.exe") -h $dbHost -p $dbPort -U $dbUser -d "postgres" -v ON_ERROR_STOP=1 -c `
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$dbName' AND pid <> pg_backend_pid();" | Out-Null
    & (Join-Path $pgBin "psql.exe") -h $dbHost -p $dbPort -U $dbUser -d "postgres" -v ON_ERROR_STOP=1 -c `
        "DROP DATABASE IF EXISTS ""$dbName"";" | Out-Null
    & (Join-Path $pgBin "psql.exe") -h $dbHost -p $dbPort -U $dbUser -d "postgres" -v ON_ERROR_STOP=1 -c `
        "CREATE DATABASE ""$dbName"" OWNER ""$dbUser"";" | Out-Null
}

# --- восстановление ---
Write-Host "[restore] pg_restore: $DumpFile -> $dbName"
& (Join-Path $pgBin "pg_restore.exe") -h $dbHost -p $dbPort -U $dbUser -d $dbName --no-owner --no-privileges $DumpFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "pg_restore вернул $LASTEXITCODE"
    exit 1
}

# --- счётчики: тот же запрос, что на проде (infra/windows/prod_counters.sh) ---
$sql = "SELECT c.relname || '|' || (xpath('/row/cnt/text()', query_to_xml(format('SELECT count(*) AS cnt FROM %I', c.relname), false, true, '')))[1]::text::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname = 'public' ORDER BY c.relname;"
$counters = & (Join-Path $pgBin "psql.exe") -h $dbHost -p $dbPort -U $dbUser -d $dbName -At -c $sql
if ($CountersOut) {
    $counters | Out-File -FilePath $CountersOut -Encoding UTF8
    Write-Host "[restore] счётчики записаны: $CountersOut ($($counters.Count) таблиц)"
}
else {
    $counters | ForEach-Object { Write-Host $_ }
}
Write-Host "[restore] ГОТОВО"
