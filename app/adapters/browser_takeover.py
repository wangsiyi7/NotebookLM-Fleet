"""Channel 3 adapter: Isolated per-account Chrome/Edge launcher with CDP debugging port."""

from __future__ import annotations

import asyncio
import ctypes
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import websockets

from ..config import get_settings


def find_chrome_executable(configured_path: str | None = None) -> Path | None:
    """Locate Chrome executable across configured path and standard locations."""
    candidates = [
        configured_path,
        get_settings().chrome_path,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("chrome"),
        shutil.which("google-chrome"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() and path.is_file():
            return path
    return None


def find_edge_executable() -> Path | None:
    """Locate Microsoft Edge executable across standard locations."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() and path.is_file():
            return path
    return None


def find_browser_executable(
    browser: str = "chrome", configured_path: str | None = None
) -> tuple[str, Path]:
    """Resolve requested browser ('chrome', 'edge', or 'auto') to (browser_name, exe_path)."""
    req = (browser or "chrome").strip().lower()
    if req == "edge":
        edge = find_edge_executable()
        if edge is not None:
            return "edge", edge
        raise FileNotFoundError("未找到 Microsoft Edge 可执行文件，请检查系统安装或改用 Chrome。")
    if req == "chrome":
        chrome = find_chrome_executable(configured_path)
        if chrome is not None:
            return "chrome", chrome
        raise FileNotFoundError("未找到 Chrome 可执行文件，请在 .env 中配置 CHROME_PATH 或改用 Edge。")
    chrome = find_chrome_executable(configured_path)
    if chrome is not None:
        return "chrome", chrome
    edge = find_edge_executable()
    if edge is not None:
        return "edge", edge
    raise FileNotFoundError("未找到 Chrome 或 Microsoft Edge 可执行文件，请在 .env 中配置 CHROME_PATH。")


def _get_cdp_browser_info(cdp_port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(cdp_port)}/json/version", timeout=0.8) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
    except (OSError, urllib.error.URLError, ValueError):
        return None
    return None


def _is_cdp_alive(cdp_port: int) -> bool:
    return _get_cdp_browser_info(cdp_port) is not None


def _close_cdp_browser(cdp_port: int) -> None:
    info = _get_cdp_browser_info(cdp_port)
    ws_url = (info or {}).get("webSocketDebuggerUrl")
    if not ws_url:
        return

    async def _send_close() -> None:
        await _cdp_call(str(ws_url), "Browser.close", req_id=99)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, _send_close()).result(timeout=3.0)
    except Exception:  # noqa: BLE001
        pass


def _launch_visible_windows(exe: Path, flag_args: list[str]) -> tuple[bool, int | None]:
    """Launch Chrome/Edge visibly in the desktop foreground even from a hidden daemon."""
    if sys.platform == "win32":
        try:
            params = subprocess.list2cmdline(flag_args)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "open", str(exe), params, None, 1)
            if int(rc) > 32:
                return True, None
        except Exception:  # noqa: BLE001
            pass

    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 1
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    proc = subprocess.Popen([str(exe), *flag_args], **kwargs)
    return True, proc.pid


def open_account_browser(
    alias: str,
    chrome_profile_dir: str | Path,
    cdp_port: int,
    *,
    url: str = "https://notebook.google.com/",
    browser: str = "chrome",
) -> dict[str, Any]:
    """Launch an isolated visible Chrome/Edge window dedicated to one account with CDP enabled."""
    port = int(cdp_port)
    resolved_browser, exe = find_browser_executable(browser)
    base_dir = Path(chrome_profile_dir)
    profile_path = (
        base_dir.parent / f"{base_dir.name}_edge" if resolved_browser == "edge" else base_dir
    )
    profile_path.mkdir(parents=True, exist_ok=True)

    info = _get_cdp_browser_info(port)
    was_alive = info is not None
    if info is not None:
        running_ua = str(info.get("Browser") or "")
        running_is_edge = "Edg/" in running_ua
        if (resolved_browser == "edge") != running_is_edge:
            _close_cdp_browser(port)
            time.sleep(0.5)
            was_alive = False

    if was_alive:
        flag_args = [
            f"--user-data-dir={profile_path}",
            "--new-window",
            url,
        ]
    else:
        flag_args = [
            f"--user-data-dir={profile_path}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url,
        ]

    _, pid = _launch_visible_windows(exe, flag_args)
    return {
        "launched": not was_alive,
        "already_running": was_alive,
        "browser": resolved_browser,
        "alias": alias,
        "pid": pid,
        "cdp_port": port,
        "cdp_url": f"http://127.0.0.1:{port}",
        "profile_dir": str(profile_path),
        "url": url,
    }


async def _cdp_call(ws_url: str, method: str, req_id: int = 1) -> dict[str, Any]:
    async with websockets.connect(ws_url, open_timeout=5, max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": req_id, "method": method}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            msg = json.loads(raw)
            if isinstance(msg, dict) and msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result") or {}


async def capture_cdp_cookies(cdp_port: int) -> list[dict[str, Any]]:
    """Read all browser cookies cleanly via Chrome/Edge DevTools Protocol without closing the browser."""
    port = int(cdp_port)
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            ver_resp = await client.get(f"{base}/json/version")
            ver_resp.raise_for_status()
            ver_data = ver_resp.json()
            ws_url = ver_data.get("webSocketDebuggerUrl")
            cookies: list[dict[str, Any]] = []
            if ws_url:
                res = await _cdp_call(ws_url, "Storage.getCookies", req_id=1)
                cookies = res.get("cookies") or []
            if not cookies:
                targets_resp = await client.get(f"{base}/json")
                if targets_resp.status_code == 200:
                    for tgt in targets_resp.json():
                        tgt_ws = tgt.get("webSocketDebuggerUrl")
                        if tgt_ws:
                            res = await _cdp_call(tgt_ws, "Network.getAllCookies", req_id=1)
                            cookies = res.get("cookies") or []
                            if cookies:
                                break
            return cookies
    except Exception as exc:
        raise RuntimeError(
            f"未检测到端口 {port} 上的专属 Chrome/Edge 调试口（{type(exc).__name__}）。请先点击打开 Chrome/Edge 窗口并登录 https://notebook.google.com/ 后再同步。"
        ) from exc
