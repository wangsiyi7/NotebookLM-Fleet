"""Unified Topics, cross-account notebooks, sharing, and ingest routes."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..adapters import nlm_py_adapter
from ..adapters.router import CapabilityRouter
from ..core.coupling import ensure_topic_notebook_for_account
from ..core.drive_hub import ensure_topic_folders
from ..db import execute, fetch_all, fetch_one, get_settings_map
from .workspace import get_allowed_roots, is_allowed_file_for_ingest

router = APIRouter(prefix="/api", tags=["inventory"])


class TopicCreate(BaseModel):
    name: str
    master_account_id: int | None = None
    coupling_mode: str = Field(
        default="shared_with_drive_fallback",
        pattern="^(shared|drive_mirror|shared_with_drive_fallback)$",
    )
    note: str = ""
    create_remote: bool = False


class IngestRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    drive_ids: list[dict[str, Any]] = Field(default_factory=list)
    texts: list[dict[str, str]] = Field(default_factory=list)
    preferred_account_id: int | None = None


@router.get("/topics")
async def list_topics() -> dict[str, Any]:
    topics = await fetch_all(
        """
        SELECT t.*,
               a.alias AS master_alias,
               a.email AS master_email
          FROM topics t
          LEFT JOIN accounts a ON a.id = t.master_account_id
         ORDER BY t.id DESC
        """
    )
    for topic in topics:
        nbs = await fetch_all(
            """
            SELECT nb.*, acc.alias AS account_alias, acc.email AS account_email
              FROM notebooks nb
              JOIN accounts acc ON acc.id = nb.account_id
             WHERE nb.topic_id = ?
             ORDER BY nb.is_master DESC, nb.id ASC
            """,
            (topic["id"],),
        )
        arts = await fetch_all(
            "SELECT * FROM artifacts WHERE topic_id = ? ORDER BY id DESC LIMIT 20",
            (topic["id"],),
        )
        topic["notebooks"] = nbs
        topic["source_count"] = max((int(nb.get("source_count") or 0) for nb in nbs), default=0)
        topic["artifact_count"] = len(arts)
        topic["artifacts"] = arts
        shared: set[str] = set()
        for nb in nbs:
            try:
                shared.update(json.loads(nb.get("shared_emails_json") or "[]"))
            except json.JSONDecodeError:
                pass
        topic["shared_emails"] = sorted(shared)
    return {"topics": topics}


@router.post("/topics")
async def create_topic(body: TopicCreate) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="主题名称不能为空")

    settings = await get_settings_map()
    hub_root = settings["drive_hub_root"]
    try:
        folders = ensure_topic_folders(hub_root, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    master_id = body.master_account_id
    if master_id is None:
        master = await fetch_one("SELECT id FROM accounts ORDER BY is_hub_master DESC, id ASC LIMIT 1")
        master_id = master["id"] if master else None

    try:
        topic_id = await execute(
            """
            INSERT INTO topics (name, drive_folder_path, master_account_id, coupling_mode, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, str(folders["topic"]), master_id, body.coupling_mode, body.note),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"创建主题失败（可能重名）: {exc}") from exc

    topic = await fetch_one("SELECT * FROM topics WHERE id = ?", (topic_id,))
    assert topic is not None

    if body.create_remote and master_id is not None:
        master_acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (master_id,))
        cap_router = CapabilityRouter(settings)
        if master_acct and (
            master_acct.get("nlm_auth_status") == "ok" or cap_router.can_use_official(master_acct)
        ):
            await ensure_topic_notebook_for_account(topic, master_acct, cap_router)

    return {"topic": topic, "folders": {k: str(v) for k, v in folders.items()}}


@router.delete("/topics/{topic_id}")
async def delete_topic(topic_id: int) -> dict[str, Any]:
    topic = await fetch_one("SELECT * FROM topics WHERE id = ?", (topic_id,))
    if topic is None:
        raise HTTPException(status_code=404, detail="主题不存在")
    await execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    return {"deleted": True, "id": topic_id}


