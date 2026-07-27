# TGJQR Bot - Telegram 群组管理与自动回复机器人

基于 **aiogram 3.x** 异步框架开发的 Telegram 机器人，集**群组管理、AI 智能客服、自动回复、定时消息推送、入群 Captcha 验证**于一体。支持 Docker 部署。

---

## 功能特性

### 1. 群组管理
- **欢迎新成员**：自动发送欢迎消息（支持占位符）
- **禁言/踢人/封禁**：管理员命令，支持限时操作
- **反垃圾 Captcha 验证**：新用户入群后需在限时内点击按钮验证，否则自动踢出
- **敏感词过滤**：自动删除含违禁词的消息
- **禁止转发**：可选禁止转发外部频道消息

### 2. AI 智能客服
- **OpenAI 兼容 API**：支持自定义 Base URL 和 API Key，可接入 DeepSeek、OpenRouter、OneAPI、Ollama 等
- **私聊自动回复**：关键词未匹配时自动调用 AI
- **群组触发方式**：支持 `@机器人`、`回复机器人` 或 `所有消息` 三种模式
- **系统提示词**：可自定义 AI 角色和行为

### 3. 自动回复客服
- **关键词匹配**：支持多关键词自动回复 FAQ
- **动态管理**：通过命令实时添加/删除/列出回复规则
- **双层配置**：支持环境变量全局配置 + 数据库分群配置

### 4. 定时消息推送
- **Cron 表达式**：支持 `分 时 日 月 星期` 标准 cron 格式
- **多任务管理**：支持添加、删除、测试、重载定时任务
- **持久化存储**：任务存储在 SQLite 中，重启后自动恢复

---

## 快速开始

### 方式一：直接运行（本地开发）

#### 1. 安装依赖

```bash
pip install -r requirements.txt
```

#### 2. 创建 .env 配置文件

在项目根目录新建 `.env` 文件：

```env
# ===== 必填项 =====
# 从 @BotFather 获取的 Bot Token
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz

# 超级管理员用户 ID（多个用逗号分隔）
ADMIN_IDS=123456789,987654321

# ===== 代理配置（中国大陆必需） =====
PROXY_URL=http://127.0.0.1:7890

# ===== 群组管理 =====
WELCOME_MESSAGE=👋 欢迎 {first_name} 加入 {chat_title}！
ANTISPAM_SECONDS=60
BLOCK_FORWARD=false
BANNED_WORDS=广告,诈骗,色情

# ===== 自动回复（JSON 格式） =====
AUTO_REPLY_RULES={"价格":"产品A 100元，产品B 200元","联系方式":"邮箱: support@example.com"}

# ===== 定时消息（JSON 格式） =====
SCHEDULED_MESSAGES=[{"chat_id":-1001234567890,"text":"早安！","cron":"0 9 * * *"}]

# ===== AI 客服配置 =====
AI_ENABLED=true
AI_BASE_URL=https://api.openai.com/v1
AI_API_KEY=sk-your-api-key
AI_MODEL=gpt-4o-mini
AI_SYSTEM_PROMPT=你是一个专业的客服助手，用中文回答用户问题，回答简洁友好。
AI_GROUP_TRIGGER=mention
AI_PRIVATE_AUTO=true
```

**AI 配置说明：**

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `AI_BASE_URL` | API 基础地址，留空使用 OpenAI 官方 | `https://api.deepseek.com` |
| `AI_API_KEY` | API 密钥 | `sk-xxx` |
| `AI_MODEL` | 模型名称 | `gpt-4o-mini`, `deepseek-chat` |
| `AI_GROUP_TRIGGER` | 群组触发方式 | `mention` / `reply` / `all` |

#### 3. 启动机器人

```bash
python main.py
```

如需代理：
```powershell
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:HTTPS_PROXY="http://127.0.0.1:7890"
python main.py
```

---

### 方式二：Docker 部署（生产环境推荐）

#### 1. 准备 .env 文件

同上，创建 `.env` 配置文件。

#### 2. 构建并启动

```bash
docker-compose up -d --build
```

#### 3. 查看日志

```bash
docker-compose logs -f tgjqr-bot
```

#### 4. 停止/重启

```bash
docker-compose down        # 停止
docker-compose restart     # 重启
```

**数据持久化**：数据库和日志通过 Docker Volume 挂载到 `./data/` 目录，重启不会丢失。

---

## 命令列表

### 群组管理命令（需管理员权限）

| 命令 | 用法 | 说明 |
|------|------|------|
| `/ban` | 回复消息 `/ban 30` | 封禁用户，可指定分钟数 |
| `/unban` | 回复被封禁用户消息 | 解封用户 |
| `/mute` | 回复消息 `/mute 60` | 禁言用户，可指定分钟数 |
| `/unmute` | 回复消息 `/unmute` | 解除禁言 |
| `/kick` | 回复消息 `/kick` | 踢出用户 |
| `/warn` | 回复消息 `/warn 原因` | 警告用户 |
| `/setwelcome` | `/setwelcome 欢迎内容` | 设置本群欢迎消息 |
| `/settings` | `/settings` | 查看当前群组设置 |

### 自动回复管理命令

