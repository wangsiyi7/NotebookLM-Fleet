"""Async SQLite data access layer for NotebookLM-Fleet."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .config import Settings, get_chrome_profile_dir, get_db_path, get_settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

TIER_LIMITS: dict[str, tuple[int, int]] = {
    "free": (100, 50),
    "plus": (200, 100),
    "pro": (500, 300),
    "ultra": (500, 600),
}


def default_settings_map(settings: Settings | None = None) -> dict[str, str]:
    """Default configuration keys stored in app_settings."""
    cfg = settings or get_settings()
    return {
        "workspace_root": cfg.workspace_root,
        "drive_hub_root": cfg.drive_hub_root,
        "drive_library_root": cfg.drive_library_root,
        "tier_policy": "conserve_ultra",
        "safety_buffer_pct": str(int(cfg.safety_buffer_pct)),
        "poll_interval_min": str(cfg.poll_interval_min),
        "task_delay_min_sec": "8",
        "task_delay_max_sec": "18",
        "coupling_mode": "shared_with_drive_fallback",
        "official_api_project_id": "",
        "official_api_enabled": "false",
        "official_proxy_url": "",
        "official_mcp_last_probe_json": "",
        "default_language": "zh-Hans",
        "supabase_enabled": "false",
        "supabase_url": "",
        "last_backup_at": "",
    }


@asynccontextmanager
async def get_db(db_path: Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """Open an async SQLite connection with row dicts and foreign keys enabled."""
    target = db_path or get_db_path()
    conn = await aiosqlite.connect(str(target), timeout=30.0)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA busy_timeout = 30000;")
        yield conn
    finally:
        await conn.close()


async def init_db(db_path: Path | None = None, settings: Settings | None = None) -> Path:
    """Create tables if missing, seed default settings and the primary Drive Hub Ultra account."""
    cfg = settings or get_settings()
    target = db_path or get_db_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    async with get_db(target) as conn:
        await conn.executescript(schema_sql)

        # Idempotent schema migration for existing SQLite databases created prior to v0.2.0.
        col_cur = await conn.execute("PRAGMA table_info(notebooks)")
        existing_cols = {row["name"] for row in await col_cur.fetchall()}
        migrations = {
            "role": "ALTER TABLE notebooks ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'",
            "studio_stats_json": "ALTER TABLE notebooks ADD COLUMN studio_stats_json TEXT NOT NULL DEFAULT '{}'",
            "remote_artifacts_json": "ALTER TABLE notebooks ADD COLUMN remote_artifacts_json TEXT NOT NULL DEFAULT '[]'",
            "remote_sources_json": "ALTER TABLE notebooks ADD COLUMN remote_sources_json TEXT NOT NULL DEFAULT '[]'",
            "studio_synced_at": "ALTER TABLE notebooks ADD COLUMN studio_synced_at TEXT",
        }
        for col_name, ddl in migrations.items():
            if col_name not in existing_cols:
                await conn.execute(ddl)

        for key, val in default_settings_map(cfg).items():
            await conn.execute(
                "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                (key, val),
            )

        # Seed a starter Ultra hub account only on an empty ledger (never hardcode personal emails).
        cur = await conn.execute("SELECT COUNT(*) AS cnt FROM accounts")
        cnt_row = await cur.fetchone()
        if int((cnt_row or {"cnt": 0})["cnt"]) == 0:
            chrome_dir = str(get_chrome_profile_dir("ultra-1"))
            await conn.execute(
                """
                INSERT INTO accounts (
                    alias, email, tier, nlm_profile, chrome_profile_dir, cdp_port,
                    is_hub_master, drive_shared_ready, notebook_limit, source_limit,
                    enabled, note
                ) VALUES (?, ?, 'ultra', ?, ?, ?, 1, 1, 500, 600, 1, ?)
                """,
                (
                    "ultra-1",
                    cfg.default_master_email,
                    "ultra-1",
                    chrome_dir,
                    9222,
                    "默认主盘账号（请在控制面板修改为你的真实 Gmail）",
                ),
            )
        await conn.commit()
    return target


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


async def fetch_all(
    sql: str, params: tuple[Any, ...] = (), *, db_path: Path | None = None
) -> list[dict[str, Any]]:
    async with get_db(db_path) as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def fetch_one(
    sql: str, params: tuple[Any, ...] = (), *, db_path: Path | None = None
) -> dict[str, Any] | None:
    async with get_db(db_path) as conn:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        return _row_to_dict(row)


async def execute(
    sql: str, params: tuple[Any, ...] = (), *, db_path: Path | None = None
) -> int:
    async with get_db(db_path) as conn:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return int(cur.lastrowid or 0)


async def get_settings_map(*, db_path: Path | None = None) -> dict[str, str]:
    rows = await fetch_all("SELECT key, value FROM app_settings", db_path=db_path)
    merged = default_settings_map()
    for row in rows:
        merged[row["key"]] = row["value"]
    return merged


async def set_settings_map(updates: dict[str, Any], *, db_path: Path | None = None) -> dict[str, str]:
    async with get_db(db_path) as conn:
        for k, v in updates.items():
            await conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, datetime('now', 'localtime'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now', 'localtime')
                """,
                (str(k), str(v)),
            )
        await conn.commit()
    return await get_settings_map(db_path=db_path)


async def get_accounts_with_latest_quota(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return every account joined with its most recent quota snapshot."""
    sql = """
    SELECT
        a.*,
        q.id AS quota_id,
        q.status AS quota_status,
        q.five_hour_used_pct,
        q.five_hour_remaining_pct,
        q.five_hour_resets_at,
        q.weekly_used_pct,
        q.weekly_remaining_pct,
        q.weekly_resets_at,
        q.is_exhausted,
        q.active_window,
        q.actions_json,
        q.error_msg AS quota_error_msg,
        q.captured_at AS quota_captured_at
    FROM accounts a
    LEFT JOIN quota_snapshots q
        ON q.id = (
            SELECT q2.id
            FROM quota_snapshots q2
            WHERE q2.account_id = a.id
            ORDER BY q2.id DESC
            LIMIT 1
        )
    ORDER BY a.is_hub_master DESC, a.id ASC
    """
    rows = await fetch_all(sql, db_path=db_path)
    for row in rows:
        raw_actions = row.get("actions_json") or "[]"
        try:
            row["actions"] = json.loads(raw_actions)
        except json.JSONDecodeError:
            row["actions"] = []
    return rows


async def get_account_by_id_or_alias(
    identifier: int | str, *, db_path: Path | None = None
) -> dict[str, Any] | None:
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        return await fetch_one("SELECT * FROM accounts WHERE id = ?", (int(identifier),), db_path=db_path)
    return await fetch_one(
        "SELECT * FROM accounts WHERE alias = ? OR email = ? OR nlm_profile = ?",
        (str(identifier), str(identifier), str(identifier)),
        db_path=db_path,
    )


async def next_cdp_port(*, db_path: Path | None = None) -> int:
    row = await fetch_one("SELECT COALESCE(MAX(cdp_port), 9221) AS max_port FROM accounts", db_path=db_path)
    return max(9222, int((row or {}).get("max_port") or 9221) + 1)
