"""Official Nov 2026 Gemini Notebook REST API & Remote MCP verification and quick-add routes."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..adapters import nlm_py_adapter, official_api, official_mcp_client
from ..db import execute, fetch_all, fetch_one, get_settings_map, set_settings_map
from .workspace import TEXT_EXTENSIONS, get_allowed_roots, is_allowed_file_for_ingest

router = APIRouter(prefix="/api/official", tags=["official"])


class OfficialMcpCallRequest(BaseModel):
    account_id: int
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = None


class OfficialVerifyRequest(BaseModel):
    account_id: int
    project_id: str | None = None
    create_test_notebook: bool = False
    sample_url: str | None = None
    sample_text: str | None = None


class QuickAddRequest(BaseModel):
    account_id: int
    notebook_id: str | None = None
    new_notebook_title: str | None = None
    channel: str = Field(default="auto", pattern="^(auto|official_mcp|official_rest|notebooklm_py)$")
    file_paths: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    inline_title: str | None = None
    inline_text: str | None = None
    mime_type: str = Field(default="text/markdown")


def _local_mcp_status() -> dict[str, Any]:
    cfg_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    registered = False
    server_keys: list[str] = []
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or {}
            server_keys = list(servers.keys())
            registered = "notebooklm-fleet" in servers
        except (OSError, ValueError):
            pass
    return {
        "config_path": str(cfg_path),
        "registered": registered,
        "servers": server_keys,
        "tools": [
            "fleet_quota_overview",
            "fleet_poll_quotas",
            "fleet_list_accounts",
            "fleet_add_account",
            "fleet_list_topics",
            "fleet_create_topic_and_ingest",
            "fleet_enqueue_batch",
            "fleet_task_status",
            "fleet_control_task",
            "fleet_open_account_browser",
        ],
    }


def _proxy_kwargs(settings: dict[str, str]) -> dict[str, Any]:
    proxy = (settings.get("official_proxy_url") or "").strip()
    return {"proxy": proxy} if proxy else {}


async def _save_last_mcp_probe(mcp_res: dict[str, Any]) -> None:
    if not mcp_res.get("reachable"):
        return
    snapshot = {
        "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mcp_url": mcp_res.get("mcp_url"),
        "protocol_version": mcp_res.get("protocol_version"),
        "server_info": mcp_res.get("server_info") or {},
        "tool_count": mcp_res.get("tool_count", 0),
        "tools": mcp_res.get("tools") or [],
    }
    await set_settings_map({"official_mcp_last_probe_json": json.dumps(snapshot, ensure_ascii=False)})


@router.get("/status")
async def get_official_status() -> dict[str, Any]:
    settings = await get_settings_map()
    accounts = await fetch_all(
        "SELECT id, alias, email, tier, nlm_auth_status, official_oauth_status, enabled FROM accounts ORDER BY id"
    )
    for acct in accounts:
        acct["official_oauth_status"] = official_api.check_oauth_status(acct["alias"])
        acct["oauth_last_error"] = official_api.get_last_oauth_error(acct["alias"])
    last_probe: dict[str, Any] | None = None
    raw_snap = (settings.get("official_mcp_last_probe_json") or "").strip()
    if raw_snap:
        try:
            parsed = json.loads(raw_snap)
            if isinstance(parsed, dict):
                last_probe = parsed
        except ValueError:
            last_probe = None
    return {
        "client_secret_present": official_api.has_client_secret(),
        "enabled": settings.get("official_api_enabled", "false") == "true",
        "project_id": settings.get("official_api_project_id", ""),
        "proxy_url": settings.get("official_proxy_url", ""),
        "official_rest_base": official_api.BASE_URL,
        "official_mcp_url": official_mcp_client.OFFICIAL_MCP_URL,
        "accounts": accounts,
        "local_mcp": _local_mcp_status(),
        "last_verified_mcp_probe": last_probe,
    }


@router.post("/upload-client-secret")
async def upload_client_secret(request: Request) -> dict[str, Any]:
    ctype = (request.headers.get("content-type") or "").lower()
    raw_json = ""
    if "multipart/form-data" in ctype:
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            raise HTTPException(status_code=400, detail="请上传 client_secret.json 文件")
        raw_bytes = await up.read()
        raw_json = raw_bytes.decode("utf-8", errors="strict")
    else:
        body = await request.json()
        raw_json = str(body.get("content") or "") if isinstance(body, dict) else ""

    if not raw_json.strip():
        raise HTTPException(status_code=400, detail="client_secret.json 内容不能为空")

    try:
        saved = official_api.save_client_secret_content(raw_json)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = await get_settings_map()
    if saved.get("project_id") and not (settings.get("official_api_project_id") or "").strip():
        await set_settings_map(
            {
                "official_api_project_id": saved["project_id"],
                "official_api_enabled": "true",
            }
        )

    return saved


@router.post("/probe")
async def probe_official_endpoints() -> dict[str, Any]:
    settings = await get_settings_map()
    pkw = _proxy_kwargs(settings)
    mcp_res = await official_mcp_client.probe_official_mcp(**pkw)
    if mcp_res.get("reachable"):
        await _save_last_mcp_probe(mcp_res)
    rest_res = await official_api.probe_rest_reachability(**pkw)
    return {"mcp": mcp_res, "rest": rest_res}


@router.post("/mcp/call")
async def invoke_official_mcp_tool(body: OfficialMcpCallRequest) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (body.account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    settings = await get_settings_map()
    pid = (body.project_id or settings.get("official_api_project_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="请先填写 Google Cloud Project ID")
    if official_api.check_oauth_status(acct["alias"]) != "ok":
        raise HTTPException(
            status_code=400,
            detail=f"账号 {acct['alias']} 尚未完成官方 OAuth 授权，请先上传 client_secret.json 并点「官方 API 授权」。",
        )
    pkw = _proxy_kwargs(settings)
    try:
        return await official_mcp_client.call_official_mcp_tool(
            acct["alias"], pid, body.tool_name, body.arguments, **pkw
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rest/verify")
async def verify_official_stack(body: OfficialVerifyRequest) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (body.account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    settings = await get_settings_map()
    pid = (body.project_id or settings.get("official_api_project_id") or "").strip()
    pkw = _proxy_kwargs(settings)
    steps: list[dict[str, Any]] = []

    # 1. Public MCP handshake + tools/list
    mcp_probe = await official_mcp_client.probe_official_mcp(**pkw)
    if mcp_probe.get("reachable"):
        await _save_last_mcp_probe(mcp_probe)
    steps.append(
        {
            "step": "1. 官方 Remote MCP 握手与工具发现",
            "status": "pass" if mcp_probe.get("reachable") and mcp_probe.get("tool_count") == 7 else "fail",
            "latency_ms": mcp_probe.get("latency_ms"),
            "detail": (
                f"HTTP {mcp_probe.get('status_code')} · 发现 {mcp_probe.get('tool_count', 0)} 个工具: "
                + ", ".join(t["name"] for t in mcp_probe.get("tools") or [])
                if mcp_probe.get("reachable")
                else mcp_probe.get("error", "连接失败")
            ),
        }
    )

    # 2. Public REST reachability
    rest_probe = await official_api.probe_rest_reachability(**pkw)
    steps.append(
        {
            "step": "2. 官方 REST v1 端点可达性",
            "status": "pass" if rest_probe.get("reachable") else "fail",
            "latency_ms": rest_probe.get("latency_ms"),
            "detail": f"HTTP {rest_probe.get('status_code')} · {rest_probe.get('detail')}",
        }
    )

    # 3. Client secret + Project ID + OAuth status
    has_secret = official_api.has_client_secret()
    oauth_st = official_api.check_oauth_status(acct["alias"])
    steps.append(
        {
            "step": "3. Google Cloud OAuth 凭据与 Project ID",
            "status": "pass" if (has_secret and pid) else "warn",
            "latency_ms": None,
            "detail": (
                f"client_secret.json 已就绪 · Project ID: {pid}"
                if (has_secret and pid)
                else f"client_secret.json={'已就绪' if has_secret else '缺失'} · Project ID={pid or '未填写'}"
            ),
        }
    )
    steps.append(
        {
            "step": f"4. 账号 {acct['alias']} OAuth 令牌",
            "status": "pass" if oauth_st == "ok" else "warn",
            "latency_ms": None,
            "detail": (
                "已持有可用 refresh_token"
                if oauth_st == "ok"
                else f"当前状态: {oauth_st}（需在配置好 client_secret.json 后点击一次「官方 API 授权」）"
            ),
        }
    )

    if not (has_secret and pid and oauth_st == "ok"):
        return {"ready": False, "steps": steps}

    # 5. Live authenticated REST list_notebooks
    try:
        nbs = await official_api.list_notebooks(acct["alias"], pid, **pkw)
        steps.append(
            {
                "step": "5. 官方 REST GET /v1/notebooks",
                "status": "pass",
                "latency_ms": None,
                "detail": f"成功拉取 {len(nbs)} 个笔记本",
            }
        )
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "5. 官方 REST GET /v1/notebooks", "status": "fail", "detail": str(exc)})
        return {"ready": False, "steps": steps}

    # 6. Live authenticated Official MCP list_notebooks
    try:
        mcp_nbs = await official_mcp_client.mcp_list_notebooks(acct["alias"], pid, **pkw)
        steps.append(
            {
                "step": "6. 官方 Remote MCP list_notebooks",
                "status": "pass",
                "latency_ms": None,
                "detail": f"Streamable HTTP JSON-RPC 成功返回 {len(mcp_nbs)} 个笔记本",
            }
        )
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "6. 官方 Remote MCP list_notebooks", "status": "fail", "detail": str(exc)})
        return {"ready": False, "steps": steps}

    # 7. Optional end-to-end write smoke test
    if body.create_test_notebook:
        try:
            created = await official_mcp_client.mcp_create_notebook(
                acct["alias"], pid, "NLM-Fleet Official API Verify", **pkw
            )
            nb_name = created["resource_name"]
            txt = body.sample_text or "# NotebookLM-Fleet 自检文档\n\n验证官方 MCP create_notebook + create_source。"
            src = await official_mcp_client.mcp_create_source_text(
                acct["alias"], pid, nb_name, "Self-Check Markdown", txt, mime_type="text/markdown", **pkw
            )
            if body.sample_url:
                await official_api.create_source_url(acct["alias"], pid, nb_name, body.sample_url, "Self-Check URL", **pkw)
            sources = await official_mcp_client.mcp_list_sources(acct["alias"], pid, nb_name, **pkw)
            steps.append(
                {
                    "step": "7. 端到端写入验证 (建本 + 导入来源 + 校验)",
                    "status": "pass",
                    "latency_ms": None,
                    "detail": f"新建 {nb_name}，最新来源 {src.get('name')}，当前来源数={len(sources)}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": "7. 端到端写入验证", "status": "fail", "detail": str(exc)})
            return {"ready": False, "steps": steps}

    await execute(
        "UPDATE accounts SET official_oauth_status = 'ok', updated_at = datetime('now', 'localtime') WHERE id = ?",
        (acct["id"],),
    )
    return {"ready": True, "steps": steps}


def _resolve_channel(req_channel: str, acct: dict[str, Any], settings: dict[str, str]) -> str:
    pid = (settings.get("official_api_project_id") or "").strip()
    oauth_ok = official_api.check_oauth_status(acct["alias"]) == "ok" and bool(pid)
    nlm_ok = nlm_py_adapter.check_local_auth(acct["nlm_profile"]).get("status") == "ok"

    if req_channel == "auto":
        if oauth_ok:
            return "official_rest"
        if nlm_ok:
            return "notebooklm_py"
        raise HTTPException(
            status_code=400,
            detail=f"账号 {acct['alias']} 的官方 OAuth 与 Ch2 会话均未登录，请先完成任意一种登录/授权。",
        )
    if req_channel in ("official_mcp", "official_rest") and not oauth_ok:
        raise HTTPException(
            status_code=400,
            detail=f"使用 {req_channel} 需要先配置 Project ID、client_secret.json 并完成账号 {acct['alias']} 的官方 OAuth 授权。",
        )
    if req_channel == "notebooklm_py" and not nlm_ok:
        raise HTTPException(
            status_code=400,
            detail=f"账号 {acct['alias']} 的 Ch2 会话未登录，请先点击「一键登录会话」。",
        )
    return req_channel


@router.get("/notebooks")
async def list_account_notebooks_live(
    account_id: int = Query(...),
    channel: str = Query(default="auto"),
    filter_expr: str | None = Query(default=None),
) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    settings = await get_settings_map()
    chosen = _resolve_channel(channel, acct, settings)
    pid = (settings.get("official_api_project_id") or "").strip()
    pkw = _proxy_kwargs(settings)

    try:
        if chosen == "official_mcp":
            items = await official_mcp_client.mcp_list_notebooks(acct["alias"], pid, filter_expr=filter_expr, **pkw)
        elif chosen == "official_rest":
            items = await official_api.list_notebooks(acct["alias"], pid, filter_expr=filter_expr, **pkw)
        else:
            items = await nlm_py_adapter.list_notebooks(acct["nlm_profile"])
        return {"channel_used": chosen, "notebooks": items}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources")
async def list_notebook_sources_live(
    account_id: int = Query(...),
    notebook_id: str = Query(...),
    channel: str = Query(default="auto"),
) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    settings = await get_settings_map()
    chosen = _resolve_channel(channel, acct, settings)
    pid = (settings.get("official_api_project_id") or "").strip()
    pkw = _proxy_kwargs(settings)

    try:
        if chosen == "official_mcp":
            sources = await official_mcp_client.mcp_list_sources(acct["alias"], pid, notebook_id, **pkw)
        elif chosen == "official_rest":
            sources = await official_api.list_sources(acct["alias"], pid, notebook_id, **pkw)
        else:
            sources = await nlm_py_adapter.list_sources(acct["nlm_profile"], notebook_id)
        return {"channel_used": chosen, "sources": sources}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/quick-add")
async def quick_add_to_notebook(body: QuickAddRequest) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (body.account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    if not body.file_paths and not body.urls and not (body.inline_text or "").strip():
        raise HTTPException(status_code=400, detail="请至少选择工作区/云盘文件、输入 URL 或输入文本内容")

    roots = await get_allowed_roots()
    validated_files: list[Path] = []
    for raw_fp in body.file_paths:
        try:
            resolved = is_allowed_file_for_ingest(raw_fp, roots)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {raw_fp}")
        validated_files.append(resolved)

    settings = await get_settings_map()
    chosen = _resolve_channel(body.channel, acct, settings)
    pid = (settings.get("official_api_project_id") or "").strip()
    pkw = _proxy_kwargs(settings)

    nb_id = (body.notebook_id or "").strip()
    created_nb: dict[str, Any] | None = None
    if not nb_id:
        title = (body.new_notebook_title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="请选择现有笔记本，或输入新笔记本名称")
        try:
            if chosen == "official_mcp":
                created_nb = await official_mcp_client.mcp_create_notebook(acct["alias"], pid, title, **pkw)
                nb_id = created_nb["resource_name"]
            elif chosen == "official_rest":
                created_nb = await official_api.create_notebook(acct["alias"], pid, title, **pkw)
                nb_id = created_nb["resource_name"]
            else:
                created_nb = await nlm_py_adapter.create_notebook(acct["nlm_profile"], title)
                nb_id = created_nb["id"]
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"创建笔记本失败: {official_api.format_exception(exc)}",
            ) from exc

    results: list[dict[str, Any]] = []

    for fpath in validated_files:
        ext = fpath.suffix.lower()
        size = (await asyncio.to_thread(fpath.stat)).st_size
        is_text_file = (ext in TEXT_EXTENSIONS or fpath.name.lower() == ".env.example") and size <= 900_000
        text_mime = "text/markdown" if ext == ".md" else "text/plain"
        try:
            if chosen == "official_mcp":
                if not is_text_file:
                    raise RuntimeError(
                        "官方 Remote MCP 仅支持 URL 与文本/Markdown；二进制 PDF/音视频请将通道切换为 Official REST API (v1) 或 Auto。"
                    )
                txt = await asyncio.to_thread(fpath.read_text, encoding="utf-8", errors="replace")
                res = await official_mcp_client.mcp_create_source_text(
                    acct["alias"], pid, nb_id, fpath.name, txt, mime_type=text_mime, **pkw
                )
                results.append(
                    {
                        "item": fpath.name,
                        "kind": "file_text",
                        "channel": "official_mcp",
                        "ok": True,
                        "source_name": res.get("name"),
                        "state": res.get("state", "PROCESSING"),
                    }
                )
            elif chosen == "official_rest":
                if is_text_file:
                    txt = await asyncio.to_thread(fpath.read_text, encoding="utf-8", errors="replace")
                    res = await official_api.create_source_text(
                        acct["alias"], pid, nb_id, fpath.name, txt, mime_type=text_mime, **pkw
                    )
                else:
                    mime, _ = mimetypes.guess_type(fpath.name)
                    res = await official_api.upload_source_file(
                        acct["alias"], pid, nb_id, fpath, display_name=fpath.name, mime_type=mime, **pkw
                    )
                results.append(
                    {
                        "item": fpath.name,
                        "kind": "file",
                        "channel": "official_rest",
                        "ok": True,
                        "source_name": res.get("name"),
                        "state": res.get("state", "PROCESSING"),
                    }
                )
            else:
                res = await nlm_py_adapter.add_file(acct["nlm_profile"], nb_id, fpath, title=fpath.name, wait=True)
                results.append(
                    {
                        "item": fpath.name,
                        "kind": "file",
                        "channel": "notebooklm_py",
                        "ok": True,
                        "source_name": res.get("id"),
                        "state": "READY",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            results.append({"item": fpath.name, "kind": "file", "channel": chosen, "ok": False, "error": str(exc)})

    for raw_url in body.urls:
        u = raw_url.strip()
        if not u:
            continue
        try:
            if chosen == "official_mcp":
                res = await official_mcp_client.mcp_create_source_url(acct["alias"], pid, nb_id, u, **pkw)
                results.append(
                    {
                        "item": u,
                        "kind": "url",
                        "channel": "official_mcp",
                        "ok": True,
                        "source_name": res.get("name"),
                        "state": res.get("state", "PROCESSING"),
                    }
                )
            elif chosen == "official_rest":
                res = await official_api.create_source_url(acct["alias"], pid, nb_id, u, **pkw)
                results.append(
                    {
                        "item": u,
                        "kind": "url",
                        "channel": "official_rest",
                        "ok": True,
                        "source_name": res.get("name"),
                        "state": res.get("state", "PROCESSING"),
                    }
                )
            else:
                res = await nlm_py_adapter.add_url(acct["nlm_profile"], nb_id, u, wait=True)
                results.append(
                    {
                        "item": u,
                        "kind": "url",
                        "channel": "notebooklm_py",
                        "ok": True,
                        "source_name": res.get("id"),
                        "state": "READY",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            results.append({"item": u, "kind": "url", "channel": chosen, "ok": False, "error": str(exc)})

    if (body.inline_text or "").strip():
        title = (body.inline_title or "").strip() or "网页直传笔记"
        safe_mime = "text/markdown" if body.mime_type == "text/markdown" else "text/plain"
        try:
            if chosen == "official_mcp":
                res = await official_mcp_client.mcp_create_source_text(
                    acct["alias"], pid, nb_id, title, body.inline_text or "", mime_type=safe_mime, **pkw
                )
                results.append(
                    {
                        "item": title,
                        "kind": "text",
                        "channel": "official_mcp",
                        "ok": True,
                        "source_name": res.get("name"),
                        "state": res.get("state", "PROCESSING"),
                    }
                )
            elif chosen == "official_rest":
                res = await official_api.create_source_text(
                    acct["alias"], pid, nb_id, title, body.inline_text or "", mime_type=safe_mime, **pkw
                )
                results.append(
                    {
                        "item": title,
                        "kind": "text",
                        "channel": "official_rest",
                        "ok": True,
                        "source_name": res.get("name"),
                        "state": res.get("state", "PROCESSING"),
                    }
                )
            else:
                res = await nlm_py_adapter.add_text(acct["nlm_profile"], nb_id, title, body.inline_text or "", wait=True)
                results.append(
                    {
                        "item": title,
                        "kind": "text",
                        "channel": "notebooklm_py",
                        "ok": True,
                        "source_name": res.get("id"),
                        "state": "READY",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            results.append({"item": title, "kind": "text", "channel": chosen, "ok": False, "error": str(exc)})

    ok_cnt = sum(1 for r in results if r["ok"])
    return {
        "notebook_id": nb_id,
        "created_notebook": created_nb,
        "channel_used": chosen,
        "results": results,
        "success_count": ok_cnt,
        "error_count": len(results) - ok_cnt,
    }
