"""Channel 3 adapter: Isolated per-account Chrome/Edge launcher with CDP debugging port."""

from __future__ import annotations

import asyncio
import ctypes
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import websockets

from ..config import get_settings
from ..core.win_runtime import (
    find_available_port,
    get_current_desktop_name,
    is_interactive_desktop,
)


def _validate_http_url(url: str) -> str:
    """Validate that a target URL uses http/https and cannot inject browser CLI flags."""
    raw = (url or "").strip()
    if not raw or raw.startswith("-") or any(ch in raw for ch in ("\r", "\n", "\x00", '"')):
        raise ValueError("URL 包含非法字符或不支持的格式")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"仅支持 http:// 或 https:// URL，当前为: {raw!r}")
    return raw


def build_google_login_url(
    email: str | None = None,
    target: str = "https://notebook.google.com/",
) -> str:
    """Build the latest Google login / AccountChooser URL redirecting back to NotebookLM.

    Non-Google targets (for example Baidu Netdisk) are validated and returned unchanged.
    """
    clean_target = _validate_http_url(target)
    host = (urllib.parse.urlparse(clean_target).hostname or "").lower()
    if host == "accounts.google.com":
        return clean_target
    if host == "google.com" or host.endswith(".google.com"):
        clean_email = (email or "").strip()
        if (
            clean_email
            and "@" in clean_email
            and not clean_email.endswith((".local", "@example.com"))
        ):
            query = urllib.parse.urlencode({"Email": clean_email, "continue": clean_target})
            return f"https://accounts.google.com/AccountChooser?{query}"
        query = urllib.parse.urlencode({"continue": clean_target})
        return f"https://accounts.google.com/AccountChooser?{query}"
    return clean_target


def resolve_browser_profile_dir(chrome_profile_dir: str | Path, browser: str) -> Path:
    """Resolve the dedicated Chrome or Edge user-data directory for an account."""
    base_dir = Path(chrome_profile_dir)
    if browser == "edge":
        return base_dir.parent / f"{base_dir.name}_edge"
    return base_dir


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


def _sanitize_batch_text(value: str) -> str:
    """Make text safe for unquoted use inside a Windows .cmd title/echo line."""
    cleaned = re.sub(r'[\r\n\x00"&|<>^`]+', " ", str(value or ""))
    return cleaned.replace("%", "%%").strip()


def _escape_batch_quoted_arg(value: str) -> str:
    """Escape a value placed inside double quotes in a Windows .cmd file.

    Inside double quotes, ``&`` is treated literally by ``cmd.exe`` while ``%`` must
    be doubled so environment variable expansion cannot trigger.
    """
    text = str(value or "").strip()
    if any(ch in text for ch in ('"', "\r", "\n", "\x00")):
        raise ValueError("批处理参数包含非法引号或换行符")
    return text.replace("%", "%%")


def _escape_batch_quoted_url(url: str) -> str:
    """Validate an http(s) URL and escape ``%`` for double-quoted cmd.exe usage."""
    clean = _validate_http_url(url)
    if any(ch.isspace() for ch in clean):
        raise ValueError("URL 不允许包含空白字符")
    return _escape_batch_quoted_arg(clean)


