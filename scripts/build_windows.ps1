# Build Windows x64 EXE, Portable ZIP, and Setup Installer for NotebookLM-Fleet
param(
    [string]$Version = "0.3.0"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $VenvPy)) {
    throw "Missing virtualenv Python at $VenvPy"
}
if (-not (Test-Path $PyInstaller)) {
    throw "PyInstaller not installed in .venv. Run: uv pip install pyinstaller pywebview pystray pillow"
}

Write-Output "=== [1/5] Generating multi-size Windows icon assets ==="
& $VenvPy (Join-Path $RepoRoot "build\assets\generate_icon.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output "=== [2/5] Cleaning previous build output ==="
$WorkDir = Join-Path $RepoRoot "build\work"
$DistDir = Join-Path $RepoRoot "dist"
$ReleaseDir = Join-Path $DistDir "release"
if (Test-Path $WorkDir) { Remove-Item $WorkDir -Recurse -Force }
if (Test-Path (Join-Path $DistDir "NotebookLM-Fleet")) { Remove-Item (Join-Path $DistDir "NotebookLM-Fleet") -Recurse -Force }
if (Test-Path $ReleaseDir) { Remove-Item $ReleaseDir -Recurse -Force }
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

Write-Output "=== [3/5] Running PyInstaller (--onedir, Windows x64) ==="
& $PyInstaller --noconfirm --clean --workpath $WorkDir --distpath $DistDir (Join-Path $RepoRoot "build\notebooklm_fleet.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$ExeDir = Join-Path $DistDir "NotebookLM-Fleet"
$ExePath = Join-Path $ExeDir "NotebookLM-Fleet.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected compiled EXE not found: $ExePath"
}

Write-Output "=== [4/5] Building Portable ZIP ==="
$PortableStage = Join-Path $WorkDir "portable_stage\NotebookLM-Fleet-v$Version-win64"
if (Test-Path $PortableStage) { Remove-Item $PortableStage -Recurse -Force }
New-Item -ItemType Directory -Path $PortableStage -Force | Out-Null
Copy-Item -Path (Join-Path $ExeDir "*") -Destination $PortableStage -Recurse -Force
"portable=1" | Set-Content -Path (Join-Path $PortableStage "portable.flag") -Encoding ASCII

$PortableZip = Join-Path $ReleaseDir "NotebookLM-Fleet-v$Version-portable-win64.zip"
Compress-Archive -Path $PortableStage -DestinationPath $PortableZip -CompressionLevel Optimal -Force

Write-Output "=== [5/5] Building Windows Setup Installer EXE via Inno Setup 6 ==="
$IsccCandidates = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    $cmdIscc = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmdIscc) { $Iscc = $cmdIscc.Source }
}
if (-not $Iscc) {
    throw "Inno Setup 6 (ISCC.exe) not found. Install via: winget install JRSoftware.InnoSetup"
}

& $Iscc "/DMyAppVersion=$Version" (Join-Path $RepoRoot "build\installer.iss")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$SetupExe = Join-Path $ReleaseDir "NotebookLM-Fleet-v$Version-Setup-win64.exe"
if (-not (Test-Path $SetupExe)) {
    throw "Expected Setup EXE not found: $SetupExe"
}

Write-Output "=== Computing SHA256SUMS.txt ==="
$ShaLines = @()
foreach ($artifact in @($SetupExe, $PortableZip)) {
    $hash = (Get-FileHash -Path $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    $name = [System.IO.Path]::GetFileName($artifact)
    $ShaLines += "$hash  $name"
}
$ShaPath = Join-Path $ReleaseDir "SHA256SUMS.txt"
[System.IO.File]::WriteAllLines($ShaPath, $ShaLines, [System.Text.UTF8Encoding]::new($false))

Write-Output "=== Build Complete ==="
Get-ChildItem $ReleaseDir | Select-Object Name, Length, FullName | Format-Table -AutoSize
