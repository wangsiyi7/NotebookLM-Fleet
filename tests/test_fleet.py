"""Comprehensive unit and API smoke tests for NotebookLM-Fleet."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.router import CapabilityRouter
from app.core.drive_hub import (
    backup_sqlite_to_drive,
    ensure_hub_structure,
    ensure_topic_folders,
    list_directory,
    resolve_safe_subpath,
)
from app.core.scheduler import select_account_for_task
from app.db import fetch_one, get_settings_map, init_db, set_settings_map
from app.main import app


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_dir = tmp_path / "data"
    db_file = data_dir / "test_fleet.db"
    hub_dir = tmp_path / "DriveHub"
    lib_dir = tmp_path / "DriveLib"
    ws_dir = tmp_path / "Workspace"
    ws_dir.mkdir(parents=True)
    (ws_dir / "sample.md").write_text("# Sample Doc", encoding="utf-8")
    (lib_dir / "论文系列").mkdir(parents=True)
    (lib_dir / "论文系列" / "rl_paper.pdf").write_bytes(b"%PDF-1.4 sample")

    monkeypatch.setenv("FLEET_DATA_DIR", str(data_dir))
    monkeypatch.setenv("FLEET_DB_PATH", str(db_file))
    monkeypatch.setenv("FLEET_DISABLE_WORKERS", "1")
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws_dir))
    monkeypatch.setenv("DRIVE_HUB_ROOT", str(hub_dir))
    monkeypatch.setenv("DRIVE_LIBRARY_ROOT", str(lib_dir))
    return {"data": data_dir, "db": db_file, "hub": hub_dir, "lib": lib_dir, "ws": ws_dir}


def test_db_init_and_settings(isolated_env: dict[str, Path]) -> None:
    async def _run() -> None:
        await init_db()
        master = await fetch_one("SELECT * FROM accounts WHERE alias = ?", ("ultra-1",))
        assert master is not None
        assert master["alias"] == "ultra-1"
        assert master["email"] == "ultra-1@example.com"
        assert master["tier"] == "ultra"
        assert master["is_hub_master"] == 1

        settings = await get_settings_map()
        assert settings["tier_policy"] == "conserve_ultra"
        updated = await set_settings_map({"safety_buffer_pct": "15"})
        assert updated["safety_buffer_pct"] == "15"

    asyncio.run(_run())


def test_drive_hub_security_and_backup(isolated_env: dict[str, Path]) -> None:
    hub = ensure_hub_structure(isolated_env["hub"])
    assert hub["inbox"].exists()
    assert hub["backups"].exists()

    folders = ensure_topic_folders(isolated_env["hub"], "RL_Papers/2026:Q4")
    assert folders["sources"].exists()
    assert folders["artifacts"].exists()

    with pytest.raises(ValueError):
        resolve_safe_subpath(isolated_env["lib"], "../../Windows/System32")

    listing = list_directory(isolated_env["lib"], "论文系列")
    assert listing["exists"] is True
    assert any(e["name"] == "rl_paper.pdf" for e in listing["entries"])

    asyncio.run(init_db())
    backup_info = backup_sqlite_to_drive(isolated_env["db"], isolated_env["hub"], keep=5)
    assert Path(backup_info["backup_path"]).exists()
    assert backup_info["size_bytes"] > 0


def _make_acct(
    aid: int,
    alias: str,
    tier: str,
    *,
    five_rem: float = 80.0,
    week_rem: float = 80.0,
    reset_hours: float = 3.0,
    exhausted: bool = False,
    deferred_slots: int = 0,
    source_limit: int = 600,
) -> dict:
    reset_iso = (datetime.now(timezone.utc) + timedelta(hours=reset_hours)).isoformat()
    return {
        "id": aid,
        "alias": alias,
        "email": f"{alias}@gmail.com",
        "tier": tier,
        "enabled": 1,
        "nlm_auth_status": "ok",
        "source_limit": source_limit,
        "quota_status": "ready",
        "five_hour_remaining_pct": five_rem,
        "weekly_remaining_pct": week_rem,
        "five_hour_resets_at": reset_iso,
        "is_exhausted": 1 if exhausted else 0,
        "actions": [
            {
                "kind": "audio_overview",
                "has_sufficient_quota": not exhausted and five_rem >= 12.0,
                "estimated_cost_percent": 12.0,
                "remaining_deferred_artifact_generations": deferred_slots,
            },
            {
                "kind": "quiz",
                "has_sufficient_quota": not exhausted and five_rem >= 3.0,
                "estimated_cost_percent": 3.0,
                "remaining_deferred_artifact_generations": deferred_slots,
            },
        ],
    }


def test_scheduler_tier_and_window_routing() -> None:
    settings = {"tier_policy": "conserve_ultra", "safety_buffer_pct": "10"}
    accts = [
        _make_acct(1, "ultra-1", "ultra", five_rem=90.0, reset_hours=4.0, source_limit=600),
        _make_acct(2, "pro-1", "pro", five_rem=85.0, reset_hours=4.0, source_limit=300),
        _make_acct(3, "free-1", "free", five_rem=85.0, reset_hours=4.0, source_limit=50),
    ]

    # Light task prefers free under conserve_ultra when source_count <= 50.
    acct, decision, _, _ = select_account_for_task({"task_type": "quiz"}, accts, settings, set(), source_count=20)
    assert decision == "run" and acct is not None and acct["alias"] == "free-1"

    # Large source count (>50) excludes free and selects pro.
    acct, decision, _, _ = select_account_for_task({"task_type": "quiz"}, accts, settings, set(), source_count=120)
    assert decision == "run" and acct is not None and acct["alias"] == "pro-1"

    # Heavy task selects ultra.
    acct, decision, _, _ = select_account_for_task({"task_type": "audio"}, accts, settings, set(), source_count=120)
    assert decision == "run" and acct is not None and acct["alias"] == "ultra-1"

    # Soonest 5-hour window tie-breaker among equal Ultra accounts.
    ultras = [
        _make_acct(1, "ultra-1", "ultra", five_rem=60.0, week_rem=80.0, reset_hours=4.5),
        _make_acct(2, "ultra-2", "ultra", five_rem=60.0, week_rem=80.0, reset_hours=0.4),
    ]
    acct, decision, _, _ = select_account_for_task({"task_type": "audio"}, ultras, settings, set(), source_count=10)
    assert decision == "run" and acct is not None and acct["alias"] == "ultra-2"

    # Exhausted with deferred slots -> deferred_later.
    exhausted_with_later = [_make_acct(1, "ultra-1", "ultra", five_rem=2.0, exhausted=True, deferred_slots=3)]
    acct, decision, _, _ = select_account_for_task({"task_type": "audio"}, exhausted_with_later, settings, set())
    assert decision == "deferred_later" and acct is not None and acct["alias"] == "ultra-1"

    # Exhausted without deferred slots -> waiting_quota after earliest reset + 2m.
    exhausted_no_later = [_make_acct(1, "ultra-1", "ultra", five_rem=2.0, exhausted=True, deferred_slots=0, reset_hours=1.0)]
    acct, decision, _, wake = select_account_for_task({"task_type": "audio"}, exhausted_no_later, settings, set())
    assert acct is None and decision == "waiting_quota" and wake is not None


def test_router_fallback_to_ch2() -> None:
    class FakeOfficial:
        async def create_notebook(self, alias: str, project_id: str, title: str) -> dict:
            raise RuntimeError("API disabled or quota")

    class FakeNlm:
        async def create_notebook(self, profile: str, title: str) -> dict:
            return {"id": "nb-fallback-1", "title": title}

    router = CapabilityRouter(
        {"official_api_enabled": "true", "official_api_project_id": "proj-1"},
        official=FakeOfficial(),
        nlm=FakeNlm(),
    )
    nb, channel = asyncio.run(
        router.create_notebook({"alias": "ultra-1", "nlm_profile": "ultra-1", "official_oauth_status": "ok"}, "Test")
    )
    assert nb["id"] == "nb-fallback-1"
    assert channel == "notebooklm_py"


def test_fastapi_endpoints_smoke(isolated_env: dict[str, Path]) -> None:
    with TestClient(app) as client:
        r_html = client.get("/")
        assert r_html.status_code == 200
        assert "NotebookLM Fleet" in r_html.text

        r_dash = client.get("/api/dashboard")
        assert r_dash.status_code == 200
        assert r_dash.json()["kpis"]["accounts_total"] >= 1

        r_add = client.post(
            "/api/accounts",
            json={"email": "ultra2@gmail.com", "tier": "ultra", "note": "2号账号"},
        )
        assert r_add.status_code == 200
        acct2 = r_add.json()["account"]
        assert acct2["alias"] == "ultra-2"

        r_topic = client.post("/api/topics", json={"name": "测试主题", "create_remote": False})
        assert r_topic.status_code == 200
        topic_id = r_topic.json()["topic"]["id"]

        r_batch = client.post(
            "/api/tasks/batch",
            json={"topic_ids": [topic_id], "actions": ["audio", "quiz"], "language": "zh-Hans"},
        )
        assert r_batch.status_code == 200
        assert r_batch.json()["created_count"] == 2

        r_inbox = client.get("/api/drive/inbox")
        assert r_inbox.status_code == 200

        r_settings = client.put("/api/settings", json={"updates": {"tier_policy": "balanced"}})
        assert r_settings.status_code == 200
        assert r_settings.json()["settings"]["tier_policy"] == "balanced"

        r_export = client.get("/api/export/json")
        assert r_export.status_code == 200
        assert len(r_export.json()["accounts"]) == 2


def test_mcp_server_clean_stdio_and_tools() -> None:
    code = (
        "import asyncio, json; "
        "from app.mcp_server import mcp; "
        "tools = asyncio.run(mcp.list_tools()); "
        "print(json.dumps([t.name for t in tools]))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    assert len(lines) == 1, f"Unexpected stdout lines: {proc.stdout!r}"
    tool_names = json.loads(lines[0])
    assert len(tool_names) == 10
    assert "fleet_quota_overview" in tool_names
    assert "fleet_enqueue_batch" in tool_names


def test_official_only_scheduling_router_file_and_crash_recovery(
    isolated_env: dict[str, Path], tmp_path: Path
) -> None:
    # 1. Official-API-only account is eligible for ingest tasks, rejected for Studio audio.
    off_settings = {
        "tier_policy": "conserve_ultra",
        "safety_buffer_pct": "10",
        "official_api_enabled": "true",
        "official_api_project_id": "proj-1",
    }
    off_acct = {
        "id": 1,
        "alias": "ultra-1",
        "email": "ultra1@gmail.com",
        "tier": "ultra",
        "enabled": 1,
        "nlm_auth_status": "unauthenticated",
        "official_oauth_status": "ok",
        "source_limit": 600,
    }
    chosen, dec_ingest, _, _ = select_account_for_task(
        {"task_type": "ingest_url"}, [off_acct], off_settings, set()
    )
    assert dec_ingest == "run" and chosen is not None and chosen["alias"] == "ultra-1"

    chosen_st, dec_audio, _, _ = select_account_for_task(
        {"task_type": "audio"}, [off_acct], off_settings, set()
    )
    assert dec_audio == "no_eligible_account" and chosen_st is None

    # 2. CapabilityRouter.ingest_file routes text/md -> create_source_text and binary -> upload_source_file.
    calls: list[tuple[str, str]] = []

    class FakeOfficialFiles:
        async def create_source_text(self, alias: str, pid: str, nb: str, title: str, text: str, **kw: object) -> dict:
            calls.append(("text", str(kw.get("mime_type"))))
            return {"name": f"{nb}/sources/txt-1"}

        async def upload_source_file(self, alias: str, pid: str, nb: str, fp: Path, **kw: object) -> dict:
            calls.append(("binary", Path(fp).suffix.lower()))
            return {"name": f"{nb}/sources/bin-1"}

    md_file = tmp_path / "doc.md"
    md_file.write_text("# 标题\n正文", encoding="utf-8")
    pdf_file = tmp_path / "paper.pdf"
    pdf_file.write_bytes(b"%PDF-1.7 test")

    router = CapabilityRouter(off_settings, official=FakeOfficialFiles())
    _, ch_md = asyncio.run(router.ingest_file(off_acct, "notebooks/nb-1", str(md_file)))
    _, ch_pdf = asyncio.run(router.ingest_file(off_acct, "notebooks/nb-1", str(pdf_file)))
    assert ch_md == "official_api" and ch_pdf == "official_api"
    assert calls == [("text", "text/markdown"), ("binary", ".pdf")]

    # 3. Lifespan startup recovers tasks left stuck in 'running' status.
    async def _seed_stale_running() -> int:
        from app.db import execute as db_exec

        await init_db()
        return await db_exec(
            "INSERT INTO tasks (task_type, payload_json, status, started_at) VALUES ('quiz', '{}', 'running', '2026-11-24 00:00:00')"
        )

    stale_id = asyncio.run(_seed_stale_running())
    with TestClient(app):
        pass
    recovered = asyncio.run(fetch_one("SELECT status, started_at FROM tasks WHERE id = ?", (stale_id,)))
    assert recovered is not None
    assert recovered["status"] == "pending"
    assert recovered["started_at"] is None
