"""
机器人配置文件
支持从环境变量或 .env 文件读取配置
"""

import os
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置类"""

    # Bot Token（从 @BotFather 获取）
    BOT_TOKEN: str = ""

    # 管理员用户 ID 列表（超级管理员，拥有所有权限）
    # 支持逗号分隔的数字字符串，如 "123,456" 或 "[123,456]"
    ADMIN_IDS: List[int] = []

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        """将逗号分隔的字符串或单个数字解析为 int 列表"""
        if isinstance(v, list):
            return [int(i) for i in v]
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            v = v.strip().strip("[]")
            if not v:
                return []
            return [int(i.strip()) for i in v.split(",") if i.strip()]
        return []

    # 代理配置（如需翻墙，请填写）
    # 格式: http://127.0.0.1:7890 或 socks5://127.0.0.1:10808
    PROXY_URL: str = ""

    # 广告检测专用 AI KEY（独立通道）
    AD_AI_API_KEY: str = ""

    # 数据库文件路径
    DATABASE_PATH: str = "./bot_data.db"

    # 欢迎消息（支持 {first_name} {username} {chat_title} 占位符）
    WELCOME_MESSAGE: str = "👋 欢迎 {first_name} 加入 {chat_title}！请遵守群规，文明交流。"

    # 反垃圾：新用户入群后 N 秒内发消息需要验证（0 表示关闭）
    ANTISPAM_SECONDS: int = 0

    # 反垃圾：禁止转发外部频道消息（True/False）
    BLOCK_FORWARD: bool = False

    # 反垃圾：敏感词列表（逗号分隔）
    BANNED_WORDS: str = ""

    # 入群验证：要求关注指定频道（频道 ID，如 -1001234567890）
    VERIFY_CHANNEL_ID: int = 0
    VERIFY_CHANNEL_TITLE: str = ""

    # 自动回复关键词配置（JSON 格式字符串）
    # 示例: '{"价格":"我们的产品价格是...","联系方式":"邮箱: support@xxx.com"}'
    AUTO_REPLY_RULES: str = "{}"

    # 定时消息配置（JSON 格式字符串）
    # 示例: '[{"chat_id":-1001234567890,"text":"早安！","cron":"0 9 * * *"}]'
    SCHEDULED_MESSAGES: str = "[]"

    # ===== AI 客服配置 =====
    # 是否启用 AI 客服
    AI_ENABLED: bool = False
    # OpenAI 兼容 API 的 Base URL（留空使用默认 OpenAI 官方地址）
    AI_BASE_URL: str = ""
    # API Key
    AI_API_KEY: str = ""
    # 模型名称
    AI_MODEL: str = "gpt-4o-mini"
    # 系统提示词（定义 AI 的角色和行为）
    AI_SYSTEM_PROMPT: str = "你是一个专业的客服助手，用中文回答用户问题，回答简洁友好。"
    # 群组中触发 AI 的方式: "mention"(仅@机器人), "reply"(回复机器人), "all"(所有消息)
    AI_GROUP_TRIGGER: str = "mention"
    # 私聊中是否自动启用 AI（如果关键词未匹配）
    AI_PRIVATE_AUTO: bool = True
    # 最大上下文消息数（保持对话连续性）
    AI_MAX_CONTEXT: int = 5
    # Exa 搜索引擎 API Key（https://dashboard.exa.ai/api-keys）
    EXA_API_KEY: str = ""
    # SearXNG 自建搜索引擎地址（Docker Compose 内网地址）
    SEARXNG_BASE_URL: str = "http://searxng:8080"

    # ===== NapCat 配置（OneBot11 协议，用于撤回/禁言） =====
    NAPCAT_API_URL: str = ""  # NapCat OneBot11 HTTP API 地址
    NAPCAT_WS_URL: str = ""  # 为空时自动由 NAPCAT_API_URL 推导为 ws://...
    NAPCAT_ACCESS_TOKEN: str = ""  # NapCat HTTP API token
    NAPCAT_WEBUI_TOKEN: str = ""  # NapCat WebUI 管理后台登录密码（用于自动恢复）
    NAPCAT_ENABLED: bool = False  # 是否启用 NapCat 桥接
    NAP_CAT_QQ: str = ""  # NapCat 快速登录 QQ 号（透传到 NapCat 容器）
    NAPCAT_QUICK_PASSWORD_MD5: str = ""  # NapCat 快速登录密码 MD5（透传）
    QQ_PASSWORD_MD5: str = ""  # QQ 密码登录自动恢复（密码的 MD5 值，用于 NapCat WebUI API 密码登录）
    # 群标识映射：旧版 QQ Bot 的 group_openid → NapCat 数字群号（兼容配置）
    QQ_GROUP_ID_MAP: str = ""
    # 广告检测后私聊通知的 QQ 号（群主）
    QQ_AD_NOTIFY_QQ: str = ""
    # 群主 QQ 号（群主发的广告不检测）
    QQ_GROUP_OWNER: str = ""

    # ===== 入群审核 =====
    JOIN_AUDIT_ENABLED: bool = True
    JOIN_AUDIT_DEFAULT: str = "approve"  # approve / reject / manual
    JOIN_AUDIT_APPROVE_WORDS: str = ""
    JOIN_AUDIT_REJECT_WORDS: str = "广告,兼职,日结,刷单,代刷,跑分,私聊,加微信,加v,引流"
    JOIN_AUDIT_USE_LEXICON: bool = True
    JOIN_AUDIT_NOTIFY_GROUP: bool = True
    JOIN_AUDIT_REJECT_REASON: str = "申请信息未通过审核"

    # ===== OCR 图片审核 =====
    OCR_ENABLED: bool = True
    OCR_MIN_TEXT_LEN: int = 4

    # ===== 名片监控 =====
    CARD_MONITOR_ENABLED: bool = True
    CARD_AUDIT_ENABLED: bool = True          # 全量审核（词库+引流）
    CARD_AUDIT_LINK_ONLY: bool = False       # 仅拦链接/店铺（宽松）
    CARD_PROTECT_ENABLED: bool = False       # 保护名单开关
    CARD_PROTECT_LIST: str = ""              # 格式: qq:预设名片,qq2:预设
    CARD_MONITOR_NOTIFY: bool = True         # 还原时群内通知
    ADMIN_CHANGE_NOTIFY: bool = True         # 管理员任免群内通知

    # ===== YesCaptcha 验证码自动解决 =====
    YESCAPTCHA_KEY: str = ""  # YesCaptcha 的 clientKey，留空不启用

    # ===== Web 管理后台 =====
    WEB_PASSWORD: str = ""  # 空=不启用密码；设置后前端需登录

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# 全局配置实例
settings = Settings()


def get_banned_words() -> List[str]:
    """获取敏感词列表"""
    if not settings.BANNED_WORDS:
        return []
    return [w.strip().lower() for w in settings.BANNED_WORDS.split(",") if w.strip()]


def get_auto_reply_rules() -> dict:
    """获取自动回复规则"""
    import json
    try:
        return json.loads(settings.AUTO_REPLY_RULES)
    except Exception:
        return {}


def get_scheduled_messages() -> list:
    """获取定时消息配置"""
    import json
    try:
        return json.loads(settings.SCHEDULED_MESSAGES)
    except Exception:
        return []
