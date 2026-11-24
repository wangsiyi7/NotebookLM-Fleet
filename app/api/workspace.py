"""Whole-workspace & Google Drive safe file browser and text preview endpoints."""

from __future__ import annotations

import asyncio
import mimetypes
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..config import get_settings
from ..core.drive_hub import resolve_safe_subpath
from ..db import get_settings_map

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "$RECYCLE.BIN",
        ".Encrypted",
        ".shortcut-targets-by-id",
        "oauth_tokens",
        "chrome_profiles",
    }
)

SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        "client_secret.json",
        "storage_state.json",
        "master_token.json",
        "baidupan_auth.json",
        "auth.json",
        "runtime_port.json",
    }
)

TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".htm",
        ".css",
        ".json",
        ".sql",
        ".yaml",
        ".yml",
        ".csv",
        ".toml",
        ".ps1",
        ".sh",
        ".xml",
        ".ini",
        ".cfg",
        ".log",
    }
)

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
PDF_EXTENSIONS = frozenset({".pdf"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".ogg", ".aac"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mov"})
CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".ps1",
        ".sh",
        ".css",
        ".xml",
        ".ini",
        ".cfg",
    }
)
INLINE_UNSAFE_EXTENSIONS = frozenset({".html", ".htm", ".svg", ".xhtml", ".mhtml"})
INLINE_SAFE_EXTENSIONS = (
    IMAGE_EXTENSIONS | PDF_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS | TEXT_EXTENSIONS
) - INLINE_UNSAFE_EXTENSIONS


async def get_allowed_roots() -> dict[str, dict[str, Any]]:
    """Return configured workspace and Google Drive roots."""
    cfg = get_settings()
    settings_map = await get_settings_map()
    ws_path = settings_map.get("workspace_root") or cfg.workspace_root
    lib_path = settings_map.get("drive_library_root") or cfg.drive_library_root
    hub_path = settings_map.get("drive_hub_root") or cfg.drive_hub_root
    return {
        "workspace": {"id": "workspace", "label": "工作目录", "path": ws_path},
        "drive_library": {"id": "drive_library", "label": "Google 云盘", "path": lib_path},
        "drive_hub": {"id": "drive_hub", "label": "NLM_Hub 中枢", "path": hub_path},
    }


def is_sensitive_path(path: Path) -> bool:
    """Return True if path points to credentials, session cookies, or VCS internals."""
    name_lower = path.name.lower()
    if name_lower in SENSITIVE_FILENAMES or (name_lower.startswith(".env.") and name_lower != ".env.example"):
        return True
    if path.suffix.lower() in (".pem", ".key", ".pfx"):
        return True
    lowered_parts = {p.lower() for p in path.parts}
    if lowered_parts & {"oauth_tokens", "chrome_profiles", ".git"}:
        return True
    return False


def is_allowed_file_for_ingest(file_path: str | Path, roots: dict[str, dict[str, Any]]) -> Path:
    """Ensure an arbitrary full path lies within one of the allowed roots and is not sensitive."""
    resolved = Path(file_path).resolve()
    if is_sensitive_path(resolved):
        raise PermissionError(f"出于安全策略禁止读取凭据/敏感文件: {resolved.name}")
    for root_info in roots.values():
        root_dir = Path(root_info["path"]).resolve()
        if resolved == root_dir or resolved.is_relative_to(root_dir):
            return resolved
    raise ValueError(f"文件路径不在允许的工作区或云盘根目录内: {file_path}")


