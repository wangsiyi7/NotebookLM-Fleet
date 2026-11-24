"""Tests for v0.2.0 notebook inventory, Studio proportion analytics, schema migration, and sub-page routes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters import nlm_py_adapter
from app.db import fetch_all, fetch_one, init_db
from app.main import app


@pytest.fixture()
def nb_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data_dir = tmp_path / "data"
    db_file = data_dir / "fleet_nb.db"
    ws_dir = tmp_path / "Workspace"
    ws_dir.mkdir(parents=True)
    (ws_dir / "note.md").write_text("# 笔记\n\n测试内容", encoding="utf-8")
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


def test_compute_studio_breakdown_percentages_and_sorting() -> None:
    empty = nlm_py_adapter.compute_studio_breakdown([])
    assert empty["total"] == 0
    assert empty["by_kind"] == []

    sample = [
        {"id": "a1", "kind": "video", "status": "completed"},
        {"id": "a2", "kind": "video", "status": "completed"},
        {"id": "a3", "kind": "video", "status": "completed"},
        {"id": "a4", "kind": "slide_deck", "status": "completed"},
        {"id": "a5", "kind": "slide_deck", "status": "in_progress"},
        {"id": "a6", "kind": "quiz", "status": "completed"},
    ]
    res = nlm_py_adapter.compute_studio_breakdown(sample, notes_count=2)
    assert res["total"] == 6
    assert res["completed"] == 5
    assert res["in_progress"] == 1
    assert res["notes_count"] == 2
    assert [x["kind"] for x in res["by_kind"]] == ["video", "slide_deck", "quiz"]
    assert res["by_kind"][0]["pct"] == 50.0
    assert res["by_kind"][1]["pct"] == 33.3
    assert res["by_kind"][2]["pct"] == 16.7


def test_legacy_notebooks_schema_migration(tmp_path: Path) -> None:
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.executescript(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'ultra',
            nlm_profile TEXT NOT NULL UNIQUE,
            chrome_profile_dir TEXT NOT NULL,
            cdp_port INTEGER,
            is_hub_master INTEGER NOT NULL DEFAULT 0,
            drive_shared_ready INTEGER NOT NULL DEFAULT 0,
            nlm_auth_status TEXT NOT NULL DEFAULT 'unauthenticated',
            official_oauth_status TEXT NOT NULL DEFAULT 'not_configured',
            notebook_limit INTEGER DEFAULT 500,
            source_limit INTEGER DEFAULT 600,
            expire_date TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE notebooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER,
            account_id INTEGER NOT NULL,
            remote_notebook_id TEXT NOT NULL,
            title TEXT NOT NULL,
            is_master INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0,
            artifact_count INTEGER NOT NULL DEFAULT 0,
            shared_emails_json TEXT NOT NULL DEFAULT '[]',
            synced_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(account_id, remote_notebook_id)
        );
        INSERT INTO accounts (alias, email, nlm_profile, chrome_profile_dir) VALUES ('ultra-1', 'u1@example.com', 'ultra-1', '/tmp/p');
        INSERT INTO notebooks (account_id, remote_notebook_id, title, source_count) VALUES (1, 'nb-legacy-1', 'Old NB', 4);
        """
    )
    conn.close()

    asyncio.run(init_db(db_path=legacy_db))
    conn = sqlite3.connect(str(legacy_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notebooks)").fetchall()}
    row = conn.execute("SELECT title, role, studio_stats_json FROM notebooks WHERE remote_notebook_id = 'nb-legacy-1'").fetchone()
    conn.close()

    assert {"role", "studio_stats_json", "remote_artifacts_json", "remote_sources_json", "studio_synced_at"} <= cols
    assert row is not None and row[0] == "Old NB" and row[1] == "owner"


def test_notebooks_sync_studio_scan_and_subpage_endpoints(
    nb_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _seed() -> None:
        from app.db import execute as db_exec

        await init_db()
        await db_exec("UPDATE accounts SET nlm_auth_status = 'ok' WHERE id = 1")

    asyncio.run(_seed())

    async def fake_list_notebooks(profile: str) -> list[dict[str, Any]]:
        return [
            {"id": "nb-alpha", "title": "Alpha Research", "sources_count": 7, "is_owner": True, "role": "owner"},
            {"id": "nb-beta", "title": "Beta Shared", "sources_count": 12, "is_owner": False, "role": "editor"},
        ]

    alpha_arts = [
        {"id": "art-1", "title": "Intro Video", "kind": "video", "label": "视频概览", "icon": "🎬", "color": "#136f63", "status": "completed", "created_at": "2026-11-24T00:00:00+00:00", "slides_count": 0, "infographics_count": 0, "source_ids_count": 3, "downloadable": True},
        {"id": "art-2", "title": "Deck", "kind": "slide_deck", "label": "幻灯片", "icon": "📊", "color": "#26547c", "status": "completed", "created_at": "2026-11-24T00:01:00+00:00", "slides_count": 10, "infographics_count": 0, "source_ids_count": 3, "downloadable": True},
    ]

    async def fake_batch_scan(profile: str, nb_ids: list[str], *, concurrency: int = 4) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for nid in nb_ids:
            arts = alpha_arts if nid == "nb-alpha" else [{"id": "art-b1", "title": "Audio", "kind": "audio", "label": "音频播客", "icon": "🎙️", "color": "#c47d2b", "status": "completed", "slides_count": 0, "infographics_count": 0, "source_ids_count": 2, "downloadable": True}]
            out[nid] = {
                "artifacts": arts,
                "studio_stats": nlm_py_adapter.compute_studio_breakdown(arts),
            }
        return out

    async def fake_detail(profile: str, nb_id: str) -> dict[str, Any]:
        return {
            "sources": [{"id": "src-1", "title": "Spec.md", "kind": "markdown", "status": "ready", "url": None, "word_count": 420, "created_at": "2026-11-24T00:00:00+00:00"}],
            "artifacts": alpha_arts,
            "notes": [{"id": "n-1", "title": "Summary", "content": "Note body", "created_at": None}],
            "studio_stats": nlm_py_adapter.compute_studio_breakdown(alpha_arts, notes_count=1),
        }

    async def fake_ask(profile: str, nb_id: str, question: str, **kw: Any) -> dict[str, Any]:
        return {"answer": f"Grounded answer to: {question}", "conversation_id": "conv-1", "turn_number": 1, "references_count": 2, "saved_note_id": None}

    async def fake_add_url(profile: str, nb_id: str, url: str, **kw: Any) -> dict[str, Any]:
        return {"id": "src-url-1", "title": url}

    monkeypatch.setattr(nlm_py_adapter, "list_notebooks", fake_list_notebooks)
    monkeypatch.setattr(nlm_py_adapter, "batch_scan_studio_stats", fake_batch_scan)
    monkeypatch.setattr(nlm_py_adapter, "list_notebook_studio_detail", fake_detail)
    monkeypatch.setattr(nlm_py_adapter, "ask_question", fake_ask)
    monkeypatch.setattr(nlm_py_adapter, "add_url", fake_add_url)

    with TestClient(app) as client:
        # 1. sync-all scans top 1 notebook Studio stats immediately
        r_sync = client.post("/api/notebooks/sync-all?scan_studio_limit=1")
        assert r_sync.status_code == 200
        d_sync = r_sync.json()
        assert d_sync["synced_notebooks"] == 2
        assert d_sync["studio_scanned"] == 1
        assert d_sync["total_in_db"] == 2

        # 2. GET /api/notebooks shows full list + Studio summary + filters
        r_list = client.get("/api/notebooks", params={"sort": "artifacts"})
        assert r_list.status_code == 200
        dl = r_list.json()
        assert dl["total"] == 2
        assert dl["global_studio_summary"]["total_artifacts"] == 2
        assert dl["notebooks"][0]["remote_notebook_id"] == "nb-alpha"
        assert dl["notebooks"][0]["source_count"] == 7
        assert dl["notebooks"][0]["studio_stats"]["by_kind"][0]["pct"] == 50.0
        alpha_db_id = dl["notebooks"][0]["id"]

        # 3. scan-studio fills remaining unscanned Beta notebook
        r_scan = client.post("/api/notebooks/scan-studio", json={"limit": 10, "only_unscanned": True})
        assert r_scan.status_code == 200
        assert r_scan.json()["scanned"] == 1
        assert r_scan.json()["remaining_unscanned"] == 0

        r_only_owner = client.get("/api/notebooks", params={"only_owner": True})
        assert r_only_owner.json()["total"] == 1

        # 4. Dedicated notebook sub-page HTML & detail API
        r_html = client.get(f"/notebooks/{alpha_db_id}")
        assert r_html.status_code == 200
        assert "Studio 内容构成与占比统计" in r_html.text

        r_det = client.get(f"/api/notebooks/{alpha_db_id}", params={"refresh": True})
        assert r_det.status_code == 200
        dd = r_det.json()
        assert len(dd["sources"]) == 1
        assert len(dd["artifacts"]) == 2
        assert dd["studio_stats"]["notes_count"] == 1

        # 5. Chat, Generate & Ingest directly from the notebook sub-page
        r_chat = client.post(
            f"/api/notebooks/{alpha_db_id}/chat",
            json={"question": "核心结论是什么", "save_markdown_export": True},
        )
        assert r_chat.status_code == 200
        assert "Grounded answer" in r_chat.json()["answer"]
        assert r_chat.json()["saved_md_path"] is not None

        r_gen = client.post(
            f"/api/notebooks/{alpha_db_id}/generate",
            json={"actions": ["audio", "quiz"], "instructions": "Focus on key diffs"},
        )
        assert r_gen.status_code == 200
        assert r_gen.json()["created_count"] == 2

        r_ing = client.post(
            f"/api/notebooks/{alpha_db_id}/ingest",
            json={"urls": ["https://example.com/spec"]},
        )
        assert r_ing.status_code == 200
        assert r_ing.json()["success_count"] == 1

        # Dashboard reflects synced notebooks + remote artifacts total
        r_dash = client.get("/api/dashboard")
        assert r_dash.json()["kpis"]["notebooks_count"] == 2
        assert r_dash.json()["kpis"]["remote_artifacts_total"] == 3
