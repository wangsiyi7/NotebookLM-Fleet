"""Unified NotebookLM-Fleet MCP Server (Stdio transport for Antigravity / Gemini CLI)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

# CRITICAL: Stdio MCP servers must NEVER write logs to stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from fastmcp import FastMCP  # noqa: E402

from .adapters import browser_takeover, nlm_py_adapter, official_api  # noqa: E402
from .config import get_chrome_profile_dir, get_settings  # noqa: E402
from .core.drive_hub import ensure_hub_structure, ensure_topic_folders  # noqa: E402
from .core.quota_poller import poll_all_accounts, poll_one_account  # noqa: E402
from .db import (  # noqa: E402
    TIER_LIMITS,
    execute,
    fetch_all,
    fetch_one,
    get_account_by_id_or_alias,
    get_accounts_with_latest_quota,
    get_settings_map,
    init_db,
    next_cdp_port,
    set_settings_map,
)

mcp = FastMCP("notebooklm-fleet")

_db_initialized = False


async def _ensure_ready() -> None:
    global _db_initialized
    if not _db_initialized:
        await init_db()
        settings = await get_settings_map()
        try:
            ensure_hub_structure(settings["drive_hub_root"])
        except OSError:
            pass
        _db_initialized = True


@mcp.tool()
async def fleet_quota_overview() -> dict[str, Any]:
    """Return 5-hour & weekly remaining percentages, reset times, and auth health for all accounts."""
    await _ensure_ready()
    accounts = await get_accounts_with_latest_quota()
    summary = []
    for a in accounts:
        summary.append(
            {
                "alias": a["alias"],
                "email": a["email"],
                "tier": a["tier"],
                "enabled": bool(a["enabled"]),
                "is_hub_master": bool(a["is_hub_master"]),
                "nlm_auth_status": a["nlm_auth_status"],
                "official_oauth_status": a["official_oauth_status"],
                "five_hour_remaining_pct": a.get("five_hour_remaining_pct"),
                "five_hour_used_pct": a.get("five_hour_used_pct"),
                "five_hour_resets_at": a.get("five_hour_resets_at"),
                "weekly_remaining_pct": a.get("weekly_remaining_pct"),
                "weekly_used_pct": a.get("weekly_used_pct"),
                "weekly_resets_at": a.get("weekly_resets_at"),
                "is_exhausted": bool(a.get("is_exhausted")),
                "captured_at": a.get("quota_captured_at"),
                "actions": a.get("actions") or [],
            }
        )
    return {"accounts": summary, "count": len(summary)}


@mcp.tool()
async def fleet_poll_quotas(alias: str | None = None) -> dict[str, Any]:
    """Force refresh live 5-hour & weekly compute usage for one account (by alias/email) or all enabled accounts."""
    await _ensure_ready()
    if alias:
        acct = await get_account_by_id_or_alias(alias)
        if acct is None:
            raise ValueError(f"找不到账号: {alias}")
        res = await poll_one_account(acct["id"])
        return {"results": [res]}
    results = await poll_all_accounts()
    return {"results": results, "count": len(results)}


@mcp.tool()
async def fleet_list_accounts() -> dict[str, Any]:
    """List all registered Ultra/Pro/Plus/Free accounts with auth & subscription metadata."""
    await _ensure_ready()
    rows = await fetch_all(
        """
        SELECT id, alias, email, tier, nlm_profile, cdp_port, is_hub_master,
               drive_shared_ready, nlm_auth_status, official_oauth_status,
               notebook_limit, source_limit, expire_date, enabled, note
          FROM accounts
         ORDER BY is_hub_master DESC, id ASC
        """
    )
    return {"accounts": rows, "count": len(rows)}


@mcp.tool()
async def fleet_add_account(
    alias: str,
    email: str,
    tier: str = "ultra",
    expire_date: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Register a new Google account (ultra/pro/plus/free) with isolated Chrome & notebooklm-py profiles."""
    await _ensure_ready()
    tier_norm = tier.lower().strip()
    if tier_norm not in TIER_LIMITS:
        raise ValueError("tier 必须是 ultra, pro, plus, free 之一")
    nb_lim, src_lim = TIER_LIMITS[tier_norm]
    chrome_dir = str(get_chrome_profile_dir(alias))
    cdp_port = await next_cdp_port()
    local_status = nlm_py_adapter.check_local_auth(alias)["status"]
    oauth_status = official_api.check_oauth_status(alias)
    new_id = await execute(
        """
        INSERT INTO accounts (
            alias, email, tier, nlm_profile, chrome_profile_dir, cdp_port,
            nlm_auth_status, official_oauth_status, notebook_limit, source_limit,
            expire_date, enabled, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            alias.strip(),
            email.strip(),
            tier_norm,
            alias.strip(),
            chrome_dir,
            cdp_port,
            local_status,
            oauth_status,
            nb_lim,
            src_lim,
            expire_date,
            note,
        ),
    )
    row = await fetch_one("SELECT * FROM accounts WHERE id = ?", (new_id,))
    return {"account": row}


@mcp.tool()
async def fleet_list_topics() -> dict[str, Any]:
    """List unified learning topics across accounts along with Google Drive Hub paths and counts."""
    await _ensure_ready()
    topics = await fetch_all(
        """
        SELECT t.*, a.alias AS master_alias,
               (SELECT COALESCE(MAX(nb.source_count), 0) FROM notebooks nb WHERE nb.topic_id = t.id) AS source_count,
               (SELECT COUNT(*) FROM artifacts ar WHERE ar.topic_id = t.id) AS artifact_count
          FROM topics t
          LEFT JOIN accounts a ON a.id = t.master_account_id
         ORDER BY t.id DESC
        """
    )
    return {"topics": topics, "count": len(topics)}


@mcp.tool()
async def fleet_create_topic_and_ingest(
    topic_name: str,
    sources: list[str],
    master_alias: str | None = None,
) -> dict[str, Any]:
    """Create (or reuse) a learning topic + Drive folder and queue URL / local file / text sources for ingest."""
    await _ensure_ready()
    settings = await get_settings_map()
    folders = ensure_topic_folders(settings["drive_hub_root"], topic_name.strip())

    master = (
        await get_account_by_id_or_alias(master_alias)
        if master_alias
        else await fetch_one("SELECT * FROM accounts ORDER BY is_hub_master DESC, id ASC LIMIT 1")
    )
    master_id = master["id"] if master else None

    topic = await fetch_one("SELECT * FROM topics WHERE name = ?", (topic_name.strip(),))
    if topic is None:
        tid = await execute(
            "INSERT INTO topics (name, drive_folder_path, master_account_id) VALUES (?, ?, ?)",
            (topic_name.strip(), str(folders["topic"]), master_id),
        )
        topic = await fetch_one("SELECT * FROM topics WHERE id = ?", (tid,))
    assert topic is not None

    batch_id = f"ING-{uuid.uuid4().hex[:8]}"
    task_ids: list[int] = []
    for item in sources:
        s = (item or "").strip()
        if not s:
            continue
        if s.startswith(("http://", "https://")):
            ttype = "ingest_url"
            payload = {"url": s}
        elif Path(s).exists():
            ttype = "ingest_file"
            payload = {"file_path": s}
        else:
            ttype = "ingest_text"
            payload = {"title": s[:40], "text": s}

        task_id = await execute(
            """
            INSERT INTO tasks (
                batch_id, topic_id, task_type, payload_json,
                preferred_account_id, tier_requirement, priority, status
            ) VALUES (?, ?, ?, ?, ?, 'any', 20, 'pending')
            """,
            (batch_id, topic["id"], ttype, json.dumps(payload, ensure_ascii=False), master_id),
        )
        task_ids.append(task_id)

    return {
        "topic": topic,
        "drive_sources_dir": str(folders["sources"]),
        "drive_artifacts_dir": str(folders["artifacts"]),
        "batch_id": batch_id,
        "queued_task_ids": task_ids,
    }


@mcp.tool()
async def fleet_enqueue_batch(
    topic_name: str,
    actions: list[str],
    instructions: str | None = None,
    language: str = "zh-Hans",
    policy: str | None = None,
    preferred_alias: str | None = None,
) -> dict[str, Any]:
    """Queue batch Studio generation or Chat Q&A tasks for a topic across the quota-aware account pool."""
    await _ensure_ready()
    topic = await fetch_one("SELECT * FROM topics WHERE name = ?", (topic_name.strip(),))
    if topic is None:
        settings = await get_settings_map()
        folders = ensure_topic_folders(settings["drive_hub_root"], topic_name.strip())
        master = await fetch_one("SELECT id FROM accounts ORDER BY is_hub_master DESC, id ASC LIMIT 1")
        tid = await execute(
            "INSERT INTO topics (name, drive_folder_path, master_account_id) VALUES (?, ?, ?)",
            (topic_name.strip(), str(folders["topic"]), master["id"] if master else None),
        )
        topic = await fetch_one("SELECT * FROM topics WHERE id = ?", (tid,))
    assert topic is not None

    if policy and policy in ("conserve_ultra", "ultra_first", "balanced"):
        await set_settings_map({"tier_policy": policy})

    pref_id = None
    if preferred_alias:
        acct = await get_account_by_id_or_alias(preferred_alias)
        if acct is None:
            raise ValueError(f"找不到偏好账号: {preferred_alias}")
        pref_id = acct["id"]

    batch_id = f"B-{uuid.uuid4().hex[:8]}"
    task_ids: list[int] = []
    for action in actions:
        payload: dict[str, Any] = {"language": language}
        if instructions:
            payload["instructions"] = instructions
        if action == "chat":
            payload["question"] = instructions or "请总结核心观点、关键概念与可执行建议。"
            payload["save_as_note"] = True

        tid = await execute(
            """
            INSERT INTO tasks (
                batch_id, topic_id, task_type, payload_json,
                preferred_account_id, tier_requirement, priority, status
            ) VALUES (?, ?, ?, ?, ?, 'any', 50, 'pending')
            """,
            (batch_id, topic["id"], action.strip(), json.dumps(payload, ensure_ascii=False), pref_id),
        )
        task_ids.append(tid)

    return {"batch_id": batch_id, "topic": topic["name"], "queued_count": len(task_ids), "task_ids": task_ids}


@mcp.tool()
async def fleet_task_status(
    batch_id: str | None = None,
    status: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Inspect queued, running, waiting_quota, deferred_later, done, or failed tasks and their Drive paths."""
    await _ensure_ready()
    clauses: list[str] = []
    params: list[Any] = []
    if batch_id:
        clauses.append("t.batch_id = ?")
        params.append(batch_id)
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await fetch_all(
        f"""
        SELECT t.id, t.batch_id, tp.name AS topic_name, t.task_type, t.status,
               aa.alias AS assigned_alias, t.channel_used, t.est_cost_pct,
               t.actual_delta_5h_pct, t.scheduled_after, t.result_artifact_path,
               t.result_text, t.error_msg, t.created_at, t.finished_at
          FROM tasks t
          LEFT JOIN topics tp ON tp.id = t.topic_id
          LEFT JOIN accounts aa ON aa.id = t.assigned_account_id
         {where_sql}
         ORDER BY t.id DESC
         LIMIT ?
        """,
        (*params, limit),
    )
    return {"tasks": rows, "count": len(rows)}


@mcp.tool()
async def fleet_control_task(task_id: int, action: str = "retry") -> dict[str, Any]:
    """Cancel a pending/waiting task or retry a failed/canceled task."""
    await _ensure_ready()
    task = await fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")
    act = action.lower().strip()
    if act == "cancel":
        await execute(
            "UPDATE tasks SET status = 'canceled', finished_at = datetime('now', 'localtime') WHERE id = ?",
            (task_id,),
        )
    elif act == "retry":
        await execute(
            "UPDATE tasks SET status = 'pending', scheduled_after = NULL, error_msg = NULL, finished_at = NULL WHERE id = ?",
            (task_id,),
        )
    else:
        raise ValueError("action 必须是 cancel 或 retry")
    return {"task": await fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))}


