# NotebookLM-Fleet 系统能力全景、开放架构与原子化扩展白皮书（v1.0）

> **文档版本**：`v1.0`  
> **归档日期**：`2026-11-24`  
> **编码格式**：`UTF-8`（支持 Markdown / PDF / Word & Google Docs 多形态交付）  
> **适用对象**：团队新人 / 非全栈同学（通俗科普）、核心研发与架构师（能力边界与集成选型）、外部技术分享与方案交流（已严格脱敏）  
> **配套产物**：跨 AI 编程工具原子技能包 [`atomic_skills/`](../../atomic_skills/README.md) · 并行知识库资产 [`Notebook_MD_Assets_NotebookLM-Fleet_v1/`](../../Notebook_MD_Assets_NotebookLM-Fleet_v1/00_资产目录与导入指南_NotebookLM-Fleet.md)

---

## 执行摘要（3 分钟读懂全部结论）

如果你时间有限，先看以下 **4 条核心结论**：

1. **我们到底建了一套什么系统？**  

    `NotebookLM-Fleet` 不是一个简单的脚本，而是一个**“本地优先的多账号 NotebookLM 能力网关与智能调度底座”**。它将 **Google 官方 API / Remote MCP**（2026 年 11 月新发布的官方协议）、**Web 增强协议通道 (`notebooklm-py`)** 以及 **独立隔离 Chrome CDP 通道** 融合在一个控制平面中，对外统一暴露 **47 个 HTTP REST 接口**、**10 个本机 Stdio MCP 工具** 和 **一套 Web 控制面板**，并由 **SQLite (WAL) 状态库 + Google Drive Hub 文件夹** 承担持久化与产物归档。

2. **核心问题一：相比点网页，API 模式到底能做什么？**  

    - **能自动建库、批量灌料、打通 AI 软件**：不再需要人工打开十几个网页标签逐个上传文档，一条 API 即可批量创建笔记本，并行写入网页 URL、UTF-8 文本/Markdown（`<=900 KB`）以及高达 **`200 MiB` 的 PDF/音频等二进制文件**，并自动轮询校验直至全部变成 `ACTIVE` 可用状态。
    - **能“摸清家底”并防止单号刷爆**：通过全系统实时监控，精确感知每个账号 **5 小时窗口** 与 **周窗口** 剩余百分比，以及多达 **29 项动作** 的估算消耗，并由调度器自动按 `conserve_ultra` 策略分流任务，撞满上限自动转 `later=True` 官方延时队列或进入 `waiting_quota` 等窗口重置后自动唤醒。
    - **能全自动“读书 + 产媒体 + 落盘归档”**：全自动排队产出音频播客、视频拆解、幻灯片 PDF、思维导图、测验、闪卡、报告等 **11 类 Studio 产出** 及可溯源 Chat 问答，产成后无需手动下载，直接归档至标准化本地网盘目录。

3. **核心问题二：能否把这些能力集成进我们后续的独立软件开发中？**  

    **明确答案：完全可以，但必须按场景“分层使用”。**

    - **内部研发工具 / 团队知识自动化 / 个人生产力产品（即刻可用）**：可以将 `NotebookLM-Fleet` 作为本地/内网 Sidecar 微服务网关，通过 **REST API** 或 **MCP 协议** 无缝嵌入到知识库构建、代码审查助手、自动周报与播客生成器、企业内部培训系统等独立项目中，相当于**零成本获得“零向量库可溯源 RAG 引擎 + 多模态内容工厂”**。
    - **面向外部付费用户的公开商业 SaaS（需平滑切换底层引擎）**：受 Google 个人版条款与 API 预览期边界限制（官方目前仅开放建本/导入，Chat/Studio 暂靠 Ch2 协议补齐，且个人预览版暂不支持 Google Workspace 域账号），未来若要做大规模多租户对外商业化，只需保留我们抽象好的上层编排逻辑，将底层路由适配器平滑替换或升级为 **Google Cloud Enterprise NotebookLM / Vertex AI Search / Gemini API**。

4. **核心问题三：能力原子化——能否做成跨 AI 编程软件通用的 Atomic Skills？**  

    **已经建成并随本文档一并交付！** 我们在 [`atomic_skills/`](../../atomic_skills/README.md) 中将完整系统拆解为 **6 个单职责原子 Skill**：`nlm-quota-guard`（额度守卫）、`nlm-official-ingest`（官方高速入库）、`nlm-grounded-qa`（可溯源问答）、`nlm-studio-factory`（11 类多模态工厂）、`nlm-repo-to-notebook`（代码库脱敏知识化）与 `nlm-account-ops`（免密认证自愈）。任何支持 `SKILL.md` 与 MCP/HTTP 的工具（**Antigravity、Claude Code、Cursor、Codex、Windsurf、Gemini CLI**）均可 1 分钟即插即用，实现“写完代码 → 自动更新知识库 → 一键输出项目文档 / PPT / 播客”。

---

## 第 1 章｜项目背景与核心痛点：为什么不能只靠“点网页”用 NotebookLM？

### 1.1 写给非技术同学的通俗理解

如果你还没深度用过 Google NotebookLM，可以把它理解成一位**“只根据你发给它的资料说话、从不乱编”的超级研究助理**。你给它投喂 50 份合同、代码文档、技术论文或会议录音，它不仅能做到**逐句标出出处页码的精准问答**，还能一键把枯燥文字变成**双人音频播客、讲解视频、PPT 幻灯片、思维导图、考试题库和学习指南**。

听起来很美好，但只要团队或重度用户真正在日常业务中用起来，**纯靠浏览器手动“点网页”** 会立刻撞上 5 堵“高墙”：

