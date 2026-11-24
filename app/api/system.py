"""Windows desktop, Google Drive auto-detection, native folder picker, and Explorer integration API."""

from __future__ import annotations

import asyncio
import platform
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..adapters import browser_takeover
from ..config import get_data_dir
from ..core.drive_hub import ensure_hub_structure
from ..core.win_runtime import (
    detect_google_drive_mounts,
    get_current_desktop_name,
    is_frozen,
    is_interactive_desktop,
    pick_folder_native,
    reveal_in_explorer,
)
from ..db import get_settings_map, set_settings_map
from .workspace import get_allowed_roots, is_sensitive_path

router = APIRouter(prefix="/api/system", tags=["system"])


class AutoDetectGDriveRequest(BaseModel):
    apply: bool = False


class PickFolderRequest(BaseModel):
    initial_dir: str | None = None
    title: str = "选择文件夹"


class RevealPathRequest(BaseModel):
    path: str = Field(..., min_length=1)


class OpenWindowRequest(BaseModel):
    path: str = Field(default="/", min_length=1)
    title: str = "NotebookLM-Fleet"
    width: int = Field(default=1280, ge=640, le=3840)
    height: int = Field(default=840, ge=480, le=2160)


@router.get("/info")
async def get_system_info(request: Request) -> dict[str, Any]:
    settings = await get_settings_map()
    mounts = await asyncio.to_thread(detect_google_drive_mounts)
    chrome_exe = await asyncio.to_thread(browser_takeover.find_chrome_executable)
    edge_exe = await asyncio.to_thread(browser_takeover.find_edge_executable)
    ctrl = getattr(request.app.state, "desktop_controller", None)
    return {
        "os": sys.platform,
        "arch": platform.machine(),
        "python": platform.python_version(),
        "is_frozen": is_frozen(),
        "app_version": "0.4.0",
        "desktop_mode": ctrl is not None,
        "desktop_name": get_current_desktop_name(),
        "interactive_desktop": is_interactive_desktop(),
        "port": getattr(request.app.state, "bound_port", None),
        "data_dir": str(get_data_dir()),
        "browsers": {
            "chrome": str(chrome_exe) if chrome_exe else None,
            "edge": str(edge_exe) if edge_exe else None,
        },
        "gdrive_mounts": mounts,
        "configured_drive_library": settings.get("drive_library_root", ""),
        "configured_drive_hub": settings.get("drive_hub_root", ""),
    }


@router.post("/auto-detect-gdrive")
async def auto_detect_gdrive(body: AutoDetectGDriveRequest | None = None) -> dict[str, Any]:
    req = body or AutoDetectGDriveRequest()
    mounts = await asyncio.to_thread(detect_google_drive_mounts)
    settings = await get_settings_map()
    lib_root = settings.get("drive_library_root", "")
    hub_root = settings.get("drive_hub_root", "")
    applied = False

    if req.apply and mounts:
        lib_root = mounts[0]["path"]
        hub_root = str(Path(lib_root) / "NLM_Hub")
        try:
            await asyncio.to_thread(ensure_hub_structure, hub_root)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"初始化 NLM_Hub 失败: {exc}") from exc
        await set_settings_map({"drive_library_root": lib_root, "drive_hub_root": hub_root})
        applied = True

    return {
        "detected": bool(mounts),
        "mounts": mounts,
        "applied": applied,
        "drive_library_root": lib_root,
        "drive_hub_root": hub_root,
    }


@router.post("/pick-folder")
async def pick_folder(request: Request, body: PickFolderRequest | None = None) -> dict[str, Any]:
    req = body or PickFolderRequest()
    ctrl = getattr(request.app.state, "desktop_controller", None)
    try:
        selected = await asyncio.to_thread(pick_folder_native, req.initial_dir, req.title, ctrl)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开目录选择器失败: {exc}") from exc
    return {"path": selected, "canceled": selected is None}


async def _is_allowed_reveal_path(target: Path) -> bool:
    resolved = target.resolve()
    if is_sensitive_path(resolved):
        return False
    allowed_bases: list[Path] = [get_data_dir().resolve()]
    roots = await get_allowed_roots()
    for info in roots.values():
        if info.get("path"):
            allowed_bases.append(Path(info["path"]).resolve())
    for mount in await asyncio.to_thread(detect_google_drive_mounts):
        if mount.get("path"):
            allowed_bases.append(Path(mount["path"]).resolve())

    for base in allowed_bases:
        if resolved == base or resolved.is_relative_to(base):
            return True
    return False


@router.post("/reveal-path")
async def reveal_path(body: RevealPathRequest) -> dict[str, Any]:
    raw = body.path.strip()
    target = Path(raw)
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"路径格式无效: {exc}") from exc

    if is_sensitive_path(resolved):
        raise HTTPException(status_code=403, detail="出于安全策略禁止打开凭据文件")
    if not await _is_allowed_reveal_path(resolved):
        raise HTTPException(status_code=403, detail="仅允许打开工作区、数据目录或 Google 云盘路径")
    if not await asyncio.to_thread(resolved.exists):
        raise HTTPException(status_code=404, detail=f"路径不存在: {resolved}")

    try:
        return await asyncio.to_thread(reveal_in_explorer, resolved)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开资源管理器失败: {exc}") from exc


@router.post("/open-window")
async def open_native_window(request: Request, body: OpenWindowRequest) -> dict[str, Any]:
    safe_path = body.path.strip()
    if not safe_path.startswith("/") or safe_path.startswith("//"):
        raise HTTPException(status_code=400, detail="仅允许打开本地站内相对路径")

    ctrl = getattr(request.app.state, "desktop_controller", None)
    if ctrl is not None and hasattr(ctrl, "open_child_window"):
        try:
            opened = await asyncio.to_thread(
                ctrl.open_child_window,
                safe_path,
                body.title[:120],
                body.width,
                body.height,
            )
            return {"opened_native": bool(opened), "path": safe_path}
        except Exception:  # noqa: BLE001
            pass

    return {"opened_native": False, "path": safe_path}
