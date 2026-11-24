"""Account ledger, auth check, browser takeover, and quota polling routes."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from notebooklm.auth import MINIMUM_REQUIRED_COOKIES
from pydantic import BaseModel, Field

from ..adapters import browser_takeover, nlm_py_adapter, official_api
from ..config import get_chrome_profile_dir
from ..core.quota_poller import poll_all_accounts, poll_one_account
from ..db import (
    TIER_LIMITS,
    execute,
    fetch_all,
    fetch_one,
    get_accounts_with_latest_quota,
    get_settings_map,
    next_cdp_port,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/accounts", tags=["accounts"])
_BG_OAUTH_TASKS: set[asyncio.Task[Any]] = set()


class AccountCreate(BaseModel):
    alias: str | None = None
    email: str
    tier: str = Field(default="ultra", pattern="^(ultra|pro|plus|free)$")
    nlm_profile: str | None = None
    is_hub_master: bool = False
    drive_shared_ready: bool = False
    expire_date: str | None = None
    enabled: bool = True
    note: str = ""


class AccountUpdate(BaseModel):
    alias: str | None = None
    email: str | None = None
    tier: str | None = Field(default=None, pattern="^(ultra|pro|plus|free)$")
    is_hub_master: bool | None = None
    drive_shared_ready: bool | None = None
    expire_date: str | None = None
    enabled: bool | None = None
    note: str | None = None


class OpenBrowserRequest(BaseModel):
    url: str = "https://notebook.google.com/"
    browser: str = Field(default="chrome", pattern="^(chrome|edge|auto)$")


class ImportCookiesRawRequest(BaseModel):
    raw_input: str


async def _next_alias(tier: str) -> str:
    rows = await fetch_all("SELECT alias FROM accounts")
    used = {r["alias"] for r in rows}
    idx = 1
    while f"{tier}-{idx}" in used:
        idx += 1
    return f"{tier}-{idx}"


@router.get("")
async def list_accounts() -> dict[str, Any]:
    accounts = await get_accounts_with_latest_quota()
    for acct in accounts:
        local = nlm_py_adapter.check_local_auth(acct["nlm_profile"])
        acct["local_cookie_exists"] = local["exists"]
        acct["has_lsid"] = bool(local.get("has_lsid"))
        acct["has_osid"] = bool(local.get("has_osid"))
        acct["persistence_level"] = local.get("persistence_level", "none")
        profile_dir = (acct.get("chrome_profile_dir") or "").strip()
        has_browser_prof = False
        if profile_dir:
            chrome_dir = browser_takeover.resolve_browser_profile_dir(profile_dir, "chrome")
            edge_dir = browser_takeover.resolve_browser_profile_dir(profile_dir, "edge")
            has_browser_prof = (
                browser_takeover._profile_has_cookies(chrome_dir)
                or browser_takeover._profile_has_cookies(edge_dir)
            )
        acct["has_browser_profile"] = has_browser_prof
        acct["oauth_status_live"] = official_api.check_oauth_status(acct["alias"])
    return {
        "accounts": accounts,
        "client_secret_present": official_api.has_client_secret(),
    }


@router.post("")
async def create_account(body: AccountCreate) -> dict[str, Any]:
    alias = (body.alias or "").strip() or await _next_alias(body.tier)
    nlm_profile = (body.nlm_profile or alias).strip()
    chrome_dir = str(get_chrome_profile_dir(alias))
    cdp_port = await next_cdp_port()
    nb_limit, src_limit = TIER_LIMITS.get(body.tier, (100, 50))
    local_status = nlm_py_adapter.check_local_auth(nlm_profile)["status"]
    oauth_status = official_api.check_oauth_status(alias)

    try:
        new_id = await execute(
            """
            INSERT INTO accounts (
                alias, email, tier, nlm_profile, chrome_profile_dir, cdp_port,
                is_hub_master, drive_shared_ready, nlm_auth_status, official_oauth_status,
                notebook_limit, source_limit, expire_date, enabled, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alias,
                body.email.strip(),
                body.tier,
                nlm_profile,
                chrome_dir,
                cdp_port,
                1 if body.is_hub_master else 0,
                1 if body.drive_shared_ready else 0,
                local_status,
                oauth_status,
                nb_limit,
                src_limit,
                body.expire_date,
                1 if body.enabled else 0,
                body.note,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"创建账号失败（检查 alias/email 是否重复）: {exc}") from exc

    row = await fetch_one("SELECT * FROM accounts WHERE id = ?", (new_id,))
    return {"account": row}


