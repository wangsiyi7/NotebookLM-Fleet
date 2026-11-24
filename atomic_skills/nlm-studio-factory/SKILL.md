---
name: nlm-studio-factory
description: Batch-produce and download all 11 NotebookLM Studio multimodal artifacts (audio, video, cinematic_video, slide_deck, quiz, flashcards, infographic, mind_map, report, study_guide, data_table) with tier-aware quota scheduling, deferred later queue handling, and Google Drive Hub archival.
---

# NLM Studio Factory（多模态 Studio 批量生产原子技能）

将文字与资料库一键转化为 **播客音频、视频概览、演示幻灯片 PDF、思维导图 JSON、信息图 PNG、学习指南、简报 Markdown、测验题、闪卡与对比数据表 CSV** 等 11 种团队协作成果，并全部自动归档至本地 Google Drive Hub。

## 支持的 11 类产出与落盘格式

| `action` 枚举 | 产物说明 | 落盘后缀 | 相对算力成本参考 |
|---|---|---|---|
| `audio` | 双人 AI 深度对谈音频播客 | `.m4a` | 高 (`audio_overview`) |
| `video` | 常规讲解视频 | `.mp4` | 高 (`video_overview`) |
| `cinematic_video` | 电影级精讲拆解视频（最耗额度） | `.mp4` | 极高 (`breakdowns_video`) |
| `slide_deck` | 演示幻灯片文稿 | `.pdf` | 高 (`slides`) |
| `infographic` | 信息图海报 | `.png` | 中 (`infographic`) |
| `report` | 简报文档（Briefing Doc） | `.md` | 中低 (`reports`) |
| `study_guide` | 系统学习指南 | `.md` | 中低 (`reports`) |
| `quiz` | 巩固测验题库 | `.md` | 低 (`quiz`) |
| `flashcards` | 记忆抽认闪卡 | `.md` | 低 (`flashcards`) |
| `mind_map` | 结构化思维导图 | `.json` | 低 (`mindmap`) |
| `data_table` | 结构化对比数据表 | `.csv` | 中低 (`tables`) |

> **关键认知**：Google 官方 REST v1/Remote MCP 目前不提供 Studio 生成端点，因此所有 Studio 产出均由 **Channel 2 (`notebooklm-py`) + Fleet 配额调度器**统一执行。

## 执行步骤

1. **先走 `nlm-quota-guard` 摸底与选档**
   - 根据重轻任务搭配选择策略：`conserve_ultra`（推荐：把 `cinematic_video`/`video`/`audio`/`slide_deck` 留给 Ultra，把 `quiz`/`flashcards`/`mind_map`/`report` 等轻任务分流给 Pro/Plus/Free）、`ultra_first` 或 `balanced`。
2. **确认 Topic 与跨账号共享耦合**
   - 通过 `fleet_list_topics()` / `GET /api/topics` 确认资料已入库。
   - 若多账号协同分摊算力，确保目标 Topic 已执行 Editor 共享或 Drive 镜像就绪（调度器也会在共享权限不足时自动回退至 Drive Mirror 副本）。
3. **批量入队 Studio 生产任务**
   - MCP：`fleet_enqueue_batch(topic_name="<topic>", actions=["report", "slide_deck", "mind_map", "audio"], language="zh-Hans", instructions="<受众、结构与语气要求>", policy="conserve_ultra")`
   - REST：`POST http://127.0.0.1:8765/api/tasks/batch`
4. **识别延时队列与配额等待状态**
   - 使用 `fleet_task_status(batch_id="<id>")` 跟进任务生命周期：
     - `done`：已自动生成并下载到 `DriveHub/02_Topics/<Topic>/Artifacts/`。
     - `deferred_later`：即时窗口已满，系统自动命中支持延时队列（`LATER_SUPPORTED_TASKS`）的 `later=True` 通道；记录 `remote_task_id` 供稍后取回。
     - `waiting_quota`：全池额度不足，已根据最早 5h 重置时间自动设定 `scheduled_after` 定时唤醒重试，无需人工死循环轮询。

## 输出规范
汇总返回 `batch_id`、每个子任务的最终状态（`done`/`deferred_later`/`waiting_quota`/`failed`）、实际执行账号与路由策略，以及所有生成文件的完整本地路径列表。
