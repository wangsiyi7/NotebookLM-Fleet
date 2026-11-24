---
name: nlm-repo-to-notebook
description: Convert a local codebase, project folder, or documentation tree into a secret-free, orthogonally sliced Markdown bundle and ingest it cleanly into NotebookLM via Official REST/MCP APIs or Fleet Topics.
---

# NLM Repo-to-Notebook（代码库安全知识化与自动入库原子技能）

在完成新功能开发、代码审查或准备对外技术分享后，把一个工程目录**安全、结构化、无敏感信息泄露地**同步进 NotebookLM，形成“活的项目知识库”。

## 铁律：敏感资产零泄露清单
在扫描与打包任何代码/配置目录时，**必须强制排除**：
- 环境变量与私钥：`.env`、`*.env.local`、`*.pem`、`*.key`、`*.p12`、`*.pfx`
- Google / NotebookLM 认证文件：`client_secret*.json`、`oauth_tokens/*`、`storage_state.json`、`cookies*`
- 构建产物与二进制依赖：`.venv/`、`node_modules/`、`__pycache__/`、`*.db`、`*.sqlite*`、`dist/`、`build/`
- 内文脱敏规则：若生成综述 Markdown 用于公开团队或对外分享，**必须替换**个人邮箱、GCP Project ID、本机用户目录路径（`C:\Users\<user>` -> `%USERPROFILE%`）、内部 Notebook UUID 及任何 API Key / Token 字样。

## 执行步骤

1. **安全浏览工作区结构**
   - 优先使用 Fleet 安全目录接口（内置敏感文件名隐身拦截）：
     - `GET http://127.0.0.1:8765/api/workspace/roots`
     - `GET http://127.0.0.1:8765/api/workspace/tree?root=workspace&subpath=<rel>`
   - 或在本地以只读方式筛选源码、设计文档、接口定义与测试文件。
2. **正交切片规划（避免超大单文件导致引文失焦）**
   - 不要把整个仓库揉成 1 个巨型 TXT；拆分为 4~8 个正交、边界清晰的 Markdown 模块（单篇控制在 `4 KB ~ 60 KB` 最佳实践区），例如：
     - `00_资产目录与提问指南.md`
     - `01_项目全景与架构蓝图.md`
     - `02_核心引擎与状态机设计.md`
     - `03_对外接口与协议契约.md`
     - `04_测试覆盖、审查记录与待办路线.md`
   - 若项目已存在成熟文档目录（如 `Notebook_MD_Assets_*` 或 `docs/`），优先复用增量更新而不是盲目重写。
3. **落盘前执行强制扫描**
   - 将切片放入独立目录（如 `<project>/Notebook_MD_Assets_<name>/`），用正则核验 **零命中** 真实邮箱、GCP ID、私钥头（`BEGIN PRIVATE KEY`）、Google Cookie 键值（`SID=` / `OSID=` / `ya29.`）。
4. **交由 `nlm-official-ingest` 入库并验活**
   - 调用 `POST /api/official/quick-add`（`channel="official_rest"` 或 `"official_mcp"`），传入新建或更新目标的 `notebook_id` 与全部 `.md` 路径列表；
   - 轮询 `GET /api/official/sources` 确认全部来源状态变成 `ACTIVE`。

## 输出规范
返回：切片目录路径、各分片文件名与字节数、敏感词零命中扫描结论、目标 Notebook ID 与 `ACTIVE` 校验计数。
