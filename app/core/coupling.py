"""Cross-account coupling engine (shared Editor mode + Google Drive Hub mirror fallback)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..adapters.router import CapabilityRouter
from ..db import execute, fetch_one

logger = logging.getLogger(__name__)

_DRIVE_URL_RE = re.compile(r"/d/([a-zA-Z0-9_-]{10,})|[?&]id=([a-zA-Z0-9_-]{10,})")


def extract_drive_file_id(value: str) -> str:
    """Extract a Google Drive file ID from a share URL or return raw ID."""
    text = (value or "").strip()
    m = _DRIVE_URL_RE.search(text)
    if m:
        return m.group(1) or m.group(2)
    return text


async def ensure_topic_notebook_for_account(
    topic: dict[str, Any],
    worker_account: dict[str, Any],
    router: CapabilityRouter,
    *,
    db_path: Path | None = None,
    force_mirror: bool = False,
) -> tuple[dict[str, Any], str, bool]:
    """Return (notebook_row, remote_notebook_id, is_shared_mode) usable by worker_account."""
    topic_id = topic["id"]
    coupling_mode = topic.get("coupling_mode") or "shared_with_drive_fallback"

    # 1. Check if worker already owns a notebook for this topic.
    own_nb = await fetch_one(
        "SELECT * FROM notebooks WHERE topic_id = ? AND account_id = ? ORDER BY is_master DESC, id ASC LIMIT 1",
        (topic_id, worker_account["id"]),
        db_path=db_path,
    )
    if own_nb is not None:
        return own_nb, own_nb["remote_notebook_id"], False

    # 2. Check master notebook for shared Editor collaboration.
    master_nb = await fetch_one(
        "SELECT * FROM notebooks WHERE topic_id = ? AND is_master = 1 ORDER BY id ASC LIMIT 1",
        (topic_id,),
        db_path=db_path,
    )
    if (
        not force_mirror
        and coupling_mode in ("shared", "shared_with_drive_fallback")
        and master_nb is not None
    ):
        worker_email = (worker_account.get("email") or "").strip()
        is_placeholder_email = (
            not worker_email
            or "@" not in worker_email
            or worker_email.lower().endswith((".local", "@example.com"))
        )
        master_acct = await fetch_one(
            "SELECT * FROM accounts WHERE id = ?",
            (master_nb["account_id"],),
            db_path=db_path,
        )
        if not is_placeholder_email and master_acct and master_acct.get("nlm_auth_status") == "ok":
            shared_emails: list[str] = []
            try:
                shared_emails = json.loads(master_nb.get("shared_emails_json") or "[]")
            except json.JSONDecodeError:
                shared_emails = []

            if worker_email not in shared_emails:
                try:
                    await router.share_editor(
                        master_acct,
                        master_nb["remote_notebook_id"],
                        worker_email,
                        notify=False,
                    )
                    shared_emails.append(worker_email)
                    await execute(
                        """
                        UPDATE notebooks
                           SET shared_emails_json = ?,
                               synced_at = datetime('now', 'localtime')
                         WHERE id = ?
                        """,
                        (json.dumps(shared_emails, ensure_ascii=False), master_nb["id"]),
                        db_path=db_path,
                    )
                    return master_nb, master_nb["remote_notebook_id"], True
                except Exception as exc:  # noqa: BLE001
                    if coupling_mode != "shared_with_drive_fallback":
                        raise
                    logger.warning(
                        "share_editor failed for %s -> %s, falling back to Drive mirror: %s",
                        master_acct.get("alias"),
                        worker_email,
                        exc,
                    )
            else:
                return master_nb, master_nb["remote_notebook_id"], True

    # 3. Create a dedicated/mirror notebook on worker_account and populate from topic Sources/.
    is_master = 1 if master_nb is None else 0
    nb_title = topic["name"] if is_master else f"{topic['name']} ({worker_account['alias']})"
    created, _channel = await router.create_notebook(worker_account, nb_title)
    remote_id = created["id"]

    nb_row_id = await execute(
        """
        INSERT INTO notebooks (topic_id, account_id, remote_notebook_id, title, is_master)
        VALUES (?, ?, ?, ?, ?)
        """,
        (topic_id, worker_account["id"], remote_id, created.get("title") or nb_title, is_master),
        db_path=db_path,
    )

    # Mirror local topic Sources/ files if any exist.
    sources_dir = Path(topic["drive_folder_path"]) / "Sources"
    uploaded = 0
    if sources_dir.exists():
        for file_path in sorted(sources_dir.iterdir()):
            if file_path.is_file() and not file_path.name.startswith("."):
                try:
                    await router.ingest_file(worker_account, remote_id, str(file_path), title=file_path.name)
                    uploaded += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Mirror ingest skipped %s: %s", file_path.name, exc)

    if uploaded:
        await execute("UPDATE notebooks SET source_count = ? WHERE id = ?", (uploaded, nb_row_id), db_path=db_path)

    new_nb = await fetch_one("SELECT * FROM notebooks WHERE id = ?", (nb_row_id,), db_path=db_path)
    assert new_nb is not None
    return new_nb, remote_id, False
