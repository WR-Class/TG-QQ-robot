"""
按钮菜单模块
功能：ReplyKeyboardMarkup 持久菜单，绑定机器人实际功能
"""

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest

from config import settings
from models.database import db
from handlers.ai_chat import chat_with_ai

router = Router()


# --- 构建菜单键盘 ---

def get_main_menu_keyboard():
    """获取主菜单键盘（两列布局）"""
    builder = ReplyKeyboardBuilder()

    builder.button(text="🤖 AI对话")
    builder.button(text="📋 回复规则")

    builder.button(text="🤖 AI状态")
    builder.button(text="⚙️ 群组设置")

    builder.button(text="🆘 帮助")
    builder.button(text="⚙️ 管理面板")
    builder.adjust(2, 2, 2)

    return builder.as_markup(resize_keyboard=True, persistent=True)


# --- 菜单按钮响应 ---

@router.message(F.text == "🤖 AI对话")
async def on_ai_chat(message: Message):
    """AI对话"""
    if not settings.AI_ENABLED:
        return await message.reply(
            "❌ AI 客服功能未启用。\n"
            "请在 .env 中配置 AI_ENABLED=true、AI_API_KEY 等参数。",
            reply_markup=get_main_menu_keyboard()
        )
    text = (
        "🤖 <b>AI 对话</b>\n\n"
        "你可以直接发送问题给我，我会调用 AI 回答。\n\n"
        "或者使用命令：\n"
        "  /ai 你的问题\n\n"
        "💡 在群组中 @我 即可触发 AI 对话。"
    )
    await message.reply(text, reply_markup=get_main_menu_keyboard())


@router.message(F.text == "📋 回复规则")
async def on_reply_rules(message: Message):
    """查看自动回复规则"""
    chat_id = message.chat.id if message.chat.type in ("group", "supergroup") else None
    rules = await db.get_auto_replies(chat_id=chat_id)

    if not rules:
        text = (
            "📋 <b>自动回复规则</b>\n\n"
            "当前暂无规则。\n\n"
            "添加规则：\n"
            "  /addreply 关键词 | 回复内容\n\n"
            "删除规则：\n"
            "  /delreply 关键词\n\n"
            "查看所有规则：\n"
            "  /listreply"
        )
    else:
        lines = ["📋 <b>自动回复规则</b>\n"]
        for i, rule in enumerate(rules[:10], 1):
            lines.append(f"{i}. <b>{rule['keyword']}</b> → {rule['response'][:40]}")
        if len(rules) > 10:
            lines.append(f"\n...还有 {len(rules) - 10} 条规则（/listreply 查看全部）")
        text = "\n".join(lines)

    await message.reply(text, reply_markup=get_main_menu_keyboard())


@router.message(F.text == "🤖 AI状态")
async def on_ai_status(message: Message):
    """查看AI状态"""
    status = "✅ 已启用" if settings.AI_ENABLED else "❌ 已禁用"
    model = settings.AI_MODEL
    base_url = settings.AI_BASE_URL or "默认 (OpenAI 官方)"
    trigger = settings.AI_GROUP_TRIGGER

    text = (
        f"🤖 <b>AI 客服状态</b>\n\n"
        f"状态: {status}\n"
        f"模型: {model}\n"
        f"API 地址: {base_url}\n"
        f"群组触发: {trigger}\n"
        f"私聊自动: {'开' if settings.AI_PRIVATE_AUTO else '关'}\n\n"
        f"📌 如需修改配置，请编辑 .env 文件后重启容器。"
    )
    await message.reply(text, reply_markup=get_main_menu_keyboard())


@router.message(F.text == "🆘 帮助")
async def on_help(message: Message):
    """帮助信息"""
    text = (
        "🆘 <b>帮助信息</b>\n\n"
        "私聊命令：\n"
        "  /start — 显示主菜单\n"
        "  /help — 显示此帮助\n"
        "  /menu — 显示按钮菜单\n"
        "  /manage — 打开管理面板\n"
        "  /ai 问题 — AI 对话\n"
        "  /ai_status — AI 状态\n\n"
        "群组命令（需管理员权限）：\n"
        "  /ban @用户 — 封禁\n"
        "  /unban @用户 — 解封\n"
        "  /mute @用户 — 禁言\n"
        "  /unmute — 解除禁言\n"
        "  /kick — 踢出\n"
        "  /warn — 警告\n"
        "  /setwelcome 内容 — 设置欢迎\n"
        "  /settings — 查看群组设置\n\n"
        "💡 把我拉入群组并设为管理员，即可使用群管功能。"
    )
    await message.reply(text, reply_markup=get_main_menu_keyboard())


@router.message(F.text == "⚙️ 群组设置")
async def on_group_settings(message: Message, bot: Bot):
    """群组设置：私聊中选择群组，群组中直接进入"""
    from handlers.visual_mgmt import select_group_list
    if message.chat.type == "private":
        await select_group_list(message, bot)
    else:
        # 群组中直接显示设置
        from handlers.visual_mgmt import show_group_settings
        from handlers.group_manager import is_admin
        if await is_admin(bot, message.chat.id, message.from_user.id):
            await show_group_settings(message, bot, message.chat.id)
        else:
            await message.reply("❌ 你没有权限执行此操作。")


@router.message(F.text == "⚙️ 管理面板")
async def on_manage_panel(message: Message):
    """打开管理面板"""
    from handlers.visual_mgmt import open_management_panel
    await open_management_panel(message)


# --- 快捷命令 ---

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    """显示菜单 /menu"""
    text = "👋 请从下方菜单选择功能："
    await message.reply(text, reply_markup=get_main_menu_keyboard())