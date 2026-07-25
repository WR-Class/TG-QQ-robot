"""
自动回复模块
功能：关键词匹配自动回复、客服 FAQ、动态添加/删除规则
"""

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

from config import settings, get_auto_reply_rules
from models.database import db
from handlers.group_manager import is_admin, is_super_admin

router = Router()


# --- 关键词自动回复 ---

@router.message(
    F.chat.type.in_({"group", "supergroup", "private"}),
    ~F.text.startswith("/")
)
async def keyword_auto_reply(message: Message):
    """监听所有消息，匹配关键词自动回复"""
    if not message.text:
        return

    text = message.text.lower()
    chat_id = message.chat.id

    # 合并环境变量配置和数据库配置
    rules_env = get_auto_reply_rules()
    rules_db = await db.get_auto_replies(chat_id=chat_id)

    # 优先匹配数据库中的规则（更具体）
    for rule in rules_db:
        keyword = rule["keyword"].lower()
        if keyword in text:
            try:
                await message.reply(rule["response"])
            except TelegramBadRequest:
                # 消息已删除无法回复，忽略
                pass
            return

    # 再匹配环境变量中的全局规则
    for keyword, response in rules_env.items():
        if keyword.lower() in text:
            try:
                await message.reply(response)
            except TelegramBadRequest:
                pass
            return


# --- 管理命令：动态管理自动回复 ---

@router.message(Command("addreply"))
async def cmd_add_reply(message: Message, bot: Bot):
    """添加自动回复规则 /addreply 关键词 | 回复内容"""
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("❌ 你没有权限执行此操作。")

    # 超级管理员在私聊中也可以管理全局规则
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("用法: /addreply 关键词 | 回复内容")

    parts = text[1].split("|", 1)
    if len(parts) != 2:
        return await message.reply("格式错误。请使用: /addreply 关键词 | 回复内容")

    keyword = parts[0].strip()
    response = parts[1].strip()
    chat_id = 0 if message.chat.type == "private" else message.chat.id
    is_global = message.chat.type == "private"

    await db.add_auto_reply(keyword, response, chat_id=chat_id, is_global=is_global)
    await message.reply(f"✅ 已添加自动回复: '{keyword}' -> '{response[:50]}...'")


@router.message(Command("delreply"))
async def cmd_del_reply(message: Message, bot: Bot):
    """删除自动回复规则 /delreply 关键词"""
    if message.chat.type in ("group", "supergroup"):
        if not await is_admin(bot, message.chat.id, message.from_user.id):
            return await message.reply("❌ 你没有权限执行此操作。")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("用法: /delreply 关键词")

    keyword = text[1].strip()
    chat_id = message.chat.id if message.chat.type in ("group", "supergroup") else None
    await db.delete_auto_reply(keyword, chat_id=chat_id)
    await message.reply(f"✅ 已删除关键词 '{keyword}' 的自动回复。")


@router.message(Command("listreply"))
async def cmd_list_reply(message: Message):
    """列出当前自动回复规则 /listreply"""
    chat_id = message.chat.id if message.chat.type in ("group", "supergroup") else None
    rules = await db.get_auto_replies(chat_id=chat_id)

    if not rules:
        return await message.reply("暂无自动回复规则。")

    lines = ["📋 自动回复规则列表:"]
    for i, rule in enumerate(rules[:20], 1):
        scope = "全局" if rule["is_global"] else f"群组{rule['chat_id']}"
        lines.append(f"{i}. [{scope}] {rule['keyword']} -> {rule['response'][:30]}...")

    if len(rules) > 20:
        lines.append(f"... 还有 {len(rules) - 20} 条规则")

    await message.reply("\n".join(lines))


# --- 私聊客服支持（转发给管理员） ---

@router.message(F.chat.type == "private", ~F.text.startswith("/"))
async def private_support(message: Message, bot: Bot):
    """私聊消息：如果没有被关键词匹配，可转发给管理员"""
    # 如果已经被上面的 handler 回复了，这里不会触发
    # 此 handler 作为兜底，提示用户已收到消息
    if not message.text:
        return

    # 简单的已收到确认
    # 如果需要人工客服，可以在这里将消息转发给 ADMIN_IDS
    # 目前只做关键词回复，没有匹配到时保持静默
    pass
