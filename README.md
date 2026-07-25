# TG-QQ-Robot

> Telegram 群组管理机器人 + QQ 群管桥接，一套配置双端覆盖

基于 **aiogram 3.x** + **NapCat (OneBot11)** 的全功能群组管理解决方案。集群组管理、AI 智能客服、自动回复、定时消息推送、入群 Captcha 验证于一体，支持通过 NapCat 桥接实现 QQ 群管功能。全 Docker 部署，开箱即用。

---

## 功能特性

### 🤖 Telegram 群组管理
- **欢迎新成员**：自动发送欢迎消息（支持 `{first_name}`、`{username}`、`{chat_title}` 占位符）
- **管理命令**：禁言/解禁、踢人、封禁/解封、警告，全部支持限时操作
- **Captcha 验证**：新用户入群按钮验证，超时自动踢出
- **敏感词过滤**：自动删除违禁消息
- **禁止转发**：可选禁止转发外部频道消息
- **自动回复**：关键词匹配 FAQ，支持分群配置 + 全局配置
- **定时消息**：Cron 表达式驱动的定时推送，重启后自动恢复
- **群设置管理**：通过命令实时查看和修改群配置

### 🧠 AI 智能客服
- **OpenAI 兼容 API**：支持 OpenAI、DeepSeek、OpenRouter、OneAPI、Ollama 等
- **联网搜索**：集成 SearXNG 元搜索引擎，AI 可实时检索互联网信息
- **私聊自动回复**：关键词未匹配时自动调用 AI
- **群组触发方式**：`@机器人` / `回复机器人` / `所有消息` 三种模式
- **自定义提示词**：可定制 AI 角色和行为
- **上下文记忆**：支持多轮对话上下文

### 📱 QQ 群管（NapCat 桥接）
通过 **NapCat (OneBot11 协议)** 桥接实现 QQ 群管理：
- **广告检测**：AI 智能识别广告/垃圾消息，自动禁言/封禁
- **名片监控**：检测违规群名片，自动还原
- **OCR 审核**：图片文字内容审核
- **入群审核**：新成员入群申请审核
- **反刷屏**：重复消息/频率检测
- **验证码自动解决**：集成 YesCaptcha，自动处理 QQ 登录验证码
- **自动续命**：健康监控自动检测 NapCat 异常并重启恢复
- **Web 管理后台**：FastAPI 提供配置管理、状态监控、日志查看

### 🌐 Web 管理后台
- 配置管理（Telegram / QQ / AI / NapCat）
- 服务状态实时监控（NapCat、SearXNG、AI）
- 违规记录查看与申诉处理
- 操作日志查询
- 会话备份与恢复
- 系统资源监控（CPU / 内存 / 磁盘）

---

## 快速开始