@router.put("/{account_id}")
async def update_account(account_id: int, body: AccountUpdate) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    updates: dict[str, Any] = {}
    if body.alias is not None:
        updates["alias"] = body.alias.strip()
    if body.email is not None:
        updates["email"] = body.email.strip()
    if body.tier is not None:
        updates["tier"] = body.tier
        nb_limit, src_limit = TIER_LIMITS.get(body.tier, (100, 50))
        updates["notebook_limit"] = nb_limit
        updates["source_limit"] = src_limit
    if body.is_hub_master is not None:
        updates["is_hub_master"] = 1 if body.is_hub_master else 0
    if body.drive_shared_ready is not None:
        updates["drive_shared_ready"] = 1 if body.drive_shared_ready else 0
    if body.expire_date is not None:
        updates["expire_date"] = body.expire_date or None
    if body.enabled is not None:
        updates["enabled"] = 1 if body.enabled else 0
    if body.note is not None:
        updates["note"] = body.note

    if updates:
        set_sql = ", ".join(f"{k} = ?" for k in updates) + ", updated_at = datetime('now', 'localtime')"
        await execute(f"UPDATE accounts SET {set_sql} WHERE id = ?", (*updates.values(), account_id))

    row = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return {"account": row}


@router.delete("/{account_id}")
async def delete_account(account_id: int) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    await execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return {"deleted": True, "id": account_id}


@router.post("/{account_id}/login")
async def login_account_nlm(account_id: int) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return nlm_py_adapter.launch_nlm_login(acct["nlm_profile"], browser="chrome")


