"""Phase 0 verification CLI probe for NotebookLM-Fleet."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import nlm_py_adapter, official_api


async def cmd_usage(profile: str) -> None:
    local = nlm_py_adapter.check_local_auth(profile)
    print("Local auth:", json.dumps(local, ensure_ascii=False, indent=2))
    if local["status"] != "ok":
        print("请先登录该账号: uv run notebooklm -p", profile, "login --browser chrome")
        return
    usage = await nlm_py_adapter.poll_account_usage(profile)
    print(json.dumps(usage, ensure_ascii=False, indent=2))


async def cmd_test_share(master_profile: str, worker_profile: str, notebook_id: str) -> None:
    print(f"1) 读取 {master_profile} 与 {worker_profile} 操作前额度...")
    before_master = await nlm_py_adapter.poll_account_usage(master_profile)
    before_worker = await nlm_py_adapter.poll_account_usage(worker_profile)
    worker_email = before_worker.get("email")
    if not worker_email:
        raise RuntimeError(f"无法获取 {worker_profile} 的邮箱")

    print(f"2) 将笔记本 {notebook_id} 共享为 Editor 给 {worker_email}...")
    share_res = await nlm_py_adapter.share_editor(master_profile, notebook_id, worker_email, notify=False)
    print("Share result:", share_res)

    print(f"3) 以 {worker_profile} 发起一次 Chat 提问测试扣费归属...")
    ask_res = await nlm_py_adapter.ask_question(worker_profile, notebook_id, "请用三句话概括本笔记本的核心内容。")
    print("Ask answer preview:", (ask_res.get("answer") or "")[:200])

    after_master = await nlm_py_adapter.poll_account_usage(master_profile)
    after_worker = await nlm_py_adapter.poll_account_usage(worker_profile)

    m_delta = (after_master.get("five_hour_used_pct") or 0) - (before_master.get("five_hour_used_pct") or 0)
    w_delta = (after_worker.get("five_hour_used_pct") or 0) - (before_worker.get("five_hour_used_pct") or 0)
    print(f"实测 5h 用量变化 -> 主号({master_profile}): {m_delta:+.3f}% | 协作者({worker_profile}): {w_delta:+.3f}%")


async def cmd_test_official(alias: str, project_id: str) -> None:
    status = official_api.check_oauth_status(alias)
    print("OAuth status:", status)
    if status != "ok":
        print("先执行 OAuth 授权流程...")
        official_api.run_oauth_flow_for_account(alias)
    nbs = await official_api.list_notebooks(alias, project_id)
    print("Official API notebooks:", json.dumps(nbs, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="NotebookLM-Fleet Phase 0 Probe")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_usage = sub.add_parser("usage", help="Test live 5h/weekly quota RPC")
    p_usage.add_argument("profile")

    p_share = sub.add_parser("test-share", help="Verify editor sharing and quota attribution")
    p_share.add_argument("master_profile")
    p_share.add_argument("worker_profile")
    p_share.add_argument("notebook_id")

    p_off = sub.add_parser("test-official", help="Test official Nov 2026 Gemini Notebook API")
    p_off.add_argument("alias")
    p_off.add_argument("--project-id", required=True)

    args = parser.parse_args()
    if args.cmd == "usage":
        asyncio.run(cmd_usage(args.profile))
    elif args.cmd == "test-share":
        asyncio.run(cmd_test_share(args.master_profile, args.worker_profile, args.notebook_id))
    elif args.cmd == "test-official":
        asyncio.run(cmd_test_official(args.alias, args.project_id))


if __name__ == "__main__":
    main()