### 前置条件
- Python 3.10+（本地开发）或 Docker & Docker Compose（生产部署）
- Telegram Bot Token（从 [@BotFather](https://t.me/BotFather) 获取）
- （可选）NapCat QQ 管理号

### 方式一：Docker 部署（推荐）

#### 1. 克隆并配置

```bash
git clone https://github.com/your-username/TG-QQ-robot.git
cd TG-QQ-robot
cp .env.example .env
```

编辑 `.env`，填写必填项：

```env
# Telegram Bot Token（必须）
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz

# 管理员 ID，多个用逗号分隔（必须）
ADMIN_IDS=123456789

# 代理（中国大陆必需）
PROXY_URL=http://127.0.0.1:7890
```

#### 2. 启动

```bash
docker-compose up -d --build
```

#### 3. 查看日志

```bash
docker-compose logs -f tgjqr-bot
```

### 方式二：本地开发

#### 1. 安装依赖

```bash
pip install -r requirements.txt
```

#### 2. 配置并运行

```bash
cp .env.example .env
# 编辑 .env 填写配置
python main.py
```

---

## 命令列表

### 🤖 Telegram 群组管理（需管理员权限）

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

### 🤖 Telegram 自动回复管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/addreply` | `/addreply 关键词 \| 回复内容` | 添加自动回复规则 |
| `/delreply` | `/delreply 关键词` | 删除自动回复规则 |
| `/listreply` | `/listreply` | 列出所有自动回复规则 |

### 🤖 Telegram 定时消息管理

| 命令 | 用法 | 说明 |
|------|------|------|
| `/addschedule` | `/addschedule chat_id cron \| 消息` | 添加定时消息 |
| `/delschedule` | `/delschedule 任务ID` | 删除定时消息 |
| `/listschedule` | `/listschedule` | 列出所有定时任务 |
| `/testschedule` | `/testschedule 任务ID` | 立即测试发送 |
| `/reloadschedule` | `/reloadschedule` | 重新加载所有任务 |

### 🤖 Telegram AI 客服

| 命令 | 用法 | 说明 |
|------|------|------|
| `/ai` | `/ai 你的问题` | 直接调用 AI 回答 |
| `/ai_status` | `/ai_status` | 查看 AI 配置状态 |

---

### 📱 QQ 群管指令（群内发送，需管理员权限）

在 QQ 群内发送以 `/` 开头的指令即可触发：

| 指令 | 用法 | 说明 |
|------|------|------|
| `/禁言` | `/禁言 <QQ> [分钟]` | 禁言成员（默认30分钟） |
| `/解禁` | `/解禁 <QQ>` | 解除禁言 |
| `/踢人` | `/踢人 <QQ>` | 踢出成员 |
| `/踢并拉黑` | `/踢并拉黑 <QQ>` | 踢出且拒绝再次加群 |
| `/全体禁言` | `/全体禁言 开启/关闭` | 全体禁言切换 |
| `/设置名片` | `/设置名片 <QQ> <名称>` | 设置群名片 |
| `/改群名` | `/改群名 <名称>` | 修改群名称 |
| `/头衔` | `/头衔 <QQ> <内容>` | 设置成员群头衔 |
| `/设管理` | `/设管理 <QQ>` | 设置管理员 |
| `/取消管理` | `/取消管理 <QQ>` | 取消管理员 |
| `/发公告` | `/发公告 <内容>` | 发送群公告 |
| `/撤回` | `/撤回 [数量]` | 撤回最近消息（默认1条，最多20） |
| `/设精华` | `/设精华 <消息ID>` | 设置精华消息 |
| `/取消精华` | `/取消精华 <消息ID>` | 取消精华消息 |
| `/删公告` | `/删公告 <公告ID>` | 删除指定公告 |
| `/删文件` | `/删文件 <文件ID> [busid]` | 删除指定群文件 |

### 📱 QQ 查询指令（所有人可用）

| 指令 | 用法 | 说明 |
|------|------|------|
| `/成员列表` | `/成员列表` | 查看群成员（前20人） |
| `/禁言列表` | `/禁言列表` | 查看被禁言成员 |
| `/群信息` | `/群信息` | 查看群基本信息 |
| `/搜索` | `/搜索 <关键词>` | 搜索群成员 |
| `/统计` | `/统计` | 成员数量统计 |
| `/查成员` | `/查成员 <QQ>` | 查询成员详细信息 |
| `/精华列表` | `/精华列表` | 查看精华消息列表 |
| `/公告列表` | `/公告列表` | 查看群公告列表 |
| `/文件列表` | `/文件列表` | 查看群文件列表 |
| `/荣誉列表` | `/荣誉列表 [类型]` | 查看群荣誉（龙王/活跃/传奇等） |
| `/帮助` | `/帮助` | 显示所有可用指令 |

也支持自然语言触发：@机器人发送"统计"、"人数"、"有多少人"等可自动统计群成员。

### 📱 QQ 群主私聊指令

群主向机器人发送以下关键词（私聊）可快速处理：

| 指令 | 说明 |
|------|------|
| `误判` | 将最近一条违规内容加入全局白名单 |
| `放行 关键词` | 手动将特定关键词加入全局白名单 |
| `拉黑词 关键词` | 手动将特定关键词加入全局黑名单 |
| `禁言QQ 分钟` | 快速禁言指定 QQ（如 `禁言123456 10`） |
| `踢QQ` | 快速踢出指定 QQ（如 `踢123456`） |
| `统计` | 查看近24小时违规统计摘要 |
| `帮助` | 显示群主指令帮助 |

---

## Captcha 入群验证流程

1. 新用户加入群组
2. Bot 自动发送验证消息（4 个按钮，其中 1 个正确）
3. 用户需在 `ANTISPAM_SECONDS` 秒内点击 **✅ 我不是机器人**
4. 验证通过 → 删除验证消息，发送欢迎消息
5. 验证失败/超时 → 自动踢出用户
6. 未验证用户发送的任何消息都会被自动删除

---

## 环境变量说明

完整配置项见 `.env.example`，核心配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `BOT_TOKEN` | Telegram Bot Token（必须） | - |
| `ADMIN_IDS` | 管理员 ID 列表（必须） | - |
| `PROXY_URL` | HTTP/SOCKS5 代理 | - |
| `NAPCAT_ENABLED` | 启用 QQ 群管桥接 | `false` |
| `AI_ENABLED` | 启用 AI 客服 | `false` |
| `AI_API_KEY` | OpenAI 兼容 API Key | - |
| `AI_MODEL` | AI 模型名称 | `gpt-4o-mini` |
| `WEB_PASSWORD` | 管理后台密码 | -（不设则无需认证） |

更多 QQ 群管、OCR、名片监控、入群审核等配置见 `.env.example`。

---

## 项目结构

```
.
├── main.py                  # 入口文件
├── config.py                # 配置管理（pydantic-settings）
├── requirements.txt         # 依赖列表
├── Dockerfile               # Docker 构建（多阶段，非 root 运行）
├── docker-compose.yml       # Docker Compose 编排
├── .env.example             # 环境变量模板
├── .gitignore               # Git 忽略规则
├── LICENSE                  # MIT 开源许可证
├── models/
│   └── database.py          # 异步 SQLite 数据库操作
├── handlers/
│   ├── group_manager.py     # 群组管理 + Captcha 验证
│   ├── auto_reply.py        # 自动回复 FAQ
│   ├── ai_chat.py           # AI 智能客服 + 联网搜索
│   ├── ai_tools.py          # AI 工具函数（天气查询等）
│   ├── scheduled_posts.py   # 定时消息推送
│   ├── ad_detector.py       # QQ 广告检测
│   ├── anti_flood.py        # QQ 反刷屏
│   ├── captcha_solver.py    # QQ 验证码自动解决
│   ├── card_monitor.py      # QQ 名片监控
│   ├── health_monitor.py    # Docker/NapCat 健康监控
│   ├── join_audit.py        # QQ 入群审核
│   ├── lexicon_engine.py    # 词库引擎
│   ├── menu.py              # 菜单命令
│   ├── moderation_store.py  # 审核数据持久化
│   ├── ocr_audit.py         # OCR 图片审核
│   ├── owner_commands.py    # 群主专属命令
│   ├── penalty_engine.py    # 处罚引擎
│   ├── qq_group_manager.py  # QQ 群管理基础操作
│   ├── semantic_faq.py      # 语义 FAQ 匹配
│   ├── unmute_worker.py     # 自动解禁调度
│   └── visual_mgmt.py       # 可视化群管理
├── napcat_ws.py             # NapCat WebSocket 客户端
├── napcat_bridge.py         # NapCat HTTP API 桥接
├── web_server.py            # FastAPI Web 管理后台
├── group_member_store.py    # 群成员缓存
├── searxng_config/          # SearXNG 搜索引擎配置
└── web/
    ├── index.html           # 管理后台前端
    └── static/              # 静态资源
```

---

## 技术栈

本项目构建在以下优秀的开源项目和服务的基石之上：

| 项目 | 用途 | 许可 |
|------|------|------|
| [aiogram](https://github.com/aiogram/aiogram) | Telegram Bot 异步框架 | MIT |
| [NapCat](https://github.com/NapNeko/NapCat) | QQ NTQQ OneBot11 协议实现 | MIT |
| [SearXNG](https://github.com/searxng/searxng) | 开源元搜索引擎 | AGPL-3.0 |
| [FastAPI](https://github.com/fastapi/fastapi) | Web 管理后台框架 | MIT |
| [APScheduler](https://github.com/agronholm/apscheduler) | 定时任务调度 | MIT |
| [OpenAI Python SDK](https://github.com/openai/openai-python) | AI API 调用 | MIT |
| [pydantic](https://github.com/pydantic/pydantic) | 配置管理 | MIT |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | 异步 SQLite | MIT |
| [wttr.in](https://github.com/chubin/wttr.in) | 天气查询 API | Apache-2.0 |
| [YesCaptcha](https://yescaptcha.com/) | 验证码自动解决 | 商业服务 |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 异步 HTTP 客户端 | Apache-2.0 |
| [httpx](https://github.com/encode/httpx) | HTTP 客户端 | BSD-3-Clause |
| [pyahocorasick](https://github.com/WojciechMula/pyahocorasick) | Aho-Corasick 自动机 | BSD-3-Clause |
| [psutil](https://github.com/giampaolo/psutil) | 系统资源监控 | BSD-3-Clause |
| [uvicorn](https://github.com/encode/uvicorn) | ASGI 服务器 | BSD-3-Clause |

### 特别鸣谢

- **[NapCat](https://github.com/NapNeko/NapCat)** — 让本项目能够实现 QQ 群管功能的核心桥接引擎，感谢 NapNeko 团队的开源贡献
- **[SearXNG](https://github.com/searxng/searxng)** — 提供去中心化的元搜索引擎能力，无需任何第三方 API Key
- **[wttr.in](https://github.com/chubin/wttr.in)** — 提供简洁优雅的命令行天气查询服务
- **所有 AI 服务商**（OpenAI、DeepSeek、OpenRouter 等）— 提供优秀的 AI 推理能力

---

## 注意事项

1. **Bot 权限**：将 Telegram Bot 拉入群组后，必须设置为**管理员**，并授予删除消息、限制用户、封禁用户、邀请用户权限
2. **获取 chat_id**：群组 ID 通常以 `-100` 开头，可通过 `@userinfobot` 或 `@getidsbot` 获取
3. **首次使用**：建议先在小群测试，确认功能正常后再投入生产环境
4. **Docker 数据备份**：定期备份 `./data/` 目录，防止数据丢失
5. **QQ 群管**：NapCat 容器首次启动需扫码登录 QQ，登录后会自动保存会话

---

## 开源许可

本项目基于 [MIT License](LICENSE) 开源。
