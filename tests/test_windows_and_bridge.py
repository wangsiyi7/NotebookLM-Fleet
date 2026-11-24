"""Tests for Windows runtime helpers, frozen-path safety, BaiduPan adapter, and Cloud Bridge."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.baidupan_adapter import (
    BaiduAuthManager,
    BaiduPanError,
    PanFileItem,
    baidu_client,
)
from app.api.workspace import is_sensitive_path
from app.core.cloud_bridge import CloudBridgeEngine, cloud_bridge_engine
from app.core.win_runtime import (
    detect_google_drive_mounts,
    find_available_port,
    get_default_data_dir,
    get_default_drive_paths,
    sanitize_windows_filename,
)
from app.db import fetch_all, init_db
from app.main import app


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_dir = tmp_path / "data"
    db_file = data_dir / "test_bridge.db"
    hub_dir = tmp_path / "DriveHub"
    lib_dir = tmp_path / "DriveLib"
    ws_dir = tmp_path / "Workspace"
    ws_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    hub_dir.mkdir(parents=True)
    (lib_dir / "资料库").mkdir(parents=True)
    (lib_dir / "资料库" / "study_note.md").write_text("# Study Note\nOK", encoding="utf-8")

    monkeypatch.setenv("FLEET_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FLEET_DB_PATH", str(db_file))
    monkeypatch.setenv("FLEET_DISABLE_WORKERS", "1")
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws_dir))
    monkeypatch.setenv("DRIVE_HUB_ROOT", str(hub_dir))
    monkeypatch.setenv("DRIVE_LIBRARY_ROOT", str(lib_dir))
    return {"data": data_dir, "db": db_file, "hub": hub_dir, "lib": lib_dir, "ws": ws_dir}


def test_win_runtime_paths_sanitization_and_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sanitize_windows_filename("CON") == "_CON"
    assert sanitize_windows_filename("nul.txt") == "_nul.txt"
    assert sanitize_windows_filename('a<b:c/d\\e|f?g*h.md') == "a_b_c_d_e_f_g_h.md"
    long_pdf = sanitize_windows_filename("x" * 180 + ".pdf", max_len=60)
    assert len(long_pdf) <= 60 and long_pdf.endswith(".pdf")

    # Test frozen portable vs LocalAppData resolution
    fake_exe_dir = tmp_path / "PortableApp"
    fake_exe_dir.mkdir(parents=True)
    (fake_exe_dir / "portable.flag").write_text("1", encoding="utf-8")
    monkeypatch.delenv("FLEET_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe_dir / "NotebookLM-Fleet.exe"))
    assert get_default_data_dir() == fake_exe_dir / "data"

    (fake_exe_dir / "portable.flag").unlink()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    assert get_default_data_dir() == (tmp_path / "LocalAppData" / "NotebookLM-Fleet" / "data")

    # Sensitive credential exclusion covers baidupan_auth.json and runtime_port.json
    assert is_sensitive_path(tmp_path / "baidupan_auth.json")
    assert is_sensitive_path(tmp_path / "runtime_port.json")
    assert not is_sensitive_path(tmp_path / "report.md")

    # Google Drive default fallback never crashes or hardcodes drive letter
    mounts = detect_google_drive_mounts()
    assert isinstance(mounts, list)
    lib_p, hub_p = get_default_drive_paths(tmp_path / "custom_data")
    assert "Drive_Library" in lib_p or "Drive" in lib_p or "云端硬盘" in lib_p
    assert hub_p.endswith("NLM_Hub")

    # find_available_port skips an occupied socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        busy_port = s.getsockname()[1]
        if busy_port < 65500:
            free_port = find_available_port(busy_port, busy_port + 20)
            assert free_port != busy_port


def test_baidu_auth_manager_curl_cookie_cdp_and_logout(tmp_path: Path) -> None:
    auth_file = tmp_path / "baidupan_auth.json"
    mgr = BaiduAuthManager(auth_file=auth_file)
    assert not mgr.status_summary()["authenticated"]

    # Chrome bash Copy as cURL
    curl_bash = (
        "curl 'https://pan.baidu.com/api/list' \\\n"
        "  -H 'accept: application/json' \\\n"
        "  -b 'BDUSS=test_bduss_12345; STOKEN=test_stoken_67890; BAIDUID=xyz'"
    )
    st = mgr.import_cookie_string(curl_bash)
    assert st.is_authenticated
    assert st.cookies["BDUSS"] == "test_bduss_12345"
    assert st.cookies["STOKEN"] == "test_stoken_67890"

    # Bare BDUSS + separate stoken
    st2 = mgr.import_cookie_string("bare_bduss_token_999", stoken="bare_st_111")
    assert st2.cookies["BDUSS"] == "bare_bduss_token_999"
    assert st2.cookies["STOKEN"] == "bare_st_111"

    # CDP cookies filter .baidu.com only
    st3 = mgr.import_cdp_cookies(
        [
            {"name": "SID", "value": "google_ignore", "domain": ".google.com"},
            {"name": "BDUSS", "value": "cdp_bduss_777", "domain": ".baidu.com"},
            {"name": "STOKEN", "value": "cdp_stoken_888", "domain": "pan.baidu.com"},
        ]
    )
    assert st3.cookies["BDUSS"] == "cdp_bduss_777"
    assert "SID" not in st3.cookies

    # OpenAPI token mode
    st4 = mgr.set_openapi_token("open_acc_token", refresh_token="open_ref_token", expires_in=3600)
    assert st4.mode == "openapi" and st4.is_authenticated

    mgr.clear()
    assert not mgr.status_summary()["authenticated"]
    assert not auth_file.exists()


class _FakeBaiduPanClient:
    def __init__(self) -> None:
        self.created_dirs: list[str] = []
        self.uploaded: list[tuple[str, str, int]] = []
        self.deleted: list[str] = []
        self.existing_remote_files: dict[str, dict[str, int]] = {}
        self.fail_next_download = False

    async def walk_files(self, path: str) -> list[PanFileItem]:
        if path == "/课程资料":
            return [
                PanFileItem(fs_id=101, path="/课程资料/week1/讲义.md", server_filename="讲义.md", size=11, isdir=False),
                PanFileItem(fs_id=102, path="/课程资料/CON.txt", server_filename="CON.txt", size=9, isdir=False),
            ]
        raise BaiduPanError(f"路径不存在: {path}", errno=-9)

    async def download_file(
        self,
        remote_path: str,
        dest_path: str | Path,
        *,
        fs_id: int | None = None,
        expected_size: int | None = None,
        progress_cb: Any = None,
        cancel_check: Any = None,
    ) -> dict[str, Any]:
        if self.fail_next_download:
            self.fail_next_download = False
            raise BaiduPanError("模拟网络超时")
        payload = f"content:{remote_path}".encode("utf-8")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload[:expected_size] if expected_size else payload)
        if progress_cb:
            await progress_cb(dest.stat().st_size, dest.stat().st_size)
        return {"remote_path": remote_path, "local_path": str(dest), "bytes": dest.stat().st_size}

    async def create_directory(self, path: str) -> dict[str, Any]:
        self.created_dirs.append(path)
        return {"errno": 0}

    async def list_all_files(self, path: str = "/") -> list[PanFileItem]:
        files = self.existing_remote_files.get(path, {})
        return [
            PanFileItem(fs_id=i + 1, path=p, server_filename=Path(p).name, size=sz, isdir=False)
            for i, (p, sz) in enumerate(files.items())
        ]

    async def upload_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        progress_cb: Any = None,
        cancel_check: Any = None,
    ) -> dict[str, Any]:
        src = Path(local_path)
        sz = src.stat().st_size
        self.uploaded.append((str(src), remote_path, sz))
        parent = "/" + "/".join(remote_path.strip("/").split("/")[:-1])
        self.existing_remote_files.setdefault(parent or "/", {})[remote_path] = sz
        if progress_cb:
            await progress_cb(sz, sz)
        return {"errno": 0, "path": remote_path}

    async def delete_files(self, paths: list[str]) -> dict[str, Any]:
        self.deleted.extend(paths)
        return {"errno": 0}


def test_cloud_bridge_bidirectional_and_auto_ingest(isolated_env: dict[str, Path]) -> None:
    async def _run() -> None:
        await init_db()
        from app.db import execute

        topic_id = await execute(
            "INSERT INTO topics (name, drive_folder_path) VALUES (?, ?)",
            ("网盘联动专题", str(isolated_env["hub"] / "02_Topics" / "网盘联动专题")),
        )
        fake_client = _FakeBaiduPanClient()
        engine = CloudBridgeEngine(client=fake_client)  # type: ignore[arg-type]

        # 1. Baidu -> Google Drive + auto ingest into topic
        job = await engine.create_baidu_to_gdrive_job(
            source_paths=["/课程资料"],
            gdrive_subdir="百度同步测试",
            preserve_tree=True,
            skip_existing=True,
            auto_ingest_topic_id=topic_id,
            wait=True,
        )
        assert job["status"] == "completed"
        assert job["completed_files"] == 2
        assert (isolated_env["lib"] / "百度同步测试" / "课程资料" / "week1" / "讲义.md").exists()
        assert (isolated_env["lib"] / "百度同步测试" / "课程资料" / "_CON.txt").exists()

        queued_tasks = await fetch_all("SELECT * FROM tasks WHERE topic_id = ?", (topic_id,))
        assert len(queued_tasks) == 2
        assert all(t["task_type"] == "ingest_file" for t in queued_tasks)

        # 2. Second run skips existing matching-size files
        job2 = await engine.create_baidu_to_gdrive_job(
            source_paths=["/课程资料"],
            gdrive_subdir="百度同步测试",
            preserve_tree=True,
            skip_existing=True,
            wait=True,
        )
        assert job2["status"] == "completed"
        assert job2["skipped_files"] == 2
        assert job2["completed_files"] == 0

        # 3. Retry failed item recovers cleanly
        fake_client.fail_next_download = True
        job_fail = await engine.create_baidu_to_gdrive_job(
            source_paths=["/课程资料"],
            gdrive_subdir="百度重试测试",
            preserve_tree=True,
            skip_existing=True,
            wait=True,
        )
        assert job_fail["status"] == "partial_failed"
        retried_done = await engine.retry_failed(job_fail["job_id"], wait=True)
        assert retried_done["status"] == "completed"
        assert retried_done["completed_files"] == 1
        assert (isolated_env["lib"] / "百度重试测试" / "课程资料" / "week1" / "讲义.md").exists()

        # 4. Google Drive -> Baidu Netdisk reverse sync + incremental skip
        rev_job = await engine.create_gdrive_to_baidu_job(
            local_paths=[str(isolated_env["lib"] / "资料库")],
            baidu_target_dir="/NotebookLM_反向同步",
            preserve_tree=True,
            skip_existing=True,
            wait=True,
        )
        assert rev_job["status"] == "completed"
        assert rev_job["completed_files"] == 1
        assert len(fake_client.uploaded) == 1
        assert fake_client.uploaded[0][1] == "/NotebookLM_反向同步/资料库/study_note.md"

        rev_job2 = await engine.create_gdrive_to_baidu_job(
            local_paths=[str(isolated_env["lib"] / "资料库")],
            baidu_target_dir="/NotebookLM_反向同步",
            preserve_tree=True,
            skip_existing=True,
            wait=True,
        )
        assert rev_job2["status"] == "completed"
        assert rev_job2["skipped_files"] == 1

        # 5. Reverse sync must reject credential files
        secret_file = isolated_env["data"] / "baidupan_auth.json"
        secret_file.write_text('{"cookies":{"BDUSS":"secret"}}', encoding="utf-8")
        with pytest.raises(PermissionError):
            await engine.create_gdrive_to_baidu_job(
                local_paths=[str(secret_file)],
                baidu_target_dir="/leak_test",
                wait=True,
            )

    asyncio.run(_run())


def test_bridge_and_system_routes_e2e(
    isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = _FakeBaiduPanClient()
    monkeypatch.setattr(cloud_bridge_engine, "client", fake_client)

    with TestClient(app) as client:
        r_sys = client.get("/api/system/info")
        assert r_sys.status_code == 200
        sys_data = r_sys.json()
        assert sys_data["app_version"] == "0.3.0"
        assert sys_data["desktop_mode"] is False

        r_win_bad = client.post("/api/system/open-window", json={"path": "//evil.example.com"})
        assert r_win_bad.status_code == 400

        r_win_ok = client.post("/api/system/open-window", json={"path": "/notebooks/1"})
        assert r_win_ok.status_code == 200
        assert r_win_ok.json()["opened_native"] is False

        # Reveal blocks credential files and outside paths
        secret_p = isolated_env["data"] / "baidupan_auth.json"
        secret_p.write_text("{}", encoding="utf-8")
        r_rev_sec = client.post("/api/system/reveal-path", json={"path": str(secret_p)})
        assert r_rev_sec.status_code == 403

        r_br_status = client.get("/api/bridge/status?live_check=false")
        assert r_br_status.status_code == 200
        assert "baidu" in r_br_status.json() and "gdrive" in r_br_status.json()

        r_browse = client.get("/api/bridge/gdrive/browse?root=drive_library&subpath=资料库")
        assert r_browse.status_code == 200
        names = [e["name"] for e in r_browse.json()["entries"]]
        assert "study_note.md" in names

        r_upload = client.post(
            "/api/bridge/jobs/gdrive-to-baidu",
            json={
                "local_paths": [str(isolated_env["lib"] / "资料库" / "study_note.md")],
                "baidu_target_dir": "/WebRouteUpload",
                "preserve_tree": False,
                "skip_existing": True,
            },
        )
        assert r_upload.status_code == 200
        jid = r_upload.json()["job_id"]

        r_jobs = client.get("/api/bridge/jobs")
        assert r_jobs.status_code == 200
        assert any(j["job_id"] == jid for j in r_jobs.json()["jobs"])
