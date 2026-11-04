$ErrorActionPreference = "Continue"
Write-Host "=== Reiniciando Codex ChatGPT Bridge (Turbo v0.2.0) ===" -ForegroundColor Cyan

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
$venvPython = Join-Path $repoDir ".venv\Scripts\python.exe"
$bridgeExe = Join-Path $repoDir ".venv\Scripts\codex-chatgpt-bridge.exe"

if (Test-Path $bridgeExe) {
    Write-Host "Iniciando bridge en segundo plano desde $bridgeExe..." -ForegroundColor Green
    Start-Process -FilePath $bridgeExe -ArgumentList "run" -WorkingDirectory $repoDir -WindowStyle Hidden
} elseif (Test-Path $venvPython) {
    Write-Host "Iniciando bridge con Python del .venv..." -ForegroundColor Green
    Start-Process -FilePath $venvPython -ArgumentList "-m", "codex_chatgpt_bridge.cli", "run" -WorkingDirectory $repoDir -WindowStyle Hidden
} else {
    Write-Host "Iniciando bridge con codex-chatgpt-bridge global..." -ForegroundColor Green
    Start-Process -FilePath "codex-chatgpt-bridge" -ArgumentList "run" -WorkingDirectory $repoDir -WindowStyle Hidden
}

Start-Sleep -Seconds 2
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8766/healthz" -TimeoutSec 5
    Write-Host "Bridge activo y respondiendo:" ($health | ConvertTo-Json -Compress) -ForegroundColor Green
} catch {
    Write-Host "Advertencia: El servidor aun esta iniciando o no respondio en 8766." -ForegroundColor Red
}
