# 错误记忆（error_memory）

> 记录重复出现或值得复盘的问题：现象、根因、解法、下次避免方法。
> 每条按时间倒序追加。

---

## 2026-07-25 NapCat WebUI API 认证失败：明文 Token vs Credential

### 现象
`captcha_solver.py` 中 `_napcat_webui_post()` 使用 `Authorization: Bearer tgjqr2024`（明文 token）调用 NapCat WebUI API，返回 401 Unauthorized。导致密码登录自动恢复、验证码提交功能完全失效。

### 根因
NapCat WebUI 中间件（`auth.ts`）不接受明文 token。正确流程是：
1. 计算 `hash = SHA256(token + '.napcat')`（注意 `.napcat` 是固定 salt 后缀）
2. `POST /api/auth/login {"hash": hash}` → 返回 `data.Credential`（Base64 编码的 HMAC-SHA256 签名 JSON）
3. 后续请求用 `Authorization: Bearer <Credential>`（有效期 1 小时）

### 解法
新增 `_get_webui_credential()` 函数实现上述流程，Credential 缓存 1 小时。`_napcat_webui_post()` 改为先获取 Credential 再请求。

### 下次避免
- 调用第三方服务 API 前，先读源码确认认证机制，不要猜测
- NapCat WebUI 的 `/api/auth/login` 路径带 `/api` 前缀（不是 `/auth/login`）

---

## 2026-07-25 YesCaptcha 不支持腾讯验证码（TencentCaptcha）

### 现象
密码登录返回 `needCaptcha: true`，调用 YesCaptcha 创建 TencentCaptcha 任务返回 `errorId=1, 任务类型不正确或不受支持`。

### 根因
YesCaptcha 不支持 TencentCaptcha 类型。经逐一测试 `TencentCaptcha`、`AntiTencentTask`、`TencentTask` 等 6 种命名均不被支持。

### 解法
密码登录遇到验证码时回退到推送二维码通知用户扫码。无验证码时密码登录可全自动恢复。

### 下次避免
- 采购验证码服务前先确认支持的验证码类型列表
- 如需全自动解决腾讯验证码，需考虑 2Captcha 或 CapSolver 等其他服务

---

## 2026-07-25 Web 管理后台批量检查：14 处同类隐患

### 现象
用户要求「检查其他页面和功能是否存在类似 bug」。全面审查后发现三类重复模式：
1. 某些页面功能点击后「没反应」或请求路径错误（AI 助手 404）。
2. 某些操作快速重复点击会产生重复请求（删除、添加、切换等）。
3. 某些加载失败被静默吞掉，用户看不到任何错误提示。

### 根因
1. **路径硬编码**：`aiChatSend` 使用 `fetch('/api/ai/chat')`，未使用 `API_BASE` 前缀，非根路径部署时直接 404。
2. **事件对象依赖**：`testConnection` 直接读取全局 `event.target`，现代浏览器严格模式或某些环境下该变量不存在，导致 `btn` 为 `undefined` 后报错。
3. **空 catch 块**：`loadQQOps` 内层 `catch (e2) {}`、`loadConfigForFields` 的 `catch (e) { /* silent */ }`，把网络/权限错误完全吞掉，用户看不到「加载失败」的任何提示。
4. **缺少 loading 状态**：`deleteLexWord`、`deleteGroupCfg`、`cancelUnmute`、`deleteAccess`、`resetPenalty`、`addFAQ`、`deleteFAQ`、`toggleFAQ`、`editFAQ`、`testFAQ` 共 10 个按钮操作函数，在异步请求期间未禁用按钮，用户可快速重复点击，产生重复删除/添加/切换等副作用。

