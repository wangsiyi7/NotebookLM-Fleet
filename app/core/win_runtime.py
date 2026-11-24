"""Windows x64 & PyInstaller frozen-EXE runtime helpers."""

from __future__ import annotations

import ctypes
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

_INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_STDIO_LOG_HANDLE: Any = None


def is_frozen() -> bool:
    """Return True when running inside a PyInstaller frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def get_bundle_root() -> Path:
    """Return the read-only resource root (`_MEIPASS` when frozen, repo root in dev)."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def get_app_home() -> Path:
    """Return the directory containing the running EXE (or repo root in source mode)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return get_bundle_root()


def get_resource_path(rel_path: str) -> Path:
    """Resolve a bundled read-only file relative to bundle root."""
    rel = rel_path.lstrip("/\\")
    candidate = get_bundle_root() / rel
    if candidate.exists():
        return candidate
    return get_app_home() / rel


def get_default_data_dir() -> Path:
    """Resolve persistent writable data directory (portable-mode & AppData aware)."""
    env_override = (os.environ.get("FLEET_DATA_DIR") or "").strip()
    if env_override:
        return Path(env_override)

    if not is_frozen():
        return get_bundle_root() / "data"

    app_home = get_app_home()
    in_program_files = "program files" in str(app_home).lower()
    if (app_home / "portable.flag").exists() or ((app_home / "data").exists() and not in_program_files):
        return app_home / "data"

    local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local_app_data) / "NotebookLM-Fleet" / "data"


def ensure_safe_stdio(log_dir: Path | None = None) -> Path | None:
    """Redirect None stdout/stderr in PyInstaller `--windowed` EXEs to a UTF-8 log file."""
    global _STDIO_LOG_HANDLE
    if sys.stdout is not None and sys.stderr is not None:
        return None
    target_dir = log_dir or (get_default_data_dir() / "logs")
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "desktop.log"
    _STDIO_LOG_HANDLE = log_path.open("a", encoding="utf-8", buffering=1, errors="replace")
    if sys.stdout is None:
        sys.stdout = _STDIO_LOG_HANDLE
    if sys.stderr is None:
        sys.stderr = _STDIO_LOG_HANDLE
    return log_path


def detect_google_drive_mounts() -> list[dict[str, Any]]:
    """Detect mounted Google Drive for Desktop 'My Drive' / '我的云端硬盘' paths on Windows."""
    if sys.platform != "win32":
        return []

    mounts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _try_add(path: Path, drive_label: str) -> None:
        try:
            if not path.exists() or not path.is_dir():
                return
            key = str(path.resolve()).lower()
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        mounts.append(
            {
                "drive": drive_label,
                "path": str(path),
                "label": f"{path.name} ({drive_label.rstrip('\\/')})",
                "exists": True,
            }
        )

    # 1. Inspect Google DriveFS registry mount point if present.
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\DriveFS\Share") as key:
            val, _ = winreg.QueryValueEx(key, "MountPoint")
            raw_mp = str(val or "").strip()
            if raw_mp:
                mp = Path(f"{raw_mp}:\\") if len(raw_mp) == 1 else Path(raw_mp)
                for sub in ("我的云端硬盘", "My Drive"):
                    _try_add(mp / sub, f"{str(mp).rstrip('\\/')}\\")
    except OSError:
        pass

    # 2. Scan active Windows logical drive letters C:..Z:.
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # noqa: BLE001
        bitmask = 0

    for idx in range(2, 26):
        if not (bitmask & (1 << idx)):
            continue
        letter = chr(ord("A") + idx)
        root = Path(f"{letter}:\\")
        for sub in ("我的云端硬盘", "My Drive"):
            _try_add(root / sub, f"{letter}:\\")

    return mounts


def get_default_drive_paths(data_dir: Path) -> tuple[str, str]:
    """Return `(drive_library_root, drive_hub_root)` without hardcoding machine drive letters."""
    if is_frozen():
        mounts = detect_google_drive_mounts()
        if mounts:
            mount_path = Path(mounts[0]["path"])
            return str(mount_path), str(mount_path / "NLM_Hub")
    return str(data_dir / "Drive_Library"), str(data_dir / "NLM_Hub")


def sanitize_windows_filename(name: str, max_len: int = 120) -> str:
    """Sanitize a filename or single folder segment for Windows NTFS / DriveFS."""
    cleaned = _INVALID_WIN_CHARS.sub("_", (name or "").strip()).strip(". ")
    if not cleaned or cleaned in (".", ".."):
        return "_"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_len:
        if "." in cleaned and not cleaned.startswith("."):
            base, ext = cleaned.rsplit(".", 1)
            keep = max(1, max_len - len(ext) - 1)
            cleaned = f"{base[:keep].rstrip('. ')}.{ext}"[:max_len]
        else:
            cleaned = cleaned[:max_len].strip(". ")
    return cleaned or "_"


def find_available_port(preferred: int = 8765, max_port: int = 8799) -> int:
    """Probe localhost starting at `preferred` and return the first bindable TCP port."""
    for port in range(int(preferred), int(max_port) + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free localhost TCP port found in range {preferred}-{max_port}")


def pick_folder_native(
    initial_dir: str | None = None,
    title: str = "选择文件夹",
    desktop_controller: Any = None,
) -> str | None:
    """Open a native OS folder picker dialog and return the chosen path or None."""
    if desktop_controller is not None and hasattr(desktop_controller, "pick_folder"):
        try:
            picked = desktop_controller.pick_folder(initial_dir=initial_dir, title=title)
            if picked is not False:
                return str(picked) if picked else None
        except Exception:  # noqa: BLE001
            pass

    if sys.platform != "win32":
        return None

    env = os.environ.copy()
    env["_NLM_PICK_TITLE"] = str(title or "选择文件夹")
    env["_NLM_PICK_INIT"] = str(initial_dir or "")
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$dlg.Description = $env:_NLM_PICK_TITLE;"
        "$dlg.ShowNewFolderButton = $true;"
        "if ($env:_NLM_PICK_INIT -and (Test-Path -LiteralPath $env:_NLM_PICK_INIT)) {"
        "  $dlg.SelectedPath = $env:_NLM_PICK_INIT"
        "};"
        "$owner = New-Object System.Windows.Forms.Form;"
        "$owner.TopMost = $true;"
        "if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {"
        "  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
        "  Write-Output $dlg.SelectedPath"
        "}"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    selected = (proc.stdout or "").strip()
    return selected or None


def reveal_in_explorer(target_path: str | Path) -> dict[str, Any]:
    """Open Windows Explorer and select a file (or open a folder)."""
    resolved = Path(target_path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"路径不存在: {resolved}")
    if sys.platform == "win32":
        if resolved.is_file():
            subprocess.Popen(["explorer.exe", f"/select,{resolved}"])
        else:
            os.startfile(str(resolved))  # noqa: S606
    return {"opened": True, "path": str(resolved), "is_dir": resolved.is_dir()}
