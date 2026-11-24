"""Official Nov 2026 Gemini Notebook Remote MCP (Streamable HTTP JSON-RPC 2.0) client."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from . import official_api

OFFICIAL_MCP_URL = "https://notebook.googleapis.com/mcp"
MCP_PROTOCOL_VERSION = "2024-11-05"


def _parse_mcp_http_response(resp: httpx.Response) -> dict[str, Any]:
    """Parse Streamable HTTP responses supporting both application/json and text/event-stream."""
    ctype = (resp.headers.get("content-type") or "").lower()
    raw_text = resp.text.strip()
    if not raw_text:
        return {}

    if "text/event-stream" in ctype or raw_text.startswith("data:") or "\ndata:" in raw_text:
        payloads: list[dict[str, Any]] = []
        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            chunk = stripped[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    payloads.append(parsed)
            except json.JSONDecodeError:
                continue
        if payloads:
            # Prefer the final JSON-RPC result/error message in the stream.
            for item in reversed(payloads):
                if "result" in item or "error" in item:
                    return item
            return payloads[-1]

    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except ValueError:
        return {"raw_text": raw_text[:1000]}


def _base_headers(*, token: str | None = None, project_id: str | None = None, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if project_id and project_id.strip():
        headers["x-goog-user-project"] = project_id.strip()
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


async def probe_official_mcp(
    mcp_url: str = OFFICIAL_MCP_URL,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Run live Streamable HTTP initialize + tools/list against Google's official remote MCP."""
    t0 = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with official_api.make_async_client(20.0, transport=transport, proxy=proxy) as client:
                init_payload = {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "notebooklm-fleet", "version": "0.1.0"},
                    },
                    "id": 1,
                }
                init_resp = await client.post(mcp_url, headers=_base_headers(), json=init_payload)
                if init_resp.status_code >= 400:
                    return {
                        "reachable": False,
                        "mcp_url": mcp_url,
                        "status_code": init_resp.status_code,
                        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "error": init_resp.text[:500],
                        "tools": [],
                    }
                init_json = _parse_mcp_http_response(init_resp)
                session_id = init_resp.headers.get("mcp-session-id")

                # Best-effort notifications/initialized per MCP Streamable HTTP spec.
                try:
                    await client.post(
                        mcp_url,
                        headers=_base_headers(session_id=session_id),
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    )
                except httpx.TransportError:
                    pass

                list_resp = await client.post(
                    mcp_url,
                    headers=_base_headers(session_id=session_id),
                    json={"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2},
                )
                list_json = _parse_mcp_http_response(list_resp)
                latency_ms = round((time.perf_counter() - t0) * 1000, 1)

            if list_resp.status_code >= 400 or "error" in list_json:
                return {
                    "reachable": False,
                    "mcp_url": mcp_url,
                    "status_code": list_resp.status_code,
                    "latency_ms": latency_ms,
                    "error": str(list_json.get("error") or list_resp.text[:500]),
                    "tools": [],
                }

            init_res = init_json.get("result") or {}
            tools = (list_json.get("result") or {}).get("tools") or []
            return {
                "reachable": True,
                "mcp_url": mcp_url,
                "status_code": list_resp.status_code,
                "latency_ms": latency_ms,
                "protocol_version": init_res.get("protocolVersion"),
                "server_info": init_res.get("serverInfo") or {},
                "capabilities": init_res.get("capabilities") or {},
                "session_id": session_id,
                "tool_count": len(tools),
                "tools": tools,
            }
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
        except Exception as exc:  # noqa: BLE001
            return {
                "reachable": False,
                "mcp_url": mcp_url,
                "status_code": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": official_api.format_exception(exc),
                "tools": [],
            }
    return {
        "reachable": False,
        "mcp_url": mcp_url,
        "status_code": None,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "error": official_api.format_exception(last_exc) if last_exc else "Unknown transport error",
        "tools": [],
    }


async def call_official_mcp_tool(
    alias: str,
    project_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    mcp_url: str = OFFICIAL_MCP_URL,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Execute an authenticated tools/call request against Google's official remote MCP."""
    if not project_id.strip():
        raise ValueError("尚未配置 official_api_project_id")
    token = access_token or await asyncio.to_thread(official_api._load_and_refresh_token, alias, proxy=proxy)
    t0 = time.perf_counter()

    client: httpx.AsyncClient | None = None
    init_resp: httpx.Response | None = None
    for attempt in range(2):
        client = official_api.make_async_client(30.0, transport=transport, proxy=proxy)
        try:
            init_resp = await client.post(
                mcp_url,
                headers=_base_headers(token=token, project_id=project_id),
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "notebooklm-fleet", "version": "0.1.0"},
                    },
                    "id": 1,
                },
            )
            break
        except httpx.TransportError as exc:
            await client.aclose()
            client = None
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "tool_name": tool_name,
                "result": None,
                "error": official_api.format_exception(exc),
            }

    assert client is not None and init_resp is not None
    try:
        if init_resp.status_code >= 400:
            return {
                "ok": False,
                "status_code": init_resp.status_code,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "tool_name": tool_name,
                "result": None,
                "error": official_api._format_http_error(init_resp),
            }

        session_id = init_resp.headers.get("mcp-session-id")
        try:
            await client.post(
                mcp_url,
                headers=_base_headers(token=token, project_id=project_id, session_id=session_id),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        except httpx.TransportError:
            pass

        try:
            call_resp = await client.post(
                mcp_url,
                headers=_base_headers(token=token, project_id=project_id, session_id=session_id),
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments or {}},
                    "id": 2,
                },
            )
        except httpx.TransportError as exc:
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "tool_name": tool_name,
                "result": None,
                "error": official_api.format_exception(exc),
            }

        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if call_resp.status_code >= 400:
            return {
                "ok": False,
                "status_code": call_resp.status_code,
                "latency_ms": latency_ms,
                "tool_name": tool_name,
                "result": None,
                "error": official_api._format_http_error(call_resp),
            }

        parsed = _parse_mcp_http_response(call_resp)
        if "error" in parsed:
            return {
                "ok": False,
                "status_code": call_resp.status_code,
                "latency_ms": latency_ms,
                "tool_name": tool_name,
                "result": None,
                "raw_response": parsed,
                "error": str(parsed["error"]),
            }

        result = parsed.get("result") or {}
        is_err = bool(result.get("isError")) if isinstance(result, dict) else False
        structured = _extract_tool_payload(result)
        return {
            "ok": not is_err,
            "status_code": call_resp.status_code,
            "latency_ms": latency_ms,
            "tool_name": tool_name,
            "result": structured,
            "raw_response": parsed,
            "error": _extract_error_text(result) if is_err else None,
        }
    finally:
        await client.aclose()


