---
name: nlm-grounded-qa
description: Ask source-grounded questions against an existing NotebookLM notebook with citations, optionally save answers as in-notebook Notes, and archive Q&A transcripts to local UTF-8 Markdown files.
---

# NLM Grounded Q&A（基于真实来源的可溯源问答原子技能）

在 AI 编程或研发调研中，将复杂需求、源码架构、论文/专利事实检索卸载给 NotebookLM，获取**严格基于已入库来源、带引用编号且支持团队共享归档**的结论，减少纯大模型“凭空编造”。

## 通道说明与前置条件
- 当前 Google 官方 REST API v1 与 Remote MCP（Personal Beta）**尚未开放 Chat 问答端点**，因此本 Skill 通过 **Channel 2 (`notebooklm-py`) + Fleet 调度队列**执行，不消耗官方 REST 写入配额，只消耗少量 5h 问答额度（约 `1%`）。
- 若目标账号 `nlm_auth_status != "ok"`，先调用 `nlm-account-ops` 恢复 Ch2 会话，或改由同主题跨号共享的其它已就绪账号执行。

## 执行步骤

1. **确定目标笔记本 / Topic 与账号健康度**
   - 查询现有学习主题：`fleet_list_topics()` 或 `GET /api/topics`（亦可通过 `GET /api/official/notebooks?account_id=<id>` 获取远程笔记本列表）。
   - 调用 `nlm-quota-guard` 确认至少有一个 `nlm_auth_status == "ok"` 的账号处于空闲且 `five_hour_remaining_pct > safety_buffer`。
2. **编写高信噪比、可验证的提问 Prompt**
   - 明确要求：**仅依据笔记本内来源回答**、指出具体文档章节/文件名、若来源中未提及必须显式回答“来源未覆盖”而不要猜测。
3. **投递问答任务并可选择归档到 Note 与 Drive**
   - MCP 路径（推荐，直接归档）：
     - 调用 `fleet_enqueue_batch(topic_name="<topic>", actions=["chat"], instructions="<你的结构化问题>", policy="conserve_ultra")`
   - REST 路径（支持直指 `remote_notebook_id`）：
     - `POST http://127.0.0.1:8765/api/tasks`
     - `payload`: `{"question": "...", "remote_notebook_id": "<id>", "save_as_note": true, "note_title": "..."}`
4. **追踪结果并读取本地导出 Markdown**
   - 轮询 `fleet_task_status(batch_id="<batch_id>")` 或 `GET /api/tasks` 直到 `status == "done"`。
   - 读取返回结果中的 `result_text`（带引用回答）与本地归档文件 `result_artifact_path`（通常位于 `<DRIVE_HUB_ROOT>/02_Topics/<Topic>/Artifacts/chat_*.md`）。

## 输出规范
- 给团队/主编程 Agent 返回**结论摘要 + 原文出处映射**，并标明是否存在“来源未覆盖”的知识盲区；
- 如已落盘导出 `.md`，附上完整的本地归档路径以便后续审阅或二次加工成 PDF/Docx。
