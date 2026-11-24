"""FastAPI entrypoint for NotebookLM-Fleet control panel and background workers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .adapters import official_api
from .api.accounts import router as accounts_router
from .api.drive import router as drive_router
from .api.inventory import router as inventory_router
from .api.official import router as official_router
from .api.tasks import router as tasks_router
from .api.workspace import router as workspace_router
from .config import PROJECT_ROOT, get_settings
from .core.drive_hub import ensure_hub_structure
from .core.quota_poller import quota_poller_loop
from .core.scheduler import scheduler_loop
from .db import (
    execute,
    fetch_all,
    get_accounts_with_latest_quota,
    get_settings_map,
    init_db,
    set_settings_map,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class SettingsUpdate(BaseModel):
    updates: dict[str, Any]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = get_settings()
    await init_db(settings=cfg)
    # Recover any tasks orphaned in 'running' state after an unexpected server restart.
    await execute(
        """
        UPDATE tasks
           SET status = 'pending',
               assigned_account_id = NULL,
               started_at = NULL,
               error_msg = '上次服务重启中断，已自动恢复待调度'
         WHERE status = 'running'
        """
    )
    settings_map = await get_settings_map()
    with contextlib.suppress(OSError):
        ensure_hub_structure(settings_map["drive_hub_root"])

    stop_event = asyncio.Event()
    bg_tasks: list[asyncio.Task[None]] = []
    if not cfg.fleet_disable_workers:
        bg_tasks.append(asyncio.create_task(quota_poller_loop(stop_event)))
        bg_tasks.append(asyncio.create_task(scheduler_loop(stop_event)))
    app.state.stop_event = stop_event
    app.state.bg_tasks = bg_tasks
    try:
        yield
    finally:
        stop_event.set()
        for t in bg_tasks:
            t.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)


app = FastAPI(title="NotebookLM-Fleet Control Center", version="0.1.0", lifespan=lifespan)

app.include_router(accounts_router)
app.include_router(inventory_router)
app.include_router(tasks_router)
app.include_router(drive_router)
app.include_router(workspace_router)
app.include_router(official_router)


@app.get("/")
async def serve_dashboard() -> FileResponse:
    return FileResponse(
        PROJECT_ROOT / "web" / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> JSONResponse:
    from fastapi.responses import Response

    return Response(status_code=204)


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "notebooklm-fleet"}


@app.get("/api/dashboard")
async def get_dashboard() -> dict[str, Any]:
    accounts = await get_accounts_with_latest_quota()
    settings = await get_settings_map()
    tasks = await fetch_all("SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status")
    status_counts = {r["status"]: int(r["cnt"]) for r in tasks}

    topics_cnt_row = await fetch_all("SELECT COUNT(*) AS cnt FROM topics")
    artifacts_cnt_row = await fetch_all("SELECT COUNT(*) AS cnt FROM artifacts")
    topics_count = int(topics_cnt_row[0]["cnt"]) if topics_cnt_row else 0
    artifacts_count = int(artifacts_cnt_row[0]["cnt"]) if artifacts_cnt_row else 0

    healthy = [a for a in accounts if a.get("enabled") and a.get("nlm_auth_status") == "ok"]
    five_vals = [float(a["five_hour_remaining_pct"]) for a in healthy if a.get("five_hour_remaining_pct") is not None]
    week_vals = [float(a["weekly_remaining_pct"]) for a in healthy if a.get("weekly_remaining_pct") is not None]

    return {
        "kpis": {
            "accounts_total": len(accounts),
            "accounts_healthy": len(healthy),
            "avg_five_hour_remaining_pct": round(sum(five_vals) / len(five_vals), 1) if five_vals else None,
            "avg_weekly_remaining_pct": round(sum(week_vals) / len(week_vals), 1) if week_vals else None,
            "pending_tasks": status_counts.get("pending", 0) + status_counts.get("waiting_quota", 0),
            "running_tasks": status_counts.get("running", 0),
            "deferred_tasks": status_counts.get("deferred_later", 0),
            "done_tasks": status_counts.get("done", 0),
            "failed_tasks": status_counts.get("failed", 0),
            "topics_count": topics_count,
            "artifacts_count": artifacts_count,
        },
        "official_api": {
            "client_secret_present": official_api.has_client_secret(),
            "enabled": settings.get("official_api_enabled", "false") == "true",
            "project_id": settings.get("official_api_project_id", ""),
        },
        "settings": settings,
    }


@app.get("/api/settings")
async def read_settings() -> dict[str, Any]:
    return {
        "settings": await get_settings_map(),
        "client_secret_present": official_api.has_client_secret(),
    }


@app.put("/api/settings")
async def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    updated = await set_settings_map(body.updates)
    return {
        "settings": updated,
        "client_secret_present": official_api.has_client_secret(),
    }


@app.get("/api/export/json")
async def export_backup_json() -> JSONResponse:
    accounts = await fetch_all(
        "SELECT id, alias, email, tier, nlm_profile, is_hub_master, drive_shared_ready, expire_date, enabled, note FROM accounts ORDER BY id"
    )
    topics = await fetch_all("SELECT * FROM topics ORDER BY id")
    tasks = await fetch_all("SELECT * FROM tasks ORDER BY id DESC LIMIT 500")
    artifacts = await fetch_all("SELECT * FROM artifacts ORDER BY id DESC LIMIT 500")
    settings = await get_settings_map()
    return JSONResponse(
        content={
            "accounts": accounts,
            "topics": topics,
            "tasks": tasks,
            "artifacts": artifacts,
            "settings": settings,
        }
    )