| 痛点维度 | 纯手点网页的真实困境 | `NotebookLM-Fleet` 带来的改变 |
|---|---|---|
| **1. 算力配额看不见，重任务随时断片** | 网页端并不展示剩余精确配额；生成视频、播客或大 PPT 到一半突然提示达到上限，只能干等 5 小时恢复。 | 实时轮询 **5 小时与周双窗口** 精确百分比（保留 2 位小数）、重置时间戳，以及 29 项细粒度动作消耗预估。 |
| **2. 多账号手工切换，容易串号或封禁** | 个人常有多枚 Free / Pro / Ultra 账号，在同一个日常 Chrome 里频繁切账号，不仅步骤繁琐，Cookie 还会互相踩踏。 | 每账号独立 Chrome User Data、独立 `.notebooklm/profiles/<alias>`、独立端口 (`:9222+`) 与独立 OAuth Token，物理级防串号。 |
| **3. 网页不开放给脚本，无法与代码流联动** | 刚改完代码架构、写完需求文档，每次都要手动打开网页 → 点上传 → 等解析 → 点生成 → 点下载 → 自己归档。 | 暴露标准 **REST API + MCP**；代码工具、CI/CD 流水线和内部系统一条指令完成导入、生成并归档到本地。 |
| **4. 跨账号资料无法协同，产物分散** | 资料在主号里，副号想帮忙分摊生成 PPT 和 Quiz 却找不到文件；生成好的文件还要人工重命名搬运到网盘。 | 通过 **Editor 共享 + Google Drive Hub 镜像双引擎** 打通多号素材，产物自动归档到统一目录 `02_Topics/<主题>/Artifacts/`。 |
| **5. 2026 官方新 API 刚起步，单一通道覆盖不全** | Google 在 2026-11 开放的个人预览版 API 只有“建本+传料”，暂时还不能用来直接 Chat、跑 Studio 或查余额。 | 首创 **“官方通道为主 + 协议通道补齐 + 本地浏览兜底”** 的三通道自适应架构，把“能走官方走官方、官方暂不支持的由增强层补齐”做成自动路由。 |

### 1.2 项目演进定位

一句话总结 `NotebookLM-Fleet` 的战略定位：  
> **对上**，为团队成员、网页控制台与各种 AI 编程工具提供一套稳定、统一、防超额的“知识库与多模态生成 API”；  
> **对下**，屏蔽多账号切换、浏览器 Cookie 锁定、域名变更、网络抖动与官方接口尚不完备等全部工程复杂性。

---

## 第 2 章｜系统全景架构：三通道融合与本地优先中枢

### 2.1 分层架构总览

系统严格采用 **“本地控制平面 + 三通道执行平面 + 本地持久化与网盘归档平面”** 三段式架构：

<!-- DIAGRAM:ARCH_START -->
```text
+-------------------------------------------------------------------------------+
|  接入与交互层（Clients & Consumers）                                           |
|  · Web 控制面板 (http://127.0.0.1:8765，含左侧全工作区预览与选材篮)             |
|  · AI 编程软件 / Agent（Antigravity / Claude Code / Cursor / Codex，走 MCP）   |
|  · 团队自研系统 / CI 脚本 / 定时自动化任务（走 FastAPI REST API）              |
+---------------------------------------+---------------------------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|  Fleet 智能编排与控制平面（Orchestration & Control Plane）                     |
|  · 42 路径 / 47 操作 FastAPI 网关   · 10 工具本机 Stdio MCP Server             |
|  · Quota Poller (双窗口 5h/周采集)  · Tier-Aware Scheduler (评分与延时调度)    |
|  · Coupling Engine (跨号共享/镜像)  · Workspace Security Guard (白名单防越界) |
|  · Capability Router (三通道能力路由引擎：能走官方优先走官方)                  |
+------------+--------------------------+--------------------------+------------+
             |                          |                          |
             v                          v                          v
+------------------------+  +------------------------+  +-----------------------+
| Channel 1: 官方通道     |  | Channel 2: 增强协议通道 |  | Channel 3: 隔离浏览器 |
| Google Official API    |  | notebooklm-py (0.8.6)  |  | Chrome + CDP (:9222+) |
| · REST v1 & Upload v1  |  | · 5h/周与29项消耗探针   |  | · 独立 Profile 目录   |
| · Remote MCP (7 工具)  |  | · 11类 Studio 生成下载 |  | · Storage.getCookies  |
| · OAuth 2.0 免风控直连 |  | · Chat 问答 / Editor   |  | · 免关日常 Chrome 同步|
+------------+-----------+  +------------+-----------+  +-----------+-----------+
             |                           |                          |
             +---------------------------+--------------------------+
                                         |
                                         v
+-------------------------------------------------------------------------------+
|  本地持久化与标准化网盘归档平面（Storage & Hub Plane）                         |
|  · SQLite WAL 数据库 (7 张核心表，每日自动备份到网盘)                          |
|  · 隔离凭据目录 (client_secret.json + oauth_tokens/<alias>.json)               |
|  · Google Drive Hub (00_Control / 01_Inbox / 02_Topics / 03_Exports)          |
+-------------------------------------------------------------------------------+
```
<!-- DIAGRAM:ARCH_END -->

### 2.2 两大核心中枢组件

1. **后端网关与后台双协程 (`app/main.py` + `app/core/*`)**

    - Web 服务以 FastAPI + Uvicorn 运行在 `127.0.0.1:8765`，启动时 `lifespan()` 自动初始化 SQLite WAL 数据库，并**自动将上次服务意外中断残留的 `'running'` 僵尸任务重置为 `'pending'`**，保证断点可恢复。
    - 常驻两个后台循环：`quota_poller_loop`（按配置间隔巡检在线账号双窗口额度，同时每日自动触发一次数据库热备到网盘）与 `scheduler_loop`（每 10 秒检查待执行队列，避免同步阻塞事件循环）。

2. **三通道执行引擎 (`app/adapters/*` + `CapabilityRouter`)**

    - **Channel 1（官方通道：`official_api.py` + `official_mcp_client.py`）**：直连 `https://notebook.googleapis.com/v1`、`/upload/v1` 与 `https://notebook.googleapis.com/mcp`，基于标准 Google Cloud Desktop OAuth 2.0 授权，完全无浏览器会话过期或风控问题。
    - **Channel 2（增强协议通道：`nlm_py_adapter.py`）**：封装 `notebooklm-py >=0.8.6`，承担官方 API 暂未开放的能力（额度检测、11 类 Studio、Chat 问答、跨号共享）。同一 Profile 内通过按 `(event_loop, profile)` 隔离的异步锁串行化保护，杜绝 Cookie 并发竞争踩踏。
    - **Channel 3（隔离接管通道：`browser_takeover.py`）**：每注册一个账号自动分配独立的 `chrome_profiles/<alias>` 与递增端口（`9222, 9223, ...`）。既可直接点击“启动专属 Chrome”在可视化页面中登录操作，也能通过 CDP WebSocket 直接抓取认证 Cookie，无需关闭日常正在办公的主 Chrome。

