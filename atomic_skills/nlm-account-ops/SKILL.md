---
name: nlm-account-ops
description: Manage passwordless authentication and session recovery for multiple NotebookLM accounts across Official Google OAuth (302 browser-direct redirect) and Channel 2 (isolated CDP Chrome capture on :9222+ or notebook.google.com cURL import with authuser preservation), without locking daily Chrome.
---

# NLM Account Ops（免密多账号认证与排障原子技能）

解决 Windows/macOS 下日常 Chrome 运行导致本地 Cookie 数据库被系统锁定（`browser database is locked`）、Playwright 登录弹窗被自动化风控拦截、以及 Google 迁移主域名至 `https://notebook.google.com/` 与多账号槽位 `?authuser=N` 不一致等全部认证痛点。

## 安全边界
- **严禁**向用户索取、记录、打印或回显 Google 明文密码、二次验证码、`client_secret.json` 私钥、`refresh_token` 或任何原始 `SID`/`OSID`/`__Secure-1PSIDTS` Cookie 值。

## 诊断与修复决策树

1. **查看当前账号池双通道状态**
   - 调用 `fleet_list_accounts()` 或 `GET http://127.0.0.1:8765/api/accounts`。
   - 对照两个状态字段：
     - **Ch1 官方 API**：`official_oauth_status`（`ok` / `not_configured` / `error` / `expired`）
     - **Ch2 协议/Studio 通道**：`nlm_auth_status`（`ok` / `unauthenticated` / `expired` / `error`）

2. **场景 A：需要打通 Channel 1（官方 REST v1 & Remote MCP）**
   - 确认 `GET /api/official/status` 中 `client_secret_present == true` 且 `project_id` 已配置。
   - **永远使用浏览器直跳端点**：引导用户打开 `http://127.0.0.1:8765/api/accounts/{id}/oauth-redirect`（或前端弹窗直接同步 `window.open`，30 分钟有效回环端口，零弹窗拦截）。
   - 若出现网络超时（如 `oauth2.googleapis.com` 瞬断），检查全局配置 `official_proxy_url` 是否需要填写本地代理（如 `http://127.0.0.1:7890`），系统层面已具备 OAuth 刷新 3 次重试能力。

3. **场景 B：需要打通或恢复 Channel 2（实时测额 / 11 类 Studio / Chat）**
   - **方式一（推荐·免关日常 Chrome·隔离 CDP）**：
     1. 启动该账号专属隔离 Chrome：`fleet_open_account_browser(alias, mode="chrome")` 或 `POST /api/accounts/{id}/open-browser`（默认打开 `https://notebook.google.com/`，按账号分配独立端口 `:9222+` 与独立目录）。
     2. 请用户在该窗口手工登录目标账号并进入 `notebook.google.com` 主页（不要关闭该窗口）。
     3. 调用 `POST /api/accounts/{id}/capture-cdp`（底层通过 Chrome DevTools Protocol `Storage.getCookies` 直接抽取并立即运行实时测额）。
   - **方式二（5 秒极速·无窗口粘贴 cURL）**：
     - 请用户在已登录的日常 Chrome 中打开 `https://notebook.google.com/`，按 `F12` → `Network` → 刷新页面 → 右键第一条 `notebook.google.com` 请求选 **Copy → Copy as cURL (bash)**，引导其在 Web UI「一键登录会话」弹窗中提交（或由用户端直接提交至 `POST /api/accounts/{id}/import-cookies-raw`）。
     - **关键坑点提醒**：若复制了旧域 `notebooklm.google.com`（跳转前请求）或漏带 `?authuser=N`，`OSID` 将因域名绑定被 Google 拒收；必须复制跳转后最终落地到 `notebook.google.com` 的请求。

4. **恢复验证**
   - 任一修复后立即运行 `nlm-quota-guard`（`POST /api/accounts/{id}/poll-quota`），确认 `nlm_auth_status == "ok"` 且成功返回 `five_hour_remaining_pct` 与 `weekly_remaining_pct`。
