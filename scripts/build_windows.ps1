# Build Windows x64 EXE, Portable ZIP, and Setup Installer for NotebookLM-Fleet
param(
    [string]$Version = "0.4.0"
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

$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$DesktopCmd = "@echo off`r`nchcp 65001 >nul`r`ncd /d `"%~dp0`"`r`nstart `"`" `"%~dp0NotebookLM-Fleet.exe`"`r`n"
$ServerCmd = "@echo off`r`nchcp 65001 >nul`r`ncd /d `"%~dp0`"`r`necho Starting NotebookLM-Fleet Local Web Server on http://127.0.0.1:8765 ...`r`n`"%~dp0NotebookLM-Fleet.exe`" --server-only --port 8765`r`npause`r`n"
$ReadmeTxt = @"
NotebookLM-Fleet v$Version Portable (Windows x64)
=================================================

1. 桌面端完整程序模式（推荐 · PyWebView2 窗口 + 系统托盘）：
   直接双击 `NotebookLM-Fleet.exe` 或 `启动桌面应用.cmd`

2. 本地 Web 服务器模式（浏览器访问 http://127.0.0.1:8765）：
   双击 `启动本地Web服务器.cmd`
   或运行：NotebookLM-Fleet.exe --server-only --port 8765

3. 局域网 / 远程无头服务器模式（绑定 0.0.0.0）：
   NotebookLM-Fleet.exe --server-only --host 0.0.0.0 --port 8765 --no-browser

4. AI IDE 原生 Stdio MCP 模式：
   NotebookLM-Fleet.exe --mcp

5. 便携模式数据目录：
   因当前目录包含 `portable.flag`，所有配置与数据库默认保存在同级 `data/` 目录（支持通过环境变量 `FLEET_DATA_DIR` 覆盖）。
"@ -replace "`r?`n", "`r`n"

[System.IO.File]::WriteAllText((Join-Path $PortableStage "启动桌面应用.cmd"), $DesktopCmd, $Utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $PortableStage "启动本地Web服务器.cmd"), $ServerCmd, $Utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $PortableStage "运行说明_README.txt"), $ReadmeTxt, $Utf8NoBom)

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