### 2.3 SQLite WAL 7 张核心表与 Drive Hub 资产拓扑

系统底层持久化只用轻量的单文件 SQLite（启用 WAL 并发模式与外键约束），由 7 张表组成清晰的状态机：

| 数据表 | 核心职责 | 关键字段摘要 |
|---|---|---|
| `accounts` | 多账号台账与双通道认证画像 | `alias`、`tier` (`ultra/pro/plus/free`)、`nlm_auth_status`、`official_oauth_status`、`cdp_port`、`notebook_limit`、`source_limit`、`is_hub_master` |
| `quota_snapshots` | 双窗口额度时序快照（含动作明细） | `five_hour_remaining_pct`、`five_hour_resets_at`、`weekly_remaining_pct`、`weekly_resets_at`、`is_exhausted`、`actions_json`（29 项动作消耗 JSON） |
| `topics` | 团队统一的主题逻辑目录 | `name`、`drive_folder_path`、`master_account_id`、`coupling_mode` (`shared_with_drive_fallback` / `shared_only` / `drive_mirror_only`) |
| `notebooks` | 跨账号远程笔记本索引（一主题多副本可映射） | `topic_id`、`account_id`、`remote_notebook_id`、`title`、`source_count`、`is_owner`、`role` |
| `tasks` | 异步任务调度全生命周期队列表 | `batch_id`、`task_type`、`status` (`pending/running/waiting_quota/deferred_later/done/failed/canceled`)、`channel_used`、`est_cost_pct`、`actual_delta_5h_pct`、`scheduled_after` |
| `artifacts` | 本地产物归档索引表 | `topic_id`、`task_id`、`kind`、`title`、`local_drive_path` |
| `app_settings` | 全局策略与网关配置中心 | `tier_policy`、`safety_buffer_pct`、`official_api_enabled`、`official_api_project_id`、`official_proxy_url`、`workspace_root`、`drive_hub_root` |

Google Drive Hub 采用严格的 4 分区目录结构，确保资料进出有迹可循：

- `00_Control/backups/`：每日 SQLite 库热备快照
- `01_Inbox/`：待审阅/待归类的原始收集区
- `02_Topics/<主题名>/Sources/` 与 `Artifacts/`：每个主题独立的原料库与产物沉淀库
- `03_Exports/`：独立问答导出或未归属主题导出物

---

## 第 3 章｜核心问题一：相比直接操作 NotebookLM 界面，在 API 模式下具体能做什么？

这是团队新人最常混淆的问题：**“Google 到底开放了哪些官方 API？那些没开放但能自动化的功能又是怎么实现的？”**  
我们必须用一张清晰、毫不夸大的对照表把 **“网页端”、“Google 官方 API/MCP（2026-11）”** 和 **“完整 NotebookLM-Fleet 编排系统”** 三者的边界划得清清楚楚。

### 3.1 Google 官方 API（Nov 2026 Personal Beta）原生能做什么与还不能做什么

经本系统在实网环境对 `https://notebook.googleapis.com/v1` 与 `https://notebook.googleapis.com/mcp` 真实握手验证，Google 官方目前定位为 **“知识库管理与高速数据摄取层（Management & Ingestion Plane）”**：

- **官方 REST API v1 & Upload v1 已支持：**

    - `POST /v1/notebooks`：创建笔记本
    - `GET /v1/notebooks`：分页拉取、按条件（如 `notebook.is_owner = true`）过滤笔记本列表
    - `GET /v1/notebooks/{id}` & `PATCH /v1/notebooks/{id}`：读取与重命名更新
    - `POST /v1/notebooks/{id}/sources`：写入 **Web URL / YouTube URL** 以及 **UTF-8 纯文本 / Markdown（`mimeType: text/markdown`）**
    - `POST /upload/v1/notebooks/{id}/sources:upload?uploadType=media`：**二进制媒体直传**，支持单个高达 **`200 MiB`** 的 PDF、音频、视频或图文文件
    - `GET /v1/notebooks/{id}/sources` & `GET /v1/notebooks/{id}/sources/{sid}`：拉取来源明细并精确追踪处理状态（`PROCESSING` → `ACTIVE` / `FAILED`）

- **官方 Remote MCP (`https://notebook.googleapis.com/mcp`，Streamable HTTP `2024-11-05`) 已支持 7 个工具：**

    - `create_notebook`、`get_notebook`、`list_notebooks`、`update_notebook`、`create_source`（支持文本与 URL）、`get_source`、`list_sources`
    - *注意*：Remote MCP 暂不支持上传二进制 PDF/音视频，系统会在检测到大于阈值或非文本文件时自动引导至 `/upload/v1`。

- **官方 API 当前明确尚未开放的能力：**

    - **暂无 Chat 问答接口**、**暂无 11 类 Studio 多模态生成/下载接口**、**暂无账号 5h/周剩余额度查询接口**、**暂无协作者 Editor 共享接口**。
    - **适用范围约束**：当前处于 Personal Preview（个人预览版），仅支持个人 `@gmail.com` 账号及 OAuth 测试用户模式，暂不支持企业 Google Workspace 域账号直接调用。

### 3.2 完整三能力对比全景表（网页 UI vs 官方 API vs NotebookLM-Fleet）