| 命令 | 用法 | 说明 |
|------|------|------|
| `/addreply` | `/addreply 关键词 \| 回复内容` | 添加自动回复规则 |
| `/delreply` | `/delreply 关键词` | 删除自动回复规则 |
| `/listreply` | `/listreply` | 列出所有自动回复规则 |

### 定时消息管理命令

| 命令 | 用法 | 说明 |
|------|------|------|
| `/addschedule` | `/addschedule chat_id cron \| 消息` | 添加定时消息 |
| `/delschedule` | `/delschedule 任务ID` | 删除定时消息 |
| `/listschedule` | `/listschedule` | 列出所有定时任务 |
| `/testschedule` | `/testschedule 任务ID` | 立即测试发送 |
| `/reloadschedule` | `/reloadschedule` | 重新加载所有任务 |

### AI 客服命令

| 命令 | 用法 | 说明 |
|------|------|------|
| `/ai` | `/ai 你的问题` | 直接调用 AI 回答 |
| `/ai_status` | `/ai_status` | 查看 AI 配置状态 |

---

## Captcha 入群验证流程

1. 新用户加入群组
2. Bot 自动发送验证消息（4 个按钮，其中 1 个正确）
3. 用户需在 `ANTISPAM_SECONDS` 秒内点击 **✅ 我不是机器人**
4. 验证通过后：删除验证消息，发送欢迎消息
5. 验证失败/超时：自动踢出用户
6. 未验证用户发送的任何消息都会被自动删除

---

## Cron 表达式格式

定时消息使用标准 5 字段 cron 表达式：

```
分 时 日 月 星期
```

| 示例 | 含义 |
|------|------|
| `0 9 * * *` | 每天 9:00 |
| `0 9,18 * * 1-5` | 工作日 9:00 和 18:00 |
| `0 8 * * 1` | 每周一 8:00 |
| `*/30 * * * *` | 每 30 分钟 |

---

## 支持的 AI 服务商

由于使用 OpenAI 兼容接口，以下服务商均可接入：

| 服务商 | Base URL | 常见模型 |
|--------|----------|----------|
| OpenAI | `https://api.openai.com/v1` | gpt-4o, gpt-4o-mini |
| DeepSeek | `https://api.deepseek.com` | deepseek-chat, deepseek-reasoner |
| OpenRouter | `https://openrouter.ai/api/v1` | 支持多厂商模型 |
| OneAPI | 你的自建地址 | 聚合多平台 |
| Ollama | `http://localhost:11434/v1` | 本地开源模型 |

---

## 项目结构

```
.
├── main.py                  # 入口文件
├── config.py                # 配置管理
├── requirements.txt         # 依赖列表
├── Dockerfile               # Docker 构建文件
├── docker-compose.yml       # Docker Compose 配置
├── .env                     # 环境变量（需手动创建）
├── .env.example             # 环境变量模板
├── .gitignore               # Git 忽略规则
├── LICENSE                  # 开源许可证（MIT）
├── data/                    # 运行时数据（Docker 挂载卷）
├── models/
│   ├── database.py          # 异步数据库操作
│   └── __init__.py
├── handlers/
│   ├── group_manager.py     # 群组管理 + Captcha
│   ├── auto_reply.py        # 自动回复
│   ├── ai_chat.py           # AI 智能客服
│   ├── scheduled_posts.py   # 定时推送
│   ├── ad_detector.py       # 广告检测（QQ 群管）
│   ├── anti_flood.py        # 反刷屏检测
│   ├── captcha_solver.py    # QQ 验证码自动解决
│   ├── card_monitor.py      # QQ 群名片监控
│   ├── health_monitor.py    # Docker/NapCat 健康监控
│   ├── moderation_store.py  # 审核数据存储
│   ├── unmute_worker.py     # 自动解禁调度
│   └── __init__.py
├── napcat_ws.py             # NapCat WebSocket 客户端（OneBot11）
├── napcat_bridge.py         # NapCat HTTP API 桥接
├── web_server.py            # FastAPI Web 管理后台
├── group_member_store.py    # 群成员缓存存储
└── web/
    └── index.html           # Web 管理后台前端
```

---

## 注意事项

1. **Bot 权限**：将 Bot 拉入群组后，必须设置为**管理员**，并授予以下权限：
   - 删除消息
   - 限制用户
   - 封禁用户
   - 邀请用户

2. **获取 chat_id**：群组 ID 通常以 `-100` 开头。可以通过 `@userinfobot` 或 `@getidsbot` 获取。

3. **获取 user_id**：通过 `@userinfobot` 发送 `/start` 即可查看自己的 ID。

4. **调试**：首次运行建议先在小群测试，确认功能正常后再投入生产环境。

5. **Docker 数据备份**：定期备份 `./data/` 目录，防止数据丢失。

---

## 技术栈

- [aiogram 3.x](https://docs.aiogram.dev/) - Telegram Bot 异步框架
- [APScheduler](https://apscheduler.readthedocs.io/) - 定时任务调度
- [aiosqlite](https://github.com/omnilib/aiosqlite) - 异步 SQLite
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) - 配置管理
- [OpenAI Python SDK](https://github.com/openai/openai-python) - AI API 调用
