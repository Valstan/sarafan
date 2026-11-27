# Обратный SSH-туннель: локальная машина → боевой VPS, для публичной витрины.
#
# Зачем он нужен (заказ владельца 2026-11-27): витрина САРАФАНА должна быть
# доступна по публичному адресу валентин.вмалмыже.рф, а данные и заявки —
# идти с локальной машины владельца. Публичного адреса у локальной машины нет
# (она в домашней сети), поэтому VPS работает как публичный edge, а туннель —
# как транспорт данных: наружу у машины владельца не открывается НИ ОДНОГО
# порта, слушает туннель только 127.0.0.1 на VPS.
#
#     ssh -N -R 127.0.0.1:18000:127.0.0.1:8000 sarafan
#
# На VPS nginx берёт витрину с 127.0.0.1:18000 (config/nginx_valentin.conf).
# Белый список путей — там же: в приложение уходит ровно четыре пути витрины.
#
# Ключ — из ~/.ssh/config (алиас sarafan → id_ed25519_setka_prod). Ключ без
# пароля, иначе планировщик (неинтерактивный) не смог бы его открыть.
#
# Применение:
#   powershell -ExecutionPolicy Bypass -File infra\windows\start-tunnel.ps1
#   powershell -ExecutionPolicy Bypass -File infra\windows\start-tunnel.ps1 -Foreground

param(
    [string]$SshHost = "sarafan",
    # Порт приложения на этой машине.
    [int]$LocalPort = 8000,
    # Порт на VPS, который слушает туннель (только 127.0.0.1 — наружу не открыт).
    [int]$RemotePort = 18000,
    # Пауза перед переподключением, если ssh отвалился.
    [int]$ReconnectDelaySec = 15,
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDir = Join-Path $repoRoot "logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir "setka-tunnel.log"
$attemptOut = Join-Path $logDir "setka-tunnel.attempt.log"
$attemptErr = Join-Path $logDir "setka-tunnel.attempt.err.log"

function Write-TunnelLog {
    param([string]$Message)
    try {
        Add-Content -Path $logFile -Encoding UTF8 -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
    }
    catch {
        # Лог не пишется — молча продолжаем: туннель важнее журнала.
    }
}

$sshExe = "C:\windows\System32\OpenSSH\ssh.exe"
if (-not (Test-Path $sshExe)) {
    $cmd = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if ($cmd) { $sshExe = $cmd.Source }
    else {
        Write-Error "ssh.exe не найден: нужен встроенный клиент OpenSSH (Windows 10+)"
        exit 2
    }
}

$sshArgs = @(
    "-N",
    # Порт занят (зависшая сессия) — падать сразу, а не молча работать без
    # форварда: иначе nginx на VPS отдаёт 502, и причина неочевидна.
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=20",
    "-o", "ServerAliveCountMax=3",
    "-o", "BatchMode=yes",
    "-R", "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}",
    $SshHost
)

Write-TunnelLog "туннель: $SshHost -R 127.0.0.1:${RemotePort} -> 127.0.0.1:${LocalPort} (локальная машина)"

if (-not $Foreground) {
    $proc = Start-Process -FilePath $sshExe -ArgumentList $sshArgs -PassThru -WindowStyle Hidden
    Write-Host "[setka-tunnel] pid=$($proc.Id) $SshHost -R 127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}"
    exit 0
}

# Режим планировщика: супервизит ssh. Упал (сеть, рестарт VPS, обрыв) — ждём
# паузу и поднимаем снова. Без этого витрина лежала бы молча до следующего
# входа владельца в систему.
$attempt = 0
while ($true) {
    $attempt++
    Write-TunnelLog "подключение, попытка $attempt"
    try {
        $proc = Start-Process -FilePath $sshExe -ArgumentList $sshArgs -PassThru -NoNewWindow -Wait `
            -RedirectStandardOutput $attemptOut -RedirectStandardError $attemptErr
        $code = $proc.ExitCode
        Write-TunnelLog "ssh завершился с кодом $code"
    }
    catch {
        Write-TunnelLog "не удалось запустить ssh: $($_.Exception.Message)"
    }
    # Вывод ssh дописываем в общий лог: сам ssh перезаписывает свои файлы при
    # каждом запуске, а лог должен быть историей (P257 — про обнуление).
    foreach ($f in @($attemptOut, $attemptErr)) {
        if (Test-Path $f) {
            try {
                $chunk = Get-Content $f -Raw -ErrorAction SilentlyContinue
                if ($chunk) { Add-Content -Path $logFile -Encoding UTF8 -Value $chunk }
            }
            catch { }
        }
    }
    Start-Sleep -Seconds $ReconnectDelaySec
}