| 能力维度 | NotebookLM 网页端 | Google 官方 API + MCP (Ch1) | 完整 `NotebookLM-Fleet` 系统 (Ch1+Ch2+Ch3) |
|---|---|---|---|
| **建本 / 改名 / 查询笔记本列表** | 手工逐一点选 | **支持**（REST + MCP，可按批自动化） | **支持**（支持智能路由与跨号全局索引同步） |
| **批量导入 URL / Markdown / 文本** | 复制粘贴、逐条确认 | **支持**（毫秒级批量写入，不占用日常浏览器） | **支持**（左侧工作区勾选一篮子文件 15 秒全进库） |
| **大文件 PDF / 音视频上传 (`<=200 MiB`)** | 手工拖拽上传 | **支持**（走官方 Upload v1 媒体流直传） | **支持**（自动按扩展名/900KB 阈值分流直写或 Upload v1） |
| **基于来源的可溯源 AI 问答 (`Chat`)** | 网页框手打提问、手动复制答案 | **官方暂未开放** | **支持**（Ch2 自动提问、引用计数、保存 Note 并归档 `.md`） |
| **11 类 Studio 多模态内容生成** | 网页逐一点击按钮、手动等完成再下载 | **官方暂未开放** | **支持**（批量队列触发并自动下载归档 `.m4a/.mp4/.pdf/.png/.json/.md/.csv`） |
| **实时 5 小时 / 周额度与 29 项动作消耗** | 不可见，耗尽才弹窗提示 | **官方暂未开放** | **支持**（实时精确百分比、重置倒计时与 29 项单次预估消耗） |
| **撞上限自动进官方延时队列 (`later=True`)** | 弹窗提示时偶尔提供选项 | **官方暂未开放** | **支持**（8 类支持延时的 Studio 任务自动降级排入延时队列） |
| **多账号联合算力池与分级路由** | 无（全靠人工切换 Chrome Profile） | 无（单 Token 调用） | **支持**（`conserve_ultra` / `ultra_first` / `balanced` 智能择优 + `waiting_quota` 自动休眠唤醒） |
| **跨账号共享协作者与镜像灾备** | 手动点 Share 输入邮箱 | **官方暂未开放** | **支持**（Master 自动共享 Editor + 权限受限时自动回退 Drive 镜像） |
| **断点恢复与本地目录安全边界** | 关掉标签页状态全失 | 需调用方自建数据库与幂等逻辑 | **支持**（SQLite WAL 崩溃自动重置僵尸任务 + 每日热备 + 敏感文件物理拦截） |

### 3.3 给小白的 4 个直觉对比场景

1. **场景 A：一次要给团队新建知识库塞进 30 篇架构 Markdown 和 5 份 80MB PDF 标书**  
   网页端需要手动点 35 次上传并盯着屏幕等转圈；在 API 模式下，勾选或传路径数组后一次 HTTP 调用全自动并发/顺序提交，`GET /sources` 自动核验全部变成 `ACTIVE`。
2. **场景 B：周五晚上想为 4 个调研主题一口气生成“音频播客 + PPT + 思维导图 + 测验题”**  
   网页端根本干不完且不知道哪个账号还能撑住；`NotebookLM-Fleet` 会先把 `audio` 与 `slide_deck` 等高耗能重任务分配给 5h 窗口充足的 Ultra 账号，把 `quiz` 与 `mind_map` 自动分流给 Pro/Free 账号，遇到某个号余量触及安全水位（默认 10%）则自动交由延时队列或等待窗口重置后夜间静默续跑，次日早上一打开 Drive Hub 文件夹就能直接收齐全部成果文件。
3. **场景 C：日常办公正在开 30 个网页，Chrome Cookie 数据库被 Windows 锁死**  
   传统脚本会直接报错崩溃逼你关掉日常 Chrome；`NotebookLM-Fleet` 提供“专属纯净 Chrome (CDP :9222+) 无感采集”和“5 秒粘贴一次 `notebook.google.com` cURL 同步”，彻底绕开本地文件锁与浏览器自动化拦截。
4. **场景 D：新同学只是想用 Cursor / Antigravity / Claude Code 看代码时顺便查项目资料**  
   无需切去网页端来回复制粘贴；编程软件通过 MCP 工具直接在编辑器内查询、写入和总结 NotebookLM，做到“写代码与沉淀知识在同一条流水线内完成”。

---

## 第 4 章｜核心问题二（核心焦点）：能否把它的功能集成进后续的独立开发中？

### 4.1 明确结论：**可以集成，但需要“分场景分层选型”**

很多同学在看到这套能力后会立刻想：**“那我以后写一个独立的新软件，能不能直接把 NotebookLM 当作我的 AI 后端？”**  
从工程架构角度给出明确答复：

- **底层协议完全可嵌入**：`NotebookLM-Fleet` 本身就是一个标准的 HTTP REST + MCP Sidecar 网关，其他语言（Python、TypeScript/Node.js、Go、Java、C#）不需要了解任何 Google OAuth 刷新细节、Cookie 隔离锁或 `batchexecute` 协议，直接调 `http://127.0.0.1:8765/api/...` 或连接 MCP 即可把全套能力当成“积木块”复用。
- **但必须严守适用范围与合规边界**：内部工具 / 团队中台 / 个人产品可以直接借势落地；面向外部大众的公开高并发商业 SaaS 则不可直接依赖非官方 Ch2 通道，必须利用本系统提供的统一抽象层，在底层平滑切换至企业级云服务。

### 4.2 后续独立软件可直接复用的 4 大“基础功能引擎”

在你们接下来开发的任何新工具或内部平台中，至少可以直接省掉 **4 个高复杂度基础设施模块** 的研发：

1. **零自建向量库的“可溯源知识引擎”（Grounded RAG-as-a-Service）**  
   自己搭建 RAG 系统需要写文档解析、分块 Chunking、Embedding 模型、向量数据库、重排 Reranker 与引文对齐。集成 `NotebookLM-Fleet` 后，只需要把文档通过 `POST /api/official/quick-add` 扔进对应笔记本，就能获得高质量文档解析 + 基于来源的严格引用检索。
2. **开箱即用的“多模态内容生产工厂”（Text-to-Media Pipeline）**  
   如果你的独立应用需要“把用户上传的会议纪要/课程讲义自动转换成 10 分钟双人音频播客、讲解视频、PDF 幻灯片、信息图 PNG 或结构化表格 CSV”，无需采购 5 家不同的音视频 AIGC 模型并做复杂拼接，直接调用 `POST /api/tasks/batch` 投递 Studio 任务，异步轮询本地 Drive 归档路径即可。
