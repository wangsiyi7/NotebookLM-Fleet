# 03 · 2026-11 官方 Gemini Notebook API & Remote MCP 协议集成手册

## 一、官方端点与已验证实测规范

2026 年 11 月 Google 开放的 **Gemini Notebook Personal Beta** 提供原生 OAuth 2.0 端点，免 Cookie 维护：

- **REST API v1 根地址**：`https://notebook.googleapis.com/v1`
- **二进制媒体上传端点**：`https://notebook.googleapis.com/upload/v1`（单文件上限 **200 MiB**）
- **Remote MCP 托管端点**：`https://notebook.googleapis.com/mcp`
- **OAuth 作用域（Scope）**：`https://www.googleapis.com/auth/notebook`
- **必传计费/配额请求头**：`x-goog-user-project: <Google Cloud Project ID>` 与 `Authorization: Bearer <access_token>`

### 本项目已完成的实盘验证基线
- **协议发现**：向 `https://notebook.googleapis.com/mcp` 发起真实 Streamable HTTP JSON-RPC 2.0 握手（`protocolVersion: 2024-11-05`），服务端响应 `StatelessServer / ESF`（`HTTP 200` + `Mcp-Session-Id` 会话头），返回 7 个官方工具完整 JSON Input Schema，并缓存快照至数据库。
- **实盘读写授权（账号 `ultra-1` · `ultra-1@example.com` · Project `<your-gcp-project-id>`）**：
  - `GET https://notebook.googleapis.com/v1/notebooks?pageSize=50` 实测返回 `HTTP 200 OK`（拉取到 **50 个真实笔记本**）。
  - Remote MCP `tools/call (list_notebooks)` 实测返回 `HTTP 200 OK`（同步返回 **50 个真实笔记本**）。

---

## 二、官方 Remote MCP 7 大工具接口规范

所有工具名及参数名均在真实 Google `tools/list` 响应下校验确认（严格使用 **camelCase**）：

1. **`list_notebooks`（只读）**：支持 `pageSize`、`pageToken` 与 `filter`（支持 `"notebook.is_owner = true"` / `"notebook.is_owner = false"` 区分自建本与共享本）。
2. **`get_notebook`（只读）**：参数 `{"name": "notebooks/{id}"}`。
3. **`create_notebook`（写入）**：参数 `{"notebook": {"displayName": "..."}}`，返回含 `name`（`notebooks/{id}`）与 `createTime` 的对象。
4. **`update_notebook`（写入）**：参数 `{"notebook": {"name": "notebooks/{id}", "displayName": "..."}, "updateMask": "display_name"}`。
5. **`list_sources`（只读）**：参数 `{"parent": "notebooks/{id}", "pageSize": 50}`。
6. **`get_source`（只读）**：参数 `{"name": "notebooks/{id}/sources/{source_id}"}`（常用于轮询等待状态由 `PROCESSING` 变为 `ACTIVE`）。
7. **`create_source`（写入）**：参数结构：
   - **网页/YouTube URL**：`{"parent": "notebooks/{id}", "source": {"displayName": "...", "webContent": {"url": "https://..."}}}`
   - **纯文本/Markdown**：`{"parent": "notebooks/{id}", "source": {"displayName": "...", "mimeType": "text/markdown", "textContent": {"content": "..."}}}`  
     > **关键约束**：官方后端在 `textContent` 场景下仅允许 `"text/plain"` 与 `"text/markdown"` 两种 `mimeType`，传入其他值会直接触发 `INVALID_ARGUMENT`；代码中已在 `official_api.py`、`official_mcp_client.py` 与 `router.py` 做强制规范化映射。

---

## 三、REST v1 / Upload v1 补充协议设计（`official_api.py`）

相比 Remote MCP 仅支持 URL 与文本，**REST Upload v1 补齐了大体积二进制文件（PDF、音频、视频、Office 文档等）直传能力**：
- 上传路由：`POST https://notebook.googleapis.com/upload/v1/notebooks/{id}/sources:upload?uploadType=media&source.displayName={url_encoded_label}`
- 请求体为文件原始字节流，`Content-Type` 自动猜测 MIME 类型，文件读取采用 `asyncio.to_thread(src.read_bytes)` 并在读取前校验 `<= 200 MiB`。
- 当碰到 `HTTP 429` 限流时自动执行指数退避抖动重试（`2**attempt + jitter`，最多 3 次）；碰到 HTTP `403` / `404` 会附加清晰的中文排查提示（例如：Preview 期暂只开放个人 Gmail，不开放 Workspace 账号；且每个账号需先在浏览器访问过一次 `notebooklm.google.com`，否则首调会返 `404`）。

---

## 四、Streamable HTTP 客户端与网络弹性工程（`official_mcp_client.py`）

1. **双格式解析器（`_parse_mcp_http_response`）**：官方 ESF 网关会根据场景返回标准 `application/json` 或 SSE 流式响应 `text/event-stream`（`data: {...}`）；解析器自动按行抽取有效 JSON-RPC 载荷并优先取最终携带 `result` 或 `error` 的事件帧。
2. **完整 3 步状态握手**：每次会话按 `initialize` →（捕获 `Mcp-Session-Id` 头）→ 尽力发送 `notifications/initialized` → `tools/call` / `tools/list` 执行。
3. **非空错码与 TUN 代理重连**：`httpx.ConnectTimeout` 异常的默认 `str(exc)` 为空字符串。系统统一使用 `format_exception()` 输出 `"ConnectTimeout（连接 notebook.googleapis.com 超时或中断...）"`，对 TLS 握手瞬断自动重试 1 次，并支持在设置中配置可选 `official_proxy_url`（如 `http://127.0.0.1:7890`）强制走指定代理出口。

---

## 五、永不过期的 `/oauth-redirect` 浏览器直跳授权流

针对后台 `uvicorn` 服务中 Python `webbrowser.open()` 无法向桌面弹出浏览器窗口的问题，系统设计了零阻塞直跳架构：
- 用户在面板点击「官方 API 授权」时，前端同步调用 `window.open('/api/accounts/{id}/oauth-redirect?t=...')`（保住用户点击手势不被 Chrome 弹窗拦截）。
- 后端瞬间在 `localhost:<随机空闲端口>` 绑定一次性 loopback HTTP 回调服务器（30 分钟有效，且自动忽略 `/favicon.ico` 探针），立刻 `302` 重定向到 Google OAuth 同意页。
- 用户完成同意后，本地监听收到 `?code=...` 即刻换取 `refresh_token` 持久化至 `data/oauth_tokens/<alias>.json`，前端以每 2 秒轮询自动关闭提示窗并刷新为「Ch1 已就绪」。
