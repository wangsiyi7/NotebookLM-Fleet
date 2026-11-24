"""FastAPI routes for Baidu Netdisk authentication, browsing, and Google Drive bidirectional bridge."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..adapters import baidupan_adapter
from ..adapters.baidupan_adapter import (
    BAIDU_CDP_PORT,
    BaiduPanError,
    auth_manager,
    baidu_client,
)
from ..core.cloud_bridge import cloud_bridge_engine, list_gdrive_bridge_dir
from ..core.win_runtime import detect_google_drive_mounts
from ..db import get_settings_map

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


class BaiduOpenBrowserRequest(BaseModel):
    browser: str = Field(default="chrome", pattern="^(chrome|edge|auto)$")


class BaiduCookieImportRequest(BaseModel):
    raw_input: str = Field(..., min_length=5)
    stoken: str = ""


class BaiduOpenApiTokenRequest(BaseModel):
    access_token: str = Field(..., min_length=5)
    refresh_token: str = ""
    expires_in: int = Field(default=2592000, ge=0)


class BaiduMkdirRequest(BaseModel):
    path: str = Field(..., min_length=1)


class BaiduToGDriveRequest(BaseModel):
    source_paths: list[str] = Field(..., min_length=1)
    gdrive_subdir: str = ""
    preserve_tree: bool = True
    skip_existing: bool = True
    delete_after_sync: bool = False
    auto_ingest_topic_id: int | None = None


class GDriveToBaiduRequest(BaseModel):
    local_paths: list[str] = Field(..., min_length=1)
    baidu_target_dir: str = "/NotebookLM_同步"
    preserve_tree: bool = True
    skip_existing: bool = True


@router.get("/status")
async def get_bridge_status(live_check: bool = Query(default=True)) -> dict[str, Any]:
    summary = auth_manager.status_summary()
    quota = None
    if summary["authenticated"] and live_check:
        try:
            user = await baidu_client.get_user_info()
            summary["user_info"] = user
            quota = await baidu_client.get_quota()
        except Exception as exc:  # noqa: BLE001
            summary["warning"] = str(exc)

    settings = await get_settings_map()
    mounts = await asyncio.to_thread(detect_google_drive_mounts)
    return {
        "baidu": {**summary, "quota": quota, "cdp_port": BAIDU_CDP_PORT},
        "gdrive": {
            "drive_library_root": settings.get("drive_library_root", ""),
            "drive_hub_root": settings.get("drive_hub_root", ""),
            "default_gdrive_subdir": settings.get("baidupan_default_gdrive_subdir", "百度网盘同步"),
            "default_upload_dir": settings.get("baidupan_default_upload_dir", "/NotebookLM_同步"),
            "mounts": mounts,
        },
    }


@router.post("/auth/open-browser")
async def open_baidu_browser(body: BaiduOpenBrowserRequest | None = None) -> dict[str, Any]:
    req = body or BaiduOpenBrowserRequest()
    try:
        return await asyncio.to_thread(baidupan_adapter.open_baidu_login_browser, req.browser)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/sync-cdp")
async def sync_baidu_cdp() -> dict[str, Any]:
    try:
        summary = await baidupan_adapter.sync_baidu_cdp_cookies()
        user = await baidu_client.get_user_info()
        quota = await baidu_client.get_quota()
        return {"ok": True, "summary": {**summary, "user_info": user}, "quota": quota}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/cookie")
async def import_baidu_cookie(body: BaiduCookieImportRequest) -> dict[str, Any]:
    try:
        auth_manager.import_cookie_string(body.raw_input, stoken=body.stoken)
        user = await baidu_client.get_user_info()
        quota = await baidu_client.get_quota()
        return {"ok": True, "user_info": user, "quota": quota}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/openapi")
async def import_baidu_openapi(body: BaiduOpenApiTokenRequest) -> dict[str, Any]:
    try:
        auth_manager.set_openapi_token(
            body.access_token, refresh_token=body.refresh_token, expires_in=body.expires_in
        )
        user = await baidu_client.get_user_info()
        quota = await baidu_client.get_quota()
        return {"ok": True, "user_info": user, "quota": quota}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/logout")
async def logout_baidu() -> dict[str, Any]:
    auth_manager.clear()
    return {"ok": True}


@router.get("/baidu/files")
async def list_baidu_files(
    path: str = Query(default="/"),
    order: str = Query(default="name"),
    desc: bool = Query(default=False),
    start: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    try:
        items = await baidu_client.list_files(path, order=order, desc=desc, start=start, limit=limit)
        return {"path": path, "items": [i.to_dict() for i in items]}
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/baidu/search")
async def search_baidu_files(
    keyword: str = Query(..., min_length=1),
    dir_path: str = Query(default="/"),
    recursion: bool = Query(default=True),
    page: int = Query(default=1, ge=1),
    num: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    try:
        items = await baidu_client.search_files(
            keyword, dir_path=dir_path, recursion=recursion, page=page, num=num
        )
        return {"keyword": keyword, "items": [i.to_dict() for i in items]}
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/baidu/mkdir")
async def mkdir_baidu(body: BaiduMkdirRequest) -> dict[str, Any]:
    try:
        return await baidu_client.create_directory(body.path)
    except BaiduPanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/gdrive/browse")
async def browse_gdrive_for_bridge(
    root: str = Query(default="drive_library", pattern="^(drive_library|drive_hub)$"),
    subpath: str = Query(default=""),
) -> dict[str, Any]:
    try:
        return await list_gdrive_bridge_dir(root_kind=root, subpath=subpath)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/baidu-to-gdrive")
async def create_baidu_to_gdrive_job(body: BaiduToGDriveRequest) -> dict[str, Any]:
    try:
        return await cloud_bridge_engine.create_baidu_to_gdrive_job(
            source_paths=body.source_paths,
            gdrive_subdir=body.gdrive_subdir or None,
            preserve_tree=body.preserve_tree,
            skip_existing=body.skip_existing,
            delete_after_sync=body.delete_after_sync,
            auto_ingest_topic_id=body.auto_ingest_topic_id,
            wait=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/gdrive-to-baidu")
async def create_gdrive_to_baidu_job(body: GDriveToBaiduRequest) -> dict[str, Any]:
    try:
        return await cloud_bridge_engine.create_gdrive_to_baidu_job(
            local_paths=body.local_paths,
            baidu_target_dir=body.baidu_target_dir or "/NotebookLM_同步",
            preserve_tree=body.preserve_tree,
            skip_existing=body.skip_existing,
            wait=False,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs")
async def list_bridge_jobs(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
    return {"jobs": await cloud_bridge_engine.list_jobs(limit=limit)}


@router.get("/jobs/{job_id}")
async def get_bridge_job(job_id: str) -> dict[str, Any]:
    try:
        return await cloud_bridge_engine.get_job(job_id, include_items=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/cancel")
async def cancel_bridge_job(job_id: str) -> dict[str, Any]:
    try:
        return await cloud_bridge_engine.cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/retry")
async def retry_bridge_job(job_id: str) -> dict[str, Any]:
    try:
        return await cloud_bridge_engine.retry_failed(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