def _scandir_entry_payload(entry: os.DirEntry[str], base_resolved: Path, rel_prefix: str) -> dict[str, Any]:
    """Build file/folder metadata from a single WIN32_FIND_DATA scandir entry without extra DriveFS RPCs."""
    is_dir = entry.is_dir(follow_symlinks=False)
    stat = entry.stat(follow_symlinks=False)
    rel = f"{rel_prefix}/{entry.name}".lstrip("/")
    ext = "" if is_dir else Path(entry.name).suffix.lower()
    is_text = (not is_dir) and (ext in TEXT_EXTENSIONS or entry.name.lower() == ".env.example")
    return {
        "name": entry.name,
        "path": entry.path,
        "relative_path": rel,
        "is_dir": is_dir,
        "size_bytes": 0 if is_dir else int(stat.st_size),
        "extension": ext,
        "is_text": is_text,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def _scan_tree_sync(base: Path, target: Path, clean_subpath: str, query: str) -> list[dict[str, Any]]:
    base_resolved = base.resolve()
    entries: list[dict[str, Any]] = []

    if query:
        visited = 0
        stack: list[tuple[Path, str, int]] = [(target, clean_subpath, 0)]
        while stack and len(entries) < 100 and visited < 1500:
            cur_dir, prefix, depth = stack.pop(0)
            try:
                with os.scandir(cur_dir) as it:
                    children = list(it)
            except OSError:
                continue
            for child in children:
                visited += 1
                name = child.name
                if name in SKIP_DIRS or (name.startswith(".") and name not in (".env.example", ".agents")):
                    continue
                if name in ("desktop.ini", "Thumbs.db") or is_sensitive_path(Path(child.path)):
                    continue
                try:
                    is_dir = child.is_dir(follow_symlinks=False)
                    if query in name.lower():
                        entries.append(_scandir_entry_payload(child, base_resolved, prefix))
                    if is_dir and depth < 4:
                        next_prefix = f"{prefix}/{name}".lstrip("/")
                        stack.append((Path(child.path), next_prefix, depth + 1))
                except OSError:
                    continue
                if len(entries) >= 100:
                    break
    else:
        with os.scandir(target) as it:
            for child in it:
                name = child.name
                if name in SKIP_DIRS or (name.startswith(".") and name not in (".env.example", ".agents")):
                    continue
                if name in ("desktop.ini", "Thumbs.db") or is_sensitive_path(Path(child.path)):
                    continue
                try:
                    entries.append(_scandir_entry_payload(child, base_resolved, clean_subpath))
                except OSError:
                    continue
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    return entries


@router.get("/roots")
async def list_workspace_roots() -> dict[str, Any]:
    roots = await get_allowed_roots()
    items = []
    for item in roots.values():
        p = Path(item["path"])
        exists = await asyncio.to_thread(p.exists)
        items.append({**item, "exists": exists})
    return {"roots": items}


@router.get("/tree")
async def get_workspace_tree(
    root: str = Query(default="workspace"),
    subpath: str = Query(default=""),
    q: str = Query(default=""),
) -> dict[str, Any]:
    roots = await get_allowed_roots()
    if root not in roots:
        raise HTTPException(status_code=400, detail=f"未知根目录: {root}")

    base = Path(roots[root]["path"])
    if not await asyncio.to_thread(base.exists):
        return {
            "root": root,
            "root_path": str(base),
            "subpath": subpath,
            "exists": False,
            "entries": [],
        }

    try:
        target = resolve_safe_subpath(base, subpath)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_sensitive_path(target):
        raise HTTPException(status_code=403, detail="不允许浏览敏感目录")
    if not await asyncio.to_thread(target.exists) or not await asyncio.to_thread(target.is_dir):
        return {
            "root": root,
            "root_path": str(base),
            "subpath": subpath,
            "exists": False,
            "entries": [],
        }

    clean_subpath = subpath.replace("\\", "/").strip("/")
    query = q.strip().lower()
    try:
        entries = await asyncio.to_thread(_scan_tree_sync, base, target, clean_subpath, query)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取目录失败: {exc}") from exc

    return {
        "root": root,
        "root_label": roots[root]["label"],
        "root_path": str(base),
        "current_path": str(target),
        "subpath": clean_subpath,
        "exists": True,
        "entries": entries,
    }


def _classify_preview_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".md":
        return "markdown"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in CODE_EXTENSIONS:
        return "code"
    if ext in TEXT_EXTENSIONS or path.name.lower() == ".env.example":
        return "text"
    return "binary"


def _read_capped_bytes(target: Path, max_bytes: int = 65536) -> tuple[bytes, bool]:
    with target.open("rb") as fh:
        chunk = fh.read(max_bytes + 1)
    if len(chunk) > max_bytes:
        return chunk[:max_bytes], True
    return chunk, False


def _build_file_preview_sync(target: Path, raw_query: str) -> dict[str, Any]:
    size = target.stat().st_size
    mime, _ = mimetypes.guess_type(target.name)
    ext = target.suffix.lower()
    kind = _classify_preview_kind(target)
    is_text = kind in {"markdown", "code", "text"}
    can_raw = (ext in INLINE_SAFE_EXTENSIONS) and (ext not in INLINE_UNSAFE_EXTENSIONS)
    raw_url = f"/api/workspace/raw?{raw_query}" if can_raw else None

    if not is_text:
        return {
            "name": target.name,
            "full_path": str(target),
            "size_bytes": size,
            "mime_type": mime or "application/octet-stream",
            "preview_kind": kind,
            "raw_url": raw_url,
            "is_binary": True,
            "truncated": False,
            "content": (
                f"[{kind.upper()} 文件: {target.name} · {size} 字节]\n\n"
                "支持在页面中直接预览/播放，或通过 Official REST API (<=200MB) / notebooklm-py 上传至笔记本。"
            ),
        }

    raw, truncated = _read_capped_bytes(target, max_bytes=65536)
    snippet = raw.decode("utf-8", errors="replace")
    return {
        "name": target.name,
        "full_path": str(target),
        "size_bytes": size,
        "mime_type": mime or "text/plain",
        "preview_kind": kind,
        "raw_url": raw_url,
        "is_binary": False,
        "truncated": truncated,
        "content": snippet,
    }


@router.get("/preview")
async def preview_workspace_file(
    root: str = Query(default="workspace"),
    subpath: str = Query(...),
) -> dict[str, Any]:
    roots = await get_allowed_roots()
    if root not in roots:
        raise HTTPException(status_code=400, detail=f"未知根目录: {root}")
    base = Path(roots[root]["path"])
    try:
        target = resolve_safe_subpath(base, subpath)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if is_sensitive_path(target):
        raise HTTPException(status_code=403, detail="出于安全策略禁止预览密钥/会话文件")
    if not await asyncio.to_thread(target.exists) or not await asyncio.to_thread(target.is_file):
        raise HTTPException(status_code=404, detail="文件不存在")

    raw_query = f"root={quote(root)}&subpath={quote(subpath)}"
    return await asyncio.to_thread(_build_file_preview_sync, target, raw_query)


@router.get("/preview-path")
async def preview_allowed_path(path: str = Query(..., min_length=1)) -> dict[str, Any]:
    """Preview an allowed local/Drive Hub file by full path (with root & secret guards)."""
    roots = await get_allowed_roots()
    try:
        resolved = is_allowed_file_for_ingest(path, roots)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not await asyncio.to_thread(resolved.exists) or not await asyncio.to_thread(resolved.is_file):
        raise HTTPException(status_code=404, detail="文件不存在")
    raw_query = f"path={quote(str(resolved))}"
    return await asyncio.to_thread(_build_file_preview_sync, resolved, raw_query)


@router.get("/raw")
async def serve_workspace_raw_file(
    root: str | None = Query(default=None),
    subpath: str | None = Query(default=None),
    path: str | None = Query(default=None),
) -> FileResponse:
    """Stream safe local media/PDF/text inline for the local UI viewer (blocks active HTML/SVG & secrets)."""
    roots = await get_allowed_roots()
    target: Path
    if path:
        try:
            target = is_allowed_file_for_ingest(path, roots)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif root and subpath is not None:
        if root not in roots:
            raise HTTPException(status_code=400, detail=f"未知根目录: {root}")
        base = Path(roots[root]["path"])
        try:
            target = resolve_safe_subpath(base, subpath)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=400, detail="请提供 path 或 root + subpath 参数")

    if is_sensitive_path(target):
        raise HTTPException(status_code=403, detail="出于安全策略禁止读取敏感/凭据文件")
    ext = target.suffix.lower()
    if ext in INLINE_UNSAFE_EXTENSIONS or ext in {".js", ".mjs"} or ext not in INLINE_SAFE_EXTENSIONS:
        raise HTTPException(status_code=403, detail=f"禁止直接以内联方式渲染 {ext or '未知类型'} 文件")
    if not await asyncio.to_thread(target.exists) or not await asyncio.to_thread(target.is_file):
        raise HTTPException(status_code=404, detail="文件不存在")

    mime, _ = mimetypes.guess_type(target.name)
    if ext in TEXT_EXTENSIONS or target.name.lower() == ".env.example":
        safe_mime = "text/plain; charset=utf-8"
    else:
        safe_mime = mime or "application/octet-stream"
    return FileResponse(
        target,
        media_type=safe_mime,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
