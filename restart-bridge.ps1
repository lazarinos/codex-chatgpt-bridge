$ErrorActionPreference = "Continue"
Write-Host "=== Reiniciando Bridge (v0.5.0) ===" -ForegroundColor Cyan

# Find any process listening on port 8766
$conns = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($p in $pids) {
        if ($p -gt 0 -and $p -ne $PID) {
            Write-Host "Deteniendo proceso anterior en puerto 8766 (PID: $p)..." -ForegroundColor Yellow
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 800
}

$repoDir = "C:\Users\LAZARINOS\codex-chatgpt-bridge-clean"

# Prefer global 'bridge' CLI command, fallback to python -m
$bridgeCmd = Get-Command "bridge" -ErrorAction SilentlyContinue
if ($bridgeCmd) {
    Write-Host "Iniciando bridge con comando 'bridge' ($($bridgeCmd.Source))..." -ForegroundColor Green
    Start-Process -FilePath $bridgeCmd.Source -ArgumentList "run" -WorkingDirectory $repoDir -WindowStyle Hidden
} else {
    Write-Host "Iniciando bridge con python -m codex_chatgpt_bridge.cli run..." -ForegroundColor Green
    Start-Process -FilePath "python.exe" -ArgumentList "-m", "codex_chatgpt_bridge.cli", "run" -WorkingDirectory $repoDir -WindowStyle Hidden
}

Start-Sleep -Seconds 2
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8766/healthz" -TimeoutSec 5
    Write-Host "Bridge activo y respondiendo:" ($health | ConvertTo-Json -Compress) -ForegroundColor Green
} catch {
    Write-Host "Advertencia: El servidor aun esta iniciando o no respondio en 8766." -ForegroundColor Red
}
