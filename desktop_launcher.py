"""Windows x64 desktop entrypoint for NotebookLM-Fleet (PyWebView2 + System Tray + Embedded FastAPI)."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import multiprocessing
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from app.core.win_runtime import (
    ensure_safe_stdio,
    find_available_port,
    get_resource_path,
    is_frozen,
    reveal_in_explorer,
)

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\NotebookLMFleetDesktopMutex"
logger = logging.getLogger("notebooklm_fleet.desktop")


def _configure_logging() -> None:
    ensure_safe_stdio()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _runtime_port_file() -> Path:
    from app.config import get_data_dir

    return get_data_dir() / "runtime_port.json"


def _write_runtime_port(port: int) -> Path:
    path = _runtime_port_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"port": int(port), "pid": os.getpid(), "url": f"http://127.0.0.1:{int(port)}/"}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _remove_runtime_port() -> None:
    try:
        p = _runtime_port_file()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) == os.getpid():
                p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _read_running_url() -> str | None:
    try:
        p = _runtime_port_file()
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        url = str(data.get("url") or "").strip()
        if not url:
            return None
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=1.5) as resp:
            if resp.status == 200:
                return url
    except Exception:  # noqa: BLE001
        return None
    return None


def _acquire_single_instance_mutex() -> Any:
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    last_err = ctypes.get_last_error()
    if handle and last_err == ERROR_ALREADY_EXISTS:
        existing_url = _read_running_url()
        if existing_url:
            webbrowser.open(existing_url)
        kernel32.CloseHandle(handle)
        return False
    return handle


def _release_mutex(handle: Any) -> None:
    if handle and sys.platform == "win32":
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


def _wait_for_health(port: int, timeout_sec: float = 20.0) -> bool:
    deadline = time.time() + timeout_sec
    url = f"http://127.0.0.1:{int(port)}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.15)
    return False


def _load_tray_image() -> Any:
    from PIL import Image, ImageDraw

    for rel in ("build/assets/app.png", "build/assets/app.ico"):
        p = get_resource_path(rel)
        if p.exists():
            try:
                return Image.open(p).convert("RGBA")
            except Exception:  # noqa: BLE001
                pass

    img = Image.new("RGBA", (64, 64), (19, 111, 99, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([14, 12, 50, 52], radius=6, fill=(246, 244, 238, 255))
    return img


class DesktopController:
    """Bridges FastAPI `/api/system/*` actions with the live PyWebView window manager."""

    def __init__(self, base_url: str, server: Any) -> None:
        self.base_url = base_url.rstrip("/")
        self.server = server
        self.main_window: Any = None
        self.child_windows: list[Any] = []
        self.tray_icon: Any = None
        self.tray_active = False
        self.shutdown_requested = False
        self._tray_hint_shown = False

    def show_main_window(self, path: str | None = None) -> None:
        if self.main_window is None:
            url = f"{self.base_url}{path or '/'}"
            webbrowser.open(url)
            return
        try:
            if path:
                self.main_window.load_url(f"{self.base_url}{path}")
            self.main_window.restore()
            self.main_window.show()
        except Exception:  # noqa: BLE001
            try:
                self.main_window.show()
            except Exception:  # noqa: BLE001
                pass

    def open_child_window(self, path: str, title: str, width: int = 1280, height: int = 840) -> bool:
        try:
            import webview

            target_url = f"{self.base_url}{path}"
            win = webview.create_window(
                title=title,
                url=target_url,
                width=width,
                height=height,
                min_size=(820, 560),
                text_select=True,
                zoomable=True,
            )
            if win is not None:
                self.child_windows.append(win)
                try:
                    def _on_child_closed(_win: Any = win) -> None:
                        try:
                            self.child_windows.remove(_win)
                        except ValueError:
                            pass

                    win.events.closed += _on_child_closed
                except Exception:  # noqa: BLE001
                    pass
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to open child window %s: %s", path, exc)
        return False

    def pick_folder(self, initial_dir: str | None = None, title: str = "选择文件夹") -> str | None | bool:
        if self.main_window is None:
            return False
        try:
            import webview

            dlg_type = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
            directory = str(initial_dir or "")
            if directory and not Path(directory).exists():
                directory = ""
            res = self.main_window.create_file_dialog(dlg_type, directory=directory)
            if isinstance(res, (list, tuple)):
                return str(res[0]) if res else None
            return str(res) if res else None
        except Exception:  # noqa: BLE001
            return False

    def notify_minimized_once(self) -> None:
        if not self._tray_hint_shown and self.tray_icon is not None:
            self._tray_hint_shown = True
            try:
                self.tray_icon.notify(
                    "后台继续运行额度轮询与跨云传输，右键托盘图标可重新打开或退出。",
                    "NotebookLM-Fleet 已最小化至系统托盘",
                )
            except Exception:  # noqa: BLE001
                pass

    def shutdown_all(self) -> None:
        if self.shutdown_requested:
            return
        self.shutdown_requested = True
        try:
            if self.tray_icon is not None:
                self.tray_icon.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            import webview

            for w in list(getattr(webview, "windows", []) or []):
                try:
                    w.destroy()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        if self.server is not None:
            self.server.should_exit = True
        _remove_runtime_port()


def _start_uvicorn_background(port: int, host: str = "127.0.0.1") -> tuple[Any, threading.Thread, Any]:
    import uvicorn

    from app.main import app as fastapi_app

    fastapi_app.state.bound_port = port
    config = uvicorn.Config(
        fastapi_app,
        host=host or "127.0.0.1",
        port=int(port),
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn-server", daemon=True)
    thread.start()
    return server, thread, fastapi_app


def _open_hub_folder_sync() -> None:
    try:
        from app.config import get_data_dir, get_db_path
        import sqlite3

        db_p = get_db_path()
        target: Path = get_data_dir()
        if db_p.exists():
            conn = sqlite3.connect(str(db_p), timeout=5.0)
            try:
                cur = conn.execute("SELECT value FROM app_settings WHERE key = 'drive_hub_root'")
                row = cur.fetchone()
                if row and row[0]:
                    candidate = Path(str(row[0]))
                    if candidate.exists():
                        target = candidate
            finally:
                conn.close()
        reveal_in_explorer(target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Open hub folder failed: %s", exc)


def _start_tray_background(controller: DesktopController) -> bool:
    try:
        import pystray

        from app.config import get_data_dir

        menu = pystray.Menu(
            pystray.MenuItem(
                "打开主控台窗口",
                lambda _icon, _item: controller.show_main_window("/"),
                default=True,
            ),
            pystray.MenuItem(
                "网盘互通 (百度 ⇄ Google)",
                lambda _icon, _item: controller.show_main_window("/?tab=panelBridge"),
            ),
            pystray.MenuItem(
                "笔记本全景与 Studio 占比",
                lambda _icon, _item: controller.show_main_window("/?tab=panelNotebooks"),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "在系统浏览器中打开",
                lambda _icon, _item: webbrowser.open(controller.base_url + "/"),
            ),
            pystray.MenuItem(
                "打开数据目录",
                lambda _icon, _item: reveal_in_explorer(get_data_dir()),
            ),
            pystray.MenuItem(
                "打开 Google Drive Hub (NLM_Hub)",
                lambda _icon, _item: _open_hub_folder_sync(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "退出 NotebookLM-Fleet",
                lambda _icon, _item: controller.shutdown_all(),
            ),
        )
        icon = pystray.Icon(
            "NotebookLM-Fleet",
            _load_tray_image(),
            "NotebookLM-Fleet v0.4.0",
            menu,
        )
        controller.tray_icon = icon
        icon.run_detached()
        controller.tray_active = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("System tray unavailable: %s", exc)
        controller.tray_active = False
        return False


def _launch_browser_app_fallback(base_url: str, controller: DesktopController) -> None:
    from app.adapters.browser_takeover import find_chrome_executable, find_edge_executable
    import subprocess

    exe = find_edge_executable() or find_chrome_executable()
    if exe is not None:
        flags = [f"--app={base_url}/", "--window-size=1400,900"]
        subprocess.Popen([str(exe), *flags])
    else:
        webbrowser.open(base_url + "/")

    while not controller.shutdown_requested and controller.server.started:
        time.sleep(0.5)


def _run_server_only(port: int | None, no_browser: bool, host: str = "127.0.0.1") -> int:
    _configure_logging()
    chosen_port = int(port) if port else find_available_port(8765, 8799)
    bind_host = (host or "127.0.0.1").strip() or "127.0.0.1"
    server, thread, _ = _start_uvicorn_background(chosen_port, host=bind_host)
    if not _wait_for_health(chosen_port, timeout_sec=20.0):
        logger.error("Embedded server failed to respond on port %s", chosen_port)
        server.should_exit = True
        return 1
    _write_runtime_port(chosen_port)
    if not no_browser:
        webbrowser.open(f"http://127.0.0.1:{chosen_port}/")
    try:
        while thread.is_alive():
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=5.0)
    finally:
        _remove_runtime_port()
    return 0


def _run_desktop_gui(port: int | None) -> int:
    _configure_logging()
    mutex_handle = _acquire_single_instance_mutex()
    if mutex_handle is False:
        return 0

    chosen_port = int(port) if port else find_available_port(8765, 8799)
    base_url = f"http://127.0.0.1:{chosen_port}"
    server, thread, fastapi_app = _start_uvicorn_background(chosen_port)
    if not _wait_for_health(chosen_port, timeout_sec=20.0):
        logger.error("Embedded backend failed to become healthy on %s", base_url)
        server.should_exit = True
        _release_mutex(mutex_handle)
        return 1

    _write_runtime_port(chosen_port)
    controller = DesktopController(base_url=base_url, server=server)
    fastapi_app.state.desktop_controller = controller
    _start_tray_background(controller)

    from app.config import get_data_dir

    storage_dir = get_data_dir() / "webview_cache"
    storage_dir.mkdir(parents=True, exist_ok=True)
    icon_path = get_resource_path("build/assets/app.ico")

    try:
        import webview

        main_win = webview.create_window(
            title="NotebookLM-Fleet v0.4.0 · 多账号中控与云盘互通",
            url=f"{base_url}/",
            width=1440,
            height=900,
            min_size=(1100, 720),
            text_select=True,
            zoomable=True,
        )
        controller.main_window = main_win

        def _on_closing() -> bool:
            if controller.tray_active and not controller.shutdown_requested:
                try:
                    main_win.hide()
                    controller.notify_minimized_once()
                except Exception:  # noqa: BLE001
                    pass
                return False
            controller.shutdown_all()
            return True

        main_win.events.closing += _on_closing
        webview.start(
            gui="edgechromium" if sys.platform == "win32" else None,
            private_mode=False,
            storage_path=str(storage_dir),
            icon=str(icon_path) if icon_path.exists() else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PyWebView initialization failed (%s); falling back to browser app mode.", exc)
        if not controller.shutdown_requested:
            _launch_browser_app_fallback(base_url, controller)
    finally:
        controller.shutdown_all()
        thread.join(timeout=5.0)
        _remove_runtime_port()
        _release_mutex(mutex_handle)
    return 0


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args_list = list(sys.argv[1:] if argv is None else argv)

    if args_list and args_list[0] == "--mcp":
        from app.mcp_server import mcp

        mcp.run()
        return 0

    if args_list and args_list[0] == "--nlm-login":
        profile = args_list[1] if len(args_list) > 1 else "ultra-1"
        browser = args_list[2] if len(args_list) > 2 else "chrome"
        from notebooklm.notebooklm_cli import main as nlm_main

        return int(nlm_main(["-p", profile, "login", "--browser", browser]) or 0)

    if args_list and args_list[0] == "--nlm-cli":
        from notebooklm.notebooklm_cli import main as nlm_main

        return int(nlm_main(args_list[1:]) or 0)

    parser = argparse.ArgumentParser(description="NotebookLM-Fleet Desktop Launcher")
    parser.add_argument("--server-only", action="store_true", help="Run headless FastAPI backend without desktop window")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host in --server-only mode (e.g. 0.0.0.0 for LAN)")
    parser.add_argument("--port", type=int, default=None, help="Bind local TCP port (default auto-probe from 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser in --server-only mode")
    parsed, _unknown = parser.parse_known_args(args_list)

    if parsed.server_only:
        return _run_server_only(parsed.port, parsed.no_browser, host=parsed.host)
    return _run_desktop_gui(parsed.port)


if __name__ == "__main__":
    raise SystemExit(main())