@mcp.tool()
async def fleet_open_account_browser(
    alias: str,
    mode: str = "nlm_login",
    url: str = "https://notebook.google.com/",
) -> dict[str, Any]:
    """Open an isolated Chrome window for one account (mode: 'nlm_login', 'chrome', or 'official_oauth')."""
    await _ensure_ready()
    acct = await get_account_by_id_or_alias(alias)
    if acct is None:
        raise ValueError(f"找不到账号: {alias}")
    m = mode.lower().strip()
    if m == "nlm_login":
        return nlm_py_adapter.launch_nlm_login(acct["nlm_profile"], browser="chrome")
    if m == "chrome":
        cdp_port = int(acct.get("cdp_port") or await next_cdp_port())
        return browser_takeover.open_account_browser(
            acct["alias"], acct["chrome_profile_dir"], cdp_port, url=url
        )
    if m == "official_oauth":
        settings = await get_settings_map()
        proxy = (settings.get("official_proxy_url") or "").strip() or None
        auth_url, port, finish_fn = official_api.start_oauth_flow_for_account(acct["alias"], proxy=proxy)

        async def _wait_and_save() -> None:
            try:
                await asyncio.to_thread(finish_fn)
                await execute(
                    "UPDATE accounts SET official_oauth_status = 'ok', updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (acct["id"],),
                )
            except Exception:  # noqa: BLE001
                await execute(
                    "UPDATE accounts SET official_oauth_status = 'error', updated_at = datetime('now', 'localtime') WHERE id = ?",
                    (acct["id"],),
                )

        asyncio.create_task(_wait_and_save())
        cfg = get_settings()
        return {
            "started": True,
            "alias": acct["alias"],
            "callback_port": port,
            "auth_url": auth_url,
            "web_redirect_url": f"http://{cfg.host}:{cfg.port}/api/accounts/{acct['id']}/oauth-redirect",
        }
    raise ValueError("mode 必须是 nlm_login, chrome 或 official_oauth")


if __name__ == "__main__":
    mcp.run()
