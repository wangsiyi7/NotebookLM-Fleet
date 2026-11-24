# NotebookLM-Fleet 并行知识库资产总览与导入指南

> **推荐目标笔记本名称**：`NotebookLM-Fleet 全景架构与代码审查知识库`  
> **存放目录**：`<NotebookLM-Fleet>\Notebook_MD_Assets_NotebookLM-Fleet_v1\`  
> **资产定位**：一套拆分为 6 篇正交、低重叠、可并行独立阅读与引用追溯的 Markdown 知识模块，专用于导入 Google Gemini Notebook / NotebookLM，生成音频播客、幻灯片、思维导图、测验与结构化报告。

---

## 一、并行资产目录总表

| 序号 | 文件名 | 核心主题 | 适合触发的 Studio 产出 |
| :--- | :--- | :--- | :--- |
| 00 | `00_资产目录与导入指南_NotebookLM-Fleet.md` | 本资产索引、导入方式与组合提问建议 | 笔记本导读摘要 |
| 01 | `01_项目全景与系统架构蓝图.md` | 三通道融合架构、目录拓扑、7 表 SQLite WAL 模型与 Drive Hub | 架构全景简报、思维导图 (`mind_map`) |
| 02 | `02_核心引擎_多账号额度调度与跨号耦合设计.md` | 5h/周额度采集、分档评分调度器、延时队列、`waiting_quota` 与跨号协作 | 算法演示 PPT (`slide_deck`)、音频播客 (`audio`) |
| 03 | `03_官方API与Remote_MCP协议集成手册.md` | 2026-11 官方 REST v1/Upload API 与 Streamable HTTP MCP 7 工具实测规范 | 开发者集成速查表 (`data_table`) |
| 04 | `04_前后端接口与全工作区控制面板指南.md` | FastAPI 路由表、左侧 `<WORKSPACE_ROOT>` 常驻工作区、选材篮与本机 10 工具 MCP | 使用手册、功能测验 (`quiz`) |
| 05 | `05_登录认证_安全边界与运维排障手册.md` | OAuth 直跳、Ch2 CDP/cURL 双模式免关浏览器登录、路径安全与灾难备份 | 运维排障闪卡 (`flashcards`) |
| 06 | `06_全量代码审查报告与修复台账_20261124.md` | 全项目代码逐文件审查结论、发现的 9 项缺陷与实测修复记录 | 代码评审报告 (`report`) |

---

## 二、如何一键存入 Notebook（3 种方式任选）

### 方式 A：通过网页控制面板一键勾选导入（推荐，直接走官方 MCP / REST）
1. 打开本地控制面板：`http://127.0.0.1:8765`。
2. 在**左侧常驻工作区**切换到根目录 `工作目录 (<WORKSPACE_ROOT>)` → 点进 `NotebookLM-Fleet` → 点进 `Notebook_MD_Assets_NotebookLM-Fleet_v1`。
3. 全选勾选 `00` ~ `06` 共 **7 份 `.md` 文件**加入左下角「已选资料篮」。
4. 点击「**⚡ 直接添加到笔记本 (官方API/MCP)**」→ 选择账号 `ultra-1` → 输入新笔记本名称 `NotebookLM-Fleet 全景架构与代码审查知识库` → 通道选 `Official Remote MCP` 或 `Auto` → 点击提交，数秒内全部 7 份文档并行转为 `ACTIVE` 来源。

### 方式 B：通过 Antigravity 对话直接调用本地 Fleet MCP
在 Antigravity 对话中直接指示：
> “用 `fleet_create_topic_and_ingest` 创建主题并把 `Notebook_MD_Assets_NotebookLM-Fleet_v1` 里的 7 个 Markdown 导入，再用 `fleet_enqueue_batch` 生成一份简报、思维导图和双人音频播客。”

### 方式 C：通过 Google Drive Hub 中枢镜像归档
将该文件夹复制一份至 `<DRIVE_HUB_ROOT>\02_Topics\NotebookLM-Fleet 全景架构与代码审查知识库\Sources\`，即可同时作为 6 个 Ultra 账号镜像备份与离线资料源。

---

## 三、高效并行提问模板（导入后可直接在 Chat 使用）

- **架构对齐审查**：*“结合 01、02 和 06 号文档，总结 NotebookLM-Fleet 在三通道路由与 5 小时/周额度调度上做了哪些防超额和防串号设计？还有哪些已知边界？”*
- **官方协议速查**：*“对比 03 号文档中的 Official Remote MCP 与 Official REST v1，列出写入文本/Markdown、网址以及大于 1MB PDF 二进制文件时分别应调用的端点和必须遵循的参数约束。”*
- **登录排障检索**：*“当 Windows 日常 Chrome 报 `browser database is locked` 时，根据 05 号文档给出不关闭日常浏览器完成 Ch2 会话授权的完整步骤。”*

---

## 四、安全声明
本目录所有文档均为脱敏后的架构设计、协议规范与代码审查记录，**不包含**任何 Google 密码、OAuth `client_secret.json` 私钥、`refresh_token` 或 Chrome 会话 Cookie。
