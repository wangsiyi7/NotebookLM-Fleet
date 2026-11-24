---
name: nlm-official-ingest
description: Create Google Gemini Notebook / NotebookLM notebooks and ingest URLs, UTF-8 text/Markdown (<=900KB), and binary files (<=200MiB such as PDF, audio, images) via Official REST v1 (/v1, /upload/v1) or Official Remote MCP (notebook.googleapis.com/mcp), verifying ACTIVE state.
---

# NLM Official Ingest（官方 API 建本与高速入库原子技能）

通过 Google 官方 Nov 2026 Gemini Notebook REST API v1 与 Remote MCP，以稳定、有官方配额保障、无浏览器风控的方式完成“建本 + 批量资料入库 + `ACTIVE` 状态确认”。

## 通道选型规则（重要）
- **文本 / Markdown (`<=900 KB`) 与网页 URL / YouTube URL**：`official_mcp` 与 `official_rest` 均支持；批量场景优先 `official_rest`。
- **二进制大文件（PDF、音频 `.mp3/.m4a`、图片等，`<=200 MiB`）**：**必须使用 `official_rest`**（走 `/upload/v1/notebooks/{id}/sources:upload?uploadType=media`）。官方 Remote MCP 当前仅支持文本与 URL。
- **单笔记本来源上限防护**：入库前先确认目标账号对应档位上限（Ultra 最高 `500` 个来源，Pro 最高 `300`，Plus `100`，Free `50`）。

## 执行步骤

1. **检查官方通道前置就绪态**
   - 请求 `GET http://127.0.0.1:8765/api/official/status`（仅确认 `client_secret_present == true`、`project_id` 非空、目标账号 `official_oauth_status == "ok"`）。
   - 若未就绪，交由 `nlm-account-ops` 指引完成一次浏览器 OAuth 302 授权；严禁读取 `client_secret.json` 或 `oauth_tokens/*.json` 内容。
2. **校验文件安全性**
   - 待上传文件路径必须属于 `<WORKSPACE_ROOT>` 或 `<DRIVE_HUB_ROOT>` 白名单，绝对禁止传入 `.env`、`client_secret.json`、`oauth_tokens/*`、`*.pem`、`*.key` 等敏感文件。
3. **执行建本与批量写入**
   - 调用 `POST http://127.0.0.1:8765/api/official/quick-add`：
     - `account_id`: 目标账号 ID
     - `notebook_id`: 现有 `notebooks/<id>`（与 `new_notebook_title` 二选一）
     - `new_notebook_title`: 新建笔记本标题
     - `channel`: `"official_rest"`（含二进制文件或稳定批处理时推荐）或 `"official_mcp"` / `"auto"`
     - `file_paths` / `urls` / `inline_text` + `mime_type`（`"text/markdown"` 或 `"text/plain"`）
4. **确认来源状态收敛为 `ACTIVE`**
   - 检查响应中的 `success_count` 与 `error_count`；若有单条失败，只针对失败项重试到已返回的 `notebook_id`（避免重复创建笔记本）。
   - 轮询 `GET http://127.0.0.1:8765/api/official/sources?account_id=<id>&notebook_id=<urlencoded>&channel=official_rest`（每隔 2 秒最多 5~6 次），直到新增来源从 `PROCESSING` 变为 `ACTIVE`。

## 输出规范
返回：
- 新建或命中的 `notebook_id`（`notebooks/<id>`）及可打开网页链接（`https://notebook.google.com/notebook/<uuid>`）；
- 每份来源的 `source_name`、当前状态（`ACTIVE`/`PROCESSING`/`FAILED`）；
- 成功/失败计数摘要与脱敏后的调用状态码（不输出任何 Token）。
