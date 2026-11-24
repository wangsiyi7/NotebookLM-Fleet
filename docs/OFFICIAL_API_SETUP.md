# 2026-11 官方 Gemini Notebook API (`notebook.googleapis.com`) 开通与个人多账号接入指南

官方在 2026 年 11 月开放的 **Gemini Notebook API (`notebook.googleapis.com/v1`)** 与 **官方远程 MCP (`notebook.googleapis.com/mcp`)** 在 Public Preview 期**专为个人 Google 账号开放**（免费、稳定性高、无 Cookie 过期风险）。

当前官方 v1/MCP 开放了笔记本与来源的 CRUD（`create/get/list/update_notebook` 与 `create/get/list_source`），额度监控、Chat、Studio 生成、Drive 引用与跨号共享仍由本系统的 Channel 2 (`notebooklm-py`) 接管，二者在后端自动协同路由。

---

## 一、一次性的 4 步申请与配置（约 3 分钟）

1. **创建或选择一个 Google Cloud 项目**（只需 1 个项目即可服务你的多组 Ultra / Pro / Free 账号）  
   点击打开：[https://console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate)  
   记下项目的 **Project ID**（例如 `my-nlm-fleet-2026`）。

2. **启用 Gemini Notebook API (`notebook.googleapis.com`)**  
   点击打开（确认左上角选中了刚才的项目，点 **启用 / Enable**）：  
   [https://console.cloud.google.com/apis/library/notebook.googleapis.com](https://console.cloud.google.com/apis/library/notebook.googleapis.com)

3. **配置 OAuth 同意屏幕并添加测试用户（关键！）**  
   点击打开：[https://console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)  
   - 若提示创建 Branding：应用名称填 `NotebookLM-Fleet`，用户支持邮箱与开发者邮箱填你自己的主 Gmail，受众（Audience）选择 **External（外部）**，保持 **Testing（测试中）** 状态即可（无需提交审核！）。
   - 在 **Test users（测试用户）** 列表点击 **Add users**，把你要接入的 **全部个人 Gmail 账号邮箱**加进去。

4. **创建桌面端 OAuth 凭据并保存到本地**  
   点击打开：[https://console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients)  
   - 点击 **Create Client（创建客户端）**
   - Application type 选择 **Desktop app（桌面应用）**，名称填 `NotebookLM-Fleet Desktop`
   - 创建后点击 **Download JSON**
   - 直接在 Web 控制面板「官方 API & MCP 实验室」上传该 JSON，或手动保存为 `<NotebookLM-Fleet>/data/client_secret.json`。

---

## 二、初始化每个个人账号（必做一步）

> **注意**：官方文档提示，任何个人 Google 账号首次通过 API 访问前，必须先在浏览器里手工打开并登录过一次 Gemini Notebook 网页端，否则接口可能返回 `404 NOT_FOUND`。

- 网页端入口：[https://notebook.google.com/](https://notebook.google.com/)
- 随后打开本地控制面板 `http://127.0.0.1:8765`：
  1. 在「**账号台账与策略**」填入你的 **Project ID**，把「启用官方 API 主通道」设为 **启用** 并保存（或在官方实验室上传 JSON 时自动填充）。
  2. 在「**额度总览**」每个账号行点击「**官方 API 授权**」，在同步弹出的浏览器标签页选择对应 Gmail 完成一次授权，系统会自动把各账号的 `refresh_token` 保存在 `data/oauth_tokens/<alias>.json`。

---

## 三、官方远程 MCP (`https://notebook.googleapis.com/mcp`) 说明

如果你希望把主账号也直连 Google 托管的官方远程 MCP，其地址为：
- URL: `https://notebook.googleapis.com/mcp`（Streamable HTTP JSON-RPC 2.0 + OAuth Bearer Token + `x-goog-user-project` 请求头）

但在多账号场景下，Google 托管端点一次只绑定一个 Bearer Token 且不支持查 5 小时/周额度与生成 Studio；因此本机配套提供**统一调度 MCP (`notebooklm-fleet`)** 与 **原子化 Skills (`atomic_skills/`)**，可以在 Antigravity / Claude Code / Cursor / Codex 中一键调度全账号池并自动路由官方 API。