def open_account_browser(
    alias: str,
    chrome_profile_dir: str | Path,
    cdp_port: int,
    *,
    url: str = "https://notebook.google.com/",
    browser: str = "chrome",
) -> dict[str, Any]:
    """Launch an isolated visible Chrome/Edge window dedicated to one account with CDP enabled.

    When running inside a non-interactive Win32 desktop (such as an isolated daemon
    desktop), it refuses to spawn invisible windows and returns desktop isolation
    metadata plus a ready-to-run ``manual_cmd`` instead.
    """
    clean_url = _validate_http_url(url)
    port = int(cdp_port)
    resolved_browser, exe = find_browser_executable(browser)
    profile_path = resolve_browser_profile_dir(chrome_profile_dir, resolved_browser)
    profile_path.mkdir(parents=True, exist_ok=True)

    desktop_name = get_current_desktop_name()
    interactive = is_interactive_desktop()

    fresh_flag_args = [
        f"--user-data-dir={profile_path}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        clean_url,
    ]
    manual_cmd = subprocess.list2cmdline([str(exe), *fresh_flag_args])

    info = _get_cdp_browser_info(port)
    was_alive = info is not None
    if not interactive and not was_alive:
        return {
            "launched": False,
            "already_running": False,
            "desktop_isolated": True,
            "desktop_name": desktop_name,
            "cdp_ready": False,
            "browser": resolved_browser,
            "alias": alias,
            "pid": None,
            "cdp_port": port,
            "cdp_url": f"http://127.0.0.1:{port}",
            "profile_dir": str(profile_path),
            "url": clean_url,
            "manual_cmd": manual_cmd,
            "message": (
                f"当前后台服务运行在隔离桌面 ({desktop_name})，直接拉起的浏览器窗口不会显示在您的桌面上。"
                "请下载并双击「一键 CMD 登录脚本」，或在 Windows 终端运行下方命令完成登录。"
            ),
        }

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
            clean_url,
        ]
    else:
        flag_args = fresh_flag_args

    _, pid = _launch_visible_windows(exe, flag_args)
    cdp_ready = was_alive
    if not was_alive:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _is_cdp_alive(port):
                cdp_ready = True
                break
            time.sleep(0.25)
    else:
        cdp_ready = _is_cdp_alive(port)

    return {
        "launched": not was_alive,
        "already_running": was_alive,
        "desktop_isolated": not interactive,
        "desktop_name": desktop_name,
        "cdp_ready": cdp_ready,
        "browser": resolved_browser,
        "alias": alias,
        "pid": pid,
        "cdp_port": port,
        "cdp_url": f"http://127.0.0.1:{port}",
        "profile_dir": str(profile_path),
        "url": clean_url,
        "manual_cmd": manual_cmd,
        "message": (
            f"已打开独立 {resolved_browser.upper()} 窗口并接入 CDP (:{port})。"
            if cdp_ready
            else "已尝试启动浏览器但 5 秒内未检测到 CDP 调试端口；如无弹窗或端口未开启，请使用「一键 CMD 登录脚本」。"
        ),
    }


def generate_windows_login_cmd_script(
    *,
    alias: str,
    chrome_profile_dir: str | Path,
    cdp_port: int,
    url: str,
    browser: str = "chrome",
    api_capture_url: str | None = None,
    capture_instruction: str | None = None,
) -> str:
    """Build a self-contained Windows UTF-8 .cmd script for visible desktop login."""
    clean_url = _validate_http_url(url)
    port = int(cdp_port)
    resolved_browser, exe = find_browser_executable(browser)
    profile_path = resolve_browser_profile_dir(chrome_profile_dir, resolved_browser)

    safe_alias = _sanitize_batch_text(alias) or "account"
    safe_exe = _escape_batch_quoted_arg(str(exe))
    safe_profile = _escape_batch_quoted_arg(str(profile_path))
    safe_url = _escape_batch_quoted_url(clean_url)
    browser_label = "Microsoft Edge" if resolved_browser == "edge" else "Google Chrome"

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        f"title NotebookLM Fleet Login - {safe_alias} ({resolved_browser.upper()})",
        "echo ============================================================",
        f"echo  NotebookLM Fleet - 独立 {browser_label} 持久登录终端",
        f"echo  账号/目标: {safe_alias}   CDP端口: {port}",
        "echo ============================================================",
        "echo.",
        f'if not exist "{safe_profile}" mkdir "{safe_profile}"',
        f"echo [1/3] 正在启动可见 {browser_label} 窗口并打开登录地址...",
        (
            f'start "" "{safe_exe}" '
            f'"--user-data-dir={safe_profile}" '
            f'"--remote-debugging-port={port}" '
            f'"--remote-debugging-address=127.0.0.1" '
            f'"--no-first-run" '
            f'"--no-default-browser-check" '
            f'"--new-window" '
            f'"{safe_url}"'
        ),
        "echo.",
    ]

    if api_capture_url:
        clean_capture = _validate_http_url(api_capture_url)
        if any(ch in clean_capture for ch in ("'", '"', "%", "$", "`", ";")):
            raise ValueError("capture URL 包含不安全字符")
        lines.extend(
            [
                "echo [2/3] 请在刚刚弹出的浏览器窗口完成 Google 登录并进入 NotebookLM 主页。",
                "echo        (建议保持此黑框不要关闭，登录完成后再回来按任意键同步)",
                "echo.",
                ":wait_sync",
                "echo [3/3] 登录完成后，按任意键通过 CDP 自动提取完整持久 Cookie 并同步回 Fleet...",
                "pause >nul",
                "echo.",
                f"echo 正在连接 127.0.0.1:{port} 提取多域 Cookie...",
                (
                    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "'
                    f"try {{ $r = Invoke-RestMethod -Method Post -Uri '{clean_capture}' "
                    "-ContentType 'application/json' -Body '{}' -TimeoutSec 25; "
                    "if ($r.ok -ne $true) { "
                    "$err = ''; if ($r.quota -and $r.quota.error_msg) { $err = $r.quota.error_msg } elseif ($r.detail) { $err = $r.detail } else { $err = '尚未检测到有效登录状态' }; "
                    "Write-Host ('[尚未完成] ' + $err); exit 1 }; "
                    "if ($r.saved) { Write-Host ('[成功] Cookie数量=' + $r.saved.cookie_count + ' 持久化=' + $r.saved.persistence_level + ' 认证状态=' + $r.quota.nlm_auth_status) } "
                    "else { Write-Host '[成功] 浏览器 Cookie 已同步并激活长效会话' }; "
                    "exit 0 } catch { $msg = $_.Exception.Message; "
                    "try { $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream()); $msg = $sr.ReadToEnd() } catch {}; "
                    "Write-Host ('[尚未完成] ' + $msg); exit 1 }\""
                ),
                "if errorlevel 1 (",
                "    echo.",
                "    echo 请先在浏览器中确认已成功看到笔记本列表，然后再回来按任意键重试。",
                "    goto wait_sync",
                ")",
                "echo.",
                "echo 会话已持久化到独立浏览器配置目录！请保留该浏览器数据以支持后续无感续期。",
            ]
        )
    else:
        tip = _sanitize_batch_text(
            capture_instruction or "请在弹出的浏览器窗口完成登录，完成后回到 Fleet Web 页面刷新状态。"
        )
        lines.extend(
            [
                f"echo [2/2] {tip}",
                "echo.",
                "echo 浏览器已保持开启，下次可直接免扫码恢复登录态。",
            ]
        )

    lines.extend(["pause", ""])
    return "\r\n".join(lines)