3. **代码库 / 研发资产自动化知识管道（Repo-to-Knowledge Sync）**  
   在你们自己的内部 DevOps 平台、文档站或自动化发布流水线中，可在每次发版后自动提取 CHANGELOG、API Spec 与架构设计文档，走官方 `/v1/notebooks/{id}/sources` 自动维护一个永不落伍的“版本活知识库”，供客服、销售和新人直接发问。
4. **可平移的“多账号算力池与窗口调度引擎”架构模板**  
   即使未来接的不是 NotebookLM，而是团队采购的其他按时间窗口计费的大模型或外部 API 池，`app/core/scheduler.py` 所实现的 **“双窗口剩余追踪 + 动作成本估算表 + 档位权重分流 + 安全水位熔断 + 窗口重置自动唤醒 (`waiting_quota`)”** 是一套完全可复用的通用任务调度器设计。

### 4.3 4 种典型的独立软件集成模式

| 集成模式 | 适用独立软件形态 | 对接方式（零侵入改造成本） | 架构优势 |
|---|---|---|---|
| **模式 A：本地 / 内网 Sidecar 微服务网关** | 桌面客户端（Electron/Tauri）、内部运营后台、Python/Node 业务服务 | 新软件把 `NotebookLM-Fleet` 当作独立进程伴随运行，全部通过 `POST /api/official/quick-add`、`POST /api/tasks/batch`、`GET /api/tasks` 进行异步交互 | 业务代码完全零耦合爬虫/OAuth/浏览器细节；服务挂起重启任务不丢。 |
| **模式 B：MCP 协议原生插件化接入** | 自研 AI Agent 平台、多智能体工作流、桌面 AI 助手 | 新系统内置 MCP Client，直连 `app.mcp_server`（10 工具）或官方 Remote MCP（7 工具） | Agent 可自行根据剩余配额决策先查库还是先提问，自动化程度最高。 |
| **模式 C：CI/CD 与文件事件触发管道** | 文档自动化中心、研发效能看板、Git Webhook 服务 | Git Push / 云盘新增文件 → 脚本调用 `POST /api/official/quick-add` 更新知识库 → 夜间闲时触发 `report` / `slide_deck` 生成 | 把“手工整理文档”彻底变成后台无人值守的异步流水线。 |
| **模式 D：依托 `CapabilityRouter` 的演进式抽象** | 计划未来走向对外商业化的孵化项目 | 独立应用只面向 Fleet 标准契约编程；今天由 Fleet 执行 Ch1+Ch2，未来只换底层 Adapter 实现 | 产品前端与业务流程零重写，即可完成个人试验 → 团队内网 → 商业云的升级跃迁。 |

#### 模式 A 最小接入示例（新软件仅需几行 HTTP 请求）

```python
import httpx

async def push_report_and_build_deck(file_path: str, topic_name: str) -> dict:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8765", timeout=60.0) as client:
        # 1) 通过官方 REST API 创建笔记本并写入研发资料
        ingest_res = (await client.post("/api/official/quick-add", json={
            "account_id": 1,
            "new_notebook_title": topic_name,
            "channel": "official_rest",
            "file_paths": [file_path],
        })).json()

        # 2) 按额度策略异步排队生成简报与 PPT 幻灯片（自动选空闲号并归档网盘）
        batch_res = (await client.post("/api/tasks/batch", json={
            "topic_ids": [1],
            "actions": ["report", "slide_deck"],
            "language": "zh-Hans",
        })).json()
        return {"ingest": ingest_res, "batch": batch_res}
```

### 4.4 生产合规与选型红线决策矩阵（团队立项必读）

| 落地场景分类 | 推荐使用方案 | 核心技术依据与避坑提示 |
|---|---|---|
| **个人生产力 / 内部极客工具（1~10 人）** | **直接使用 `NotebookLM-Fleet` 完整版（Ch1 + Ch2 + Ch3）** | 功能最全，官方建本/传料稳定，Ch2 补齐 11 类 Studio、Chat 与双窗口精确测额。 |
| **仅做资料自动归集与检索准备（零风控诉求）** | **纯 Channel 1 模式（官方 REST v1 / Remote MCP）** | 完全遵循 Google Cloud OAuth 2.0，零网页模拟、零 Cookie 过期维护成本；注意当前为 Personal Beta，需将团队邮箱加入 GCP OAuth Test Users，且不适用 Workspace 企业域账号。 |
| **企业内部规模化应用 / 外部公开商业 SaaS** | **保留 Fleet 编排思想，底层平滑迁移至企业级官方栈** | **不可把 Ch2 逆向协议打包进面向公众的高并发商业后台**。对外商业产品应采用 **Google Cloud Enterprise NotebookLM API** 或 **Gemini API（Context Caching / File Search）+ 自有 TTS/PPT 渲染链路**，把 `NotebookLM-Fleet` 作为前端敏捷原型验证与内部知识助手。 |

---

## 第 5 章｜核心问题三：能力原子化——跨 AI 编程工具的 Atomic Skills 体系

### 5.1 为什么要把能力“原子化（Atomic Skills）”？

很多 AI 编程软件都面临一个通病：**要么只会直接读写当前工程目录的源码，缺长期沉淀；要么一次接入一个巨型说明书，上下文过长导致模型乱调用写接口。**  
把 `NotebookLM-Fleet` 的全套能力拆成 **“单职责原子 Skill”**，有 3 个直接收益：
1. **读与写物理隔离**：查询余额只需加载纯只读的 `nlm-quota-guard`，绝不会误触发耗时 10 分钟的音频/视频生成任务。
2. **自由乐高式组合**：改完代码用 `nlm-repo-to-notebook` + `nlm-official-ingest`；做架构调研用 `nlm-grounded-qa`；做汇报材料才激活 `nlm-studio-factory`。
3. **跨工具一次编写、到处复用**：采用开放的 `SKILL.md` 规范 + 标准 MCP / 本地 REST 接口解耦，不受限于某一家 IDE 厂商。

