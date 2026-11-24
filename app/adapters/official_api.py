"""Channel 1 adapter: Official Nov 2026 Gemini Notebook Personal Beta REST API."""

from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import random
import threading
import time
import wsgiref.simple_server
import wsgiref.util
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import requests
from google.auth.exceptions import TransportError as GoogleAuthTransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import (
    InstalledAppFlow,
    _ExclusiveWSGIServer,
    _WSGIRequestHandler,
)

from ..config import get_client_secret_path, get_oauth_token_path

BASE_URL = "https://notebook.googleapis.com/v1"
UPLOAD_BASE_URL = "https://notebook.googleapis.com/upload/v1"
SCOPES = ["https://www.googleapis.com/auth/notebook"]


def has_client_secret() -> bool:
    """Return True when Google Cloud Desktop OAuth client_secret.json is installed."""
    path = get_client_secret_path()
    return path.exists() and path.is_file()


def save_client_secret_content(raw_json: str) -> dict[str, Any]:
    """Validate and save Google Cloud OAuth client_secret.json content."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 格式无效: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("client_secret.json 必须是 JSON 对象")

    kind = "installed" if "installed" in parsed else ("web" if "web" in parsed else None)
    if kind is None or not isinstance(parsed[kind], dict):
        raise ValueError("缺少 'installed'（推荐 Desktop app）或 'web' OAuth 客户端配置字段")

    section = parsed[kind]
    client_id = str(section.get("client_id") or "").strip()
    client_secret = str(section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("OAuth 配置中缺少 client_id 或 client_secret")

    dest = get_client_secret_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    preview = f"{client_id[:16]}...{client_id[-12:]}" if len(client_id) > 32 else client_id
    return {
        "saved": True,
        "type": kind,
        "project_id": str(section.get("project_id") or "").strip(),
        "client_id_preview": preview,
        "path": str(dest),
    }


def check_oauth_status(alias: str) -> str:
    """Check whether per-account OAuth token exists and is usable/refreshable."""
    if not has_client_secret():
        return "not_configured"
    token_path = get_oauth_token_path(alias)
    if not token_path.exists():
        return "not_configured"
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (OSError, ValueError):
        return "error"
    if creds.valid or creds.refresh_token:
        return "ok"
    return "expired"


_PENDING_OAUTH_LOCK = threading.Lock()
_PENDING_OAUTH_SERVERS: dict[str, Any] = {}
_LAST_OAUTH_ERRORS: dict[str, str] = {}


def get_last_oauth_error(alias: str) -> str:
    """Return the most recent OAuth callback/token-exchange error for this alias, if any."""
    with _PENDING_OAUTH_LOCK:
        return _LAST_OAUTH_ERRORS.get(alias, "")


def set_last_oauth_error(alias: str, err: str) -> None:
    with _PENDING_OAUTH_LOCK:
        _LAST_OAUTH_ERRORS[alias] = err


def clear_last_oauth_error(alias: str) -> None:
    with _PENDING_OAUTH_LOCK:
        _LAST_OAUTH_ERRORS.pop(alias, None)


def close_pending_oauth_server(alias: str) -> None:
    """Close any previously listening OAuth loopback server for this account alias."""
    with _PENDING_OAUTH_LOCK:
        srv = _PENDING_OAUTH_SERVERS.pop(alias, None)
    if srv is not None:
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass


class _OAuthCallbackWSGIApp:
    """Minimal loopback WSGI handler returning a friendly UTF-8 HTML page."""

    def __init__(self, alias: str) -> None:
        self.alias = alias
        self.last_request_uri: str | None = None

    def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        req_uri = wsgiref.util.request_uri(environ)
        path_info = str(environ.get("PATH_INFO") or "")
        if path_info == "/favicon.ico":
            start_response("204 No Content", [])
            return [b""]

        self.last_request_uri = req_uri
        safe_alias = html.escape(self.alias)
        has_err = "error=" in req_uri
        if has_err:
            body_html = (
                "<!DOCTYPE html><meta charset='utf-8'>"
                "<body style='font-family:sans-serif;padding:40px;line-height:1.7;background:#f5f3ee;color:#20241f'>"
                "<h2>❌ 官方 API 授权未完成</h2>"
                f"<p>账号 <b>{safe_alias}</b> 授权被取消或拒绝，请返回控制面板重试。</p>"
                "<p><a href='http://127.0.0.1:8765' style='display:inline-block;padding:10px 16px;background:#136f63;color:#fff;border-radius:8px;text-decoration:none'>返回 NotebookLM-Fleet 控制面板</a></p>"
                "</body>"
            )
        else:
            body_html = (
                "<!DOCTYPE html><meta charset='utf-8'>"
                "<body style='font-family:sans-serif;padding:40px;line-height:1.7;background:#f5f3ee;color:#20241f'>"
                "<h2>✅ 官方 Gemini Notebook API 授权成功</h2>"
                f"<p>账号 <b>{safe_alias}</b> 的 OAuth 令牌正在保存中，可返回控制面板查看「Ch1 已就绪」状态。</p>"
                "<p><a href='http://127.0.0.1:8765' style='display:inline-block;padding:10px 16px;background:#136f63;color:#fff;border-radius:8px;text-decoration:none'>返回 NotebookLM-Fleet 控制面板</a></p>"
                "</body>"
            )
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body_html.encode("utf-8")]


def start_oauth_flow_for_account(
    alias: str,
    *,
    timeout_seconds: int = 1800,
    proxy: str | None = None,
) -> tuple[str, int, Callable[[], dict[str, Any]]]:
    """Bind a loopback OAuth callback server immediately and return (auth_url, port, finish_fn)."""
    secret_path = get_client_secret_path()
    if not secret_path.exists():
        raise FileNotFoundError(
            f"缺少 {secret_path}。请先在 Google Cloud Console 创建桌面端 OAuth 客户端并下载 JSON 保存到此路径。"
        )

    close_pending_oauth_server(alias)
    clear_last_oauth_error(alias)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    clean_proxy = (proxy or "").strip()
    if clean_proxy:
        flow.oauth2session.proxies = {"http": clean_proxy, "https": clean_proxy}

    wsgi_app = _OAuthCallbackWSGIApp(alias)
    local_server = wsgiref.simple_server.make_server(
        "localhost",
        0,
        wsgi_app,
        server_class=_ExclusiveWSGIServer,
        handler_class=_WSGIRequestHandler,
    )
    port = int(local_server.server_port)
    with _PENDING_OAUTH_LOCK:
        _PENDING_OAUTH_SERVERS[alias] = local_server

    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _ = flow.authorization_url(prompt="select_account consent", access_type="offline")

    def finish_flow() -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                local_server.timeout = min(remaining, 30.0)
                local_server.handle_request()
                if wsgi_app.last_request_uri and (
                    "code=" in wsgi_app.last_request_uri or "error=" in wsgi_app.last_request_uri
                ):
                    break

            if not wsgi_app.last_request_uri:
                raise TimeoutError("等待 Google OAuth 浏览器回调超时（30 分钟），请重新点击授权链接。")

            authorization_response = wsgi_app.last_request_uri
            if authorization_response.startswith("http://"):
                authorization_response = "https://" + authorization_response[len("http://") :]

            flow.fetch_token(authorization_response=authorization_response)
            creds = flow.credentials
            token_path = get_oauth_token_path(alias)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
            clear_last_oauth_error(alias)
            return {"status": "ok", "alias": alias, "token_path": str(token_path)}
        except Exception as exc:
            set_last_oauth_error(alias, format_exception(exc))
            raise
        finally:
            with _PENDING_OAUTH_LOCK:
                if _PENDING_OAUTH_SERVERS.get(alias) is local_server:
                    _PENDING_OAUTH_SERVERS.pop(alias, None)
            try:
                local_server.server_close()
            except Exception:  # noqa: BLE001
                pass

    return auth_url, port, finish_flow


def run_oauth_flow_for_account(alias: str) -> dict[str, Any]:
    """Run local browser OAuth 2.0 consent flow for one personal Google account."""
    secret_path = get_client_secret_path()
    if not secret_path.exists():
        raise FileNotFoundError(
            f"缺少 {secret_path}。请先在 Google Cloud Console 创建桌面端 OAuth 客户端并下载 JSON 保存到此路径。"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="select_account consent")
    token_path = get_oauth_token_path(alias)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return {"status": "ok", "alias": alias, "token_path": str(token_path)}


def _load_and_refresh_token(alias: str, *, proxy: str | None = None) -> str:
    token_path = get_oauth_token_path(alias)
    if not token_path.exists():
        raise RuntimeError(f"账号 {alias} 尚未完成官方 API OAuth 授权 ({token_path})")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            clean_proxy = (proxy or "").strip()
            last_exc: Exception | None = None
            for attempt in range(3):
                session = requests.Session()
                try:
                    if clean_proxy:
                        session.proxies = {"http": clean_proxy, "https": clean_proxy}
                    creds.refresh(Request(session=session))
                    last_exc = None
                    break
                except (GoogleAuthTransportError, requests.RequestException, TimeoutError, OSError) as exc:
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                finally:
                    session.close()
            if last_exc is not None:
                raise RuntimeError(f"刷新 OAuth 令牌失败: {format_exception(last_exc)}") from last_exc
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(f"账号 {alias} 的 OAuth token 已失效且无法刷新，请重新授权")
    if not creds.token:
        raise RuntimeError(f"账号 {alias} 未获取到有效 access_token")
    return creds.token


def _format_http_error(resp: httpx.Response) -> str:
    body = resp.text[:500]
    hint = ""
    if resp.status_code == 404:
        hint = " | 提示：个人账号首次调用官方 API 前，需先在浏览器访问一次 https://notebooklm.google.com/"
    elif resp.status_code == 403:
        hint = " | 提示：请确认已在 Google Cloud 启用 notebook.googleapis.com，将邮箱加入 OAuth Test users，且账号为个人 Gmail（预览期暂不支持 Workspace）"
    return f"Official API HTTP {resp.status_code}: {body}{hint}"


def format_exception(exc: Exception) -> str:
    """Return a non-empty, user-friendly error message for HTTP/TLS transport exceptions."""
    msg = str(exc).strip()
    base = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    if isinstance(exc, httpx.TransportError):
        return f"{base}（连接 notebook.googleapis.com 超时或中断，请检查代理/隧道状态后重试）"
    return base


def make_async_client(
    timeout_sec: float = 20.0,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy: str | None = None,
) -> httpx.AsyncClient:
    """Build an httpx AsyncClient with explicit connect/read timeouts and optional proxy."""
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(
            timeout_sec,
            connect=min(15.0, timeout_sec),
            read=timeout_sec,
            write=timeout_sec,
            pool=10.0,
        ),
        "transport": transport,
    }
    clean_proxy = (proxy or "").strip()
    if clean_proxy and transport is None:
        kwargs["proxy"] = clean_proxy
    return httpx.AsyncClient(**kwargs)


async def probe_rest_reachability(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Probe network/TLS reachability of https://notebook.googleapis.com/v1/notebooks."""
    t0 = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with make_async_client(15.0, transport=transport, proxy=proxy) as client:
                resp = await client.get(f"{BASE_URL}/notebooks")
            ms = round((time.perf_counter() - t0) * 1000, 1)
            reachable = resp.status_code in (200, 401, 403)
            return {
                "reachable": reachable,
                "status_code": resp.status_code,
                "latency_ms": ms,
                "endpoint": f"{BASE_URL}/notebooks",
                "detail": "端点可达，按预期返回 401 鉴权要求" if resp.status_code == 401 else resp.text[:240],
            }
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
        except Exception as exc:  # noqa: BLE001
            return {
                "reachable": False,
                "status_code": None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "endpoint": f"{BASE_URL}/notebooks",
                "detail": format_exception(exc),
            }
    return {
        "reachable": False,
        "status_code": None,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "endpoint": f"{BASE_URL}/notebooks",
        "detail": format_exception(last_exc) if last_exc else "Unknown transport error",
    }


