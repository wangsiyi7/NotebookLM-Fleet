"""Unified Topics, cross-account notebooks, Studio analytics, sharing, and ingest routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..adapters import nlm_py_adapter, official_api
from ..adapters.router import CapabilityRouter
from ..core.coupling import ensure_topic_notebook_for_account
from ..core.drive_hub import ensure_topic_folders, sanitize_topic_name, save_text_export
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


class ScanStudioRequest(BaseModel):
    account_id: int | None = None
    notebook_ids: list[int] | None = None
    limit: int = Field(default=30, ge=1, le=200)
    only_unscanned: bool = True


class NotebookChatRequest(BaseModel):
    question: str
    source_ids: list[str] | None = None
    save_as_note: bool = False
    note_title: str | None = None
    save_markdown_export: bool = True


class NotebookGenerateRequest(BaseModel):
    actions: list[str]
    instructions: str | None = None
    cinematic_instructions: str | None = None
    language: str | None = None


class NotebookIngestRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    inline_title: str | None = None
    inline_text: str | None = None


class ArtifactDownloadRequest(BaseModel):
    kind: str | None = None
    title: str | None = None


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def _enrich_notebook_row(row: dict[str, Any]) -> dict[str, Any]:
    stats = _safe_json_loads(row.get("studio_stats_json"), {})
    if not isinstance(stats, dict) or not stats:
        stats = {
            "total": int(row.get("artifact_count") or 0),
            "completed": int(row.get("artifact_count") or 0),
            "in_progress": 0,
            "failed": 0,
            "notes_count": 0,
            "by_kind": [],
            "synced_at": row.get("studio_synced_at"),
        }
    remote_id = str(row.get("remote_notebook_id") or "")
    nb = dict(row)
    nb.pop("remote_artifacts_json", None)
    nb.pop("remote_sources_json", None)
    nb.pop("studio_stats_json", None)
    nb["studio_stats"] = stats
    nb["studio_scanned"] = bool(row.get("studio_synced_at"))
    nb["web_url"] = f"https://notebook.google.com/notebook/{remote_id}" if remote_id else None
    nb["detail_url"] = f"/notebooks/{row['id']}"
    return nb


def _aggregate_global_studio(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    total_arts = 0
    scanned_nbs = 0
    for row in rows:
        if row.get("studio_synced_at"):
            scanned_nbs += 1
        stats = _safe_json_loads(row.get("studio_stats_json"), {})
        if not isinstance(stats, dict):
            continue
        for item in stats.get("by_kind") or []:
            kind = str(item.get("kind") or "unknown")
            cnt = int(item.get("count") or 0)
            if cnt <= 0:
                continue
            counts[kind] = counts.get(kind, 0) + cnt
            total_arts += cnt

    by_kind = []
    for kind, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        meta = nlm_py_adapter.STUDIO_KIND_META.get(kind, nlm_py_adapter.STUDIO_KIND_META["unknown"])
        pct = round((cnt / total_arts) * 100.0, 1) if total_arts > 0 else 0.0
        by_kind.append(
            {
                "kind": kind,
                "label": meta["label"],
                "icon": meta["icon"],
                "color": meta["color"],
                "count": cnt,
                "pct": pct,
            }
        )
    return {
        "total_artifacts": total_arts,
        "scanned_notebooks": scanned_nbs,
        "total_notebooks": len(rows),
        "by_kind": by_kind,
    }


async def _persist_studio_scan(
    account_id: int, scanned_map: dict[str, dict[str, Any]]
) -> int:
    saved = 0
    for remote_nb_id, data in scanned_map.items():
        stats = data.get("studio_stats") or {}
        arts = data.get("artifacts") or []
        await execute(
            """
            UPDATE notebooks
               SET artifact_count = ?,
                   studio_stats_json = ?,
                   remote_artifacts_json = ?,
                   studio_synced_at = datetime('now', 'localtime'),
                   synced_at = datetime('now', 'localtime')
             WHERE account_id = ? AND remote_notebook_id = ?
            """,
            (
                int(stats.get("total") or len(arts)),
                json.dumps(stats, ensure_ascii=False),
                json.dumps(arts, ensure_ascii=False),
                account_id,
                remote_nb_id,
            ),
        )
        saved += 1
    return saved


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
            SELECT nb.id, nb.topic_id, nb.account_id, nb.remote_notebook_id, nb.title,
                   nb.is_master, nb.role, nb.source_count, nb.artifact_count,
                   nb.shared_emails_json, nb.studio_stats_json, nb.studio_synced_at, nb.synced_at,
                   acc.alias AS account_alias, acc.email AS account_email
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
        topic["notebooks"] = [_enrich_notebook_row(nb) for nb in nbs]
        topic["source_count"] = max((int(nb.get("source_count") or 0) for nb in nbs), default=0)
        topic["artifact_count"] = len(arts)
        topic["remote_artifact_count"] = max((int(nb.get("artifact_count") or 0) for nb in nbs), default=0)
        topic["artifacts"] = arts
        shared: set[str] = set()
        for nb in nbs:
            shared.update(_safe_json_loads(nb.get("shared_emails_json"), []))
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
    existing_shared = list(_safe_json_loads(master_nb.get("shared_emails_json"), []))

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
async def sync_all_notebooks(scan_studio_limit: int = Query(default=12, ge=0, le=100)) -> dict[str, Any]:
    accounts = await fetch_all(
        "SELECT * FROM accounts WHERE enabled = 1 AND (nlm_auth_status = 'ok' OR official_oauth_status = 'ok')"
    )
    settings = await get_settings_map()
    cap_router = CapabilityRouter(settings)
    synced = 0
    studio_scanned = 0
    errors = []

    for acct in accounts:
        ch2_ready = acct.get("nlm_auth_status") == "ok"
        ch1_ready = cap_router.can_use_official(acct)
        if not ch2_ready and not ch1_ready:
            continue
        try:
            nbs: list[dict[str, Any]] = []
            used_ch2 = False
            if ch2_ready:
                try:
                    nbs = await nlm_py_adapter.list_notebooks(acct["nlm_profile"])
                    used_ch2 = True
                except Exception as ch2_exc:  # noqa: BLE001
                    if not ch1_ready:
                        raise
                    errors.append({"alias": acct["alias"], "error": f"Ch2 列本回退至官方 API: {ch2_exc}"})

            if not used_ch2 and ch1_ready:
                pid = (settings.get("official_api_project_id") or "").strip()
                nbs = await official_api.list_notebooks(
                    acct["alias"],
                    pid,
                    page_size=100,
                    fetch_all=True,
                    **cap_router._proxy_kwargs(),
                )

            for nb in nbs:
                topic = await fetch_one("SELECT id FROM topics WHERE name = ?", (nb["title"],))
                topic_id = topic["id"] if topic else None
                role_val = str(nb.get("role") or ("owner" if nb.get("is_owner", True) else "shared"))
                src_cnt = int(nb.get("sources_count") or 0)
                await execute(
                    """
                    INSERT INTO notebooks (
                        topic_id, account_id, remote_notebook_id, title, is_master, role, source_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, remote_notebook_id) DO UPDATE SET
                        title = excluded.title,
                        is_master = excluded.is_master,
                        role = excluded.role,
                        source_count = CASE
                            WHEN excluded.source_count > 0 OR notebooks.source_count = 0
                            THEN excluded.source_count
                            ELSE notebooks.source_count
                        END,
                        topic_id = COALESCE(notebooks.topic_id, excluded.topic_id),
                        synced_at = datetime('now', 'localtime')
                    """,
                    (
                        topic_id,
                        acct["id"],
                        nb["id"],
                        nb["title"],
                        1 if nb.get("is_owner", True) else 0,
                        role_val,
                        src_cnt,
                    ),
                )
                synced += 1

            if used_ch2 and scan_studio_limit > 0 and nbs:
                scanned_rows = await fetch_all(
                    "SELECT remote_notebook_id FROM notebooks WHERE account_id = ? AND studio_synced_at IS NOT NULL",
                    (acct["id"],),
                )
                already_scanned = {r["remote_notebook_id"] for r in scanned_rows}
                remote_order = [nb["id"] for nb in nbs]
                targets = [nid for nid in remote_order if nid not in already_scanned][:scan_studio_limit]
                if not targets:
                    targets = remote_order[: min(scan_studio_limit, len(remote_order))]
                if targets:
                    scanned_map = await nlm_py_adapter.batch_scan_studio_stats(acct["nlm_profile"], targets)
                    studio_scanned += await _persist_studio_scan(acct["id"], scanned_map)
        except Exception as exc:  # noqa: BLE001
            errors.append({"alias": acct["alias"], "error": str(exc)})

    cnt_row = await fetch_one("SELECT COUNT(*) AS cnt FROM notebooks")
    total_in_db = int((cnt_row or {}).get("cnt") or 0)
    return {
        "synced_notebooks": synced,
        "studio_scanned": studio_scanned,
        "total_in_db": total_in_db,
        "errors": errors,
    }


@router.get("/notebooks")
async def list_synced_notebooks(
    q: str = Query(default=""),
    account_id: int | None = Query(default=None),
    only_with_studio: bool = Query(default=False),
    only_owner: bool = Query(default=False),
    studio_kind: str = Query(default=""),
    sort: str = Query(default="recent"),
    limit: int = Query(default=60, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List synced remote notebooks with Studio breakdown stats, filtering, sorting, and pagination."""
    clauses: list[str] = []
    params: list[Any] = []
    q_clean = q.strip()
    if q_clean:
        clauses.append("(nb.title LIKE ? OR nb.remote_notebook_id LIKE ?)")
        like = f"%{q_clean}%"
        params.extend([like, like])
    if account_id is not None:
        clauses.append("nb.account_id = ?")
        params.append(account_id)
    if only_with_studio:
        clauses.append("nb.artifact_count > 0")
    if only_owner:
        clauses.append("nb.is_master = 1")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_map = {
        "artifacts": "nb.artifact_count DESC, (nb.studio_synced_at IS NOT NULL) DESC, nb.source_count DESC, nb.id ASC",
        "sources": "nb.source_count DESC, nb.artifact_count DESC, nb.id ASC",
        "title": "nb.title COLLATE NOCASE ASC, nb.id ASC",
        "recent": "(nb.studio_synced_at IS NOT NULL) DESC, nb.synced_at DESC, nb.id ASC",
    }
    order_sql = order_map.get(sort, order_map["recent"])

    clean_kind = studio_kind.strip()
    select_sql = f"""
        SELECT nb.id, nb.topic_id, nb.account_id, nb.remote_notebook_id, nb.title,
               nb.is_master, nb.role, nb.source_count, nb.artifact_count,
               nb.shared_emails_json, nb.studio_stats_json, nb.studio_synced_at, nb.synced_at,
               acc.alias AS account_alias, acc.email AS account_email, acc.tier AS account_tier,
               acc.nlm_auth_status, acc.official_oauth_status,
               t.name AS topic_name
          FROM notebooks nb
          JOIN accounts acc ON acc.id = nb.account_id
          LEFT JOIN topics t ON t.id = nb.topic_id
          {where_sql}
         ORDER BY {order_sql}
    """

    if clean_kind:
        all_rows = await fetch_all(select_sql, tuple(params))
        enriched_all = [_enrich_notebook_row(r) for r in all_rows]
        matched = [
            nb
            for nb in enriched_all
            if any(
                k.get("kind") == clean_kind and int(k.get("count") or 0) > 0
                for k in (nb.get("studio_stats") or {}).get("by_kind") or []
            )
        ]
        total = len(matched)
        notebooks_out = matched[offset : offset + limit]
    else:
        total_row = await fetch_one(f"SELECT COUNT(*) AS cnt FROM notebooks nb {where_sql}", tuple(params))
        total = int((total_row or {}).get("cnt") or 0)
        rows = await fetch_all(f"{select_sql} LIMIT ? OFFSET ?", (*params, limit, offset))
        notebooks_out = [_enrich_notebook_row(r) for r in rows]

    all_stats_rows = await fetch_all(
        "SELECT studio_stats_json, artifact_count, studio_synced_at FROM notebooks"
    )
    global_summary = _aggregate_global_studio(all_stats_rows)
    return {
        "notebooks": notebooks_out,
        "total": total,
        "limit": limit,
        "offset": offset,
        "studio_kind": clean_kind or None,
        "scanned_notebooks_count": global_summary["scanned_notebooks"],
        "global_studio_summary": global_summary,
    }