async def _launch_oauth_session(acct: dict[str, Any]) -> tuple[str, int]:
    settings = await get_settings_map()
    proxy = (settings.get("official_proxy_url") or "").strip() or None
    auth_url, port, finish_fn = official_api.start_oauth_flow_for_account(acct["alias"], proxy=proxy)
    account_id = int(acct["id"])

    async def _run_and_save() -> None:
        try:
            await asyncio.to_thread(finish_fn)
            await execute(
                "UPDATE accounts SET official_oauth_status = 'ok', updated_at = datetime('now', 'localtime') WHERE id = ?",
                (account_id,),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OAuth flow failed for %s: %s", acct["alias"], exc)
            await execute(
                "UPDATE accounts SET official_oauth_status = 'error', updated_at = datetime('now', 'localtime') WHERE id = ?",
                (account_id,),
            )

    task = asyncio.create_task(_run_and_save())
    _BG_OAUTH_TASKS.add(task)
    task.add_done_callback(_BG_OAUTH_TASKS.discard)
    return auth_url, port


@router.post("/{account_id}/oauth")
async def oauth_account_official(account_id: int) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not official_api.has_client_secret():
        raise HTTPException(
            status_code=400,
            detail="尚未放置 data/client_secret.json，请先按页面引导创建桌面端 OAuth 客户端。",
        )

    auth_url, port = await _launch_oauth_session(acct)
    return {
        "started": True,
        "alias": acct["alias"],
        "email": acct["email"],
        "auth_url": auth_url,
        "callback_port": port,
        "message": f"已为 {acct['alias']} 生成 Google 授权链接（回调端口 localhost:{port}），请在打开的页面选择 {acct['email']} 完成授权。",
    }


@router.get("/{account_id}/oauth-redirect")
async def oauth_account_redirect(account_id: int) -> Any:
    """Browser-direct 302 redirect creating a fresh OAuth session on every click."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        return HTMLResponse("<h2>账号不存在</h2>", status_code=404)
    if not official_api.has_client_secret():
        return HTMLResponse(
            "<h2>尚未上传 data/client_secret.json</h2><p>请先在控制面板「官方 API & MCP 实验室」上传 Desktop app OAuth JSON。</p>",
            status_code=400,
        )
    auth_url, _ = await _launch_oauth_session(acct)
    return RedirectResponse(url=auth_url, status_code=302, headers={"Cache-Control": "no-store"})


async def _ensure_cdp_port(acct: dict[str, Any]) -> int:
    if acct.get("cdp_port"):
        return int(acct["cdp_port"])
    port = await next_cdp_port()
    await execute("UPDATE accounts SET cdp_port = ? WHERE id = ?", (port, int(acct["id"])))
    return port


@router.post("/{account_id}/open-browser")
async def open_account_chrome(
    account_id: int,
    request: Request,
    body: OpenBrowserRequest | None = None,
) -> dict[str, Any]:
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    raw_url = body.url if body else "https://notebook.google.com/"
    browser = body.browser if body else "chrome"
    cdp_port = await _ensure_cdp_port(acct)
    try:
        login_url = browser_takeover.build_google_login_url(acct.get("email"), raw_url)
        res = await asyncio.to_thread(
            browser_takeover.open_account_browser,
            acct["alias"],
            acct["chrome_profile_dir"],
            cdp_port,
            url=login_url,
            browser=browser,
        )
        res["script_download_url"] = (
            f"/api/accounts/{account_id}/login-script.cmd?browser={res['browser']}"
        )
        return res
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/launch-cmd-login")
async def launch_account_cmd_login(
    account_id: int,
    request: Request,
    body: OpenBrowserRequest | None = None,
) -> dict[str, Any]:
    """Open an interactive Windows CMD + Chrome/Edge login terminal (or generate a 1-click script on isolated desktop)."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    raw_url = body.url if body else "https://notebook.google.com/"
    browser = body.browser if body else "chrome"
    cdp_port = await _ensure_cdp_port(acct)
    try:
        login_url = browser_takeover.build_google_login_url(acct.get("email"), raw_url)
        capture_url = str(request.base_url).rstrip("/") + f"/api/accounts/{account_id}/capture-cdp"
        res = await asyncio.to_thread(
            browser_takeover.launch_cmd_login_console,
            alias=acct["alias"],
            chrome_profile_dir=acct["chrome_profile_dir"],
            cdp_port=cdp_port,
            url=login_url,
            browser=browser,
            api_capture_url=capture_url,
        )
        res["script_download_url"] = (
            f"/api/accounts/{account_id}/login-script.cmd?browser={res['browser']}"
        )
        return res
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{account_id}/login-script.cmd")
async def download_account_login_script(
    account_id: int,
    request: Request,
    browser: str = Query(default="chrome", pattern="^(chrome|edge|auto)$"),
    url: str = Query(default="https://notebook.google.com/"),
) -> PlainTextResponse:
    """Download a self-contained Windows .cmd login script that runs directly on WinSta0\\Default."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    cdp_port = await _ensure_cdp_port(acct)
    try:
        login_url = browser_takeover.build_google_login_url(acct.get("email"), url)
        capture_url = str(request.base_url).rstrip("/") + f"/api/accounts/{account_id}/capture-cdp"
        script = await asyncio.to_thread(
            browser_takeover.generate_windows_login_cmd_script,
            alias=acct["alias"],
            chrome_profile_dir=acct["chrome_profile_dir"],
            cdp_port=cdp_port,
            url=login_url,
            browser=browser,
            api_capture_url=capture_url,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    safe_alias = re.sub(r"[^A-Za-z0-9._-]+", "_", acct["alias"]).strip("_") or f"account_{account_id}"
    return PlainTextResponse(
        content=script,
        media_type="application/octet-stream; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="login_{safe_alias}.cmd"'},
    )


@router.post("/{account_id}/refresh-session")
async def refresh_account_session(account_id: int) -> dict[str, Any]:
    """Silently renew short-lived NotebookLM tokens using the account's persistent Chrome/Edge profile."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    res = await browser_takeover.silent_refresh_cookies_from_profile(
        acct["chrome_profile_dir"],
        cdp_port=acct.get("cdp_port"),
        target_url="https://notebook.google.com/",
    )
    cookies = res.get("cookies") or []
    if not res.get("refreshed") or not cookies:
        return {
            "ok": False,
            "refreshed": False,
            "reason": res.get("reason") or "no_browser_profile",
            "detail": "未能从独立 Chrome/Edge 配置目录静默续期（可能尚未通过独立窗口登录，或 Google 账号需要重新输入密码）。",
        }
    try:
        saved = nlm_py_adapter.import_cookies_into_profile(
            acct["nlm_profile"],
            cookies,
            email=acct["email"],
            authuser=0,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "refreshed": False,
            "reason": "login_required",
            "final_url": res.get("final_url"),
            "detail": f"独立浏览器内的 Google 会话已失效，需要重新登录: {exc}",
        }
    quota = await poll_one_account(account_id, auto_refresh=False)
    return {
        "ok": quota.get("nlm_auth_status") == "ok",
        "refreshed": True,
        "source": res.get("source"),
        "saved": saved,
        "quota": quota,
    }