@router.post("/topics/{topic_id}/share-all")
async def share_topic_to_all(topic_id: int) -> dict[str, Any]:
    topic = await fetch_one("SELECT * FROM topics WHERE id = ?", (topic_id,))
    if topic is None:
        raise HTTPException(status_code=404, detail="主题不存在")
    master_nb = await fetch_one(
        "SELECT * FROM notebooks WHERE topic_id = ? AND is_master = 1 LIMIT 1",
        (topic_id,),
    )
    if master_nb is None:
        raise HTTPException(status_code=400, detail="该主题尚未在主账号创建远程笔记本，请先导入资料或同步。")
    master_acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (master_nb["account_id"],))
    if not master_acct or master_acct.get("nlm_auth_status") != "ok":
        raise HTTPException(status_code=400, detail="主账号尚未登录 NotebookLM 会话")

    others = await fetch_all(
        "SELECT * FROM accounts WHERE enabled = 1 AND id != ?",
        (master_acct["id"],),
    )
    settings = await get_settings_map()
    cap_router = CapabilityRouter(settings)
    shared = []
    errors = []
    try:
        existing_shared = json.loads(master_nb.get("shared_emails_json") or "[]")
    except json.JSONDecodeError:
        existing_shared = []

    for acct in others:
        if "@" not in acct["email"] or acct["email"].endswith(".local"):
            continue
        try:
            await cap_router.share_editor(master_acct, master_nb["remote_notebook_id"], acct["email"], notify=False)
            if acct["email"] not in existing_shared:
                existing_shared.append(acct["email"])
            shared.append(acct["email"])
        except Exception as exc:  # noqa: BLE001
            errors.append({"email": acct["email"], "error": str(exc)})

    await execute(
        "UPDATE notebooks SET shared_emails_json = ?, synced_at = datetime('now', 'localtime') WHERE id = ?",
        (json.dumps(existing_shared, ensure_ascii=False), master_nb["id"]),
    )
    return {"shared": shared, "errors": errors}


@router.post("/topics/{topic_id}/ingest")
async def ingest_to_topic(topic_id: int, body: IngestRequest) -> dict[str, Any]:
    topic = await fetch_one("SELECT * FROM topics WHERE id = ?", (topic_id,))
    if topic is None:
        raise HTTPException(status_code=404, detail="主题不存在")

    roots = await get_allowed_roots()
    validated_paths: list[str] = []
    for fp in body.file_paths:
        p = fp.strip()
        if not p:
            continue
        try:
            resolved = is_allowed_file_for_ingest(p, roots)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        validated_paths.append(str(resolved))

    pref_id = body.preferred_account_id or topic.get("master_account_id")
    batch_id = f"ING-{uuid.uuid4().hex[:8]}"
    created_ids: list[int] = []

    async def _enqueue(task_type: str, payload: dict[str, Any]) -> None:
        tid = await execute(
            """
            INSERT INTO tasks (
                batch_id, topic_id, task_type, payload_json,
                preferred_account_id, tier_requirement, priority, status
            ) VALUES (?, ?, ?, ?, ?, 'any', 20, 'pending')
            """,
            (batch_id, topic_id, task_type, json.dumps(payload, ensure_ascii=False), pref_id),
        )
        created_ids.append(tid)

    for url in body.urls:
        u = url.strip()
        if u:
            await _enqueue("ingest_url", {"url": u})
    for fp_valid in validated_paths:
        await _enqueue("ingest_file", {"file_path": fp_valid})
    for item in body.drive_ids:
        if item.get("file_id"):
            await _enqueue("ingest_drive", item)
    for txt in body.texts:
        if txt.get("text"):
            await _enqueue("ingest_text", txt)

    return {"batch_id": batch_id, "created_count": len(created_ids), "task_ids": created_ids}


@router.post("/notebooks/sync-all")
async def sync_all_notebooks() -> dict[str, Any]:
    accounts = await fetch_all(
        "SELECT * FROM accounts WHERE enabled = 1 AND (nlm_auth_status = 'ok' OR official_oauth_status = 'ok')"
    )
    settings = await get_settings_map()
    cap_router = CapabilityRouter(settings)
    synced = 0
    errors = []

    for acct in accounts:
        if acct.get("nlm_auth_status") != "ok" and not cap_router.can_use_official(acct):
            continue
        try:
            nbs, _ = await cap_router.list_notebooks(acct)
            for nb in nbs:
                topic = await fetch_one("SELECT id FROM topics WHERE name = ?", (nb["title"],))
                topic_id = topic["id"] if topic else None
                await execute(
                    """
                    INSERT INTO notebooks (topic_id, account_id, remote_notebook_id, title, is_master, source_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, remote_notebook_id) DO UPDATE SET
                        title = excluded.title,
                        source_count = excluded.source_count,
                        topic_id = COALESCE(notebooks.topic_id, excluded.topic_id),
                        synced_at = datetime('now', 'localtime')
                    """,
                    (
                        topic_id,
                        acct["id"],
                        nb["id"],
                        nb["title"],
                        1 if nb.get("is_owner", True) else 0,
                        int(nb.get("sources_count") or 0),
                    ),
                )
                synced += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"alias": acct["alias"], "error": str(exc)})
    return {"synced_notebooks": synced, "errors": errors}
