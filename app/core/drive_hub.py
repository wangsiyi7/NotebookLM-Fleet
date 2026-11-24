"""Google Drive Hub management, safe library browsing, and SQLite backup."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_topic_name(name: str) -> str:
    """Make a topic name safe for Windows/Google Drive directory creation."""
    cleaned = _INVALID_WIN_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    if not cleaned or cleaned in (".", ".."):
        raise ValueError(f"无效的主题名称: {name!r}")
    return cleaned[:120]


def ensure_hub_structure(hub_root: str | Path) -> dict[str, Path]:
    """Ensure the NLM_Hub folder hierarchy exists on Google Drive."""
    root = Path(hub_root)
    paths = {
        "root": root,
        "system": root / "00_System",
        "backups": root / "00_System" / "backups",
        "inbox": root / "01_Inbox",
        "topics": root / "02_Topics",
        "exports": root / "03_Exports",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def ensure_topic_folders(hub_root: str | Path, topic_name: str) -> dict[str, Path]:
    """Create Sources/ and Artifacts/ subfolders for a topic under 02_Topics."""
    hub = ensure_hub_structure(hub_root)
    safe = sanitize_topic_name(topic_name)
    topic_dir = (hub["topics"] / safe).resolve()
    if not topic_dir.is_relative_to(hub["topics"].resolve()):
        raise ValueError(f"主题路径越界: {topic_name!r}")
    sources_dir = topic_dir / "Sources"
    artifacts_dir = topic_dir / "Artifacts"
    sources_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    return {"topic": topic_dir, "sources": sources_dir, "artifacts": artifacts_dir}


def resolve_safe_subpath(root: str | Path, subpath: str = "") -> Path:
    """Resolve a user-supplied subpath strictly within root (prevents path traversal)."""
    base = Path(root).resolve()
    rel = (subpath or "").lstrip("/\\")
    target = (base / rel).resolve() if rel else base
    if target != base and not target.is_relative_to(base):
        raise ValueError("禁止访问根目录以外的路径")
    return target


def list_directory(root: str | Path, subpath: str = "") -> dict[str, Any]:
    """Safely list folders and non-hidden files under root[/subpath] via fast scandir."""
    import os

    base = Path(root)
    if not base.exists():
        return {"root": str(base), "subpath": subpath, "exists": False, "entries": []}
    target = resolve_safe_subpath(base, subpath)
    if not target.exists():
        return {"root": str(base), "subpath": subpath, "exists": False, "entries": []}

    clean_sub = subpath.replace("\\", "/").strip("/")
    items: list[dict[str, Any]] = []
    with os.scandir(target) as it:
        for child in it:
            name = child.name
            if name.startswith(".") or name in ("$RECYCLE.BIN", "desktop.ini", "Thumbs.db"):
                continue
            try:
                is_dir = child.is_dir(follow_symlinks=False)
                stat = child.stat(follow_symlinks=False)
                rel = f"{clean_sub}/{name}".lstrip("/")
                items.append(
                    {
                        "name": name,
                        "path": child.path,
                        "relative_path": rel,
                        "is_dir": is_dir,
                        "size_bytes": 0 if is_dir else int(stat.st_size),
                        "extension": "" if is_dir else Path(name).suffix.lower(),
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            except OSError:
                continue

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {
        "root": str(base),
        "current_path": str(target),
        "subpath": subpath,
        "exists": True,
        "entries": items,
    }


def scan_inbox(hub_root: str | Path) -> list[dict[str, Any]]:
    """Return files currently sitting in 01_Inbox."""
    inbox = Path(hub_root) / "01_Inbox"
    if not inbox.exists():
        return []
    res = list_directory(inbox)
    return res["entries"]


def save_text_export(
    hub_root: str | Path,
    topic_name: str | None,
    filename: str,
    content: str,
) -> Path:
    """Save Markdown chat/report output into topic Artifacts/ or 03_Exports/."""
    safe_name = _INVALID_WIN_CHARS.sub("_", filename.strip()) or "export.md"
    if topic_name:
        folders = ensure_topic_folders(hub_root, topic_name)
        dest_dir = folders["artifacts"]
    else:
        dest_dir = ensure_hub_structure(hub_root)["exports"]
    dest = (dest_dir / safe_name).resolve()
    if not dest.is_relative_to(dest_dir.resolve()):
        raise ValueError("非法导出文件名")
    dest.write_text(content, encoding="utf-8")
    return dest


def backup_sqlite_to_drive(
    db_path: str | Path, hub_root: str | Path, *, keep: int = 14
) -> dict[str, Any]:
    """Create an online consistent SQLite backup inside 00_System/backups and prune old ones."""
    src_path = Path(db_path)
    if not src_path.exists():
        raise FileNotFoundError(f"数据库不存在: {src_path}")

    backups_dir = ensure_hub_structure(hub_root)["backups"]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = backups_dir / f"fleet_{stamp}.db"

    src_conn = sqlite3.connect(str(src_path), timeout=30.0)
    dst_conn = sqlite3.connect(str(dest_path), timeout=30.0)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    all_backups = sorted(backups_dir.glob("fleet_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in all_backups[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass

    return {
        "backup_path": str(dest_path),
        "size_bytes": dest_path.stat().st_size,
        "retained_count": min(len(all_backups), keep),
        "pruned_count": removed,
    }
