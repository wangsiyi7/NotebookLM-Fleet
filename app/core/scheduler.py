"""Quota-aware, tier-aware task scheduler and background execution worker."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..adapters import nlm_py_adapter
from ..adapters.router import CapabilityRouter
from ..db import (
    TIER_LIMITS,
    execute,
    fetch_all,
    fetch_one,
    get_accounts_with_latest_quota,
    get_settings_map,
)
from .coupling import ensure_topic_notebook_for_account, extract_drive_file_id
from .drive_hub import ensure_topic_folders, save_text_export
from .quota_poller import poll_one_account

logger = logging.getLogger(__name__)

HEAVY_TASKS = frozenset({"cinematic_video", "video", "audio", "slide_deck"})
OFFICIAL_SUPPORTED_TASKS = frozenset({"ingest_url", "ingest_text", "ingest_file"})
STUDIO_TASKS = frozenset(
    {
        "audio",
        "video",
        "cinematic_video",
        "slide_deck",
        "quiz",
        "flashcards",
        "infographic",
        "mind_map",
        "report",
        "study_guide",
        "data_table",
    }
)
LATER_SUPPORTED_TASKS = frozenset(
    {"audio", "video", "slide_deck", "quiz", "flashcards", "infographic", "report", "study_guide"}
)

TASK_TO_USAGE_ACTION: dict[str, str] = {
    "audio": "audio_overview",
    "video": "video_overview",
    "cinematic_video": "breakdowns_video",
    "slide_deck": "slides",
    "quiz": "quiz",
    "flashcards": "flashcards",
    "infographic": "infographic",
    "mind_map": "mindmap",
    "report": "reports",
    "study_guide": "reports",
    "data_table": "tables",
    "chat": "qna",
}

DEFAULT_COST_EST: dict[str, float] = {
    "cinematic_video": 20.0,
    "video": 15.0,
    "audio": 12.0,
    "slide_deck": 10.0,
    "infographic": 6.0,
    "report": 4.0,
    "study_guide": 4.0,
    "quiz": 3.0,
    "flashcards": 3.0,
    "mind_map": 2.0,
    "data_table": 3.0,
    "chat": 1.0,
    "ingest_url": 0.0,
    "ingest_file": 0.0,
    "ingest_drive": 0.0,
    "ingest_text": 0.0,
    "share_notebook": 0.0,
}


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _find_action_quota(account: dict[str, Any], task_type: str) -> dict[str, Any] | None:
    usage_kind = TASK_TO_USAGE_ACTION.get(task_type)
    if not usage_kind:
        return None
    for act in account.get("actions") or []:
        if act.get("kind") == usage_kind:
            return act
    return None


def _tier_matches(tier: str, req: str) -> bool:
    if req in ("any", "free_ok", "", None):
        return True
    if req == "ultra":
        return tier == "ultra"
    if req == "pro_plus":
        return tier in ("ultra", "pro", "plus")
    return True


def _tier_weight(tier: str, task_type: str, policy: str) -> float:
    if policy == "ultra_first":
        return {"ultra": 100.0, "pro": 50.0, "plus": 30.0, "free": 10.0}.get(tier, 10.0)
    if policy == "balanced":
        return 40.0
    # Default: conserve_ultra (heavy -> ultra, light -> free/pro first)
    if task_type in HEAVY_TASKS:
        return {"ultra": 100.0, "pro": 50.0, "plus": 30.0, "free": 10.0}.get(tier, 10.0)
    return {"free": 70.0, "pro": 50.0, "plus": 35.0, "ultra": 10.0}.get(tier, 10.0)


def select_account_for_task(
    task: dict[str, Any],
    accounts_with_quota: list[dict[str, Any]],
    settings: dict[str, str],
    busy_account_ids: set[int],
    *,
    source_count: int = 0,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str, float, str | None]:
    """Select the best account for a task.

    Returns:
        (account_or_none, decision, est_cost_pct, scheduled_after_iso)
        decision in: 'run' | 'deferred_later' | 'waiting_quota' | 'no_eligible_account'
    """
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    task_type = task["task_type"]
    tier_req = task.get("tier_requirement") or "any"
    policy = settings.get("tier_policy") or "conserve_ultra"
    safety_buffer = float(settings.get("safety_buffer_pct") or 10.0)

    payload: dict[str, Any] = {}
    try:
        payload = json.loads(task.get("payload_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    strict_account = bool(payload.get("strict_account"))
    pref_id = task.get("preferred_account_id")
    official_ready = (
        str(settings.get("official_api_enabled", "false")).lower() == "true"
        and bool((settings.get("official_api_project_id") or "").strip())
    )

    eligible: list[dict[str, Any]] = []
    for acct in accounts_with_quota:
        if not acct.get("enabled"):
            continue
        can_ch2 = acct.get("nlm_auth_status") == "ok"
        can_ch1 = (
            task_type in OFFICIAL_SUPPORTED_TASKS
            and official_ready
            and acct.get("official_oauth_status") == "ok"
        )
        if not can_ch2 and not can_ch1:
            continue
        if acct["id"] in busy_account_ids:
            continue
        if strict_account and pref_id and acct["id"] != pref_id:
            continue
        tier = acct.get("tier") or "free"
        if not _tier_matches(tier, tier_req):
            continue
        src_limit = acct.get("source_limit") or TIER_LIMITS.get(tier, (100, 50))[1]
        if source_count > src_limit:
            continue
        eligible.append(acct)

    if not eligible:
        return None, "no_eligible_account", DEFAULT_COST_EST.get(task_type, 2.0), None

    best_acct: dict[str, Any] | None = None
    best_score = float("-inf")
    best_cost = DEFAULT_COST_EST.get(task_type, 2.0)

    for acct in eligible:
        action_row = _find_action_quota(acct, task_type)
        est_cost = (
            float(action_row["estimated_cost_percent"])
            if action_row and action_row.get("estimated_cost_percent") is not None
            else DEFAULT_COST_EST.get(task_type, 2.0)
        )
        has_snapshot = acct.get("quota_status") not in (None, "error") and (
            acct.get("five_hour_remaining_pct") is not None
        )
        if has_snapshot:
            if acct.get("is_exhausted"):
                continue
            if action_row is not None and action_row.get("has_sufficient_quota") is False:
                continue
            five_rem = float(acct["five_hour_remaining_pct"])
            weekly_rem = (
                float(acct["weekly_remaining_pct"])
                if acct.get("weekly_remaining_pct") is not None
                else 100.0
            )
            if est_cost > 0:
                if (five_rem - est_cost) < safety_buffer:
                    continue
                if weekly_rem < 5.0:
                    continue
        else:
            five_rem = 100.0
            weekly_rem = 100.0

        tier = acct.get("tier") or "free"
        score = _tier_weight(tier, task_type, policy)

        reset_dt = _parse_iso_utc(acct.get("five_hour_resets_at"))
        if reset_dt is not None and reset_dt > now_utc:
            hours_to_reset = max(0.0, min(5.0, (reset_dt - now_utc).total_seconds() / 3600.0))
            score += (5.0 - hours_to_reset) * 6.0

        score += weekly_rem * 0.2
        score += five_rem * 0.05
        if pref_id and acct["id"] == pref_id:
            score += 500.0

        if score > best_score:
            best_score = score
            best_acct = acct
            best_cost = est_cost

    if best_acct is not None:
        return best_acct, "run", best_cost, None

    # No immediate quota capacity — check Generate Later slot if supported.
    if task_type in LATER_SUPPORTED_TASKS:
        deferred_candidates: list[tuple[int, dict[str, Any], float]] = []
        for acct in eligible:
            action_row = _find_action_quota(acct, task_type)
            slots = int((action_row or {}).get("remaining_deferred_artifact_generations") or 0)
            if slots > 0:
                est_c = (
                    float(action_row["estimated_cost_percent"])
                    if action_row and action_row.get("estimated_cost_percent") is not None
                    else DEFAULT_COST_EST.get(task_type, 4.0)
                )
                deferred_candidates.append((slots, acct, est_c))
        if deferred_candidates:
            deferred_candidates.sort(key=lambda item: item[0], reverse=True)
            _, chosen, chosen_cost = deferred_candidates[0]
            return chosen, "deferred_later", chosen_cost, None

    # Compute earliest 5-hour window reset + 2 minutes buffer.
    resets = [
        dt
        for dt in (_parse_iso_utc(a.get("five_hour_resets_at")) for a in eligible)
        if dt is not None and dt > now_utc
    ]
    if resets:
        wake_dt = min(resets) + timedelta(minutes=2)
    else:
        wake_dt = now_utc + timedelta(minutes=15)
    return None, "waiting_quota", best_cost, wake_dt.isoformat()


async def _execute_single_task(
    task: dict[str, Any],
    account: dict[str, Any],
    *,
    later: bool,
    est_cost: float,
    db_path: Path | None = None,
) -> None:
    settings = await get_settings_map(db_path=db_path)
    router = CapabilityRouter(settings)
    task_type = task["task_type"]
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(task.get("payload_json") or "{}")
    except json.JSONDecodeError:
        payload = {}

    pre_five_used = account.get("five_hour_used_pct")
    topic = (
        await fetch_one("SELECT * FROM topics WHERE id = ?", (task["topic_id"],), db_path=db_path)
        if task.get("topic_id")
        else None
    )

    await execute(
        """
        UPDATE tasks
           SET status = 'running',
               assigned_account_id = ?,
               est_cost_pct = ?,
               started_at = datetime('now', 'localtime'),
               error_msg = NULL
         WHERE id = ?
        """,
        (account["id"], est_cost, task["id"]),
        db_path=db_path,
    )

    channel_used = "notebooklm_py"
    remote_task_id = None
    result_artifact_path = None
    result_text = None
    final_status = "done"

    try:
        # Resolve target notebook via coupling engine (or explicit notebook_id).
        nb_row = None
        remote_nb_id = payload.get("remote_notebook_id")
        if not remote_nb_id and task.get("notebook_id"):
            nb_row = await fetch_one(
                "SELECT * FROM notebooks WHERE id = ?", (task["notebook_id"],), db_path=db_path
            )
            if nb_row:
                remote_nb_id = nb_row["remote_notebook_id"]

        if not remote_nb_id and topic is not None:
            nb_row, remote_nb_id, is_shared = await ensure_topic_notebook_for_account(
                topic, account, router, db_path=db_path
            )
        else:
            is_shared = False

        if not remote_nb_id:
            raise ValueError("任务缺少关联主题或笔记本 ID")

        hub_root = settings.get("drive_hub_root") or ""
        topic_name = topic["name"] if topic else None
        out_dir = (
            str(ensure_topic_folders(hub_root, topic_name)["artifacts"])
            if topic_name and hub_root
            else str(Path(hub_root) / "03_Exports")
        )

        if task_type == "ingest_url":
            _, channel_used = await router.ingest_url(
                account, remote_nb_id, payload["url"], title=payload.get("title")
            )
            result_text = f"已导入 URL: {payload['url']}"
        elif task_type == "ingest_text":
            _, channel_used = await router.ingest_text(
                account, remote_nb_id, payload.get("title") or "笔记文本", payload["text"]
            )
            result_text = f"已导入文本: {payload.get('title') or '笔记文本'}"
        elif task_type == "ingest_file":
            _, channel_used = await router.ingest_file(
                account, remote_nb_id, payload["file_path"], title=payload.get("title")
            )
            result_text = f"已导入文件: {payload['file_path']}"
        elif task_type == "ingest_drive":
            fid = extract_drive_file_id(payload["file_id"])
            _, channel_used = await router.ingest_drive(
                account,
                remote_nb_id,
                fid,
                payload.get("title") or fid,
                mime_type=payload.get("mime_type") or "google-doc",
                upload_only=bool(payload.get("upload_only")),
            )
            result_text = f"已导入 Drive 资源: {fid}"
        elif task_type == "share_notebook":
            _, channel_used = await router.share_editor(
                account, remote_nb_id, payload["target_email"], notify=bool(payload.get("notify", False))
            )
            result_text = f"已共享 Editor 给 {payload['target_email']}"
        elif task_type == "chat":
            res, channel_used = await router.ask(
                account,
                remote_nb_id,
                payload["question"],
                save_as_note=bool(payload.get("save_as_note", True)),
                note_title=payload.get("note_title"),
            )
            result_text = res.get("answer", "")
            if hub_root and result_text:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                export_md = f"# {payload['question']}\n\n{result_text}\n"
                saved_path = save_text_export(hub_root, topic_name, f"chat_{stamp}.md", export_md)
                result_artifact_path = str(saved_path)
                await execute(
                    """
                    INSERT INTO artifacts (topic_id, task_id, account_id, notebook_id, kind, title, local_drive_path)
                    VALUES (?, ?, ?, ?, 'chat_export', ?, ?)
                    """,
                    (
                        task.get("topic_id"),
                        task["id"],
                        account["id"],
                        nb_row["id"] if nb_row else None,
                        payload["question"][:80],
                        result_artifact_path,
                    ),
                    db_path=db_path,
                )
        elif task_type in STUDIO_TASKS:
            try:
                res, channel_used = await router.generate_and_download(
                    account,
                    remote_nb_id,
                    task_type,
                    out_dir,
                    instructions=payload.get("instructions"),
                    language=payload.get("language") or settings.get("default_language") or "zh-Hans",
                    later=later,
                    title_prefix=topic_name or "",
                )
            except Exception as shared_exc:
                # Fallback to Drive mirror mode if shared Editor execution failed due to permission/owner limits.
                if is_shared and topic and (topic.get("coupling_mode") or "shared_with_drive_fallback") == "shared_with_drive_fallback":
                    logger.warning("Shared execution failed (%s), retrying via Drive mirror on %s", shared_exc, account["alias"])
                    nb_row, mirror_nb_id, _ = await ensure_topic_notebook_for_account(
                        topic, account, router, db_path=db_path, force_mirror=True
                    )
                    res, channel_used = await router.generate_and_download(
                        account,
                        mirror_nb_id,
                        task_type,
                        out_dir,
                        instructions=payload.get("instructions"),
                        language=payload.get("language") or settings.get("default_language") or "zh-Hans",
                        later=later,
                        title_prefix=topic_name or "",
                    )
                else:
                    raise

            remote_task_id = res.get("remote_task_id")
            result_artifact_path = res.get("local_drive_path")
            if res.get("status") == "deferred_later":
                final_status = "deferred_later"
                result_text = f"已提交 Generate Later 延时队列 (task_id={remote_task_id})"
            else:
                result_text = f"已生成并归档至 {result_artifact_path}"
                await execute(
                    """
                    INSERT INTO artifacts (
                        topic_id, task_id, account_id, notebook_id, remote_artifact_id,
                        kind, title, local_drive_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.get("topic_id"),
                        task["id"],
                        account["id"],
                        nb_row["id"] if nb_row else None,
                        remote_task_id,
                        task_type,
                        res.get("title") or task_type,
                        result_artifact_path,
                    ),
                    db_path=db_path,
                )
        else:
            raise ValueError(f"不支持的任务类型: {task_type}")

        # Measure actual 5h usage delta after task execution.
        actual_delta = None
        if task_type in STUDIO_TASKS or task_type == "chat":
            try:
                post_usage = await poll_one_account(account["id"], db_path=db_path)
                post_five = post_usage.get("five_hour_used_pct")
                if pre_five_used is not None and post_five is not None:
                    actual_delta = round(max(0.0, float(post_five) - float(pre_five_used)), 3)
            except Exception as poll_exc:  # noqa: BLE001
                logger.warning("Post-task quota poll failed for %s: %s", account["alias"], poll_exc)

        await execute(
            """
            UPDATE tasks
               SET status = ?,
                   channel_used = ?,
                   remote_task_id = ?,
                   result_artifact_path = ?,
                   result_text = ?,
                   actual_delta_5h_pct = ?,
                   finished_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (
                final_status,
                channel_used,
                remote_task_id,
                result_artifact_path,
                result_text,
                actual_delta,
                task["id"],
            ),
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Task %s failed on %s: %s", task["id"], account["alias"], exc)
        await execute(
            """
            UPDATE tasks
               SET status = 'failed',
                   channel_used = ?,
                   error_msg = ?,
                   finished_at = datetime('now', 'localtime')
             WHERE id = ?
            """,
            (channel_used, str(exc)[:1000], task["id"]),
            db_path=db_path,
        )


async def run_scheduler_tick(*, db_path: Path | None = None) -> int:
    """Inspect pending/waiting tasks and dispatch eligible work across idle accounts."""
    now_utc = datetime.now(timezone.utc)
    tasks = await fetch_all(
        """
        SELECT t.*, nb.source_count AS nb_source_count
          FROM tasks t
          LEFT JOIN notebooks nb ON nb.id = t.notebook_id
         WHERE t.status IN ('pending', 'waiting_quota')
         ORDER BY t.priority ASC, t.id ASC
         LIMIT 20
        """,
        db_path=db_path,
    )
    if not tasks:
        return 0

    running_rows = await fetch_all(
        "SELECT assigned_account_id FROM tasks WHERE status = 'running' AND assigned_account_id IS NOT NULL",
        db_path=db_path,
    )
    busy_ids: set[int] = {int(r["assigned_account_id"]) for r in running_rows}
    accounts = await get_accounts_with_latest_quota(db_path=db_path)
    settings = await get_settings_map(db_path=db_path)

    dispatched = 0
    for task in tasks:
        sched_after = _parse_iso_utc(task.get("scheduled_after"))
        if sched_after is not None and sched_after > now_utc:
            continue

        source_count = int(task.get("nb_source_count") or 0)
        if source_count == 0 and task.get("topic_id"):
            row = await fetch_one(
                "SELECT COALESCE(MAX(source_count), 0) AS max_src FROM notebooks WHERE topic_id = ?",
                (task["topic_id"],),
                db_path=db_path,
            )
            source_count = int((row or {}).get("max_src") or 0)

        acct, decision, est_cost, wake_iso = select_account_for_task(
            task, accounts, settings, busy_ids, source_count=source_count, now=now_utc
        )

        if decision == "run" and acct is not None:
            busy_ids.add(acct["id"])
            await _execute_single_task(task, acct, later=False, est_cost=est_cost, db_path=db_path)
            dispatched += 1
            delay_min = float(settings.get("task_delay_min_sec") or 8)
            delay_max = float(settings.get("task_delay_max_sec") or 18)
            await asyncio.sleep(random.uniform(delay_min, max(delay_min, delay_max)))
        elif decision == "deferred_later" and acct is not None:
            busy_ids.add(acct["id"])
            await _execute_single_task(task, acct, later=True, est_cost=est_cost, db_path=db_path)
            dispatched += 1
        elif decision == "waiting_quota":
            await execute(
                """
                UPDATE tasks
                   SET status = 'waiting_quota',
                       scheduled_after = ?,
                       est_cost_pct = ?
                 WHERE id = ?
                """,
                (wake_iso, est_cost, task["id"]),
                db_path=db_path,
            )
        elif decision == "no_eligible_account":
            # Keep pending until an eligible account is enabled / authenticated.
            await execute(
                "UPDATE tasks SET est_cost_pct = ? WHERE id = ?",
                (est_cost, task["id"]),
                db_path=db_path,
            )
    return dispatched


async def scheduler_loop(stop_event: asyncio.Event, *, db_path: Path | None = None) -> None:
    """Continuous scheduler background loop."""
    while not stop_event.is_set():
        try:
            await run_scheduler_tick(db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduler tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            continue