@router.post("/notebooks/scan-studio")
async def scan_notebooks_studio(body: ScanStudioRequest | None = None) -> dict[str, Any]:
    """Batch scan remote Studio artifact compositions for synced notebooks."""
    req = body or ScanStudioRequest()
    clauses: list[str] = ["acc.nlm_auth_status = 'ok'"]
    params: list[Any] = []
    if req.account_id is not None:
        clauses.append("nb.account_id = ?")
        params.append(req.account_id)
    if req.notebook_ids:
        placeholders = ",".join("?" for _ in req.notebook_ids)
        clauses.append(f"nb.id IN ({placeholders})")
        params.extend(req.notebook_ids)
    elif req.only_unscanned:
        clauses.append("nb.studio_synced_at IS NULL")

    where_sql = " AND ".join(clauses)
    targets = await fetch_all(
        f"""
        SELECT nb.id, nb.account_id, nb.remote_notebook_id, acc.nlm_profile, acc.alias
          FROM notebooks nb
          JOIN accounts acc ON acc.id = nb.account_id
         WHERE {where_sql}
         ORDER BY nb.source_count DESC, nb.id ASC
         LIMIT ?
        """,
        (*params, req.limit),
    )

    by_acct: dict[int, list[dict[str, Any]]] = {}
    for row in targets:
        by_acct.setdefault(int(row["account_id"]), []).append(row)

    scanned = 0
    errors = []
    for acct_id, rows in by_acct.items():
        prof = rows[0]["nlm_profile"]
        alias = rows[0]["alias"]
        nb_ids = [r["remote_notebook_id"] for r in rows]
        try:
            scanned_map = await nlm_py_adapter.batch_scan_studio_stats(prof, nb_ids)
            scanned += await _persist_studio_scan(acct_id, scanned_map)
        except Exception as exc:  # noqa: BLE001
            errors.append({"alias": alias, "error": str(exc)})

    rem_row = await fetch_one("SELECT COUNT(*) AS cnt FROM notebooks WHERE studio_synced_at IS NULL")
    return {
        "scanned": scanned,
        "remaining_unscanned": int((rem_row or {}).get("cnt") or 0),
        "errors": errors,
    }


