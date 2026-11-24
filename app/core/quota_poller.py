"""Background and on-demand quota polling across all NotebookLM accounts."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..adapters import browser_takeover, nlm_py_adapter, official_api
from ..config import get_db_path
from ..db import execute, fetch_all, fetch_one, get_settings_map, set_settings_map
from .drive_hub import backup_sqlite_to_drive

logger = logging.getLogger(__name__)


async def _try_self_heal_session(
    acct: dict[str, Any],
    adapter: Any,
) -> tuple[bool, dict[str, Any] | None]:
    """Attempt silent cookie refresh via this account's persistent Chrome/Edge profile."""
    profile_dir = (acct.get("chrome_profile_dir") or "").strip()
    if not profile_dir or not hasattr(adapter, "import_cookies_into_profile"):
        return False, None
    try:
        refreshed = await browser_takeover.silent_refresh_cookies_from_profile(
            profile_dir,
            cdp_port=acct.get("cdp_port"),
            target_url="https://notebook.google.com/",
        )
        cookies = refreshed.get("cookies") or []
        if not refreshed.get("refreshed") or not cookies:
            return False, None
        saved = adapter.import_cookies_into_profile(
            acct["nlm_profile"],
            cookies,
            email=acct.get("email"),
            authuser=0,
        )
        logger.info("Silent session refresh succeeded for %s", acct.get("alias"))
        return True, saved
    except Exception as exc:  # noqa: BLE001
        logger.debug("Silent session refresh skipped/failed for %s: %s", acct.get("alias"), exc)
        return False, None


async def poll_one_account(
    account_id: int,
    *,
    db_path: Path | None = None,
    adapter: Any = nlm_py_adapter,
    auto_refresh: bool = True,
) -> dict[str, Any]:
    """Check auth status and poll live 5-hour & weekly quota for one account."""
    acct = await fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,), db_path=db_path)
    if acct is None:
        raise ValueError(f"账号不存在: id={account_id}")

    oauth_status = official_api.check_oauth_status(acct["alias"])
    local_auth = adapter.check_local_auth(acct["nlm_profile"])
    session_refreshed = False

    if (
        (not local_auth.get("exists") or local_auth.get("status") == "unauthenticated")
        and auto_refresh
    ):
        healed, _ = await _try_self_heal_session(acct, adapter)
        if healed:
            session_refreshed = True
            local_auth = adapter.check_local_auth(acct["nlm_profile"])

    if not local_auth.get("exists") or local_auth.get("status") == "unauthenticated":
        await execute(
            """
            UPDATE accounts
               SET nlm_auth_status = 'unauthenticated',
                   official_oauth_status = ?,
                   updated_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (oauth_status, account_id),
            db_path=db_path,
        )
        return {
            "account_id": account_id,
            "alias": acct["alias"],
            "nlm_auth_status": "unauthenticated",
            "official_oauth_status": oauth_status,
            "session_refreshed": session_refreshed,
            "status": "unauthenticated",
        }

    usage = await adapter.poll_account_usage(acct["nlm_profile"])
    auth_status = usage.get("auth_status") or "error"

    if auth_status == "expired" and auto_refresh:
        healed, _ = await _try_self_heal_session(acct, adapter)
        if healed:
            session_refreshed = True
            usage = await adapter.poll_account_usage(acct["nlm_profile"])
            auth_status = usage.get("auth_status") or "error"

    if auth_status == "ok":
        actions_json = json.dumps(usage.get("actions") or [], ensure_ascii=False)
        await execute(
            """
            INSERT INTO quota_snapshots (
                account_id, status, five_hour_used_pct, five_hour_remaining_pct,
                five_hour_resets_at, weekly_used_pct, weekly_remaining_pct,
                weekly_resets_at, is_exhausted, active_window, actions_json, error_msg
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                account_id,
                usage.get("status", "ready"),
                usage.get("five_hour_used_pct"),
                usage.get("five_hour_remaining_pct"),
                usage.get("five_hour_resets_at"),
                usage.get("weekly_used_pct"),
                usage.get("weekly_remaining_pct"),
                usage.get("weekly_resets_at"),
                1 if usage.get("is_exhausted") else 0,
                usage.get("active_window"),
                actions_json,
            ),
            db_path=db_path,
        )
        nb_limit = usage.get("notebook_limit") or acct.get("notebook_limit")
        src_limit = usage.get("source_limit") or acct.get("source_limit")
        await execute(
            """
            UPDATE accounts
               SET nlm_auth_status = 'ok',
                   official_oauth_status = ?,
                   notebook_limit = ?,
                   source_limit = ?,
                   updated_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (oauth_status, nb_limit, src_limit, account_id),
            db_path=db_path,
        )
        effective_auth_status = "ok"
    else:
        err_msg = usage.get("error_msg") or "Unknown polling error"
        await execute(
            """
            INSERT INTO quota_snapshots (account_id, status, error_msg)
            VALUES (?, 'error', ?)
            """,
            (account_id, err_msg),
            db_path=db_path,
        )
        # Preserve 'ok' across transient API/network errors; only downgrade on real auth expiry.
        effective_auth_status = (
            "ok"
            if auth_status == "error" and acct.get("nlm_auth_status") == "ok"
            else auth_status
        )
        await execute(
            """
            UPDATE accounts
               SET nlm_auth_status = ?,
                   official_oauth_status = ?,
                   updated_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (effective_auth_status, oauth_status, account_id),
            db_path=db_path,
        )

    return {
        "account_id": account_id,
        "alias": acct["alias"],
        "nlm_auth_status": effective_auth_status,
        "official_oauth_status": oauth_status,
        "session_refreshed": session_refreshed,
        **usage,
    }


