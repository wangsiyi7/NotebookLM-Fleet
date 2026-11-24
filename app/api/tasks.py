"""Task queue and batch Studio/Chat scheduling routes."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..adapters import nlm_py_adapter
from ..core.scheduler import run_scheduler_tick
from ..db import execute, fetch_all, fetch_one

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    topic_id: int | None = None
    notebook_id: int | None = None
    task_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    preferred_account_id: int | None = None
    tier_requirement: str = Field(default="any", pattern="^(any|ultra|pro_plus|free_ok)$")
    priority: int = 50


class BatchTaskCreate(BaseModel):
    topic_ids: list[int]
    actions: list[str]
    instructions: str | None = None
    cinematic_instructions: str | None = None
    question: str | None = None
    language: str = "zh_Hans"
    preferred_account_id: int | None = None
    tier_requirement: str = Field(default="any", pattern="^(any|ultra|pro_plus|free_ok)$")
    priority: int = 50


@router.get("")
async def list_tasks(
    status: str | None = Query(default=None),
    topic_id: int | None = Query(default=None),
    account_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if status and status != "all":
        clauses.append("t.status = ?")
        params.append(status)
    if topic_id is not None:
        clauses.append("t.topic_id = ?")
        params.append(topic_id)
    if account_id is not None:
        clauses.append("t.assigned_account_id = ?")
        params.append(account_id)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await fetch_all(
        f"""
        SELECT t.*,
               tp.name AS topic_name,
               nb.title AS notebook_title,
               pa.alias AS preferred_alias,
               aa.alias AS assigned_alias
          FROM tasks t
          LEFT JOIN topics tp ON tp.id = t.topic_id
          LEFT JOIN notebooks nb ON nb.id = t.notebook_id
          LEFT JOIN accounts pa ON pa.id = t.preferred_account_id
          LEFT JOIN accounts aa ON aa.id = t.assigned_account_id
         {where_sql}
         ORDER BY t.id DESC
         LIMIT ?
        """,
        (*params, limit),
    )
    return {"tasks": rows}


@router.post("")
async def create_task(body: TaskCreate) -> dict[str, Any]:
    tid = await execute(
        """
        INSERT INTO tasks (
            topic_id, notebook_id, task_type, payload_json,
            preferred_account_id, tier_requirement, priority, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            body.topic_id,
            body.notebook_id,
            body.task_type,
            json.dumps(body.payload, ensure_ascii=False),
            body.preferred_account_id,
            body.tier_requirement,
            body.priority,
        ),
    )
    row = await fetch_one("SELECT * FROM tasks WHERE id = ?", (tid,))
    return {"task": row}


@router.post("/batch")
async def create_batch_tasks(body: BatchTaskCreate) -> dict[str, Any]:
    if not body.topic_ids or not body.actions:
        raise HTTPException(status_code=400, detail="请至少选择一个主题和一个动作类型")

    try:
        lang = nlm_py_adapter.normalize_studio_language(body.language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch_id = f"B-{uuid.uuid4().hex[:8]}"
    created_ids: list[int] = []

    general_inst = (body.instructions or "").strip() or None
    cinematic_inst = (body.cinematic_instructions or "").strip() or general_inst

    for topic_id in body.topic_ids:
        for action in body.actions:
            act_lang, _ = nlm_py_adapter.resolve_studio_language_for_kind(action, lang)
            payload: dict[str, Any] = {
                "language": act_lang,
            }
            act_inst = cinematic_inst if action == "cinematic_video" else general_inst
            if act_inst:
                payload["instructions"] = act_inst
            if action == "chat":
                payload["question"] = body.question or act_inst or "请基于全部资料总结核心脉络、关键结论与行动清单。"
                payload["save_as_note"] = True

            tid = await execute(
                """
                INSERT INTO tasks (
                    batch_id, topic_id, task_type, payload_json,
                    preferred_account_id, tier_requirement, priority, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    batch_id,
                    topic_id,
                    action,
                    json.dumps(payload, ensure_ascii=False),
                    body.preferred_account_id,
                    body.tier_requirement,
                    body.priority,
                ),
            )
            created_ids.append(tid)

    return {"batch_id": batch_id, "created_count": len(created_ids), "task_ids": created_ids}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict[str, Any]:
    task = await fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] in ("done", "canceled"):
        return {"task": task}
    await execute(
        "UPDATE tasks SET status = 'canceled', finished_at = datetime('now', 'localtime') WHERE id = ?",
        (task_id,),
    )
    row = await fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return {"task": row}


@router.post("/{task_id}/retry")
async def retry_task(task_id: int) -> dict[str, Any]:
    task = await fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    payload_str = task.get("payload_json") or "{}"
    try:
        payload = json.loads(payload_str)
        if isinstance(payload, dict):
            raw_lang = str(payload.get("language") or "zh_Hans")
            resolved_lang, _ = nlm_py_adapter.resolve_studio_language_for_kind(task["task_type"], raw_lang)
            payload["language"] = resolved_lang
            payload_str = json.dumps(payload, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        pass
    await execute(
        """
        UPDATE tasks
           SET status = 'pending',
               payload_json = ?,
               remote_task_id = NULL,
               result_artifact_path = NULL,
               result_text = NULL,
               scheduled_after = NULL,
               error_msg = NULL,
               started_at = NULL,
               finished_at = NULL
         WHERE id = ?
        """,
        (payload_str, task_id),
    )
    row = await fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return {"task": row}


@router.post("/tick")
async def trigger_tick() -> dict[str, Any]:
    dispatched = await run_scheduler_tick()
    return {"dispatched": dispatched}