def launch_cmd_login_console(
    *,
    alias: str,
    chrome_profile_dir: str | Path,
    cdp_port: int,
    url: str,
    browser: str = "chrome",
    api_capture_url: str | None = None,
    capture_instruction: str | None = None,
) -> dict[str, Any]:
    """Write a login .cmd helper script and open a visible CMD console when on interactive desktop."""
    clean_url = _validate_http_url(url)
    port = int(cdp_port)
    resolved_browser, exe = find_browser_executable(browser)
    profile_path = resolve_browser_profile_dir(chrome_profile_dir, resolved_browser)
    profile_path.mkdir(parents=True, exist_ok=True)

    script_text = generate_windows_login_cmd_script(
        alias=alias,
        chrome_profile_dir=chrome_profile_dir,
        cdp_port=port,
        url=clean_url,
        browser=resolved_browser,
        api_capture_url=api_capture_url,
        capture_instruction=capture_instruction,
    )
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", alias).strip("_") or "account"
    script_path = profile_path.parent / f"login_{safe_name}.cmd"
    # Never write a UTF-8 BOM to .cmd scripts because cmd.exe fails on leading BOM bytes.
    script_path.write_text(script_text, encoding="utf-8", newline="")

    desktop_name = get_current_desktop_name()
    interactive = is_interactive_desktop()
    manual_cmd = subprocess.list2cmdline(
        [
            str(exe),
            f"--user-data-dir={profile_path}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            clean_url,
        ]
    )

    if sys.platform != "win32" or not interactive:
        return {
            "launched": False,
            "desktop_isolated": sys.platform == "win32" and not interactive,
            "desktop_name": desktop_name,
            "browser": resolved_browser,
            "alias": alias,
            "cdp_port": port,
            "url": clean_url,
            "script_path": str(script_path),
            "manual_cmd": manual_cmd,
            "message": (
                f"当前服务运行在隔离桌面 ({desktop_name})，已生成登录脚本，请点击「下载一键 CMD 登录脚本」并在桌面双击运行。"
                if sys.platform == "win32"
                else "当前非 Windows 桌面环境，已生成脚本路径。"
            ),
        }

    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen(["cmd.exe", "/c", str(script_path)], creationflags=flags)
    return {
        "launched": True,
        "desktop_isolated": False,
        "desktop_name": desktop_name,
        "pid": proc.pid,
        "browser": resolved_browser,
        "alias": alias,
        "cdp_port": port,
        "url": clean_url,
        "script_path": str(script_path),
        "manual_cmd": manual_cmd,
        "message": f"已在桌面打开 CMD 登录终端，将引导 {resolved_browser.upper()} 访问最新登录页面并自动回写 Cookie。",
    }


def _profile_has_cookies(profile_path: Path) -> bool:
    return (
        (profile_path / "Default" / "Network" / "Cookies").exists()
        or (profile_path / "Default" / "Cookies").exists()
    )


