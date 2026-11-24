"""Protocol, workspace security, and live endpoint tests for Official Gemini Notebook API & MCP."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import official_api, official_mcp_client
from app.main import app


@pytest.fixture()
def lab_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_dir = tmp_path / "data"
    db_file = data_dir / "fleet_lab.db"
    ws_dir = tmp_path / "WorkspaceRoot"
    (ws_dir / "NotebookLM-Fleet" / "docs").mkdir(parents=True)
    (ws_dir / "NotebookLM-Fleet" / "docs" / "note.md").write_text("# 笔记\n\n测试 Markdown 内容。", encoding="utf-8")
    (ws_dir / "NotebookLM-Fleet" / "data").mkdir(parents=True)
    (ws_dir / "NotebookLM-Fleet" / "data" / "client_secret.json").write_text('{"secret":"leak"}', encoding="utf-8")
    (ws_dir / ".env").write_text("SECRET=123", encoding="utf-8")

    hub_dir = tmp_path / "DriveHub"
    lib_dir = tmp_path / "DriveLib"
    lib_dir.mkdir(parents=True)

    monkeypatch.setenv("FLEET_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FLEET_DB_PATH", str(db_file))
    monkeypatch.setenv("FLEET_DISABLE_WORKERS", "1")
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws_dir))
    monkeypatch.setenv("DRIVE_HUB_ROOT", str(hub_dir))
    monkeypatch.setenv("DRIVE_LIBRARY_ROOT", str(lib_dir))
    return {"data": data_dir, "db": db_file, "ws": ws_dir, "hub": hub_dir, "lib": lib_dir}


def test_official_mcp_streamable_http_json_and_sse() -> None:
    seen_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_calls.append(
            {
                "method": body.get("method"),
                "auth": request.headers.get("authorization"),
                "project": request.headers.get("x-goog-user-project"),
                "session": request.headers.get("mcp-session-id"),
                "params": body.get("params"),
            }
        )
        m = body.get("method")
        if m == "initialize":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Mcp-Session-Id": "sess-xyz"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "MockGoogle"}}},
            )
        if m == "notifications/initialized":
            return httpx.Response(202)
        if m == "tools/call":
            # Return Streamable HTTP SSE format to verify SSE parsing!
            tool_name = body["params"]["name"]
            payload = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"name": "notebooks/nb-99/sources/src-1", "state": "ACTIVE", "echo_tool": tool_name}}}
            sse_body = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=sse_body)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    res = asyncio.run(
        official_mcp_client.mcp_create_source_text(
            "ultra-1",
            "proj-demo",
            "nb-99",
            "Spec Note",
            "# Hello",
            mime_type="text/markdown",
            transport=transport,
            access_token="tok-123",
        )
    )
    assert res["name"] == "notebooks/nb-99/sources/src-1"
    assert res["echo_tool"] == "create_source"
    assert [c["method"] for c in seen_calls] == ["initialize", "notifications/initialized", "tools/call"]
    assert seen_calls[2]["session"] == "sess-xyz"
    assert seen_calls[2]["auth"] == "Bearer tok-123"
    assert seen_calls[2]["project"] == "proj-demo"
    assert seen_calls[2]["params"]["arguments"]["source"]["mimeType"] == "text/markdown"


def test_official_rest_api_upload_and_retry(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7 test-bytes")
    attempts = {"list": 0, "poll": 0}
    seen_upload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/notebooks" and request.method == "POST":
            return httpx.Response(200, json={"name": "notebooks/nb-1", "displayName": "Demo"})
        if request.url.path == "/v1/notebooks" and request.method == "GET":
            attempts["list"] += 1
            if attempts["list"] == 1:
                return httpx.Response(429, json={"error": {"code": 429, "message": "rate limit"}})
            assert request.url.params.get("filter") == "notebook.is_owner = true"
            return httpx.Response(200, json={"notebooks": [{"name": "notebooks/nb-1", "displayName": "Demo"}]})
        if request.url.path == "/upload/v1/notebooks/nb-1/sources:upload":
            seen_upload["uploadType"] = request.url.params.get("uploadType")
            seen_upload["displayName"] = request.url.params.get("source.displayName")
            seen_upload["ctype"] = request.headers.get("content-type")
            seen_upload["bytes"] = request.content
            return httpx.Response(200, json={"name": "notebooks/nb-1/sources/s-pdf", "state": "PROCESSING"})
        if request.url.path == "/v1/notebooks/nb-1/sources/s-pdf":
            attempts["poll"] += 1
            state = "PROCESSING" if attempts["poll"] == 1 else "ACTIVE"
            return httpx.Response(200, json={"name": "notebooks/nb-1/sources/s-pdf", "state": state})
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)

    async def _run() -> None:
        nb = await official_api.create_notebook("ultra-1", "proj-1", "Demo", transport=transport, access_token="t")
        assert nb["id"] == "nb-1"
        nbs = await official_api.list_notebooks(
            "ultra-1", "proj-1", filter_expr="notebook.is_owner = true", transport=transport, access_token="t"
        )
        assert len(nbs) == 1 and attempts["list"] == 2
        up = await official_api.upload_source_file(
            "ultra-1", "proj-1", "nb-1", pdf_path, transport=transport, access_token="t"
        )
        assert up["name"] == "notebooks/nb-1/sources/s-pdf"
        assert seen_upload["uploadType"] == "media"
        assert seen_upload["displayName"] == "paper.pdf"
        assert seen_upload["ctype"] == "application/pdf"
        assert seen_upload["bytes"] == b"%PDF-1.7 test-bytes"
        final_src = await official_api.wait_source_active(
            "ultra-1", "proj-1", up["name"], poll_interval=0.01, transport=transport, access_token="t"
        )
        assert final_src["state"] == "ACTIVE"

    asyncio.run(_run())


def test_official_mcp_probe_retry_and_error_formatting() -> None:
    calls = {"n": 0}

    def retry_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("")
        body = json.loads(request.content.decode("utf-8"))
        m = body.get("method")
        if m == "initialize":
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Mcp-Session-Id": "sess-retry"},
                json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "MockGoogle"}}},
            )
        if m == "notifications/initialized":
            return httpx.Response(202)
        if m == "tools/list":
            tools = [
                {"name": n, "description": n, "inputSchema": {"type": "object"}}
                for n in (
                    "create_notebook",
                    "get_notebook",
                    "list_notebooks",
                    "update_notebook",
                    "create_source",
                    "get_source",
                    "list_sources",
                )
            ]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}})
        return httpx.Response(404)

    ok_res = asyncio.run(official_mcp_client.probe_official_mcp(transport=httpx.MockTransport(retry_handler)))
    assert ok_res["reachable"] is True
    assert ok_res["tool_count"] == 7

    def always_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    fail_res = asyncio.run(official_mcp_client.probe_official_mcp(transport=httpx.MockTransport(always_fail)))
    assert fail_res["reachable"] is False
    assert "ConnectTimeout" in str(fail_res.get("error", ""))


def test_live_official_mcp_public_probe() -> None:
    """Verify live reachability & 7 official tools on Google's real https://notebook.googleapis.com/mcp."""
    res = asyncio.run(official_mcp_client.probe_official_mcp())
    err = str(res.get("error", ""))
    if not res["reachable"] and any(tok in err for tok in ("ConnectTimeout", "ConnectError", "ReadTimeout")):
        pytest.skip(f"External TUN/VPN network unreachable during live probe: {err}")
    assert res["reachable"] is True, f"Official MCP probe failed: {res}"
    assert res["tool_count"] == 7
    names = [t["name"] for t in res["tools"]]
    assert names == [
        "create_notebook",
        "get_notebook",
        "list_notebooks",
        "update_notebook",
        "create_source",
        "get_source",
        "list_sources",
    ]


