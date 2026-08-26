<#
.SYNOPSIS
    Start the service and a Cloudflare quick tunnel, detached from the console.

.DESCRIPTION
    Reads configuration from .env, launches uvicorn and cloudflared as hidden
    background processes that outlive the shell that started them, waits for the
    tunnel to register, and prints the public URL.

    Re-run this after a reboot. A quick tunnel gets a NEW hostname every time it
    starts, so re-running means re-sending the URL.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
#>
param(
    [int]$Port = 8080,
    [string]$Cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root ".run"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# --- configuration ---------------------------------------------------------
if (-not (Test-Path ".env")) { throw ".env not found. Copy .env.example and fill it in." }
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*?)\s*$') {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
    }
}
if (-not $env:API_BEARER_TOKEN) { throw "API_BEARER_TOKEN is not set in .env" }
$env:PORT = $Port

# --- stop anything already running ----------------------------------------
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cloudflared.exe'" |
    Where-Object { $_.CommandLine -match 'uvicorn app.main:app|trycloudflare|tunnel --url' } |
    ForEach-Object {
        Write-Host "stopping existing pid $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

# --- the service -----------------------------------------------------------
$server = Start-Process -PassThru -WindowStyle Hidden -FilePath "python" `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port", "--log-level", "warning" `
    -RedirectStandardOutput "$logDir\server.out.log" -RedirectStandardError "$logDir\server.err.log"
Write-Host "server pid $($server.Id) on 127.0.0.1:$Port"

# --- the tunnel ------------------------------------------------------------
$tunnelLog = "$logDir\tunnel.log"
Remove-Item $tunnelLog -ErrorAction SilentlyContinue
$tunnel = Start-Process -PassThru -WindowStyle Hidden -FilePath $Cloudflared `
    -ArgumentList "tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate" `
    -RedirectStandardOutput "$logDir\tunnel.out.log" -RedirectStandardError $tunnelLog
Write-Host "tunnel pid $($tunnel.Id)"

# cloudflared prints the assigned hostname a few seconds after registering.
$url = $null
foreach ($attempt in 1..40) {
    Start-Sleep -Seconds 1
    if (Test-Path $tunnelLog) {
        $match = Select-String -Path $tunnelLog -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' |
                 Select-Object -First 1
        if ($match) { $url = $match.Matches[0].Value; break }
    }
}
if (-not $url) { throw "tunnel did not report a URL; see $tunnelLog" }

$url | Out-File -FilePath "$logDir\public-url.txt" -Encoding utf8 -NoNewline

Write-Host ""
Write-Host "public base URL : $url"
Write-Host "bearer token    : $env:API_BEARER_TOKEN"
Write-Host "logs            : $logDir"
Write-Host ""
Write-Host "verify with:"
Write-Host "  python scripts\probe.py $url $env:API_BEARER_TOKEN"
