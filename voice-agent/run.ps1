# Run the Twilio ConversationRelay comparison pilot on Windows in ONE terminal:
# starts a Cloudflare quick tunnel to 127.0.0.1:8101, saves its URL with
# configure.py --public-url, serves app.py, and once the tunnel answers /health
# points the Twilio number's own Voice webhook at it. Ctrl+C stops both.
#
#   powershell -ExecutionPolicy Bypass -File voice-agent\run.ps1 [-NoTunnel] [-NoNumberUpdate]
#
# First time only: .venv\Scripts\python.exe configure.py (hidden credential prompts).
# -NoNumberUpdate leaves Twilio routing alone and prints the URL to set by hand.
param([int]$Port = 8101, [switch]$NoTunnel, [switch]$NoNumberUpdate)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = '1'

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { py -3 -m venv .venv; if ($LASTEXITCODE) { throw 'Could not create .venv' } }
& $python -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE) { throw 'Dependency install failed' }

$configPath = & $python -c "import configure; print(configure.CONFIG)"
if (-not (Test-Path $configPath)) {
    throw "No private configuration at $configPath. Run first: .venv\Scripts\python.exe configure.py"
}
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

$tunnel = $null
$server = $null
$log = Join-Path ([IO.Path]::GetTempPath()) ("lspcare-twilio-tunnel-" + [guid]::NewGuid() + ".log")
try {
    if (-not $NoTunnel) {
        $tunnel = Start-Process -FilePath $cloudflared -NoNewWindow -PassThru -RedirectStandardError $log `
            -ArgumentList 'tunnel', '--no-autoupdate', '--url', "http://127.0.0.1:$Port"
        $url = $null
        for ($i = 0; $i -lt 60 -and -not $url -and -not $tunnel.HasExited; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-Path $log) {
                $match = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -First 1
                if ($match) { $url = $match.Matches[0].Value }
            }
        }
        if (-not $url) { throw "Tunnel URL not found; see $log" }
        & $python configure.py --public-url $url
        if ($LASTEXITCODE) { throw 'Could not save the tunnel URL' }
    }
    $server = Start-Process -FilePath $python -NoNewWindow -PassThru `
        -ArgumentList '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', $Port, '--no-access-log'
    if (-not $NoTunnel) {
        Write-Host ''
        if ($NoNumberUpdate) {
            Write-Host '=== Set the Twilio number by hand: A call comes in -> Webhook, HTTP POST ==='
            Write-Host "    $url/voice"
        } else {
            Write-Host '=== Pointing the Twilio number at this run (waits for the tunnel to answer) ==='
            $pointed = $false
            for ($attempt = 1; $attempt -le 3 -and -not $pointed; $attempt++) {
                & $python configure.py --update-number
                $pointed = ($LASTEXITCODE -eq 0)
                if (-not $pointed -and $attempt -lt 3) { Write-Host "Retrying the number update ($attempt of 3 failed)..." }
            }
            Write-Host ''
            if ($pointed) {
                Write-Host '#############################################################' -ForegroundColor Green
                Write-Host "  READY: the Twilio number now routes to $url/voice" -ForegroundColor Green
                Write-Host '#############################################################' -ForegroundColor Green
            } else {
                Write-Host '#############################################################' -ForegroundColor Red
                Write-Host '  NOT READY: calls still go to the OLD address and will fail.' -ForegroundColor Red
                Write-Host '  In a second terminal, run:  .venv\Scripts\python.exe configure.py --update-number' -ForegroundColor Red
                Write-Host '#############################################################' -ForegroundColor Red
            }
        }
        Write-Host "    Health check: $url/health"
        Write-Host ''
    }
    $server.WaitForExit()
} finally {
    if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force }
    if (Test-Path $log) { Remove-Item $log -ErrorAction SilentlyContinue }
}
