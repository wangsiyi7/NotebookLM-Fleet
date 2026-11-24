---
name: nlm-account-ops
description: Manage passwordless authentication and long-term session recovery for multiple NotebookLM accounts across Official Google OAuth (302 browser-direct redirect), Channel 2 (isolated CDP Chrome/Edge capture on :9222+, Windows interactive .cmd login script for isolated desktops, silent persistent-profile session renewal, or notebook.google.com cURL import with authuser preservation), without locking daily Chrome.
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
   - **优先无感续期（如之前已走独立窗口登录、保留有本地浏览器档案）**：
     - 调用 `POST /api/accounts/{id}/refresh-session`，后端通过无头持久配置目录静默续期并重写 `OSID` / `__Secure-1PSIDTS`。
   - **方式一（推荐·免关日常 Chrome·长期持久 `LSID + OSID`）**：
     1. 启动该账号专属隔离 Chrome/Edge：`fleet_open_account_browser(alias, mode="chrome")` 或 `POST /api/accounts/{id}/open-browser`（自动跳转 `accounts.google.com/AccountChooser` 最新登录页，按账号分配独立端口 `:9222+` 与独立目录）。
     2. **隔离桌面环境处理（`desktop_isolated == true`）**：若当前终端/守护进程处于非交互桌面，请引导用户下载 `GET /api/accounts/{id}/login-script.cmd` 并双击运行，或调用 `POST /api/accounts/{id}/launch-cmd-login` 在真实桌面激活 CMD 终端拉起可见浏览器。
     3. 用户在该窗口登录目标账号并进入 `notebook.google.com` 主页后，调用 `POST /api/accounts/{id}/capture-cdp`（底层通过 CDP `Storage.getCookies` 抓取多域 Cookie 并持久化保存 `LSID`）。
   - **方式二（5 秒极速·无窗口粘贴 cURL，适合临时应急）**：
     - 请用户在已登录的日常 Chrome 中打开 `https://notebook.google.com/`，按 `F12` → `Network` → 刷新页面 → 右键第一条 `notebook.google.com` 请求选 **Copy → Copy as cURL (bash)**，引导其在 Web UI「一键登录会话」弹窗中提交（或由用户端直接提交至 `POST /api/accounts/{id}/import-cookies-raw`）。
     - **关键坑点提醒**：若复制了旧域 `notebooklm.google.com`（跳转前请求）或漏带 `?authuser=N`，`OSID` 将因域名绑定被 Google 拒收；必须复制跳转后最终落地到 `notebook.google.com` 的请求。

4. **恢复验证**
   - 任一修复后立即运行 `nlm-quota-guard`（`POST /api/accounts/{id}/poll-quota`），确认 `nlm_auth_status == "ok"` 且成功返回 `five_hour_remaining_pct` 与 `weekly_remaining_pct`。