### 解法
1. **统一路径前缀**：所有 `fetch` 调用必须使用 `API_BASE + '/api/...'`。
2. **统一按钮获取方式**：使用 `document.activeElement` 代替 `event.target`，并加 `if (btn)` 空值保护。
3. **空 catch 必须输出日志+提示**：至少写 `console.error('[模块] 失败:', e)`，用户可见的加载流程加 `showToast(e.message, true)`。
4. **所有「点击→请求」的按钮统一加 loading**：在函数开头 `const btn = document.activeElement; setBtnLoading(btn, true);`，在 `finally` 中 `setBtnLoading(btn, false);`。已有 `setBtnLoading` 辅助函数，可直接复用。

### 验证
- 对 `web/index.html` 全文搜索 `fetch(`，确认所有 API 调用均含 `API_BASE`。
- 对 `web/index.html` 全文搜索 `catch (e) {}` / `catch (e2) {}` / `/* silent */`，确认已无空 catch。
- 对 `web/index.html` 中所有 `onclick` 触发且涉及 `fetch` 的按钮函数，确认均已包裹 `setBtnLoading`。

### 下次避免
- **新增前端功能 checklist**：任何新按钮必须检查 (a) `fetch` 路径是否含 `API_BASE`；(b) 请求期间是否有 loading/禁用；(c) `catch` 是否至少输出日志。
- **代码审查关键词**：搜索 `catch\s*\(\s*\w+\s*\)\s*\{\s*\}` 和 `/* silent */`，这类写法直接视为 bug。
- **事件获取规范**：禁止依赖全局 `event` 对象，统一使用 `document.activeElement` 或从 HTML 显式传入 `this`。

---

## 2026-07-25 健康监控"立即探测"点击无反应

### 现象
Web 管理后台「健康监控」页面点击「立即探测」按钮，用户感觉没有任何反应：
- 探测区域一直停留在「点击刷新...」或显示「在线：未知 账号：(-)」，看起来像没探测到。

### 根因（两层）
1. **前后端字段不匹配（核心）**
   - 后端 `/api/health` 的 `services.napcat` 只返回 `status / message / last_event`（来自 `latest_health_status()` 读取数据库历史记录）。
   - 前端 `loadHealth()` 期望 `p.online`(布尔) / `p.nickname` / `p.user_id`。
   - 结果 `p.online` 恒为 `undefined`，「在线」永远显示「未知」；`nickname/user_id` 为空，账号显示「(-)」。
2. **按钮缺少即时反馈**
   - 点击后请求需 ~1 秒（SearXNG 探测 + psutil），期间探测区域与按钮无任何变化，用户以为「没反应」。

### 解法
1. **后端 `web_server.py` 的 `/api/health`**：在 `services.napcat` 中新增实时探测——调用 `_napcat_request("/get_login_info")` 和 `_napcat_request("/get_status")`，返回 `online`(布尔) / `nickname` / `user_id`，并用实时结果覆盖 `status/message`。API 无响应时 `online=false, status=offline`。
2. **前端 `web/index.html`**：
   - 新增 `loadHealthProbe()` 包装函数：点击立即把探测区设为「⏳ 探测中...」、按钮禁用并改文案为「探测中...」，请求完成后恢复。
   - `loadHealth()` 增加 `onlineFlag` 兜底：`p.online` 为空时从 `p.status === 'online'` 推断，保证旧数据也能正确显示。
3. 按钮加 `id="btn-health-probe"`，`onclick` 改为 `loadHealthProbe()`。

### 验证
- `curl /api/health` 返回 `online=true, nickname=1295232927, user_id=1295232927`。
- 浏览器实测：点击立即显示「⏳ 探测中...」，按钮禁用；~1 秒后显示「在线：是(绿) 账号：1295232927(1295232927) 状态：online」，顶栏状态点变绿。

### 下次避免
- 新增前端展示字段时，必须同时确认后端接口是否返回该字段，避免「字段名对不上导致静默失败」。
- 任何「点击触发请求」的按钮，必须提供即时视觉反馈（loading 文案 / 禁用），否则用户会判定为「没反应」。
- 前端解析后端字段时，对关键字段（如 online）加兜底推断，提升容错。
