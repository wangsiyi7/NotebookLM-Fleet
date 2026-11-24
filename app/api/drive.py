"""Google Drive Hub & Library browsing + manual SQLite backup routes."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..config import get_db_path
from ..core.drive_hub import backup_sqlite_to_drive, list_directory, scan_inbox
from ..db import fetch_all, get_settings_map, set_settings_map

router = APIRouter(prefix="/api/drive", tags=["drive"])


@router.get("/inbox")
async def get_inbox() -> dict[str, Any]:
    settings = await get_settings_map()
    hub_root = settings["drive_hub_root"]
    entries = await asyncio.to_thread(scan_inbox, hub_root)
    return {"hub_root": hub_root, "entries": entries}


@router.get("/library")
async def browse_library(subpath: str = Query(default="")) -> dict[str, Any]:
    settings = await get_settings_map()
    lib_root = settings["drive_library_root"]
    try:
        return await asyncio.to_thread(list_directory, lib_root, subpath)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/topics")
async def browse_topics_drive(subpath: str = Query(default="")) -> dict[str, Any]:
    settings = await get_settings_map()
    hub_root = settings["drive_hub_root"]
    try:
        listing = await asyncio.to_thread(list_directory, f"{hub_root}/02_Topics", subpath)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    artifacts = await fetch_all(
        """
        SELECT ar.*, tp.name AS topic_name, acc.alias AS account_alias
          FROM artifacts ar
          LEFT JOIN topics tp ON tp.id = ar.topic_id
          LEFT JOIN accounts acc ON acc.id = ar.account_id
         ORDER BY ar.id DESC
         LIMIT 50
        """
    )
    return {"drive": listing, "recent_artifacts": artifacts}


@router.post("/backup-now")
async def trigger_backup() -> dict[str, Any]:
    settings = await get_settings_map()
    hub_root = settings["drive_hub_root"]
    result = await asyncio.to_thread(backup_sqlite_to_drive, get_db_path(), hub_root, keep=14)
    await set_settings_map({"last_backup_at": datetime.now().isoformat(timespec="seconds")})
    return result
