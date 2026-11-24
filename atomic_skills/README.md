# NotebookLM-Fleet Atomic Skills Pack（跨 AI 编程工具原子技能包）

本目录将 **NotebookLM + NotebookLM-Fleet** 的完整能力拆解为 **6 个高内聚、低耦合、单职责（Single-Responsibility）** 的原子 Skill。  
任何支持 `SKILL.md` 规范或 MCP / HTTP 工具调用的 AI 编程软件，都可以按需加载其中 1 个或多个 Skill，零改造成本打通“代码库 / 文档 ↔ NotebookLM 知识引擎 ↔ 多模态产出”。

---

## 一、6 大原子 Skill 能力矩阵

| 原子 Skill | 核心职责 | 读写属性 | 底层通道依赖 | 典型输入 → 输出 |
|---|---|---|---|---|
| [nlm-quota-guard](./nlm-quota-guard/SKILL.md) | 实时巡检多账号 5 小时与周窗口剩余额度、重置倒计时与 29 项动作消耗 | **纯只读（安全无副作用）** | Fleet MCP (`fleet_quota_overview`, `fleet_poll_quotas`) / `GET /api/accounts` | 账号池状态 → 额度健康评分与调度建议 |
| [nlm-official-ingest](./nlm-official-ingest/SKILL.md) | 通过官方 REST v1 或 Remote MCP 创建笔记本并导入 URL、文本/MD、<=200MiB 二进制文件 | 写入（仅新建/追加来源） | `POST /api/official/quick-add`, `GET /api/official/sources`, Official MCP (`https://notebook.googleapis.com/mcp`) | Notebook ID + URL/文件列表 → 来源 ID 与 `ACTIVE` 校验结果 |
| [nlm-grounded-qa](./nlm-grounded-qa/SKILL.md) | 围绕指定笔记本做**严格基于来源的可溯源问答**，支持保存 Note 并导出本地 Markdown | 只读问答 / 可选落盘归档 | `fleet_enqueue_batch(actions=["chat"])` / `POST /api/tasks` / `ask_question` | Notebook/Topic + 问题列表 → 带引用出处的答案 + `.md` 归档路径 |
| [nlm-studio-factory](./nlm-studio-factory/SKILL.md) | 调度生成并自动下载全套 **11 类 Studio 多模态产出**，含 `conserve_ultra` 与 `later=True` 保护 | 高算力写入 | `fleet_enqueue_batch` / `POST /api/tasks/batch` / `fleet_task_status` | Topic ID + 产出列表 + 提示词 → `DriveHub/02_Topics/<Topic>/Artifacts/*` |
| [nlm-repo-to-notebook](./nlm-repo-to-notebook/SKILL.md) | 将代码仓库或项目目录切片、**强制脱敏过滤敏感文件**后批量知识化入库 NotebookLM | 读写流水线 | `GET /api/workspace/tree` + `POST /api/official/quick-add` + `fleet_create_topic_and_ingest` | 项目目录 + 主题名 → 脱敏后的正交 `.md` 包 + 激活的远程笔记本 |
| [nlm-account-ops](./nlm-account-ops/SKILL.md) | 免密多账号授权与故障自愈（Official OAuth 直跳、CMD/Chrome 独立窗口持久登录、无感续期、cURL 导入） | 会话配置（零明文密码） | `GET /api/accounts/{id}/oauth-redirect`, `GET .../login-script.cmd`, `POST .../refresh-session`, `POST .../capture-cdp`, `POST .../import-cookies-raw` | 账号别名 + 登录态问题 → 恢复长期持久与双通道 `ok` 状态 |

---

## 二、1 分钟接入主流 AI 编程工具

所有原子 Skill 与原生 MCP 服务端均通过本地服务解耦：
- **本地 REST 控制面**：`http://127.0.0.1:8765`
- **本机 Stdio MCP (`notebooklm-fleet`, 15 工具)**：`uv --directory <NotebookLM-Fleet> run python -m app.mcp_server`
- **Google 官方 Remote MCP (`https://notebook.googleapis.com/mcp`, 7 工具)**：Bearer OAuth + `x-goog-user-project`

### 各工具安装方式
1. **Antigravity / Codex（原生 `.agents/skills` 标准）**  
   将 `atomic_skills/nlm-*` 目录复制或软链接到工作区的 `.agents/skills/`（或全局配置目录 `~/.gemini/config/skills/`）：
   ```powershell
   # 示例：批量复制到当前项目的 .agents/skills/
   Copy-Item -Recurse .\atomic_skills\nlm-* .\.agents\skills\
   ```
2. **Claude Code（`~/.claude/skills/` + `.mcp.json`）**  
   - 将 `nlm-*` 目录复制到 `~/.claude/skills/` 或项目 `.claude/skills/`。
   - 在项目根目录 `.mcp.json` 注册 `notebooklm-fleet` Stdio MCP。
3. **Cursor / Windsurf / Cline（Project Rules & Skills + MCP）**  
   - Cursor 支持将每个 `nlm-*/SKILL.md` 直接放入 `.cursor/rules/`（改为 `.mdc` 或由 Cursor Skills 识别）或引用本目录路径；同时在 MCP Settings 添加 `notebooklm-fleet`。
4. **Gemini CLI / 通用 MCP Agent**  
   - 将 `atomic_skills/nlm-*` 挂载到对应 Agent 的 Skills 目录，或由系统 Prompt 直接读取对应 `SKILL.md` 并调用本地 HTTP REST `http://127.0.0.1:8765`。

---

## 三、推荐原子组合配方（Workflow Recipes）

### 配方 1：代码改造后自动更新项目知识库并生成周报 PPT + 播客
`nlm-repo-to-notebook`（扫描脱敏）→ `nlm-quota-guard`（确认余额充足）→ `nlm-official-ingest`（写入并等待 `ACTIVE`）→ `nlm-studio-factory`（生成 `report` + `slide_deck` + `audio`）

### 配方 2：跨工具架构评审（不改代码，零幻觉引用源码）
`nlm-quota-guard`（确认账号可用）→ `nlm-grounded-qa`（按架构、接口、边界条件并发提问并保存为 `.md` 综述）

### 配方 3：批量账号到期 / Cookie 过期急救
`nlm-quota-guard`（定位 `expired` / `error` 账号）→ `nlm-account-ops`（走 Official OAuth 302 直跳或 CDP/cURL 无锁恢复）→ `nlm-quota-guard`（复测刷新额度）

---

## 四、安全红线（所有原子 Skill 强制遵循）
- 严禁读取、打印、上传 `.env`、OAuth `client_secret*.json`、`oauth_tokens/*.json`、Chrome Cookie/cURL 原文、`*.pem`/`*.key` 等凭据资产。
- 对外生成分享报告时，必须用占位符脱敏个人邮箱、GCP Project ID、本机用户名路径及内部 Notebook UUID。