@router.post("/{account_id}/poll-quota")
async def poll_account_quota(account_id: int) -> dict[str, Any]:
    try:
        return await poll_one_account(account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/poll-all")
async def poll_all() -> dict[str, Any]:
    results = await poll_all_accounts()
    return {"results": results, "count": len(results)}


@router.post("/{account_id}/capture-cdp")
async def capture_cdp_session(account_id: int, silent: bool = False) -> dict[str, Any]:
    """Capture Google/NotebookLM cookies from this account's isolated CDP Chrome/Edge without closing it."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    cdp_port = int(acct.get("cdp_port") or 9222)
    try:
        raw_cookies = await browser_takeover.capture_cdp_cookies(cdp_port)
        if silent:
            names = {
                str(c.get("name") or "").strip()
                for c in raw_cookies
                if isinstance(c, dict) and str(c.get("value") or "").strip()
            }
            has_app_osid = any(
                str(c.get("name") or "").strip() in ("OSID", "__Secure-OSID", "__Host-OSID")
                and str(c.get("domain") or "").lstrip(".").lower()
                in ("notebook.google.com", "notebooklm.google.com")
                for c in raw_cookies
                if isinstance(c, dict) and str(c.get("value") or "").strip()
            )
            has_lsid_binding = {"LSID", "APISID", "SAPISID"} <= names
            if (set(MINIMUM_REQUIRED_COOKIES) - names) or (not has_app_osid and not has_lsid_binding):
                return {
                    "ok": False,
                    "waiting": True,
                    "detail": "等待在弹出的浏览器窗口中登录 Google 并进入 notebook.google.com 主页…",
                }
        saved = nlm_py_adapter.import_cookies_into_profile(
            acct["nlm_profile"], raw_cookies, email=acct["email"], authuser=0
        )
    except (RuntimeError, ValueError) as exc:
        if silent:
            return {"ok": False, "waiting": True, "detail": str(exc)}
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    quota = await poll_one_account(account_id)
    return {
        "ok": quota.get("nlm_auth_status") == "ok",
        "waiting": False,
        "saved": saved,
        "quota": quota,
    }


@router.post("/{account_id}/import-cookies-raw")
async def import_cookies_raw(account_id: int, body: ImportCookiesRawRequest) -> dict[str, Any]:
    """Import pasted cURL (bash/cmd), raw Cookie header, or JSON cookies into this account's Ch2 profile."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        cookies, meta = nlm_py_adapter.parse_raw_cookie_bundle(body.raw_input)
        saved = nlm_py_adapter.import_cookies_into_profile(
            acct["nlm_profile"],
            cookies,
            email=acct["email"],
            authuser=meta.get("authuser"),
        )
        saved["source_host"] = meta.get("source_host")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    quota = await poll_one_account(account_id)
    if quota.get("nlm_auth_status") != "ok" and meta.get("source_host") == "notebooklm.google.com":
        prev_err = (quota.get("error_msg") or "").strip()
        hint = "提示：Google 已迁移主域名至 notebook.google.com，若浏览器已自动跳转，请复制跳转后 notebook.google.com 页面请求对应的 cURL。"
        quota["error_msg"] = f"{prev_err}\n{hint}" if prev_err else hint
    return {"ok": quota.get("nlm_auth_status") == "ok", "saved": saved, "quota": quota}


@router.post("/{account_id}/import-browser-cookies")
async def import_browser_cookies(account_id: int) -> dict[str, Any]:
    """Extract cookies from this account's running isolated Chrome (CDP) first, or fallback to local Chrome."""
    import subprocess
    import sys

    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if acct is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    cdp_port = int(acct.get("cdp_port") or 9222)
    if await asyncio.to_thread(browser_takeover._is_cdp_alive, cdp_port):
        try:
            raw_cookies = await browser_takeover.capture_cdp_cookies(cdp_port)
            saved = nlm_py_adapter.import_cookies_into_profile(
                acct["nlm_profile"], raw_cookies, email=acct["email"], authuser=0
            )
        except (RuntimeError, ValueError) as exc:
            return {
                "ok": False,
                "source": "cdp",
                "status": "unauthenticated",
                "detail": str(exc),
            }
        quota = await poll_one_account(account_id)
        return {
            "ok": quota.get("nlm_auth_status") == "ok",
            "source": "cdp",
            "status": quota.get("nlm_auth_status"),
            "saved": saved,
            "quota": quota,
            "detail": quota.get("error_msg"),
        }

    from ..core.win_runtime import is_frozen

    if is_frozen():
        cmd = [
            sys.executable,
            "--nlm-cli",
            "-p",
            acct["nlm_profile"],
            "login",
            "--browser-cookies",
            "chrome",
            "--account",
            acct["email"],
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "notebooklm",
            "-p",
            acct["nlm_profile"],
            "login",
            "--browser-cookies",
            "chrome",
            "--account",
            acct["email"],
        ]
    proc = await asyncio.to_thread(
        subprocess.run,
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    local = nlm_py_adapter.check_local_auth(acct["nlm_profile"])
    if local["status"] == "ok":
        await execute(
            "UPDATE accounts SET nlm_auth_status = 'ok', updated_at = datetime('now', 'localtime') WHERE id = ?",
            (account_id,),
        )
        return {"ok": True, "source": "rookie", "status": "ok", "output": (proc.stdout or proc.stderr)[:500]}
    raw_detail = (proc.stderr or proc.stdout or "").strip()
    if "database is locked" in raw_detail.lower():
        detail = (
            "日常 Chrome 正在运行导致本地 Cookie 库被系统锁定。无需关闭 Chrome，请点「弹出专属Chrome」登录后再点同步，或用「一键登录会话」5 秒粘贴 cURL。\n原始提示: "
            + raw_detail
        )[:500]
    else:
        detail = (
            raw_detail
            or "无法自动提取 Chrome Cookie，请改用「一键登录会话」（专属 Chrome CDP 同步或粘贴 cURL）。"
        )[:500]
    return {
        "ok": False,
        "source": "rookie",
        "status": local["status"],
        "detail": detail,
    }


@router.post("/import-existing-profiles")
async def import_existing_profiles() -> dict[str, Any]:
    discovered = nlm_py_adapter.discover_existing_profiles()
    imported: list[str] = []
    for item in discovered:
        prof = item["profile"]
        existing = await fetch_one(
            "SELECT id FROM accounts WHERE nlm_profile = ? OR alias = ?",
            (prof, prof),
        )
        if existing is not None:
            continue
        cdp_port = await next_cdp_port()
        chrome_dir = str(get_chrome_profile_dir(prof))
        await execute(
            """
            INSERT INTO accounts (
                alias, email, tier, nlm_profile, chrome_profile_dir, cdp_port,
                nlm_auth_status, official_oauth_status, note
            ) VALUES (?, ?, 'ultra', ?, ?, ?, ?, ?, '从本地 .notebooklm/profiles 导入')
            """,
            (
                prof,
                f"{prof}@pending.local",
                prof,
                chrome_dir,
                cdp_port,
                item["status"],
                official_api.check_oauth_status(prof),
            ),
        )
        imported.append(prof)
    return {"discovered": discovered, "imported": imported}
