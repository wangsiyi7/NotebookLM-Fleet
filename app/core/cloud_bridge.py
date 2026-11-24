"""Bidirectional cloud transfer engine: Baidu Netdisk <-> Google Drive + NotebookLM ingest hook."""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..adapters.baidupan_adapter import (
    BaiduPanClient,
    BaiduPanError,
    PanFileItem,
    baidu_client,
    normalize_pan_path,
)
from ..api.workspace import SKIP_DIRS, get_allowed_roots, is_sensitive_path
from ..config import get_data_dir
from ..db import execute, fetch_all, fetch_one, get_settings_map
from .drive_hub import list_directory, resolve_safe_subpath
from .win_runtime import detect_google_drive_mounts, sanitize_windows_filename


async def _allowed_local_bases() -> list[Path]:
    roots = await get_allowed_roots()
    bases: list[Path] = []
    for info in roots.values():
        if info.get("path"):
            bases.append(Path(info["path"]).resolve())
    for mount in await asyncio.to_thread(detect_google_drive_mounts):
        if mount.get("path"):
            bases.append(Path(mount["path"]).resolve())
    return bases


async def validate_local_transfer_path(raw_path: str | Path) -> Path:
    """Validate that a local source/target path sits inside an allowed root and is non-sensitive."""
    resolved = Path(raw_path).resolve()
    if is_sensitive_path(resolved):
        raise PermissionError(f"出于安全策略禁止读取或同步凭据/敏感文件: {resolved.name}")
    bases = await _allowed_local_bases()
    for base in bases:
        if resolved == base or resolved.is_relative_to(base):
            return resolved
    raise ValueError(f"路径不在允许的 Google 云盘或工作区目录内: {raw_path}")


async def resolve_gdrive_target_dir(subdir: str | None = None) -> Path:
    """Resolve target directory under configured Google Drive root safely."""
    settings = await get_settings_map()
    lib_root = Path(settings["drive_library_root"]).resolve()
    raw = (subdir or settings.get("baidupan_default_gdrive_subdir") or "百度网盘同步").strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return await validate_local_transfer_path(candidate)
    parts = [
        sanitize_windows_filename(seg)
        for seg in re.split(r"[/\\]+", raw)
        if seg and seg not in (".", "..")
    ]
    resolved = (lib_root.joinpath(*parts) if parts else lib_root).resolve()
    if resolved != lib_root and not resolved.is_relative_to(lib_root):
        raise ValueError("禁止跨目录越界写入 Google 云盘目标路径")
    return resolved


async def list_gdrive_bridge_dir(root_kind: str = "drive_library", subpath: str = "") -> dict[str, Any]:
    """List folders and non-sensitive files in Google Drive library or NLM_Hub."""
    settings = await get_settings_map()
    root_key = "drive_hub_root" if root_kind == "drive_hub" else "drive_library_root"
    base_str = settings.get(root_key) or ""
    listing = await asyncio.to_thread(list_directory, base_str, subpath)
    mounts = await asyncio.to_thread(detect_google_drive_mounts)
    # Strip any accidental sensitive files before returning to UI.
    entries = [e for e in listing.get("entries", []) if not is_sensitive_path(Path(e["path"]))]
    listing["entries"] = entries
    listing["root_kind"] = root_kind
    listing["mounts"] = mounts
    return listing