<!-- DIAGRAM:SKILLS_START -->
```text
  [ AI 编程工具层 ]      [ 原子化 Skill 路由与组合层 ]          [ 底层协议能力层 ]
  +---------------+      +------------------------------+
  | Antigravity   |----->| 1. nlm-quota-guard    (只读) |---+--> 本地 Stdio MCP (10 工具)
  | Claude Code   |----->| 2. nlm-official-ingest(写入) |---|    app.mcp_server
  | Cursor        |----->| 3. nlm-grounded-qa    (问答) |---+--> 本地 REST API (47 操作)
  | Codex         |----->| 4. nlm-studio-factory (生产) |---|    http://127.0.0.1:8765
  | Windsurf / CLI|----->| 5. nlm-repo-to-notebook(切片)|---+--> 官方 Remote MCP (7 工具)
  +---------------+      | 6. nlm-account-ops    (认证) |        notebook.googleapis.com
                         +------------------------------+
```
<!-- DIAGRAM:SKILLS_END -->

### 5.2 本次交付的 6 大原子 Skill 全景清单

所有原子 Skill 已真实落地在 [`atomic_skills/`](../../atomic_skills/README.md) 目录，每个文件均可直接复制即用：

| 原子 Skill 目录 | 核心能力定位 | 触发时机与价值 | 安全/风控护栏 |
|---|---|---|---|
| `nlm-quota-guard` | 只读巡检全部账号 5h/周剩余额度、重置时间与 29 项动作估算耗能 | 任何重任务开始前先做“体检与预算审查” | 优先读库内快照，仅过期或显式请求才发线上 poll，防频繁请求 |
| `nlm-official-ingest` | 通过官方 REST v1 / Remote MCP 建本并写入 URL、MD/TXT 及 `<=200 MiB` 二进制文件 | 需要毫秒级稳定入库海量文档/代码规范/PDF | 强制路径白名单校验，并轮询直至全部来源状态达 `ACTIVE` |
| `nlm-grounded-qa` | 针对指定笔记本发起严格基于来源的可溯源 Chat 问答，支持保存 Note 并导出 `.md` | 编程排错、方案对齐、避免纯大模型幻觉 | 显式要求引用来源章节，来源未覆盖必须直言未知 |
| `nlm-studio-factory` | 批量生产并下载 `audio`、`video`、`cinematic_video`、`slide_deck` 等全部 11 类产物 | 周报分享、技术布道、培训出题、导图梳理 | 强制按 `conserve_ultra` 与安全水位护栏调度，满额转 `later=True` |
| `nlm-repo-to-notebook` | 扫描工作区/代码库，自动正交切片、脱敏打包成 Markdown 后推送入库 | 新模块上线或代码审查后沉淀为“活知识库” | **硬拦截** `.env`、`client_secret*.json`、`*.key`、Cookie 与个人隐私 |
| `nlm-account-ops` | 处理 Official OAuth 302 直跳、专属 Chrome CDP 采集与 `notebook.google.com` cURL 导入 | 新账号入池或遇到 Cookie 过期/`database is locked` | 严禁索要明文密码或输出任何 Cookie/Refresh Token 原文 |

### 5.3 主流 AI 编程工具接入指南

- **Antigravity / Codex**：直接将 `atomic_skills/nlm-*` 复制到项目的 `.agents/skills/` 或用户全局目录，并在本地 MCP 配置注册 `notebooklm-fleet`。
- **Claude Code**：将 `atomic_skills/nlm-*` 放入 `~/.claude/skills/` 或项目 `.claude/skills/`，在 `.mcp.json` 中配置 `uv --directory <NotebookLM-Fleet路径> run python -m app.mcp_server`。
- **Cursor / Windsurf**：将各 `SKILL.md` 添加为项目规则/Skills 引用，并在编辑器的 MCP Servers 界面填入同款 `notebooklm-fleet` 启动命令；或者直接在对话中让内置工具调用 `http://127.0.0.1:8765` 本地 REST 网关。
- **团队自研 Agent**：无需完整理解 `SKILL.md` 的自然语言指令，直接按第 4 章的 REST 接口与 MCP JSON Schema 做代码级硬编码对接。

---

## 第 6 章｜团队项目里程碑台账：已完成任务全量清单（截至 2026-11-24）

截至本文档归档时，团队已经完成从底层协议验证到全栈交付、深度代码审查、安全加固以及实网端到端验证的全部核心阶段：

### 6.1 分阶段核心交付台账

| 阶段 / 模块 | 已落地的核心成果与关键验证事实 |
|---|---|
| **1. 基础设施与核心引擎** | 完成 FastAPI 控制中心（42 路径 / 47 HTTP 操作）、SQLite WAL 7 表数据库模型、Drive Hub 四区自动建目录与每日自动热备、5h/周双窗口与 29 项动作耗能轮询器、三分档（`conserve_ultra`/`ultra_first`/`balanced`）评分调度器、`later=True` 延时降级、`waiting_quota` 自动定时唤醒，以及 Editor/Drive 镜像跨号耦合引擎。 |
| **2. 官方协议与本地 MCP 双打通** | 完成 Google Cloud Desktop OAuth 2.0 对接、`official_api.py`（REST v1 `/v1/notebooks`、`/sources` 与 `/upload/v1` 最大 200 MiB 上传）和 `official_mcp_client.py`（Streamable HTTP JSON-RPC + SSE 解析，实测握手 Google 官方 Remote MCP 返回全部 7 工具），同时注册向 Antigravity 暴露 10 个编排工具的本机 Stdio MCP Server。 |
| **3. Web 控制台升级** | 构建暖纸质风中文控制台，包含 KPI 总览、多账号额度条与重置倒计时、主题索引、任务队列、Drive 浏览、官方 API & MCP 实验室，以及**常驻左侧全工作区文件浏览器**（支持 `d:\...` 工作目录、Drive Hub、Drive 资料总库切换、实时搜索、预览与多选资料篮直推官方入库）。 |
| **4. 全量代码审查与 9 项关键缺陷修复** | 完成全部后端/前端/脚本/测试代码的逐行审查并修复 9 项问题：启动自动重置 `'running'` 僵尸任务、纯官方 OAuth 账号参与摄取任务调度、`CapabilityRouter.ingest_file` 支持 900KB 边界与代理透传、同步远程笔记本纳入 Ch1 就绪账号、`/api/topics/{id}/ingest` 增补 `.env` 等敏感文件拦截、Drive 备份改异步线程防卡顿、大文件读写异步化与 200MiB 校验、Windows 非法文件前缀清洗、以及修复 `fleet_open_account_browser(..., mode="official_oauth")` 非阻塞返回与本地 Cookie 判活严格校验。 |
| **5. 免锁免拦截认证体系重构** | 解决日常 Chrome 运行导致的 `browser database is locked` 与 Playwright 登录拦截痛点：推出浏览器同步直跳 `302 /oauth-redirect`（30 分钟回环超时）+ **Ch2 双模式极速弹窗**（① 专属隔离 Chrome `:9222+` CDP `Storage.getCookies` 无锁抓取；② 5 秒粘贴 `Copy as cURL`/Cookie/JSON）；进一步定位并修复 Google 切换主域至 `notebook.google.com` 后 `OSID` 域名绑定与 `?authuser=N` 槽位解析漏洞。 |
| **6. 官方鉴权刷新加固与知识资产实网闭环** | 修复 `oauth2.googleapis.com` 瞬时 `ConnectTimeout` 导致未捕获 HTTP 500 的隐患（增加 OAuth Refresh 3 次退避重试与代理支持、Connect 阶段 1 次安全重试、建本异常处理）；并将团队构造的 7 份并行安全脱敏 Markdown 资产包（`00`~`06`）通过 `POST /api/official/quick-add` 成功实测写入新笔记本，全部 7 份来源在 15 秒内批量创建完成并在复查轮询中全部转为 **`ACTIVE`**。 |
| **7. 自动化测试验证** | 全部 13 项自动化单元测试与实网协议探测测试（`tests/test_fleet.py` + `tests/test_official_protocol.py`）持续 **13 / 13 PASSED（0 失败）**。 |