def _extract_tool_payload(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                txt = part.get("text", "")
                try:
                    return json.loads(txt)
                except ValueError:
                    return {"text": txt}
    return result


def _extract_error_text(result: Any) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            texts = [str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("text")]
            if texts:
                return " ".join(texts)[:500]
    return str(result)[:500]


def _normalize_notebook_name(notebook_id_or_name: str) -> str:
    s = notebook_id_or_name.strip()
    return s if s.startswith("notebooks/") else f"notebooks/{s}"


async def mcp_list_notebooks(
    alias: str,
    project_id: str,
    *,
    filter_expr: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"pageSize": 50}
    if filter_expr:
        args["filter"] = filter_expr
    res = await call_official_mcp_tool(
        alias, project_id, "list_notebooks", args, transport=transport, access_token=access_token, proxy=proxy
    )
    if not res["ok"]:
        raise RuntimeError(res.get("error") or "Official MCP list_notebooks failed")
    data = res["result"] if isinstance(res["result"], dict) else {}
    nbs = data.get("notebooks") or []
    out = []
    for nb in nbs:
        name = nb.get("name", "")
        nb_id = name.split("/")[-1] if "/" in name else name
        out.append(
            {
                "id": nb_id,
                "resource_name": name,
                "title": nb.get("displayName") or nb_id,
                "create_time": nb.get("createTime"),
                "is_owner": True,
                "role": "owner",
            }
        )
    return out


async def mcp_create_notebook(
    alias: str,
    project_id: str,
    display_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    res = await call_official_mcp_tool(
        alias,
        project_id,
        "create_notebook",
        {"notebook": {"displayName": display_name[:500]}},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    if not res["ok"]:
        raise RuntimeError(res.get("error") or "Official MCP create_notebook failed")
    nb = res["result"] if isinstance(res["result"], dict) else {}
    name = nb.get("name", "")
    nb_id = name.split("/")[-1] if "/" in name else name
    return {
        "id": nb_id,
        "resource_name": name,
        "title": nb.get("displayName") or display_name,
        "create_time": nb.get("createTime"),
    }


async def mcp_create_source_url(
    alias: str,
    project_id: str,
    notebook_name: str,
    url: str,
    display_name: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    source_obj: dict[str, Any] = {"webContent": {"url": url}}
    if display_name:
        source_obj["displayName"] = display_name[:500]
    res = await call_official_mcp_tool(
        alias,
        project_id,
        "create_source",
        {"parent": _normalize_notebook_name(notebook_name), "source": source_obj},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    if not res["ok"]:
        raise RuntimeError(res.get("error") or "Official MCP create_source(url) failed")
    return res["result"]


async def mcp_create_source_text(
    alias: str,
    project_id: str,
    notebook_name: str,
    display_name: str,
    content: str,
    *,
    mime_type: str = "text/markdown",
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    safe_mime = "text/markdown" if mime_type == "text/markdown" else "text/plain"
    source_obj = {
        "displayName": (display_name or "Note")[:500],
        "mimeType": safe_mime,
        "textContent": {"content": content},
    }
    res = await call_official_mcp_tool(
        alias,
        project_id,
        "create_source",
        {"parent": _normalize_notebook_name(notebook_name), "source": source_obj},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    if not res["ok"]:
        raise RuntimeError(res.get("error") or "Official MCP create_source(text) failed")
    return res["result"]


async def mcp_list_sources(
    alias: str,
    project_id: str,
    notebook_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    res = await call_official_mcp_tool(
        alias,
        project_id,
        "list_sources",
        {"parent": _normalize_notebook_name(notebook_name), "pageSize": 50},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    if not res["ok"]:
        raise RuntimeError(res.get("error") or "Official MCP list_sources failed")
    data = res["result"] if isinstance(res["result"], dict) else {}
    return data.get("sources") or []


async def mcp_get_source(
    alias: str,
    project_id: str,
    source_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    res = await call_official_mcp_tool(
        alias,
        project_id,
        "get_source",
        {"name": source_name},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    if not res["ok"]:
        raise RuntimeError(res.get("error") or "Official MCP get_source failed")
    return res["result"]