class CloudBridgeEngine:
    """Persistent async queue bridging Baidu Netdisk and local Google Drive mounts."""

    def __init__(self, client: BaiduPanClient | None = None) -> None:
        self.client = client or baidu_client
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_flags: dict[str, bool] = {}

    @staticmethod
    def _compute_local_dest(
        source_root: str,
        file_item: PanFileItem,
        target_dir: Path,
        preserve_tree: bool,
    ) -> Path:
        norm_src = normalize_pan_path(source_root)
        if not preserve_tree or file_item.path == norm_src:
            return target_dir / sanitize_windows_filename(file_item.server_filename)

        base_name = sanitize_windows_filename(posixpath.basename(norm_src.rstrip("/")) or "root")
        rel = posixpath.relpath(file_item.path, norm_src)
        rel_parts = [
            sanitize_windows_filename(seg)
            for seg in rel.split("/")
            if seg and seg not in (".", "..")
        ]
        if norm_src == "/":
            return target_dir.joinpath(*rel_parts)
        return target_dir.joinpath(base_name, *rel_parts)

    @staticmethod
    def _collect_local_files(source_root: Path, target_remote_dir: str, preserve_tree: bool) -> list[tuple[Path, str]]:
        norm_remote = normalize_pan_path(target_remote_dir)
        collected: list[tuple[Path, str]] = []
        if source_root.is_file():
            if is_sensitive_path(source_root):
                return []
            rel_target = posixpath.join(norm_remote, source_root.name)
            return [(source_root, normalize_pan_path(rel_target))]

        root_name = source_root.name or "upload"
        for cur_str, dir_names, file_names in os.walk(source_root):
            cur = Path(cur_str)
            dir_names[:] = sorted(
                d for d in dir_names
                if d not in SKIP_DIRS and not d.startswith(".") and not is_sensitive_path(cur / d)
            )
            for fn in sorted(file_names):
                if fn in ("desktop.ini", "Thumbs.db") or fn.startswith("."):
                    continue
                fp = cur / fn
                if is_sensitive_path(fp):
                    continue
                if preserve_tree:
                    rel = fp.relative_to(source_root).as_posix()
                    remote_path = normalize_pan_path(posixpath.join(norm_remote, root_name, rel))
                else:
                    remote_path = normalize_pan_path(posixpath.join(norm_remote, fn))
                collected.append((fp, remote_path))
        return collected

    def _row_to_job(self, row: dict[str, Any]) -> dict[str, Any]:
        total_bytes = int(row.get("total_bytes") or 0)
        transferred = int(row.get("transferred_bytes") or 0)
        total_files = int(row.get("total_files") or 0)
        done_files = int(row.get("completed_files") or 0) + int(row.get("skipped_files") or 0)
        if total_bytes > 0:
            pct = round(min(100.0, (transferred / total_bytes) * 100.0), 1)
        elif total_files > 0:
            pct = round(min(100.0, (done_files / total_files) * 100.0), 1)
        else:
            pct = 100.0 if row.get("status") == "completed" else 0.0

        try:
            sources = json.loads(row.get("source_paths_json") or "[]")
        except ValueError:
            sources = []

        return {
            "job_id": row["job_id"],
            "direction": row["direction"],
            "status": row["status"],
            "source_paths": sources,
            "target_dir": row["target_dir"],
            "preserve_tree": bool(row.get("preserve_tree", 1)),
            "skip_existing": bool(row.get("skip_existing", 1)),
            "delete_after_sync": bool(row.get("delete_after_sync", 0)),
            "auto_ingest_topic_id": row.get("auto_ingest_topic_id"),
            "total_files": total_files,
            "completed_files": int(row.get("completed_files") or 0),
            "skipped_files": int(row.get("skipped_files") or 0),
            "failed_files": int(row.get("failed_files") or 0),
            "total_bytes": total_bytes,
            "transferred_bytes": transferred,
            "progress_percent": pct,
            "error_message": row.get("error_message") or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row.get("finished_at"),
        }

    async def recover_interrupted_jobs(self, *, db_path: Path | None = None) -> int:
        rows = await fetch_all(
            "SELECT job_id FROM cloud_bridge_jobs WHERE status IN ('queued', 'scanning', 'running')",
            db_path=db_path,
        )
        if not rows:
            return 0
        now = time.time()
        await execute(
            """
            UPDATE cloud_bridge_jobs
               SET status = 'interrupted',
                   error_message = '应用重启中断，可点击重试未完成文件继续',
                   updated_at = ?,
                   finished_at = ?
             WHERE status IN ('queued', 'scanning', 'running')
            """,
            (now, now),
            db_path=db_path,
        )
        return len(rows)

    async def list_jobs(self, limit: int = 30, *, db_path: Path | None = None) -> list[dict[str, Any]]:
        rows = await fetch_all(
            "SELECT * FROM cloud_bridge_jobs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
            db_path=db_path,
        )
        return [self._row_to_job(r) for r in rows]

    async def get_job(
        self, job_id: str, include_items: bool = True, *, db_path: Path | None = None
    ) -> dict[str, Any]:
        row = await fetch_one(
            "SELECT * FROM cloud_bridge_jobs WHERE job_id = ?", (job_id,), db_path=db_path
        )
        if row is None:
            raise KeyError(f"同步任务不存在: {job_id}")
        job = self._row_to_job(row)
        if include_items:
            items = await fetch_all(
                "SELECT * FROM cloud_bridge_items WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
                db_path=db_path,
            )
            job["items"] = items
        return job

    async def _refresh_counters(self, job_id: str, *, db_path: Path | None = None) -> None:
        row = await fetch_one(
            """
            SELECT
                COUNT(*) AS total_files,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_files,
                SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped_files,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_files,
                COALESCE(SUM(file_size), 0) AS total_bytes,
                COALESCE(SUM(CASE
                    WHEN status IN ('completed', 'skipped') THEN file_size
                    ELSE transferred_bytes
                END), 0) AS transferred_bytes
              FROM cloud_bridge_items
             WHERE job_id = ?
            """,
            (job_id,),
            db_path=db_path,
        )
        if row:
            await execute(
                """
                UPDATE cloud_bridge_jobs
                   SET total_files = ?, completed_files = ?, skipped_files = ?,
                       failed_files = ?, total_bytes = ?, transferred_bytes = ?,
                       updated_at = ?
                 WHERE job_id = ?
                """,
                (
                    int(row.get("total_files") or 0),
                    int(row.get("completed_files") or 0),
                    int(row.get("skipped_files") or 0),
                    int(row.get("failed_files") or 0),
                    int(row.get("total_bytes") or 0),
                    int(row.get("transferred_bytes") or 0),
                    time.time(),
                    job_id,
                ),
                db_path=db_path,
            )

    async def create_baidu_to_gdrive_job(
        self,
        source_paths: list[str],
        gdrive_subdir: str | None = None,
        *,
        preserve_tree: bool = True,
        skip_existing: bool = True,
        delete_after_sync: bool = False,
        auto_ingest_topic_id: int | None = None,
        wait: bool = False,
        db_path: Path | None = None,
    ) -> dict[str, Any]:
        cleaned = [normalize_pan_path(p) for p in source_paths if p and p.strip()]
        if not cleaned:
            raise ValueError("请至少选择一个百度网盘文件或文件夹。")

        if auto_ingest_topic_id is not None:
            topic = await fetch_one("SELECT id FROM topics WHERE id = ?", (auto_ingest_topic_id,), db_path=db_path)
            if topic is None:
                raise ValueError(f"联动主题不存在: id={auto_ingest_topic_id}")

        target_dir = await resolve_gdrive_target_dir(gdrive_subdir)
        job_id = f"BR-{uuid.uuid4().hex[:10]}"
        now = time.time()
        await execute(
            """
            INSERT INTO cloud_bridge_jobs (
                job_id, direction, status, source_paths_json, target_dir,
                preserve_tree, skip_existing, delete_after_sync, auto_ingest_topic_id,
                created_at, updated_at
            ) VALUES (?, 'baidu_to_gdrive', 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                json.dumps(cleaned, ensure_ascii=False),
                str(target_dir),
                1 if preserve_tree else 0,
                1 if skip_existing else 0,
                1 if delete_after_sync else 0,
                auto_ingest_topic_id,
                now,
                now,
            ),
            db_path=db_path,
        )

        self._cancel_flags[job_id] = False
        task = asyncio.create_task(self._run_baidu_to_gdrive_job(job_id, db_path=db_path))
        self._active_tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._active_tasks.pop(jid, None))

        if wait:
            await task
        return await self.get_job(job_id, include_items=True, db_path=db_path)

    async def create_gdrive_to_baidu_job(
        self,
        local_paths: list[str],
        baidu_target_dir: str = "/NotebookLM_同步",
        *,
        preserve_tree: bool = True,
        skip_existing: bool = True,
        wait: bool = False,
        db_path: Path | None = None,
    ) -> dict[str, Any]:
        cleaned_local: list[str] = []
        for raw in local_paths:
            if not raw or not str(raw).strip():
                continue
            validated = await validate_local_transfer_path(raw)
            if not validated.exists():
                raise FileNotFoundError(f"本地/云盘路径不存在: {validated}")
            cleaned_local.append(str(validated))
        if not cleaned_local:
            raise ValueError("请至少选择一个 Google 云盘或工作区文件/文件夹。")

        norm_target = normalize_pan_path(baidu_target_dir or "/NotebookLM_同步")
        job_id = f"BR-{uuid.uuid4().hex[:10]}"
        now = time.time()
        await execute(
            """
            INSERT INTO cloud_bridge_jobs (
                job_id, direction, status, source_paths_json, target_dir,
                preserve_tree, skip_existing, delete_after_sync, created_at, updated_at
            ) VALUES (?, 'gdrive_to_baidu', 'queued', ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                job_id,
                json.dumps(cleaned_local, ensure_ascii=False),
                norm_target,
                1 if preserve_tree else 0,
                1 if skip_existing else 0,
                now,
                now,
            ),
            db_path=db_path,
        )

        self._cancel_flags[job_id] = False
        task = asyncio.create_task(self._run_gdrive_to_baidu_job(job_id, db_path=db_path))
        self._active_tasks[job_id] = task
        task.add_done_callback(lambda _t, jid=job_id: self._active_tasks.pop(jid, None))

        if wait:
            await task
        return await self.get_job(job_id, include_items=True, db_path=db_path)

    async def cancel_job(self, job_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
        await self.get_job(job_id, include_items=False, db_path=db_path)
        self._cancel_flags[job_id] = True
        task = self._active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        now = time.time()
        await execute(
            """
            UPDATE cloud_bridge_jobs
               SET status = 'cancelled', updated_at = ?, finished_at = ?
             WHERE job_id = ? AND status IN ('queued', 'scanning', 'running')
            """,
            (now, now, job_id),
            db_path=db_path,
        )
        await execute(
            """
            UPDATE cloud_bridge_items
               SET status = 'cancelled', updated_at = ?
             WHERE job_id = ? AND status IN ('pending', 'transferring')
            """,
            (now, job_id),
            db_path=db_path,
        )
        return await self.get_job(job_id, include_items=False, db_path=db_path)

    async def retry_failed(
        self, job_id: str, *, wait: bool = False, db_path: Path | None = None
    ) -> dict[str, Any]:
        job = await self.get_job(job_id, include_items=True, db_path=db_path)
        retry_items = [
            item
            for item in job.get("items", [])
            if item.get("status") in ("failed", "cancelled", "pending", "transferring")
        ]
        if not retry_items and not job.get("source_paths"):
            raise ValueError(f"任务 {job_id} 没有可重试的源路径")

        new_job_id = f"BR-{uuid.uuid4().hex[:10]}"
        now = time.time()
        sources_for_row = [it["source_path"] for it in retry_items] if retry_items else list(job["source_paths"])
        await execute(
            """
            INSERT INTO cloud_bridge_jobs (
                job_id, direction, status, source_paths_json, target_dir,
                preserve_tree, skip_existing, delete_after_sync, auto_ingest_topic_id,
                created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_job_id,
                job["direction"],
                json.dumps(sources_for_row, ensure_ascii=False),
                job["target_dir"],
                1 if job["preserve_tree"] else 0,
                1 if job["skip_existing"] else 0,
                1 if job["delete_after_sync"] else 0,
                job.get("auto_ingest_topic_id"),
                now,
                now,
            ),
            db_path=db_path,
        )
        for it in retry_items:
            await execute(
                """
                INSERT INTO cloud_bridge_items (
                    job_id, source_path, target_path, fs_id, file_size, remote_md5,
                    status, transferred_bytes, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '', ?)
                """,
                (
                    new_job_id,
                    it["source_path"],
                    it["target_path"],
                    int(it.get("fs_id") or 0),
                    int(it.get("file_size") or 0),
                    it.get("remote_md5") or "",
                    now,
                ),
                db_path=db_path,
            )

        self._cancel_flags[new_job_id] = False
        coro = (
            self._run_gdrive_to_baidu_job(new_job_id, db_path=db_path)
            if job["direction"] == "gdrive_to_baidu"
            else self._run_baidu_to_gdrive_job(new_job_id, db_path=db_path)
        )
        task = asyncio.create_task(coro)
        self._active_tasks[new_job_id] = task
        task.add_done_callback(lambda _t, jid=new_job_id: self._active_tasks.pop(jid, None))
        if wait:
            await task
        return await self.get_job(new_job_id, include_items=True, db_path=db_path)

    async def _run_baidu_to_gdrive_job(self, job_id: str, *, db_path: Path | None = None) -> None:
        stage_root = get_data_dir() / "bridge_tmp" / job_id
        try:
            job = await self.get_job(job_id, include_items=False, db_path=db_path)
            target_dir = Path(job["target_dir"])
            await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
            preserve_tree = bool(job["preserve_tree"])
            skip_existing = bool(job["skip_existing"])
            delete_after_sync = bool(job["delete_after_sync"])
            auto_topic_id = job.get("auto_ingest_topic_id")

            preseeded = await fetch_all(
                "SELECT * FROM cloud_bridge_items WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
                db_path=db_path,
            )
            item_records: list[tuple[int, PanFileItem, Path]] = []
            if preseeded:
                for row in preseeded:
                    src_p = str(row["source_path"])
                    f_item = PanFileItem(
                        fs_id=int(row.get("fs_id") or 0),
                        path=src_p,
                        server_filename=posixpath.basename(src_p),
                        size=int(row.get("file_size") or 0),
                        isdir=False,
                        md5=str(row.get("remote_md5") or ""),
                    )
                    item_records.append((int(row["id"]), f_item, Path(str(row["target_path"]))))
            else:
                await execute(
                    "UPDATE cloud_bridge_jobs SET status = 'scanning', updated_at = ? WHERE job_id = ?",
                    (time.time(), job_id),
                    db_path=db_path,
                )

                expanded: list[tuple[PanFileItem, Path]] = []
                seen_remote: set[str] = set()
                for src in job["source_paths"]:
                    if self._cancel_flags.get(job_id):
                        raise asyncio.CancelledError()
                    files = await self.client.walk_files(src)
                    for f in files:
                        if f.path in seen_remote:
                            continue
                        seen_remote.add(f.path)
                        local_dest = self._compute_local_dest(src, f, target_dir, preserve_tree)
                        expanded.append((f, local_dest))

                now = time.time()
                for f, local_dest in expanded:
                    item_id = await execute(
                        """
                        INSERT INTO cloud_bridge_items (
                            job_id, source_path, target_path, fs_id, file_size, remote_md5,
                            status, transferred_bytes, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                        """,
                        (job_id, f.path, str(local_dest), f.fs_id, f.size, f.md5, now),
                        db_path=db_path,
                    )
                    item_records.append((item_id, f, local_dest))

            await execute(
                "UPDATE cloud_bridge_jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
                (time.time(), job_id),
                db_path=db_path,
            )
            await self._refresh_counters(job_id, db_path=db_path)

            sem = asyncio.Semaphore(2)
            synced_remote: list[str] = []

            async def _transfer_one(item_id: int, f: PanFileItem, local_dest: Path) -> None:
                async with sem:
                    if self._cancel_flags.get(job_id):
                        raise asyncio.CancelledError()

                    if skip_existing and local_dest.exists() and local_dest.is_file():
                        try:
                            if f.size == 0 or local_dest.stat().st_size == f.size:
                                await execute(
                                    "UPDATE cloud_bridge_items SET status = 'skipped', transferred_bytes = ?, updated_at = ? WHERE id = ?",
                                    (f.size, time.time(), item_id),
                                    db_path=db_path,
                                )
                                await self._refresh_counters(job_id, db_path=db_path)
                                return
                        except OSError:
                            pass

                    await execute(
                        "UPDATE cloud_bridge_items SET status = 'transferring', updated_at = ? WHERE id = ?",
                        (time.time(), item_id),
                        db_path=db_path,
                    )
                    stage_file = stage_root / f"{item_id}_{sanitize_windows_filename(f.server_filename)}"
                    stage_file.parent.mkdir(parents=True, exist_ok=True)

                    async def _on_prog(done_bytes: int, _total: int) -> None:
                        await execute(
                            "UPDATE cloud_bridge_items SET transferred_bytes = ?, updated_at = ? WHERE id = ?",
                            (done_bytes, time.time(), item_id),
                            db_path=db_path,
                        )
                        await self._refresh_counters(job_id, db_path=db_path)

                    try:
                        await self.client.download_file(
                            f.path,
                            stage_file,
                            fs_id=f.fs_id,
                            expected_size=f.size,
                            progress_cb=_on_prog,
                            cancel_check=lambda: bool(self._cancel_flags.get(job_id)),
                        )
                        await asyncio.to_thread(local_dest.parent.mkdir, parents=True, exist_ok=True)
                        await asyncio.to_thread(shutil.move, str(stage_file), str(local_dest))
                        synced_remote.append(f.path)

                        final_size = f.size or local_dest.stat().st_size
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'completed', transferred_bytes = ?, error_message = '', updated_at = ? WHERE id = ?",
                            (final_size, time.time(), item_id),
                            db_path=db_path,
                        )
                        if auto_topic_id is not None:
                            await execute(
                                """
                                INSERT INTO tasks (topic_id, task_type, payload_json, priority, status)
                                VALUES (?, 'ingest_file', ?, 40, 'pending')
                                """,
                                (
                                    int(auto_topic_id),
                                    json.dumps(
                                        {
                                            "file_path": str(local_dest),
                                            "title": local_dest.name,
                                            "bridge_job_id": job_id,
                                        },
                                        ensure_ascii=False,
                                    ),
                                ),
                                db_path=db_path,
                            )
                    except asyncio.CancelledError:
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'cancelled', updated_at = ? WHERE id = ?",
                            (time.time(), item_id),
                            db_path=db_path,
                        )
                        raise
                    except Exception as exc:  # noqa: BLE001
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                            (str(exc)[:500], time.time(), item_id),
                            db_path=db_path,
                        )
                    finally:
                        await self._refresh_counters(job_id, db_path=db_path)

            await asyncio.gather(*(_transfer_one(iid, f, dest) for iid, f, dest in item_records))

            if delete_after_sync and synced_remote:
                try:
                    await self.client.delete_files(synced_remote)
                except Exception:  # noqa: BLE001
                    pass

            await self._finalize_job(job_id, db_path=db_path)
        except asyncio.CancelledError:
            now = time.time()
            await execute(
                "UPDATE cloud_bridge_jobs SET status = 'cancelled', updated_at = ?, finished_at = ? WHERE job_id = ?",
                (now, now, job_id),
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            now = time.time()
            await execute(
                "UPDATE cloud_bridge_jobs SET status = 'failed', error_message = ?, updated_at = ?, finished_at = ? WHERE job_id = ?",
                (str(exc)[:1000], now, now, job_id),
                db_path=db_path,
            )
        finally:
            if stage_root.exists():
                shutil.rmtree(stage_root, ignore_errors=True)
            self._cancel_flags.pop(job_id, None)

    async def _ensure_remote_dir(self, remote_dir: str, created_cache: set[str]) -> None:
        norm = normalize_pan_path(remote_dir)
        if norm == "/" or norm in created_cache:
            return
        try:
            await self.client.create_directory(norm)
        except BaiduPanError as exc:
            if exc.errno not in (-8,):
                # Fallback create parent segments recursively
                parts = [p for p in norm.strip("/").split("/") if p]
                cur = ""
                for part in parts:
                    cur = f"{cur}/{part}"
                    if cur in created_cache:
                        continue
                    try:
                        await self.client.create_directory(cur)
                    except BaiduPanError as sub_exc:
                        if sub_exc.errno != -8:
                            raise
                    created_cache.add(cur)
                return
        created_cache.add(norm)

    async def _run_gdrive_to_baidu_job(self, job_id: str, *, db_path: Path | None = None) -> None:
        try:
            job = await self.get_job(job_id, include_items=False, db_path=db_path)
            target_remote_dir = normalize_pan_path(job["target_dir"])
            preserve_tree = bool(job["preserve_tree"])
            skip_existing = bool(job["skip_existing"])

            preseeded = await fetch_all(
                "SELECT * FROM cloud_bridge_items WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
                db_path=db_path,
            )
            item_records: list[tuple[int, Path, str, int]] = []
            if preseeded:
                for row in preseeded:
                    item_records.append(
                        (
                            int(row["id"]),
                            Path(str(row["source_path"])),
                            str(row["target_path"]),
                            int(row.get("file_size") or 0),
                        )
                    )
            else:
                await execute(
                    "UPDATE cloud_bridge_jobs SET status = 'scanning', updated_at = ? WHERE job_id = ?",
                    (time.time(), job_id),
                    db_path=db_path,
                )

                expanded: list[tuple[Path, str, int]] = []
                seen_local: set[str] = set()
                for raw_src in job["source_paths"]:
                    if self._cancel_flags.get(job_id):
                        raise asyncio.CancelledError()
                    src_path = Path(raw_src)
                    pairs = await asyncio.to_thread(
                        self._collect_local_files, src_path, target_remote_dir, preserve_tree
                    )
                    for fp, r_dest in pairs:
                        key = str(fp.resolve()).lower()
                        if key in seen_local:
                            continue
                        seen_local.add(key)
                        try:
                            sz = fp.stat().st_size
                        except OSError:
                            sz = 0
                        expanded.append((fp, r_dest, sz))

                now = time.time()
                for fp, r_dest, sz in expanded:
                    item_id = await execute(
                        """
                        INSERT INTO cloud_bridge_items (
                            job_id, source_path, target_path, fs_id, file_size, remote_md5,
                            status, transferred_bytes, updated_at
                        ) VALUES (?, ?, ?, 0, ?, '', 'pending', 0, ?)
                        """,
                        (job_id, str(fp), r_dest, sz, now),
                        db_path=db_path,
                    )
                    item_records.append((item_id, fp, r_dest, sz))

            await execute(
                "UPDATE cloud_bridge_jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
                (time.time(), job_id),
                db_path=db_path,
            )
            await self._refresh_counters(job_id, db_path=db_path)

            created_dirs: set[str] = set()
            remote_listing_cache: dict[str, dict[str, int]] = {}
            dir_lock = asyncio.Lock()
            sem = asyncio.Semaphore(2)

            async def _upload_one(item_id: int, fp: Path, r_dest: str, sz: int) -> None:
                async with sem:
                    if self._cancel_flags.get(job_id):
                        raise asyncio.CancelledError()

                    parent_dir = normalize_pan_path(posixpath.dirname(r_dest))
                    async with dir_lock:
                        await self._ensure_remote_dir(parent_dir, created_dirs)
                        if skip_existing and parent_dir not in remote_listing_cache:
                            try:
                                existing = await self.client.list_all_files(parent_dir)
                                remote_listing_cache[parent_dir] = {
                                    it.path: it.size for it in existing if not it.isdir
                                }
                            except Exception:  # noqa: BLE001
                                remote_listing_cache[parent_dir] = {}

                    if skip_existing and remote_listing_cache.get(parent_dir, {}).get(r_dest) == sz:
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'skipped', transferred_bytes = ?, updated_at = ? WHERE id = ?",
                            (sz, time.time(), item_id),
                            db_path=db_path,
                        )
                        await self._refresh_counters(job_id, db_path=db_path)
                        return

                    await execute(
                        "UPDATE cloud_bridge_items SET status = 'transferring', updated_at = ? WHERE id = ?",
                        (time.time(), item_id),
                        db_path=db_path,
                    )

                    async def _on_prog(done_bytes: int, _total: int) -> None:
                        await execute(
                            "UPDATE cloud_bridge_items SET transferred_bytes = ?, updated_at = ? WHERE id = ?",
                            (done_bytes, time.time(), item_id),
                            db_path=db_path,
                        )
                        await self._refresh_counters(job_id, db_path=db_path)

                    try:
                        await self.client.upload_file(
                            fp,
                            r_dest,
                            progress_cb=_on_prog,
                            cancel_check=lambda: bool(self._cancel_flags.get(job_id)),
                        )
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'completed', transferred_bytes = ?, error_message = '', updated_at = ? WHERE id = ?",
                            (sz, time.time(), item_id),
                            db_path=db_path,
                        )
                    except asyncio.CancelledError:
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'cancelled', updated_at = ? WHERE id = ?",
                            (time.time(), item_id),
                            db_path=db_path,
                        )
                        raise
                    except Exception as exc:  # noqa: BLE001
                        await execute(
                            "UPDATE cloud_bridge_items SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
                            (str(exc)[:500], time.time(), item_id),
                            db_path=db_path,
                        )
                    finally:
                        await self._refresh_counters(job_id, db_path=db_path)

            await asyncio.gather(*(_upload_one(iid, fp, r_dest, sz) for iid, fp, r_dest, sz in item_records))
            await self._finalize_job(job_id, db_path=db_path)
        except asyncio.CancelledError:
            now = time.time()
            await execute(
                "UPDATE cloud_bridge_jobs SET status = 'cancelled', updated_at = ?, finished_at = ? WHERE job_id = ?",
                (now, now, job_id),
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            now = time.time()
            await execute(
                "UPDATE cloud_bridge_jobs SET status = 'failed', error_message = ?, updated_at = ?, finished_at = ? WHERE job_id = ?",
                (str(exc)[:1000], now, now, job_id),
                db_path=db_path,
            )
        finally:
            self._cancel_flags.pop(job_id, None)

    async def _finalize_job(self, job_id: str, *, db_path: Path | None = None) -> None:
        await self._refresh_counters(job_id, db_path=db_path)
        final_job = await self.get_job(job_id, include_items=False, db_path=db_path)
        if final_job["failed_files"] > 0 and (final_job["completed_files"] + final_job["skipped_files"]) > 0:
            final_status = "partial_failed"
        elif final_job["failed_files"] > 0:
            final_status = "failed"
        else:
            final_status = "completed"
        now = time.time()
        await execute(
            "UPDATE cloud_bridge_jobs SET status = ?, updated_at = ?, finished_at = ? WHERE job_id = ?",
            (final_status, now, now, job_id),
            db_path=db_path,
        )


cloud_bridge_engine = CloudBridgeEngine()