### 6.2 关键实测性能与可用性基线（已脱敏）

- **官方 API 批量建本 + 7 篇并行 Markdown 入库**：`POST /api/official/quick-add`（`channel=official_rest`）总耗时约 **`15.1 秒`** 返回 `HTTP 200 OK`（`success_count=7, error_count=0`），数秒后复核 `GET /api/official/sources` 全部 **`7/7` 状态收敛为 `ACTIVE`**。
- **官方 Remote MCP 探测与列库**：对 `https://notebook.googleapis.com/mcp` 实网握手支持 JSON-RPC `initialize -> notifications/initialized -> tools/list -> tools/call`，成功拉取账号内已有全部笔记本清单与 7 工具完整 Schema。
- **Ch2 实时测额与 29 项动作解析**：基于修正后的 `notebook.google.com` + `?authuser=` 会话策略，`POST /api/accounts/{id}/poll-quota` 稳定返回 `nlm_auth_status: "ok"`、5 小时/周精确剩余百分比及 **29 个动作耗时/成本权重记录**。

---

## 第 7 章｜未来演进路线图：后续可开发任务 Backlog（P0 / P1 / P2）

为便于团队规划下一阶段迭代与新人认领任务，按优先级与业务收益梳理以下明确可落地的演进路线：

### P0｜近期高优落地（夯实多账号生产力与自动化闭环）

1. **`deferred_later` 官方延时产物自动巡检与收割器**

    - **目标与价值**：目前当配额吃紧进入官方 `later=True` 延时队列时，任务置为 `deferred_later` 并保存了 `remote_task_id`；需要补齐后台定时轮询状态并在远程生成完成后自动下载归档到 `DriveHub/02_Topics/<Topic>/Artifacts/`。
    - **技术方向**：在 `scheduler.py` 或独立协程中定期扫描 `status='deferred_later'` 且带有 `remote_task_id` 的任务，调用 `client.artifacts.wait_for_completion` / 状态查询接口完成闭环落盘。
    - **预估复杂度**：中低（2~3 人日）。

2. **多账号扩充上线与交叉协作实战验证（Pro / Plus / Free 并入池）**

    - **目标与价值**：当前主号已完成双通道实测打通，下一步可利用刚升级的 CDP / cURL 工具批量接入团队其余 Pro/Free 账号，完整演练“重任务自动压给 Ultra、轻任务分流 Free/Pro、交叉共享 Editor 协作”。
    - **技术方向**：Web UI 批导入 + 一键全池测额 + 配置跨号耦合模式验证。
    - **预估复杂度**：低（配置与联调为主，1 人日）。

3. **NotebookLM 一键文档导出与双通道 PDF/Docx 排版工坊 API 化**

    - **目标与价值**：把本次验证成功的“Markdown → 矢量架构图 SVG/PNG → 排版级 PDF + 可编辑 Word/Docs (`.docx`)”能力正式封装为系统端点（如 `POST /api/exports/document`），让 `chat` 导出结果和 `report`/`study_guide` 生成后可直接一键转为汇报用 PDF/Docx。
    - **技术方向**：复用 `docs/sharing_archive_v1/build_exports.py` 模块封装为独立异步 Service，支持 Playwright/Chrome 无头印刷与 `python-docx` 导出。
    - **预估复杂度**：中（2~3 人日）。

### P1｜中期能力扩展（深化外部集成与生态联动）

4. **本地安全鉴权（API Key）与局域网只读审计模式**

    - **目标与价值**：目前默认安全绑定 `127.0.0.1` 单机使用；若团队内部多人共用一台宿主机跑 `NotebookLM-Fleet` 作为内网共享网关，需要引入 `X-Fleet-API-Key` 鉴权中间件与操作审计日志，保护团队账号与资料边界。
    - **技术方向**：FastAPI Dependency / Middleware 注入共享密钥与来源 IP 白名单。
    - **预估复杂度**：中（2 人日）。

5. **Webhook / 企业通知回调（飞书 / 钉钉 / 企业微信 / Slack）**

    - **目标与价值**：批量生成视频、播客等大耗时任务往往在夜间或排队数十分钟后完成，接入 Webhook 后可在任务全部 `done` 或发生 `expired` 掉线告警时主动推送卡片通知到团队群，附带网盘产出路径。
    - **技术方向**：在 `app_settings` 增加 Webhook URL，于 `_execute_single_task` 终态与 `quota_poller` 状态跳变处异步发送通知。
    - **预估复杂度**：低~中（1~2 人日）。

