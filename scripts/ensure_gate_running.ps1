$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$logDir = Join-Path $projectRoot 'logs'
$watchdogLog = Join-Path $logDir 'gate_watchdog.log'
$stdoutLog = Join-Path $logDir 'gate_web_stdout.log'
$stderrLog = Join-Path $logDir 'gate_web_stderr.log'

New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-WatchdogLog([string]$Message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $watchdogLog -Encoding UTF8 -Value "[$stamp] $Message"
}

try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8081/api/v1/health' -TimeoutSec 5
    if ($health.status -eq 'ok') {
        exit 0
    }
} catch {
    Write-WatchdogLog "health check failed: $($_.Exception.Message)"
}

if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-WatchdogLog "pythonw missing: $pythonw"
    exit 1
}

# Remove only stale Gate Web launchers from this project. Never match the OKX path.
$projectPattern = [regex]::Escape($projectRoot)
$stale = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match $projectPattern -and $_.CommandLine -match 'gate_quant\.web'
}
foreach ($process in $stale) {
    try { Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop } catch {}
}

Start-Process -FilePath $pythonw `
    -ArgumentList '-m','uvicorn','gate_quant.web:app','--host','127.0.0.1','--port','8081' `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden

Start-Sleep -Seconds 6
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8081/api/v1/health' -TimeoutSec 5
    Write-WatchdogLog "Gate Web restarted; status=$($health.status), environment=$($health.environment), tunnel=$($health.tunnel_running)"
    exit 0
} catch {
    Write-WatchdogLog "restart failed: $($_.Exception.Message)"
    exit 1
}
