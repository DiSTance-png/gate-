$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location $root

& $python -m uvicorn r20_backend.app:app --host 127.0.0.1 --port 8080 `
    1>> (Join-Path $logDir "backend_runtime.out.log") `
    2>> (Join-Path $logDir "backend_runtime.err.log")
exit $LASTEXITCODE
