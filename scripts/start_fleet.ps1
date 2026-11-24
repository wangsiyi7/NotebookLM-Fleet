$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$env:PYTHONUTF8 = "1"

$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Start-Process "http://127.0.0.1:8765"
if (Test-Path $VenvPy) {
    & $VenvPy -m uvicorn app.main:app --host 127.0.0.1 --port 8765
} else {
    uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
}
