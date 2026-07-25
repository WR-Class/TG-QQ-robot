"""
群组管理模块
功能：欢迎消息、禁言/踢人/封禁、反垃圾、敏感词过滤、管理员权限检查
"""

import asyncio
from datetime import datetime, timedelta

import random
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, ChatMemberUpdated, CallbackQuery
from aiogram.filters import Command, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings, get_banned_words
from models.database import db

logger = logging.getLogger(__name__)

router = Router()


# --- 权限检查工具 ---

def is_super_admin(user_id: int) -> bool:
    """检查是否为超级管理员"""
    return user_id in settings.ADMIN_IDS


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """检查用户在群组中是否为管理员（含群主）"""
    if is_super_admin(user_id):
        return True
    # 检查数据库中记录的群管
    if await db.is_group_admin(chat_id, user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except TelegramBadRequest:
        return False


# --- 欢迎新成员 ---

@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join(event: ChatMemberUpdated, bot: Bot):
    """新成员入群事件：发送频道关注验证"""
    chat = event.chat
    # 记录群组
    try:
        await db.add_bot_group(chat.id, chat.title or f"群组{chat.id}")
    except Exception as e:
        logger.warning(f"记录群组失败: {e}")

    new_member = event.new_chat_member.user

    # 如果配置了频道验证
    if settings.VERIFY_CHANNEL_ID:
        channel_title = settings.VERIFY_CHANNEL_TITLE or "指定频道"
        verify_text = (
            f"👋 欢迎 {new_member.first_name or ''} 加入 <b>{chat.title or '本群'}</b>！\n\n"
            f"🛡️ 为了群组安全，请先关注频道 <b>{channel_title}</b>，然后点击下方按钮完成验证。\n"
            f"⏰ 请在 <b>{settings.ANTISPAM_SECONDS} 秒</b> 内完成，超时将被移出群组。"
        )

        builder = InlineKeyboardBuilder()
        builder.button(
            text="✅ 我已关注频道",
            callback_data=f"verifych:{new_member.id}:{chat.id}"
        )

        try:
            msg = await bot.send_message(
                chat.id,
                verify_text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            # 记录验证挑战（复用 captcha 表）
            await db.add_captcha_challenge(new_member.id, chat.id, msg.message_id)
            # 启动超时检查
            asyncio.create_task(_verify_timeout_kick(bot, chat.id, new_member.id, msg.message_id))
        except TelegramBadRequest as e:
            logger.error(f"发送验证消息失败: {e}")
        return

    # 没有频道验证配置：直接发送欢迎消息
    try:
        welcome_text = settings.WELCOME_MESSAGE.format(
            first_name=new_member.first_name or "",
            username=f"@{new_member.username}" if new_member.username else new_member.first_name or "",
            chat_title=chat.title or "本群"
        )
    except KeyError:
        welcome_text = f"欢迎 {new_member.first_name or ''} 加入 {chat.title or '本群'}！"
    try:
        await bot.send_message(chat.id, welcome_text)
    except TelegramBadRequest:
        pass


async def _verify_timeout_kick(bot: Bot, chat_id: int, user_id: int, message_id: int):
    """频道验证超时检查：超时未验证则踢出用户"""
    await asyncio.sleep(settings.ANTISPAM_SECONDS)
    verified = await db.is_captcha_verified(user_id, chat_id)
    if not verified:
        # 删除验证消息
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass
        # 踢出用户
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await asyncio.sleep(1)
            await bot.unban_chat_member(chat_id, user_id)
        except TelegramBadRequest:
            pass
        # 清理记录
        await db.delete_captcha_challenge(user_id, chat_id)
        try:
            await bot.send_message(chat_id, f"🚫 用户验证超时，已被移出群组。")
        except TelegramBadRequest:
            pass


# --- 反垃圾：消息过滤 ---

@router.message(F.chat.type.in_({"group", "supergroup"}))
async def anti_spam_filter(message: Message, bot: Bot):
    """消息反垃圾过滤：检查 Captcha 验证状态 + 自动记录群组"""
    # 自动记录机器人所在的群组
    try:
        await db.add_bot_group(message.chat.id, message.chat.title or f"群组{message.chat.id}")
    except Exception as e:
        logger.warning(f"记录群组失败: {e}")

    if not message.text and not message.caption:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # 1. Captcha 验证期检查：未验证用户的发言将被删除
    if settings.ANTISPAM_SECONDS > 0:
        # 检查是否已通过验证
        verified = await db.is_captcha_verified(user_id, chat_id)
        # 如果数据库中没有该用户的验证记录（老用户或验证已清理），视为已通过
        has_challenge = await db.get_captcha_message_id(user_id, chat_id) is not None
        if has_challenge and not verified:
            # 删除用户未验证时发送的消息
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            return

    # 2. 禁止转发外部频道消息
    if settings.BLOCK_FORWARD and message.forward_from_chat:
        try:
            await message.delete()
            await message.answer("❌ 禁止转发外部频道消息。")
        except TelegramBadRequest:
            pass
        return

    # 3. 敏感词过滤
    banned_words = get_banned_words()
    text = (message.text or message.caption or "").lower()
    for word in banned_words:
        if word in text:
            try:
                await message.delete()
                warn_msg = await message.answer(
                    f"⚠️ {message.from_user.first_name} 的消息包含违禁词，已被删除。"
                )
                await asyncio.sleep(10)
                await warn_msg.delete()
            except TelegramBadRequest:
                pass
            return

    # 4. AI 广告检测（异步，不阻塞其他消息）
    if settings.AI_ENABLED and text and len(text) >= 5:
        try:
            from handlers.ad_detector import check_and_handle_ad
            handled = await check_and_handle_ad(message, bot)
            if handled:
                return
        except Exception as e:
            logger.error(f"[广告检测] 调用异常: {e}")


# --- 管理命令 ---

@router.message(Command("ban"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_ban(message: Message, bot: Bot):
    """封禁用户 /ban @用户名 [时长(分钟)]"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    if not message.reply_to_message and len(message.text.split()) < 2:
        return await message.reply("用法: /ban @用户名 [时长(分钟)] 或 回复某条消息")

    target_user = None
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
    else:
        # 从命令参数解析用户名
        args = message.text.split()
        username = args[1].lstrip("@")
        try:
            # 尝试从群成员中查找
            members = await bot.get_chat_administrators(message.chat.id)
            for m in members:
                if m.user.username == username:
                    target_user = m.user
                    break
        except Exception:
            pass

    if not target_user:
        return await message.reply("❌ 找不到该用户。")

    if await is_admin(bot, message.chat.id, target_user.id):
        return await message.reply("❌ 不能封禁管理员。")

    # 解析时长
    duration = None
    args = message.text.split()
    if len(args) >= 3:
        try:
            minutes = int(args[2])
            duration = datetime.now() + timedelta(minutes=minutes)
        except ValueError:
            return await message.reply("❌ 封禁时长格式错误，请输入数字（分钟）。例如: /ban @user 60")

    try:
        await bot.ban_chat_member(
            message.chat.id, target_user.id,
            until_date=duration if duration else None
        )
        dur_text = f" {args[2]} 分钟" if duration else ""
        await message.reply(f"✅ 已封禁 {target_user.first_name}{dur_text}。")
    except TelegramBadRequest as e:
        await message.reply(f"❌ 封禁失败: {e.message}")


@router.message(Command("unban"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unban(message: Message, bot: Bot):
    """解封用户 /unban @用户名"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    if not message.reply_to_message and len(message.text.split()) < 2:
        return await message.reply("用法: /unban @用户名 或 回复某条消息")

    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
        name = message.reply_to_message.from_user.first_name
    else:
        return await message.reply("请回复被封禁用户的消息，或提供用户 ID。")

    try:
        await bot.unban_chat_member(message.chat.id, user_id)
        await message.reply(f"✅ 已解封 {name}。")
    except TelegramBadRequest as e:
        await message.reply(f"❌ 解封失败: {e.message}")


@router.message(Command("mute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_mute(message: Message, bot: Bot):
    """禁言用户 /mute @用户名 [时长(分钟)]"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    if not message.reply_to_message:
        return await message.reply("用法: 回复某条消息 /mute [时长(分钟)]")

    target_user = message.reply_to_message.from_user
    if await is_admin(bot, message.chat.id, target_user.id):
        return await message.reply("❌ 不能禁言管理员。")

    args = message.text.split()
    duration = None
    if len(args) >= 2:
        try:
            minutes = int(args[1])
            duration = datetime.now() + timedelta(minutes=minutes)
        except ValueError:
            return await message.reply("❌ 禁言时长格式错误，请输入数字（分钟）。例如: /mute 60")

    try:
        await bot.restrict_chat_member(
            message.chat.id, target_user.id,
            until_date=duration if duration else None,
            can_send_messages=False
        )
        dur_text = f" {args[1]} 分钟" if duration else ""
        await message.reply(f"✅ 已禁言 {target_user.first_name}{dur_text}。")
    except TelegramBadRequest as e:
        await message.reply(f"❌ 禁言失败: {e.message}")


@router.message(Command("unmute"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_unmute(message: Message, bot: Bot):
    """解除禁言 /unmute（回复消息）"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    if not message.reply_to_message:
        return await message.reply("用法: 回复被禁言用户的消息 /unmute")

    target_user = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(
            message.chat.id, target_user.id,
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await message.reply(f"✅ 已解除 {target_user.first_name} 的禁言。")
    except TelegramBadRequest as e:
        await message.reply(f"❌ 操作失败: {e.message}")


@router.message(Command("kick"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_kick(message: Message, bot: Bot):
    """踢出用户 /kick（回复消息）"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    if not message.reply_to_message:
        return await message.reply("用法: 回复某条消息 /kick")

    target_user = message.reply_to_message.from_user
    if await is_admin(bot, message.chat.id, target_user.id):
        return await message.reply("❌ 不能踢出管理员。")

    try:
        await bot.ban_chat_member(message.chat.id, target_user.id)
        await asyncio.sleep(1)
        await bot.unban_chat_member(message.chat.id, target_user.id)
        await message.reply(f"✅ 已踢出 {target_user.first_name}。")
    except TelegramBadRequest as e:
        await message.reply(f"❌ 踢出失败: {e.message}")


@router.message(Command("warn"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_warn(message: Message, bot: Bot):
    """警告用户 /warn（回复消息）"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    if not message.reply_to_message:
        return await message.reply("用法: 回复某条消息 /warn [原因]")

    target_user = message.reply_to_message.from_user
    reason = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else "无"
    await message.reply(f"⚠️ {target_user.first_name} 被警告: {reason}")


@router.message(Command("setwelcome"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_set_welcome(message: Message, bot: Bot):
    """设置欢迎消息 /setwelcome 欢迎内容"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply(
            "用法: /setwelcome 欢迎内容\n"
            "支持占位符: {first_name} {username} {chat_title}"
        )

    welcome_msg = text[1]
    await db.set_group_setting(message.chat.id, welcome_msg=welcome_msg)
    await message.reply("✅ 欢迎消息已设置。")


@router.message(Command("settings"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_settings(message: Message, bot: Bot):
    """查看当前群组设置"""
    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return await message.reply("❌ 你没有权限执行此操作。")

    setting = await db.get_group_setting(message.chat.id)
    welcome = setting["welcome_msg"] if setting and setting["welcome_msg"] else "默认"
    antispam = setting["antispam"] if setting and setting["antispam"] else settings.ANTISPAM_SECONDS
    block_fwd = setting["block_forward"] if setting and setting["block_forward"] else settings.BLOCK_FORWARD
    banned = setting["banned_words"] if setting and setting["banned_words"] else settings.BANNED_WORDS

    text = (
        f"📋 当前群组设置:\n"
        f"欢迎消息: {welcome[:50]}...\n"
        f"反垃圾验证: {antispam} 秒\n"
        f"禁止转发: {'开' if block_fwd else '关'}\n"
        f"敏感词: {banned[:100]}"
    )
    await message.reply(text)


# --- 频道关注验证回调 ---

@router.callback_query(F.data.startswith("verifych:"))
async def verify_channel_callback(callback: CallbackQuery, bot: Bot):
    """处理频道关注验证按钮点击"""
    data_parts = callback.data.split(":")
    if len(data_parts) != 3:
        return await callback.answer("验证数据错误", show_alert=True)

    _, target_user_id, chat_id = data_parts
    target_user_id = int(target_user_id)
    chat_id = int(chat_id)
    clicked_by = callback.from_user.id

    # 只有目标用户可以点击自己的验证按钮
    if clicked_by != target_user_id:
        return await callback.answer("这不是你的验证，请不要乱点！", show_alert=True)

    # 检查用户是否关注了指定频道
    channel_id = settings.VERIFY_CHANNEL_ID
    if not channel_id:
        return await callback.answer("验证配置错误", show_alert=True)

    try:
        member = await bot.get_chat_member(channel_id, clicked_by)
        is_member = member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except TelegramBadRequest:
        is_member = False

    if is_member:
        # 验证通过
        await db.verify_captcha(target_user_id, chat_id)
        await callback.answer("✅ 验证通过！欢迎加入群组。", show_alert=False)

        # 删除验证消息
        try:
            await bot.delete_message(chat_id, callback.message.message_id)
        except TelegramBadRequest:
            pass

        # 发送欢迎消息
        try:
            welcome_text = settings.WELCOME_MESSAGE.format(
                first_name=callback.from_user.first_name or "",
                username=f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name or "",
                chat_title=callback.message.chat.title or "本群"
            )
        except KeyError:
            welcome_text = f"欢迎 {callback.from_user.first_name or ''} 加入 {callback.message.chat.title or '本群'}！"
        try:
            await bot.send_message(chat_id, welcome_text)
        except TelegramBadRequest:
            pass
    else:
        channel_title = settings.VERIFY_CHANNEL_TITLE or "指定频道"
        await callback.answer(
            f"❌ 验证失败！你尚未关注频道「{channel_title}」。请先关注后再点击验证。",
            show_alert=True
        )


# --- 机器人入群/退群记录 ---

@router.my_chat_member()
async def on_bot_chat_member(event: ChatMemberUpdated, bot: Bot):
    """记录机器人被加入/移出群组"""
    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    # 机器人被加入群组
    if old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        await db.add_bot_group(chat.id, chat.title or f"群组{chat.id}")
        logger.info(f"机器人被加入群组: {chat.title} ({chat.id})")

    # 机器人被踢出群组
    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.remove_bot_group(chat.id)
        logger.info(f"机器人被移出群组: {chat.title} ({chat.id})")
