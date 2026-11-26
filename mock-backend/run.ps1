# Windows equivalent of run.sh: run the LSPcare mock (used by the Bland agent) on
# 127.0.0.1:8099 and expose it through a Cloudflare quick tunnel.
#
#   powershell -ExecutionPolicy Bypass -File mock-backend\run.ps1 [-NoTunnel]
#
# Prereqs: Python 3.11+ (py launcher) and, for the tunnel, cloudflared on PATH
# (winget install --id Cloudflare.cloudflared). Set $env:MOCK_BEARER first to
# keep the same token across restarts; otherwise a new one is generated.
param([int]$Port = 8099, [switch]$NoTunnel)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'

# PATH first, then the usual install folders (a fresh winget install is not on
# this terminal's PATH until it is reopened).
$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) {
    $cloudflared = @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe",
                     "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $NoTunnel -and -not $cloudflared) {
    throw 'cloudflared not found. Install it: winget install --id Cloudflare.cloudflared  (or use -NoTunnel)'
}
if (-not $env:MOCK_BEARER) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $env:MOCK_BEARER = -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { py -3 -m venv .venv; if ($LASTEXITCODE) { throw 'Could not create .venv' } }
& $python -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE) { throw 'Dependency install failed' }

Write-Host ''
Write-Host '=== LSPcare mock ==='
Write-Host "Bearer token: $env:MOCK_BEARER"
Write-Host "(paste this into the Bland secret used for the mock's Authorization header)"
Write-Host ''

$server = Start-Process -FilePath $python -NoNewWindow -PassThru `
    -ArgumentList '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', $Port
try {
    # Do not expose a tunnel unless this process is serving the API.
    $healthy = $false
    for ($i = 0; $i -lt 50 -and -not $server.HasExited; $i++) {
        try {
            if ((Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200) { $healthy = $true; break }
        } catch { Start-Sleep -Milliseconds 200 }
    }
    if (-not $healthy) { throw 'Mock did not become healthy; tunnel was not started' }
    if ($NoTunnel) {
        Write-Host "Mock running at http://127.0.0.1:$Port (no tunnel). Ctrl+C to stop."
        $server.WaitForExit()
    } else {
        Write-Host '=== Cloudflare tunnel starting: copy the trycloudflare.com URL below into the Bland agent ==='
        & $cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$Port"
    }
} finally {
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
}