async def _request(
    alias: str,
    project_id: str,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    if not project_id.strip():
        raise ValueError("尚未配置 official_api_project_id")
    token = access_token or await asyncio.to_thread(_load_and_refresh_token, alias, proxy=proxy)
    url = f"{BASE_URL}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "x-goog-user-project": project_id.strip(),
        "Content-Type": "application/json",
    }

    for attempt in range(max_retries + 1):
        try:
            async with make_async_client(30.0, transport=transport, proxy=proxy) as client:
                resp = await client.request(method, url, headers=headers, json=json_body, params=params)
        except httpx.TransportError as exc:
            is_connect_only = isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
            if (is_connect_only and attempt < 1) or (method.upper() == "GET" and attempt < min(max_retries, 2)):
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise RuntimeError(format_exception(exc)) from exc
        if resp.status_code == 429 and attempt < max_retries:
            delay = (2**attempt) + random.uniform(0.05, 0.3)
            await asyncio.sleep(delay)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(_format_http_error(resp))
        return resp.json() if resp.content else {}
    raise RuntimeError("Official API retries exhausted")


def _normalize_notebook_path(notebook_name: str) -> str:
    return notebook_name if notebook_name.startswith("notebooks/") else f"notebooks/{notebook_name}"


async def list_notebooks(
    alias: str,
    project_id: str,
    *,
    filter_expr: str | None = None,
    page_size: int = 50,
    page_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"pageSize": min(max(1, page_size), 100)}
    if filter_expr:
        params["filter"] = filter_expr
    if page_token:
        params["pageToken"] = page_token

    data = await _request(
        alias,
        project_id,
        "GET",
        "notebooks",
        params=params,
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    items = data.get("notebooks") or []
    result = []
    for nb in items:
        name = nb.get("name", "")
        nb_id = name.split("/")[-1] if "/" in name else name
        result.append(
            {
                "id": nb_id,
                "resource_name": name,
                "title": nb.get("displayName") or nb.get("display_name") or nb_id,
                "create_time": nb.get("createTime"),
                "is_owner": True,
                "role": "owner",
            }
        )
    return result


async def get_notebook(
    alias: str,
    project_id: str,
    notebook_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    path = _normalize_notebook_path(notebook_name)
    return await _request(
        alias, project_id, "GET", path, transport=transport, access_token=access_token, proxy=proxy
    )


async def create_notebook(
    alias: str,
    project_id: str,
    display_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    data = await _request(
        alias,
        project_id,
        "POST",
        "notebooks",
        json_body={"displayName": display_name[:500]},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    name = data.get("name", "")
    nb_id = name.split("/")[-1] if "/" in name else name
    return {
        "id": nb_id,
        "resource_name": name,
        "title": data.get("displayName") or data.get("display_name") or display_name,
        "create_time": data.get("createTime"),
    }


async def update_notebook(
    alias: str,
    project_id: str,
    notebook_name: str,
    display_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    path = _normalize_notebook_path(notebook_name)
    return await _request(
        alias,
        project_id,
        "PATCH",
        path,
        params={"updateMask": "display_name"},
        json_body={"name": path, "displayName": display_name[:500]},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )


async def list_sources(
    alias: str,
    project_id: str,
    notebook_name: str,
    *,
    page_size: int = 50,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> list[dict[str, Any]]:
    path = _normalize_notebook_path(notebook_name)
    data = await _request(
        alias,
        project_id,
        "GET",
        f"{path}/sources",
        params={"pageSize": min(max(1, page_size), 100)},
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )
    return data.get("sources") or []


async def get_source(
    alias: str,
    project_id: str,
    source_name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    return await _request(
        alias,
        project_id,
        "GET",
        source_name.lstrip("/"),
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )


async def create_source_url(
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
    path = _normalize_notebook_path(notebook_name)
    payload: dict[str, Any] = {"webContent": {"url": url}}
    if display_name:
        payload["displayName"] = display_name[:500]
    return await _request(
        alias,
        project_id,
        "POST",
        f"{path}/sources",
        json_body=payload,
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )


async def create_source_text(
    alias: str,
    project_id: str,
    notebook_name: str,
    title: str,
    text: str,
    *,
    mime_type: str = "text/markdown",
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    path = _normalize_notebook_path(notebook_name)
    safe_mime = "text/markdown" if mime_type == "text/markdown" else "text/plain"
    payload = {
        "displayName": (title or "Note")[:500],
        "mimeType": safe_mime,
        "textContent": {"content": text},
    }
    return await _request(
        alias,
        project_id,
        "POST",
        f"{path}/sources",
        json_body=payload,
        transport=transport,
        access_token=access_token,
        proxy=proxy,
    )


async def upload_source_file(
    alias: str,
    project_id: str,
    notebook_name: str,
    file_path: str | Path,
    *,
    display_name: str | None = None,
    mime_type: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Upload a local binary/text file (<= 200 MiB) via the official /upload/v1 endpoint."""
    if not project_id.strip():
        raise ValueError("尚未配置 official_api_project_id")
    src = Path(file_path)
    if not await asyncio.to_thread(src.exists) or not await asyncio.to_thread(src.is_file):
        raise FileNotFoundError(f"文件不存在: {src}")
    size = (await asyncio.to_thread(src.stat)).st_size
    if size > 200 * 1024 * 1024:
        raise ValueError("单个文件超过官方 REST API 200 MiB 上传上限")

    token = access_token or await asyncio.to_thread(_load_and_refresh_token, alias, proxy=proxy)
    parent = _normalize_notebook_path(notebook_name)
    label = (display_name or src.name)[:500]
    guessed_mime = mime_type or mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    url = f"{UPLOAD_BASE_URL}/{parent}/sources:upload?uploadType=media&source.displayName={quote(label)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "x-goog-user-project": project_id.strip(),
        "Content-Type": guessed_mime,
    }
    raw_bytes = await asyncio.to_thread(src.read_bytes)
    try:
        async with make_async_client(120.0, transport=transport, proxy=proxy) as client:
            resp = await client.post(url, headers=headers, content=raw_bytes)
    except httpx.TransportError as exc:
        raise RuntimeError(format_exception(exc)) from exc
    if resp.status_code >= 400:
        raise RuntimeError(_format_http_error(resp))
    return resp.json() if resp.content else {}


async def wait_source_active(
    alias: str,
    project_id: str,
    source_name: str,
    *,
    timeout_sec: float = 60.0,
    poll_interval: float = 2.0,
    transport: httpx.AsyncBaseTransport | None = None,
    access_token: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Poll get_source until state reaches ACTIVE or FAILED."""
    deadline = time.monotonic() + timeout_sec
    latest: dict[str, Any] = {}
    while True:
        latest = await get_source(
            alias, project_id, source_name, transport=transport, access_token=access_token, proxy=proxy
        )
        state = str(latest.get("state") or "")
        if state in ("ACTIVE", "FAILED"):
            return latest
        if time.monotonic() >= deadline:
            return latest
        await asyncio.sleep(poll_interval)
