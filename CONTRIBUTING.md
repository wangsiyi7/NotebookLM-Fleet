# Contributing to NotebookLM-Fleet

感谢关注与参与共建 `NotebookLM-Fleet`！为保证团队成员本地环境与公共仓库安全，请遵循以下协作规范：

---

## 1. 本地开发环境搭建

需要安装 **Python >= 3.11** 与 [`uv`](https://docs.astral.sh/uv/)：

```powershell
# 1) 克隆仓库并安装依赖
git clone https://github.com/wangsiyi7/NotebookLM-Fleet.git
cd NotebookLM-Fleet
uv sync

# 2) 初始化本机环境配置文件（.env 默认已被 .gitignore 忽略）
Copy-Item .env.example .env

# 3) 运行全量单元与协议测试
uv run pytest -v
```

---

## 2. 提交前强制脱敏红线（Strict Pre-Commit Security Rules）

在执行 `git add` 与 `git commit` 前，**严禁将以下任何内容带入 Git 历史**：

1. **`data/` 与 `.env` 文件**：包含本地 SQLite 数据库 `fleet.db`、Google Cloud OAuth `client_secret.json`、`oauth_tokens/*.json` 及 Chrome/Edge 隔离用户档案。
2. **个人账号与环境信息**：真实 Gmail 地址、Google Cloud `project_id`、本机用户名绝对路径（如 `C:\Users\<name>` / `D:\...`）、个人网盘路径与远程 Notebook UUID。
3. **浏览器会话凭据**：任何 Cookie / `cURL` 字符串、`SID` / `OSID` / `__Secure-1PSIDTS` / `ya29.` Token 取值。
4. **Git 提交身份**：建议使用 GitHub 提供的无隐私邮箱（例如 `username@users.noreply.github.com`）执行本地仓库提交。

---

## 3. 代码与测试规范

- **异步与非阻塞**：FastAPI 路由中涉及文件 I/O、SQLite 热备或浏览器提取时使用 `asyncio.to_thread` 或 `aiosqlite` 异步接口，禁止阻塞主事件循环。
- **路由安全**：所有接受本地路径的端点必须经过 `app/core/drive_hub.py` 的 `is_allowed_file_for_ingest()` 与 `resolve_safe_subpath()` 白名单校验。
- **测试要求**：提交 PR 前确保离线单测全绿：
  ```bash
  uv run pytest -v -k "not test_live_official_mcp_public_probe"
  ```
- **白皮书联动更新**：若修改了架构或能力矩阵并更新 `docs/sharing_archive_v1/NotebookLM-Fleet_系统能力全景与开放架构白皮书_v1.0.md`，请运行 `build_exports.py` 同步刷新 `.pdf` 与 `.docx` 版本。