async def _refresh_single_notebook_live(
    nb_row: dict[str, Any], settings: dict[str, str]
) -> dict[str, Any]:
    cap_router = CapabilityRouter(settings)
    remote_id = nb_row["remote_notebook_id"]
    ch2_ok = nb_row.get("nlm_auth_status") == "ok"
    ch1_ok = cap_router.can_use_official(
        {"alias": nb_row["account_alias"], "official_oauth_status": nb_row.get("official_oauth_status")}
    )

    sources: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    studio_stats: dict[str, Any] = _safe_json_loads(nb_row.get("studio_stats_json"), {})
    refresh_error: str | None = None

    if ch2_ok:
        try:
            detail = await nlm_py_adapter.list_notebook_studio_detail(nb_row["nlm_profile"], remote_id)
            sources = detail["sources"]
            artifacts = detail["artifacts"]
            notes = detail["notes"]
            studio_stats = detail["studio_stats"]
            await execute(
                """
                UPDATE notebooks
                   SET source_count = ?,
                       artifact_count = ?,
                       studio_stats_json = ?,
                       remote_artifacts_json = ?,
                       remote_sources_json = ?,
                       studio_synced_at = datetime('now', 'localtime'),
                       synced_at = datetime('now', 'localtime')
                 WHERE id = ?
                """,
                (
                    len(sources),
                    int(studio_stats.get("total") or len(artifacts)),
                    json.dumps(studio_stats, ensure_ascii=False),
                    json.dumps(artifacts, ensure_ascii=False),
                    json.dumps(sources, ensure_ascii=False),
                    nb_row["id"],
                ),
            )
            return {
                "sources": sources,
                "artifacts": artifacts,
                "notes": notes,
                "studio_stats": studio_stats,
                "refresh_error": None,
            }
        except Exception as exc:  # noqa: BLE001
            refresh_error = str(exc)

    if ch1_ok:
        try:
            pid = (settings.get("official_api_project_id") or "").strip()
            off_srcs = await official_api.list_sources(
                nb_row["account_alias"], pid, remote_id, **cap_router._proxy_kwargs()
            )
            sources = [
                {
                    "id": s.get("name", ""),
                    "title": s.get("displayName") or s.get("name") or "",
                    "kind": str(s.get("mimeType") or "official_source"),
                    "status": str(s.get("state") or "ACTIVE").lower(),
                    "url": None,
                    "created_at": s.get("createTime"),
                }
                for s in off_srcs
            ]
            await execute(
                """
                UPDATE notebooks
                   SET source_count = ?,
                       remote_sources_json = ?,
                       synced_at = datetime('now', 'localtime')
                 WHERE id = ?
                """,
                (len(sources), json.dumps(sources, ensure_ascii=False), nb_row["id"]),
            )
            refresh_error = None
        except Exception as exc:  # noqa: BLE001
            refresh_error = refresh_error or str(exc)

    return {
        "sources": sources or _safe_json_loads(nb_row.get("remote_sources_json"), []),
        "artifacts": artifacts or _safe_json_loads(nb_row.get("remote_artifacts_json"), []),
        "notes": notes,
        "studio_stats": studio_stats or nlm_py_adapter.compute_studio_breakdown([]),
        "refresh_error": refresh_error,
    }


