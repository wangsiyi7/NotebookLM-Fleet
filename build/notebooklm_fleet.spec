# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for NotebookLM-Fleet Windows x64 Desktop Application."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "app" / "schema.sql"), "app"),
    (str(ROOT / "build" / "assets" / "app.ico"), "build/assets"),
    (str(ROOT / "build" / "assets" / "app.png"), "build/assets"),
]
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "aiosqlite",
    "httpx",
    "httpcore",
    "websockets",
    "pydantic_settings",
    "multipart",
    "python_multipart",
    "rookiepy",
    "rookie_cookies",
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "win32api",
    "win32con",
    "win32gui",
    "win32event",
    "winerror",
]

_SKIP_PREFIXES = (
    "mcp.cli",
    "playwright",
    "webview.platforms.cocoa",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.android",
    "webview.platforms.cef",
)


def _keep_submod(name: str) -> bool:
    if name == "tests" or name.endswith(".tests") or ".tests." in name:
        return False
    return not any(name == p or name.startswith(p + ".") for p in _SKIP_PREFIXES)


for pkg in ("notebooklm", "fastmcp", "mcp", "webview", "clr_loader", "pythonnet", "certifi", "truststore"):
    try:
        pkg_datas, pkg_bins, pkg_mods = collect_all(pkg, filter_submodules=_keep_submod, on_error="ignore")
        datas.extend(pkg_datas)
        binaries.extend(pkg_bins)
        hiddenimports.extend(pkg_mods)
    except BaseException:
        hiddenimports.extend(collect_submodules(pkg, filter=_keep_submod, on_error="ignore"))

# Deduplicate hiddenimports preserving order
hiddenimports = list(dict.fromkeys(hiddenimports))

a = Analysis(
    [str(ROOT / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "playwright",
        "pytest",
        "_pytest",
        "mcp.cli",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.qt",
        "webview.platforms.android",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NotebookLM-Fleet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "build" / "assets" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NotebookLM-Fleet",
)
