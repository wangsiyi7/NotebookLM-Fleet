---
name: notebooklm-fleet
description: Orchestrate multiple NotebookLM (Gemini Notebook) accounts (Ultra/Pro/Free), check 5-hour and weekly quotas, manage the configured Drive Hub (NLM_Hub), batch-schedule Studio artifacts and Chat tasks, and open isolated per-account Chrome/Edge browsers for passwordless takeover.
---

# NotebookLM Fleet Orchestration Skill

Use the `notebooklm-fleet` MCP tools whenever the user wants to inspect NotebookLM quotas across their accounts, organize learning topics into their configured Drive Hub (`NLM_Hub`), ingest Drive/URL/workspace materials, batch-generate Studio content (Audio, Video, Slide Deck, Quiz, Flashcards, Infographic, Mind Map, Report, Data Table) or Chat Q&A, or troubleshoot/login an account.

## Core Rules

1. **Always check quota health first**: Call `fleet_quota_overview` (or `fleet_poll_quotas` if snapshots are stale or missing) before recommending manual assignment or explaining capacity.
2. **Never ask the user for plaintext passwords**: If any account shows `unauthenticated` or `expired`, try `POST /api/accounts/{id}/refresh-session` (silent persistent-profile renewal) first, or guide the user via the UI Ch2 login modal (1-click Chrome/Edge, interactive Windows `.cmd` login script `/api/accounts/{id}/login-script.cmd` which captures long-term `LSID + OSID`, or 5-second cURL) or `fleet_open_account_browser(alias, mode="chrome")`.
3. **Anchor everything to Topics & Drive Hub**:
   - Primary Drive Hub lives at the user's configured `DRIVE_HUB_ROOT` (typically `<DriveHub>/NLM_Hub`).
   - Prefer `fleet_create_topic_and_ingest` followed by `fleet_enqueue_batch` so the quota-aware scheduler distributes work across idle accounts according to `conserve_ultra` / `ultra_first` / `balanced` policies.
4. **Surface concrete outputs**: When reporting queued batches or finished tasks via `fleet_task_status`, always include the `batch_id`, assigned account alias, status, and resulting Drive path inside `02_Topics/<topic>/Artifacts/`.