async def _load_notebook_context(notebook_id: int) -> dict[str, Any]:
    row = await fetch_one(
        """
        SELECT nb.*,
               acc.alias AS account_alias, acc.email AS account_email, acc.tier AS account_tier,
               acc.nlm_profile, acc.nlm_auth_status, acc.official_oauth_status,
               t.name AS topic_name, t.drive_folder_path
          FROM notebooks nb
          JOIN accounts acc ON acc.id = nb.account_id
          LEFT JOIN topics t ON t.id = nb.topic_id
         WHERE nb.id = ?
        """,
        (notebook_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="笔记本不存在")
    return row


async def _build_notebook_detail_response(
    nb_row: dict[str, Any], *, force_refresh: bool = False
) -> dict[str, Any]:
    settings = await get_settings_map()
    cached_sources = _safe_json_loads(nb_row.get("remote_sources_json"), [])
    cached_artifacts = _safe_json_loads(nb_row.get("remote_artifacts_json"), [])
    needs_auto_refresh = (
        force_refresh
        or nb_row.get("studio_synced_at") is None
        or (int(nb_row.get("source_count") or 0) > 0 and not cached_sources)
    )
    notes: list[dict[str, Any]] = []
    refresh_error: str | None = None

    if needs_auto_refresh and (
        nb_row.get("nlm_auth_status") == "ok" or nb_row.get("official_oauth_status") == "ok"
    ):
        live = await _refresh_single_notebook_live(nb_row, settings)
        sources = live["sources"]
        artifacts = live["artifacts"]
        notes = live["notes"]
        studio_stats = live["studio_stats"]
        refresh_error = live["refresh_error"]
        nb_row = await _load_notebook_context(int(nb_row["id"]))
    else:
        sources = cached_sources
        artifacts = cached_artifacts
        studio_stats = _safe_json_loads(nb_row.get("studio_stats_json"), {})
        if not isinstance(studio_stats, dict) or not studio_stats:
            studio_stats = nlm_py_adapter.compute_studio_breakdown(artifacts)

    topic_id = nb_row.get("topic_id")
    local_artifacts = await fetch_all(
        """
        SELECT * FROM artifacts
         WHERE notebook_id = ? OR (? IS NOT NULL AND topic_id = ?)
         ORDER BY id DESC LIMIT 30
        """,
        (nb_row["id"], topic_id, topic_id),
    )
    recent_tasks = await fetch_all(
        """
        SELECT t.*, acc.alias AS assigned_alias
          FROM tasks t
          LEFT JOIN accounts acc ON acc.id = t.assigned_account_id
         WHERE t.notebook_id = ?
         ORDER BY t.id DESC LIMIT 30
        """,
        (nb_row["id"],),
    )
    return {
        "notebook": _enrich_notebook_row(nb_row),
        "studio_stats": studio_stats,
        "sources": sources,
        "artifacts": artifacts,
        "notes": notes,
        "local_artifacts": local_artifacts,
        "recent_tasks": recent_tasks,
        "refresh_error": refresh_error,
    }


@router.get("/notebooks/{notebook_id}")
async def get_notebook_detail(notebook_id: int, refresh: bool = Query(default=False)) -> dict[str, Any]:
    """Return full detail payload for a single notebook sub-page (sources, Studio breakdown, tasks)."""
    nb_row = await _load_notebook_context(notebook_id)
    return await _build_notebook_detail_response(nb_row, force_refresh=refresh)


@router.post("/notebooks/{notebook_id}/refresh")
async def refresh_notebook_detail(notebook_id: int) -> dict[str, Any]:
    """Force live refresh of remote sources and Studio artifacts breakdown for one notebook."""
    nb_row = await _load_notebook_context(notebook_id)
    return await _build_notebook_detail_response(nb_row, force_refresh=True)


@router.post("/notebooks/{notebook_id}/chat")
async def chat_with_notebook(notebook_id: int, body: NotebookChatRequest) -> dict[str, Any]:
    """Run grounded Q&A directly against a synced notebook and optionally archive as Markdown."""
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题内容不能为空")
    nb_row = await _load_notebook_context(notebook_id)
    if nb_row.get("nlm_auth_status") != "ok":
        raise HTTPException(
            status_code=400,
            detail=f"所属账号 {nb_row['account_alias']} 尚未完成 Ch2 会话登录，请先在首页一键登录 Chrome/Edge。",
        )
    try:
        res = await nlm_py_adapter.ask_question(
            nb_row["nlm_profile"],
            nb_row["remote_notebook_id"],
            question,
            source_ids=body.source_ids,
            save_as_note=body.save_as_note,
            note_title=body.note_title,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"对话请求失败: {exc}") from exc

    saved_md_path: str | None = None
    settings = await get_settings_map()
    hub_root = (settings.get("drive_hub_root") or "").strip()
    if body.save_markdown_export and hub_root and res.get("answer"):
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            content = (
                f"# {nb_row['title']} · Grounded Chat\n\n"
                f"**提问**：{question}\n\n"
                f"**引用来源数**：{res.get('references_count', 0)}\n\n---\n\n{res['answer']}\n"
            )
            p = await asyncio.to_thread(
                save_text_export,
                hub_root,
                nb_row.get("topic_name"),
                f"nb{notebook_id}_chat_{stamp}.md",
                content,
            )
            saved_md_path = str(p)
        except Exception:  # noqa: BLE001
            saved_md_path = None

    return {**res, "saved_md_path": saved_md_path}


@router.post("/notebooks/{notebook_id}/generate")
async def generate_for_notebook(notebook_id: int, body: NotebookGenerateRequest) -> dict[str, Any]:
    """Queue Studio artifact generation tasks directly targeting one synced notebook."""
    actions = [a.strip() for a in body.actions if a and a.strip()]
    if not actions:
        raise HTTPException(status_code=400, detail="请至少选择一种 Studio 生成类型")
    nb_row = await _load_notebook_context(notebook_id)
    settings = await get_settings_map()
    try:
        lang = nlm_py_adapter.normalize_studio_language(
            body.language or settings.get("default_language") or "zh_Hans"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    batch_id = f"NB-{notebook_id}-{uuid.uuid4().hex[:6]}"
    task_ids: list[int] = []

    general_inst = (body.instructions or "").strip() or None
    cinematic_inst = (body.cinematic_instructions or "").strip() or general_inst

    for act in actions:
        act_lang, _ = nlm_py_adapter.resolve_studio_language_for_kind(act, lang)
        payload: dict[str, Any] = {
            "remote_notebook_id": nb_row["remote_notebook_id"],
            "language": act_lang,
            "strict_account": True,
        }
        act_inst = cinematic_inst if act == "cinematic_video" else general_inst
        if act_inst:
            payload["instructions"] = act_inst
        if act == "chat":
            payload["question"] = act_inst or "请系统梳理本笔记本全部来源的核心框架与可执行建议。"
            payload["save_as_note"] = True

        tid = await execute(
            """
            INSERT INTO tasks (
                batch_id, topic_id, notebook_id, task_type, payload_json,
                preferred_account_id, tier_requirement, priority, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'any', 35, 'pending')
            """,
            (
                batch_id,
                nb_row.get("topic_id"),
                notebook_id,
                act,
                json.dumps(payload, ensure_ascii=False),
                nb_row["account_id"],
            ),
        )
        task_ids.append(tid)
    return {"batch_id": batch_id, "created_count": len(task_ids), "task_ids": task_ids}


@router.post("/notebooks/{notebook_id}/ingest")
async def ingest_into_notebook(notebook_id: int, body: NotebookIngestRequest) -> dict[str, Any]:
    """Directly ingest URLs, inline Markdown/text, or whitelisted local files into one notebook."""
    nb_row = await _load_notebook_context(notebook_id)
    urls = [u.strip() for u in body.urls if u and u.strip()]
    inline_text = (body.inline_text or "").strip()
    if not urls and not body.file_paths and not inline_text:
        raise HTTPException(status_code=400, detail="请提供至少一条网址、文本笔记或工作区文件路径")

    roots = await get_allowed_roots()
    validated_files: list[str] = []
    for raw_fp in body.file_paths:
        p = raw_fp.strip()
        if not p:
            continue
        try:
            resolved = is_allowed_file_for_ingest(p, roots)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        validated_files.append(str(resolved))

    settings = await get_settings_map()
    cap_router = CapabilityRouter(settings)
    acct = {
        "id": nb_row["account_id"],
        "alias": nb_row["account_alias"],
        "nlm_profile": nb_row["nlm_profile"],
        "nlm_auth_status": nb_row["nlm_auth_status"],
        "official_oauth_status": nb_row["official_oauth_status"],
    }
    remote_id = nb_row["remote_notebook_id"]
    results: list[dict[str, Any]] = []

    for u in urls:
        try:
            res, ch = await cap_router.ingest_url(acct, remote_id, u)
            results.append({"item": u, "kind": "url", "channel": ch, "ok": True, "res": res})
        except Exception as exc:  # noqa: BLE001
            results.append({"item": u, "kind": "url", "ok": False, "error": str(exc)})

    for fp in validated_files:
        try:
            res, ch = await cap_router.ingest_file(acct, remote_id, fp)
            results.append({"item": fp, "kind": "file", "channel": ch, "ok": True, "res": res})
        except Exception as exc:  # noqa: BLE001
            results.append({"item": fp, "kind": "file", "ok": False, "error": str(exc)})

    if inline_text:
        title = (body.inline_title or f"笔记_{datetime.now().strftime('%m%d_%H%M')}").strip()
        try:
            res, ch = await cap_router.ingest_text(acct, remote_id, title, inline_text)
            results.append({"item": title, "kind": "text", "channel": ch, "ok": True, "res": res})
        except Exception as exc:  # noqa: BLE001
            results.append({"item": title, "kind": "text", "ok": False, "error": str(exc)})

    await _refresh_single_notebook_live(nb_row, settings)
    ok_cnt = sum(1 for r in results if r.get("ok"))
    return {"notebook_id": notebook_id, "success_count": ok_cnt, "results": results}


@router.post("/notebooks/{notebook_id}/artifacts/{artifact_id}/download")
async def download_notebook_artifact(
    notebook_id: int, artifact_id: str, body: ArtifactDownloadRequest | None = None
) -> dict[str, Any]:
    """Download a completed remote Studio artifact into DriveHub and record it locally."""
    nb_row = await _load_notebook_context(notebook_id)
    if nb_row.get("nlm_auth_status") != "ok":
        raise HTTPException(
            status_code=400,
            detail=f"所属账号 {nb_row['account_alias']} 尚未完成 Ch2 登录，无法下载远程产物。",
        )

    cached_arts = _safe_json_loads(nb_row.get("remote_artifacts_json"), [])
    matched = next((a for a in cached_arts if str(a.get("id")) == artifact_id), None)
    kind = (body.kind if body and body.kind else None) or (matched.get("kind") if matched else None)
    title = (body.title if body and body.title else None) or (matched.get("title") if matched else artifact_id)
    if not kind:
        raise HTTPException(status_code=400, detail="请提供产物类型 kind，或先刷新该笔记本详情。")

    settings = await get_settings_map()
    hub_root = settings.get("drive_hub_root") or "./data/NLM_Hub"
    safe_nb = sanitize_topic_name(nb_row["title"])
    out_dir = f"{hub_root}/03_Exports/{safe_nb}"

    try:
        dl = await nlm_py_adapter.download_remote_artifact(
            nb_row["nlm_profile"],
            nb_row["remote_notebook_id"],
            artifact_id,
            kind,
            out_dir,
            title=str(title),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"下载远程产物失败: {exc}") from exc

    await execute(
        """
        INSERT INTO artifacts (
            topic_id, account_id, notebook_id, remote_artifact_id, kind, title, local_drive_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            nb_row.get("topic_id"),
            nb_row["account_id"],
            notebook_id,
            artifact_id,
            kind,
            dl["title"],
            dl["local_drive_path"],
        ),
    )
    return dl