async def silent_refresh_cookies_from_profile(
    profile_dir: str | Path,
    *,
    cdp_port: int | None = None,
    browser: str = "auto",
    target_url: str = "https://notebook.google.com/",
    wait_seconds: float = 8.0,
) -> dict[str, Any]:
    """Silently refresh short-lived session cookies using the persistent Chromium profile.

    Works either against an already-open CDP browser on ``cdp_port`` or by starting a
    short-lived ``--headless=new`` browser against the saved user-data-dir so long-lived
    ``LSID``/``SID`` cookies mint fresh ``OSID`` and ``__Secure-1PSIDTS`` tokens.
    """
    clean_url = _validate_http_url(target_url)

    # If the user's isolated GUI browser is already running on cdp_port, capture directly.
    if cdp_port is not None:
        port_num = int(cdp_port)
        if await asyncio.to_thread(_is_cdp_alive, port_num):
            cookies = await capture_cdp_cookies(port_num)
            return {
                "refreshed": bool(cookies),
                "cookies": cookies,
                "source": "live_cdp",
                "final_url": clean_url,
            }

    base_dir = Path(profile_dir)
    candidates: list[tuple[str, Path]] = [
        ("chrome", resolve_browser_profile_dir(base_dir, "chrome")),
        ("edge", resolve_browser_profile_dir(base_dir, "edge")),
    ]
    if browser == "edge":
        candidates.reverse()

    chosen_browser = ""
    active_profile: Path | None = None
    for b_name, p_dir in candidates:
        if _profile_has_cookies(p_dir):
            chosen_browser = b_name
            active_profile = p_dir
            break

    if active_profile is None:
        return {
            "refreshed": False,
            "cookies": [],
            "reason": "no_browser_profile",
        }

    try:
        resolved_browser, exe = find_browser_executable(chosen_browser)
    except FileNotFoundError:
        try:
            resolved_browser, exe = find_browser_executable("auto")
        except FileNotFoundError as exc:
            return {
                "refreshed": False,
                "cookies": [],
                "reason": f"no_browser_executable: {exc}",
            }

    temp_port = find_available_port(9500, 9699)
    launch_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        launch_kwargs["startupinfo"] = si
        launch_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = subprocess.Popen(
        [
            str(exe),
            f"--user-data-dir={active_profile}",
            f"--remote-debugging-port={temp_port}",
            "--remote-debugging-address=127.0.0.1",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            clean_url,
        ],
        **launch_kwargs,
    )

    last_url = clean_url
    try:
        cdp_up = False
        up_deadline = time.monotonic() + 5.0
        while time.monotonic() < up_deadline:
            if proc.poll() is not None:
                return {
                    "refreshed": False,
                    "cookies": [],
                    "reason": "profile_locked_or_exited",
                    "browser": resolved_browser,
                }
            if await asyncio.to_thread(_is_cdp_alive, temp_port):
                cdp_up = True
                break
            await asyncio.sleep(0.25)

        if not cdp_up:
            return {
                "refreshed": False,
                "cookies": [],
                "reason": "headless_cdp_timeout",
                "browser": resolved_browser,
            }

        page_deadline = time.monotonic() + max(2.0, float(wait_seconds))
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < page_deadline:
                try:
                    resp = await client.get(f"http://127.0.0.1:{temp_port}/json")
                    if resp.status_code == 200:
                        targets = resp.json()
                        for tgt in targets:
                            if tgt.get("type") == "page":
                                cur = str(tgt.get("url") or "")
                                if cur:
                                    last_url = cur
                                if cur.startswith(
                                    ("https://notebook.google.com", "https://notebooklm.google.com")
                                ):
                                    await asyncio.sleep(1.0)
                                    page_deadline = 0.0
                                    break
                except Exception:  # noqa: BLE001
                    pass
                if page_deadline == 0.0:
                    break
                await asyncio.sleep(0.5)

        cookies = await capture_cdp_cookies(temp_port)
        return {
            "refreshed": bool(cookies),
            "cookies": cookies,
            "source": f"headless_{resolved_browser}",
            "browser": resolved_browser,
            "profile_dir": str(active_profile),
            "final_url": last_url,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "refreshed": False,
            "cookies": [],
            "reason": f"{type(exc).__name__}: {exc}",
            "final_url": last_url,
        }
    finally:
        await asyncio.to_thread(_close_cdp_browser, temp_port)
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 3)
            except Exception:  # noqa: BLE001
                proc.kill()


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