async def poll_all_accounts(
    *,
    db_path: Path | None = None,
    adapter: Any = nlm_py_adapter,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    """Poll all accounts sequentially with gentle spacing and trigger daily Drive backup."""
    where = "" if include_disabled else "WHERE enabled = 1"
    accounts = await fetch_all(f"SELECT id, alias FROM accounts {where} ORDER BY id ASC", db_path=db_path)
    results = []
    for idx, acct in enumerate(accounts):
        try:
            res = await poll_one_account(acct["id"], db_path=db_path, adapter=adapter)
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed polling account %s: %s", acct["alias"], exc)
            results.append({"account_id": acct["id"], "alias": acct["alias"], "status": "error", "error_msg": str(exc)})
        if idx < len(accounts) - 1:
            await asyncio.sleep(0.3)

    await _maybe_run_daily_backup(db_path=db_path)
    return results


async def _maybe_run_daily_backup(*, db_path: Path | None = None) -> None:
    try:
        settings = await get_settings_map(db_path=db_path)
        hub_root = settings.get("drive_hub_root", "")
        if not hub_root or not Path(hub_root).parent.exists():
            return
        last_str = (settings.get("last_backup_at") or "").strip()
        due = True
        if last_str:
            try:
                last_dt = datetime.fromisoformat(last_str)
                due = (datetime.now() - last_dt) >= timedelta(hours=24)
            except ValueError:
                due = True
        if due:
            resolved_db = db_path or get_db_path()
            await asyncio.to_thread(backup_sqlite_to_drive, resolved_db, hub_root, keep=14)
            await set_settings_map({"last_backup_at": datetime.now().isoformat(timespec="seconds")}, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Daily Drive backup skipped: %s", exc)


async def quota_poller_loop(stop_event: asyncio.Event, *, db_path: Path | None = None) -> None:
    """Periodic background quota polling loop."""
    while not stop_event.is_set():
        try:
            await poll_all_accounts(db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Quota poller iteration failed: %s", exc)

        settings = await get_settings_map(db_path=db_path)
        interval_min = max(2, int(settings.get("poll_interval_min") or 12))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_min * 60)
        except asyncio.TimeoutError:
            continue
