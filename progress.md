# 进度记录（progress）

> 记录已完成的工作与当前状态。按时间倒序追加。

---

## 2026-07-25 修复 AI 天气工具选择：系统提示词引导

### 问题描述
用户私聊 "明天青岛天气" 时，AI 调用了 `web_search` 而非 `get_weather`，因为系统提示词只告诉 AI "你具备联网搜索能力"，AI 不知道有专门的天气工具。

### 修复内容
修改 `napcat_ws.py` 中**群聊**和**私聊**两处的 `system_prompt`：
- 第 1 条从 "联网搜索和获取网页内容" 改为 "联网搜索、获取网页内容和**查询天气**"
- 新增第 2 条："当用户询问任何城市的天气时，**必须使用 `get_weather` 工具查询，禁止用 `web_search` 查天气**"
- 后续规则序号顺延

**注意**：此修改**仅约束天气场景**，不影响 AI 对其他问题（新闻、百科、股价等）正常使用 `web_search` 和 `fetch_webpage`。

### 当前状态
- tgjqr-bot 容器已重启，新提示词已生效

---

## 2026-07-25 修复私聊天气查询：支持具体日期格式 + 修正索引偏移

### 问题描述
用户私聊查询 "明天青岛的天气" 时，AI 传入具体日期 `date="2026-07-26"`，但 `get_weather` 工具仅支持中文枚举值（今天/明天/后天/3天后），导致：
- `ai_tools.py` 的 `execute_weather`：将具体日期当成默认 "明天" 处理
- `napcat_ws.py` 的 `_execute_weather`：直接返回 `error: 不支持的日期参数`

此外，`napcat_ws.py` 的预报索引存在**偏移错误**：`forecasts[date_offset - 1]` 导致 "明天" 实际返回今天的数据。

### 修复内容
1. **`handlers/ai_tools.py`**：
   - 工具 schema 去掉 `enum` 限制，`date` 字段允许任意字符串
   - 新增 `_parse_weather_date()` 辅助函数，支持解析：
     - 中文枚举：今天、明天、后天、3天后
     - 具体日期：`YYYY-MM-DD`、`YYYY/MM/DD`
   - 自动计算与今天的天数差，超出 0-3 范围给出明确提示
   - 修正预报数组越界检查：`len(forecast) < days_offset` → `days_offset >= len(forecast)`

2. **`napcat_ws.py`**：
   - `_execute_weather` 内联同样的日期解析逻辑
   - 修正预报索引偏移：`forecasts[date_offset - 1]` → `forecasts[date_offset]`
   - 越界检查同步修正：`date_offset > len(forecasts)` → `date_offset >= len(forecasts)`

### 验证
- 本地单元测试 14 个用例全部通过（空值、中文枚举、具体日期、过去日期、超范围、兜底匹配）
- 重启 tgjqr-bot 容器后启动正常，日志无报错

---

## 2026-07-25 项目全面深度代码审计：修复 24 处 bug

### 审计范围
覆盖全部 25 个 Python 文件 + web_server.py（1912行）+ web/index.html，逐函数逐代码块检查。

### 已修复（按严重程度排序）

**严重级（会导致运行时崩溃）- 9 处**
1. `scheduled_posts.py` 缺少 `is_super_admin` 导入 → 所有定时消息管理命令 NameError 崩溃
2. `group_manager.py` `/ban` 参数解析失败默认永久封禁 → 改为报错提示
3. `group_manager.py` `/mute` 参数解析失败默认永久禁言 → 同上
4. `ad_detector.py` callback_data 分割无边界检查 → IndexError 崩溃
5. `napcat_ws.py` `_delayed_ocr_check` 中 `detect_source` 未定义 → UnboundLocalError
6. `napcat_ws.py` `group_create_time` 误用作 `group_id` → 创建虚假群配置
7. `group_member_store.py` `data["data"]["user_id"]` 直接下标 → KeyError
8. `captcha_solver.py` `proofWaterUrl` 为 null 时 NoneType 切片崩溃
9. `scheduled_posts.py` `hash()` 随机化导致 job_id 不稳定 → 改用 hashlib.md5

**高级（资源泄漏/数据损坏）- 7 处**
10. `ai_tools.py` 3 处 `httpx.Client` 未用 `with` → 连接泄漏
11. `ocr_audit.py` 2 处 `httpx.Client` 未用 `with` → 连接泄漏
12. `lexicon_engine.py` SQLite 连接未用 `finally`/`with` 保护
13. `scheduled_posts.py` `init_scheduler` 多次调用泄漏线程
14. `health_monitor.py` `_START_TIME` 初始值为 0 → Prometheus 指标数值异常
15. `ai_chat.py` 温度高低位未交换 → 丢失有效温度数据
16. `ai_chat.py` 单温范围过严（10-45）→ 北方冬季数据丢失

**中级（逻辑错误/信息泄露）- 8 处**
17. `main.py` 代理日志可能泄露用户名密码
18. `group_manager.py` `WELCOME_MESSAGE.format()` KeyError 崩溃
19. `visual_mgmt.py` `build_inline_panel` 死代码双重循环
20. `health_monitor.py` Prometheus HELP/TYPE 行在循环内重复
21. `napcat_ws.py` WS URL 日志泄露 access_token