def test_workspace_and_official_api_routes(lab_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        r_roots = client.get("/api/workspace/roots")
        assert r_roots.status_code == 200
        assert r_roots.json()["roots"][0]["id"] == "workspace"

        r_tree = client.get("/api/workspace/tree", params={"root": "workspace"})
        assert r_tree.status_code == 200
        names = [e["name"] for e in r_tree.json()["entries"]]
        assert "NotebookLM-Fleet" in names
        assert ".env" not in names  # Sensitive file hidden from listing

        r_prev = client.get(
            "/api/workspace/preview",
            params={"root": "workspace", "subpath": "NotebookLM-Fleet/docs/note.md"},
        )
        assert r_prev.status_code == 200
        assert "测试 Markdown 内容" in r_prev.json()["content"]

        # Security: traversal and secret files must be rejected.
        assert client.get("/api/workspace/tree", params={"root": "workspace", "subpath": "../"}).status_code == 400
        assert client.get("/api/workspace/preview", params={"root": "workspace", "subpath": ".env"}).status_code == 403
        assert (
            client.get(
                "/api/workspace/preview",
                params={"root": "workspace", "subpath": "NotebookLM-Fleet/data/client_secret.json"},
            ).status_code
            == 403
        )

        # Upload invalid vs valid client_secret.json into isolated FLEET_DATA_DIR.
        r_bad = client.post("/api/official/upload-client-secret", json={"content": '{"foo":"bar"}'})
        assert r_bad.status_code == 400

        valid_secret = json.dumps({
            "installed": {
                "client_id": "1234567890-abc.apps.googleusercontent.com",
                "project_id": "auto-proj-1",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_secret": "sec-xyz",
                "redirect_uris": ["http://localhost"],
            }
        })
        r_ok = client.post("/api/official/upload-client-secret", json={"content": valid_secret})
        assert r_ok.status_code == 200
        assert r_ok.json()["saved"] is True
        assert r_ok.json()["project_id"] == "auto-proj-1"
        assert (lab_env["data"] / "client_secret.json").exists()
        assert client.get("/api/official/status").json()["project_id"] == "auto-proj-1"

        r_oauth = client.post("/api/accounts/1/oauth")
        try:
            assert r_oauth.status_code == 200
            o_data = r_oauth.json()
            assert o_data["auth_url"].startswith("https://accounts.google.com/o/oauth2/auth")
            assert o_data["callback_port"] > 0

            r_redir = client.get("/api/accounts/1/oauth-redirect", follow_redirects=False)
            assert r_redir.status_code == 302
            assert r_redir.headers["location"].startswith("https://accounts.google.com/o/oauth2/auth")
            assert "no-store" in r_redir.headers.get("cache-control", "")
        finally:
            official_api.close_pending_oauth_server("ultra-1")

        # Quick-add security check & mocked official_mcp execution.
        r_forbid = client.post(
            "/api/official/quick-add",
            json={
                "account_id": 1,
                "new_notebook_title": "Test",
                "channel": "official_mcp",
                "file_paths": [str(lab_env["ws"] / ".env")],
            },
        )
        assert r_forbid.status_code == 403

        client.put("/api/settings", json={"updates": {"official_api_project_id": "demo-proj", "official_api_enabled": "true"}})
        monkeypatch.setattr(official_api, "check_oauth_status", lambda alias: "ok")

        async def fake_mcp_create_nb(alias: str, pid: str, title: str) -> dict[str, Any]:
            return {"id": "nb-x", "resource_name": "notebooks/nb-x", "title": title}

        async def fake_mcp_create_src(alias: str, pid: str, nb: str, title: str, content: str, mime_type: str = "text/markdown") -> dict[str, Any]:
            return {"name": f"{nb}/sources/s-1", "state": "ACTIVE", "mimeType": mime_type}

        monkeypatch.setattr(official_mcp_client, "mcp_create_notebook", fake_mcp_create_nb)
        monkeypatch.setattr(official_mcp_client, "mcp_create_source_text", fake_mcp_create_src)

        r_qa = client.post(
            "/api/official/quick-add",
            json={
                "account_id": 1,
                "new_notebook_title": "Workspace Study",
                "channel": "official_mcp",
                "file_paths": [str(lab_env["ws"] / "NotebookLM-Fleet" / "docs" / "note.md")],
            },
        )
        assert r_qa.status_code == 200
        payload = r_qa.json()
        assert payload["success_count"] == 1
        assert payload["channel_used"] == "official_mcp"
        assert payload["results"][0]["state"] == "ACTIVE"


def test_ch2_cookie_parser_and_cdp_import_routes(
    lab_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters import browser_takeover, nlm_py_adapter
    from app.api import accounts as accounts_api

    rebrand_curl = (
        "curl --url 'https://notebook.google.com/?authuser=1' \\\n"
        "  -H 'accept: text/html' \\\n"
        "  -H 'referer: https://accounts.google.com/' \\\n"
        "  -b 'OSID=o1; __Secure-OSID=o2; SID=s1; __Secure-1PSIDTS=t1'"
    )
    parsed_rebrand, meta_rebrand = nlm_py_adapter.parse_raw_cookie_bundle(rebrand_curl)
    assert meta_rebrand == {"source_host": "notebook.google.com", "authuser": 1}
    rebrand_by_name = {c["name"]: c for c in parsed_rebrand}
    assert rebrand_by_name["OSID"]["domain"] == "notebook.google.com"
    assert rebrand_by_name["__Secure-OSID"]["domain"] == "notebook.google.com"
    assert rebrand_by_name["SID"]["domain"] == ".google.com"

    legacy_curl = (
        "curl 'https://notebooklm.google.com/' \\\n"
        "  -H 'accept: text/html' \\\n"
        "  -H 'cookie: SID=sid-val-1; __Secure-1PSIDTS=ts-val-2; OSID=osid-val-3'"
    )
    parsed_curl = nlm_py_adapter.parse_raw_cookie_input(legacy_curl)
    by_name = {c["name"]: c for c in parsed_curl}
    assert by_name["SID"]["domain"] == ".google.com"
    assert by_name["__Secure-1PSIDTS"]["value"] == "ts-val-2"
    assert by_name["OSID"]["domain"] == "notebooklm.google.com"

    raw_hdr = "Cookie: SID=s2; __Secure-1PSIDTS=t2; OSID=o2"
    raw_parsed = nlm_py_adapter.parse_raw_cookie_input(raw_hdr)
    assert len(raw_parsed) == 3
    assert {c["name"]: c["domain"] for c in raw_parsed}["OSID"] == "notebook.google.com"

    json_list = json.dumps([
        {"name": "SID", "value": "s3", "domain": ".google.com", "sameSite": "no_restriction"},
        {"name": "__Secure-1PSIDTS", "value": "t3", "domain": ".google.com"},
        {"name": "OSID", "value": "o3", "domain": "notebook.google.com"},
    ])
    parsed_json = nlm_py_adapter.parse_raw_cookie_input(json_list)
    assert parsed_json[0]["sameSite"] == "None"

    storage_file = tmp_path / "profiles" / "ultra-1" / "storage_state.json"
    monkeypatch.setattr(nlm_py_adapter, "get_storage_path", lambda profile=None: storage_file)

    async def fake_poll_one(account_id: int) -> dict[str, Any]:
        return {
            "id": account_id,
            "alias": "ultra-1",
            "nlm_auth_status": "ok",
            "five_hour_remaining_pct": 98.0,
            "weekly_remaining_pct": 95.0,
        }

    monkeypatch.setattr(accounts_api, "poll_one_account", fake_poll_one)

    async def fake_capture_cdp(port: int) -> list[dict[str, Any]]:
        return json.loads(json_list)

    monkeypatch.setattr(browser_takeover, "capture_cdp_cookies", fake_capture_cdp)

    with TestClient(app) as client:
        r_bad = client.post(
            "/api/accounts/1/import-cookies-raw",
            json={"raw_input": "Cookie: SID=only-sid"},
        )
        assert r_bad.status_code == 400
        assert "__Secure-1PSIDTS" in r_bad.json()["detail"]

        r_curl = client.post(
            "/api/accounts/1/import-cookies-raw",
            json={"raw_input": rebrand_curl},
        )
        assert r_curl.status_code == 200
        d_curl = r_curl.json()
        assert d_curl["ok"] is True
        assert d_curl["saved"]["has_secondary_binding"] is True
        assert d_curl["saved"]["authuser"] == 1
        assert d_curl["saved"]["source_host"] == "notebook.google.com"
        assert storage_file.exists()

        saved_state = json.loads(storage_file.read_text(encoding="utf-8"))
        saved_domains = {c["name"]: c["domain"] for c in saved_state["cookies"]}
        assert saved_domains["OSID"] == "notebook.google.com"
        assert saved_domains["__Secure-OSID"] == "notebook.google.com"
        assert saved_domains["SID"] == ".google.com"
        assert saved_state["notebooklm"]["account"]["authuser"] == 1

        r_cdp = client.post("/api/accounts/1/capture-cdp")
        assert r_cdp.status_code == 200
        assert r_cdp.json()["ok"] is True

        # Silent CDP capture must stay waiting when only SID (no OSID/PSIDTS) is present, then succeed cleanly.
        monkeypatch.setattr(
            browser_takeover,
            "capture_cdp_cookies",
            lambda port: asyncio.sleep(0, result=[{"name": "SID", "value": "only-sid", "domain": ".google.com"}]),
        )
        r_wait = client.post("/api/accounts/1/capture-cdp?silent=true")
        assert r_wait.status_code == 200
        assert r_wait.json()["ok"] is False
        assert r_wait.json()["waiting"] is True

        monkeypatch.setattr(browser_takeover, "capture_cdp_cookies", fake_capture_cdp)
        r_silent_ok = client.post("/api/accounts/1/capture-cdp?silent=true")
        assert r_silent_ok.status_code == 200
        assert r_silent_ok.json()["ok"] is True
        assert r_silent_ok.json()["waiting"] is False

        # import-browser-cookies must auto-prioritize running isolated CDP Chrome over locked rookie-cookies.
        monkeypatch.setattr(browser_takeover, "_is_cdp_alive", lambda port: True)
        r_imp_cdp = client.post("/api/accounts/1/import-browser-cookies")
        assert r_imp_cdp.status_code == 200
        assert r_imp_cdp.json()["ok"] is True
        assert r_imp_cdp.json()["source"] == "cdp"

        # Topic ingest must block sensitive workspace files just like workspace/official routes.
        t_resp = client.post("/api/topics", json={"name": "SafeTopic", "create_remote": False})
        assert t_resp.status_code == 200
        tid = t_resp.json()["topic"]["id"]
        r_ing_bad = client.post(
            f"/api/topics/{tid}/ingest",
            json={"file_paths": [str(lab_env["ws"] / ".env")]},
        )
        assert r_ing_bad.status_code == 403


def test_browser_takeover_chrome_and_edge_visible_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters import browser_takeover

    fake_edge = tmp_path / "msedge.exe"
    fake_edge.write_bytes(b"")
    launched_calls: list[tuple[Path, list[str]]] = []

    monkeypatch.setattr(browser_takeover, "find_edge_executable", lambda: fake_edge)
    monkeypatch.setattr(browser_takeover, "_get_cdp_browser_info", lambda port: None)
    monkeypatch.setattr(
        browser_takeover,
        "_launch_visible_windows",
        lambda exe, args: (launched_calls.append((exe, args)) or (True, 1234)),
    )

    res = browser_takeover.open_account_browser(
        "ultra-2",
        tmp_path / "profiles" / "ultra-2",
        9223,
        url="https://notebook.google.com/",
        browser="edge",
    )
    assert res["launched"] is True
    assert res["browser"] == "edge"
    assert res["profile_dir"].endswith("ultra-2_edge")
    assert launched_calls[0][0] == fake_edge
    assert "--remote-debugging-port=9223" in launched_calls[0][1]
    assert "https://notebook.google.com/" in launched_calls[0][1]