6. **Web 控制面板实时交互式问答与 Notebook 成果在线预览中心**

    - **目标与价值**：目前用户主要通过队列看结果或去网盘看文件，可在 Web 面板增加“在线对指定笔记本直接发起可溯源问答对话框”和“已下载幻灯片 PDF / 播客音频在线播放预览卡片”。
    - **技术方向**：新增同步/流式 Chat 端点与只读静态文件安全预览端点（严格限定在 `drive_hub_root` 白名单内）。
    - **预估复杂度**：中（3 人日）。

### P2｜远期商业化与架构演进（面向企业级 & 公共 SaaS）

7. **多租户隔离与企业云端后端适配器（Enterprise Adapter）**

    - **目标与价值**：若后续团队要把内部沉淀的产品孵化为对外公开 SaaS，在 `CapabilityRouter` 中新增第三种纯云端企业级实现（对接 **Google Cloud Enterprise NotebookLM** 或 **Gemini API File Search / Context Caching**），彻底脱离浏览器 Session 维护，支撑高并发商业流量。
    - **技术方向**：保持现有任务与前端交互契约，新增 `enterprise_cloud_adapter.py`。
    - **预估复杂度**：高（5~8 人日）。

8. **任务级成本账本与可视化用量分析面板**

    - **目标与价值**：利用 SQLite 里长期积累的 `est_cost_pct` 与 `actual_delta_5h_pct` 真实前后差值数据，统计各主题、各账号及各动作类型的历史真实能耗曲线，反向校准默认估算模型并输出团队 AI 算力月报。
    - **技术方向**：聚合 SQL 视图 + 前端轻量趋势图（Canvas / SVG）。
    - **预估复杂度**：中（2~3 人日）。

---

## 第 8 章｜小白 5 分钟上手与内外部演示脚本（Demo Playbook）

### 8.1 新人首次接入 4 步冷启动清单

1. **启动服务与检查健康度**：运行团队启动脚本后，在浏览器访问 `http://127.0.0.1:8765`，确认右上角绿灯显示「已连接本地服务」。
2. **打通 Channel 1（官方 API）**：在「官方 API & MCP 实验室」上传团队的 `client_secret.json`（自动识别 Project ID），在账号行点「官方 API 授权」完成一次 Google OAuth 授权确认；随后点击「全链自检」看到全绿 PASS。
3. **打通 Channel 2（实时测额 & Studio）**：点击账号行「一键登录会话」，**推荐使用方式二（5 秒极速）**：在已登录 `https://notebook.google.com/` 的日常 Chrome 按 `F12` → `Network` → 刷新 → 右键第一条 `notebook.google.com` 请求选 **Copy as cURL (bash)** 粘贴提交，即刻看到 `5h剩余%` 与 `周剩余%`。
4. **勾选文件 15 秒入库**：在网页左侧常驻工作区点选 `Notebook_MD_Assets_NotebookLM-Fleet_v1/` 等项目文件加入选材篮 → 点击「立即添加到笔记本」，即可在手机/网页端 `notebook.google.com` 随时查看与发问。

### 8.2 10 分钟对内 / 对外技术分享演讲脚本（可直接照着讲）

- **第 1 分钟｜开场破冰（抛痛点）**：展示团队日常使用 NotebookLM 面对的 3 个痛点——“多账号额度瞎摸、十几个文件手工拖拽半小时、AI 编程工具读不到外部资料”。
- **第 2~4 分钟｜架构解密（展示三通道全景图）**：用通俗语言解释为什么我们要做 `Official API + 增强协议 + CDP 隔离浏览器` 三通道混合路由，明确交代 Google 官方 2026-11 API 目前擅长什么、团队调度器又补齐了什么。
- **第 5~7 分钟｜现场高频实操演示（最抓人眼球）**：在控制台左侧工作区勾选多份脱敏文档，点一次按钮通过 **Official REST API v1** 当场新建笔记本并批量入库，刷新页面展示全部 `ACTIVE` 与秒级日志，接着点击测额展示 `5h/周双窗口进度条` 与 `29 项消耗表`。
- **第 8~9 分钟｜编程软件联动（原子 Skill 实战）**：展示 Antigravity / Claude Code / Cursor 如何通过 [`atomic_skills/`](../../atomic_skills/README.md) 里的 `nlm-quota-guard`、`nlm-repo-to-notebook` 和 `nlm-studio-factory`，在写完代码的最后一秒钟自动打包生成 PPT 和播客音频。
- **第 10 分钟｜工程边界与未来想象**：坦诚给出“内部工具即插即用 vs 公开商业 SaaS 平滑迁移云端企业后端”的专业结论，引发现场架构讨论与 Q&A。

---

## 第 9 章｜安全边界、对外分享脱敏铁律与文档维护指南

### 9.1 对外分享前必查脱敏清单

1. 任何对外文档、PPT 截图与日志样例中，**绝不出现**：个人真实 Gmail 地址、真实 Google Cloud `project_id`、本机 Windows 真实用户名目录、真实生产环境的 Notebook/Source UUID，以及任何 `client_secret.json` / `oauth_tokens/*.json` / `SID` / `OSID` 等字样真实取值。
2. 分享演示时优先打开脱敏后的专属知识库或新建临时演示笔记本，不在大屏直播中直接展开 `data/` 目录或 `Network` Request Cookie 面板。

### 9.2 多形态交付与一键重导出规范（Markdown ↔ PDF / Docx）

本归档目录配套提供自动导出工具 [`build_exports.py`](./build_exports.py)，确保后续每次系统迭代只维护一份单源权威 Markdown：

```powershell
# 在团队更新本 Markdown 文件后，一键重新生成全新排版的 PDF 与 Word/Google Docs (.docx)
Set-Location './docs/sharing_archive_v1'
uv run --with markdown --with python-docx python .\build_exports.py
```

导出产物清单：

- **源码权威文档（UTF-8 Markdown）**：`NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.md`
- **印刷与对外分发版（矢量图表 PDF）**：`NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.pdf`
- **团队协同与 Google Docs / Word 再编辑版（DOCX）**：`NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.docx`