**web_server.py 审计发现（未修改，记录备忘）**
- 路径遍历风险（group_openid 参数）
- `subprocess.check_output` 在 async 中阻塞事件循环
- `int(body.get("score"))` 无 ValueError 保护
- CORS `allow_origins=["*"]` + `allow_credentials=True` 不安全
- API 响应中泄露 NapCat WebUI token 明文
- `_write_env` value 未过滤换行符

### 部署验证
- 重启后日志干净，无报错
- APScheduler 正常启动（1 个定时任务）
- 健康监控正常启动
- WS 连接正常（token 已隐藏）

---

## 2026-07-25 修复密码登录自动恢复：NapCat WebUI 认证流程

### 已完成
- **根因**：`captcha_solver.py` 中 `_napcat_webui_post()` 直接使用 `Bearer tgjqr2024`（明文 token）调用 WebUI API，但 NapCat 的中间件要求的是 Base64 编码的 HMAC-SHA256 签名 Credential，导致所有密码登录和验证码提交请求返回 401 Unauthorized
- **修复方案**：
  1. 新增 `_get_webui_credential()` 函数：计算 `SHA256(token + '.napcat')` → POST `/api/auth/login` → 获取 Credential
  2. Credential 缓存 1 小时（NapCat 有效期 1 小时），提前 60 秒刷新
  3. `_napcat_webui_post()` 改为先获取 Credential 再用 `Bearer <Credential>` 请求
  4. `submit_captcha_via_webui()` 改为复用 `_napcat_webui_post()`
  5. `_wait_login_success()` 优先用 OneBot11 API（不需要 WebUI 认证），其次用 WebUI API
- **验证**：部署后日志确认 Credential 获取成功，密码登录 API 调用成功（返回 `needCaptcha: true`）
- **限制**：YesCaptcha 不支持 TencentCaptcha 类型，密码登录遇到验证码时回退到推送二维码
- **恢复链路**：QQ掉线 → 健康监控检测 → 重启NapCat → 尝试密码登录（无验证码时全自动恢复）→ 遇验证码则推送二维码到TG

### 当前状态
- 密码登录认证流程已修复，可正常调用 NapCat WebUI API
- 遇到验证码时仍需用户手动扫码

---

## 2026-07-25 全面检查并修复 Web 管理后台类似 bug

### 已完成
- **前端 `web/index.html` 修复 14 处隐患**：
  1. **AI 助手路径错误**：`fetch('/api/ai/chat')` → `fetch(API_BASE + '/api/ai/chat')`，修复非根路径部署时 404。
  2. **`testConnection` 事件获取不稳定**：`event.target` → `document.activeElement`，增加空值保护，避免部分浏览器严格模式下获取不到按钮。
  3. **`loadQQOps` 静默吞错**：内层 `catch (e2) {}` 改为输出 `console.error` 并将统计数显示为 `-`。
  4. **`loadConfigForFields` 静默吞错**：`catch (e) { /* silent */ }` 改为 `console.error` + `showToast`，配置加载失败时用户可感知。
  5. **10 个函数缺少按钮 loading 保护**：快速重复点击会导致重复操作，全部添加 `setBtnLoading`：
     - `deleteLexWord`、`deleteGroupCfg`、`cancelUnmute`
     - `deleteAccess`、`resetPenalty`
     - `addFAQ`、`deleteFAQ`、`toggleFAQ`、`editFAQ`、`testFAQ`
- **后端检查**：确认所有 API 返回格式统一（`{"ok": bool}` 或 `{"success": bool, "data": ...}`），无额外数据契约问题。

### 当前状态
- 所有修复已写入 `web/index.html`，无需重启容器（前端静态文件由 tgjqr-bot 直接 serve，刷新页面即生效）。
- 用户需强制刷新页面（Ctrl+F5）获取最新 JS。

---

## 2026-07-25 修复健康监控「立即探测」无反应

### 已完成
- **后端 `web_server.py` `/api/health`**：NapCat 状态从「读数据库历史」升级为「实时调用 OneBot11 API（get_login_info + get_status）」，返回 `online / nickname / user_id` 字段，并用实时结果覆盖 `status / message`。
- **前端 `web/index.html`**：
  - 新增 `loadHealthProbe()` 包装函数，提供「⏳ 探测中...」即时反馈 + 按钮禁用。
  - 按钮 `id="btn-health-probe"`，`onclick` 改为 `loadHealthProbe()`。
  - `loadHealth()` 增加 `onlineFlag` 兜底推断（`p.online` 为空时用 `p.status === 'online'`）。
- **验证**：重启 tgjqr-bot 容器后，`curl` 接口返回正确字段；浏览器实测点击立即有反馈，结果正确显示「在线：是」。

### 当前状态
- tgjqr-bot 容器已重启，NapCat 在线（1295232927），健康监控「立即探测」功能恢复正常。
- 修复详情见 `error_memory.md`。
