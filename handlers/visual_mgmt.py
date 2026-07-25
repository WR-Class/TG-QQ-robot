"""
可视化管理系统
功能：Inline 按钮向导式操作，替代命令行输入
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from config import settings, get_banned_words
from models.database import db
from handlers.group_manager import is_admin, is_super_admin
from handlers.menu import get_main_menu_keyboard

router = Router()
logger = logging.getLogger(__name__)


# ==================== 状态定义 ====================

class AddReplyState(StatesGroup):
    """添加自动回复的状态机"""
    waiting_keyword = State()
    waiting_response = State()


class AddScheduleState(StatesGroup):
    """添加定时消息的状态机"""
    waiting_mode = State()       # 选择模式：固定文案/随机文案
    waiting_text = State()      # 输入固定文案 或 随机文案库
    waiting_suffix = State()    # 输入固定后缀（小店地址）
    waiting_cron = State()      # 输入 Cron 表达式
    waiting_target = State()    # 选择目标群组


class SetWelcomeState(StatesGroup):
    """设置欢迎消息的状态机"""
    waiting_message = State()


# ==================== 通用工具 ====================

def build_inline_panel(*rows, title: str = None) -> str:
    """构建内联键盘"""
    builder = InlineKeyboardBuilder()
    for row in rows:
        for btn in row:
            builder.button(text=btn[0], callback_data=btn[1])
    builder.adjust(*[len(r) for r in rows])
    return builder.as_markup()


# ==================== 回复规则管理面板 ====================

@router.callback_query(F.data == "mgmt:reply")
async def reply_rules_panel(callback: CallbackQuery):
    """回复规则管理面板"""
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 添加规则", callback_data="reply:add")
    builder.button(text="🗑️ 删除规则", callback_data="reply:list_del")
    builder.button(text="📋 查看所有规则", callback_data="reply:list")
    builder.button(text="🔙 返回设置", callback_data="mgmt:back")
    builder.adjust(2, 1, 1)

    await callback.message.edit_text(
        "📋 <b>自动回复规则管理</b>\n\n"
        "在这里管理关键词自动回复规则：\n"
        "• 添加规则：设置关键词和回复内容\n"
        "• 删除规则：选择要删除的规则\n"
        "• 查看所有规则：列出所有已配置的规则",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "reply:add")
async def reply_add_start(callback: CallbackQuery, state: FSMContext):
    """开始添加回复规则"""
    await state.set_state(AddReplyState.waiting_keyword)
    await callback.message.edit_text(
        "📝 <b>添加自动回复规则</b> — 第 1 步\n\n"
        "请输入 <b>关键词</b>：\n"
        "（用户发送包含此关键词的消息时，机器人会自动回复）\n\n"
        "💡 直接输入关键词即可，支持中文。\n"
        "❌ 发送 /cancel 取消。",
        reply_markup=InlineKeyboardBuilder().button(
            text="❌ 取消", callback_data="mgmt:reply"
        ).as_markup()
    )
    await callback.answer()


@router.message(AddReplyState.waiting_keyword)
async def reply_add_keyword(message: Message, state: FSMContext):
    """接收关键词"""
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("已取消。", reply_markup=get_main_menu_keyboard())

    keyword = message.text.strip()
    if not keyword:
        return await message.reply("关键词不能为空，请重新输入：")

    await state.update_data(keyword=keyword)
    await state.set_state(AddReplyState.waiting_response)

    await message.reply(
        f"📝 <b>第 2 步</b>\n\n"
        f"关键词已设为：<b>{keyword}</b>\n\n"
        f"现在请输入 <b>回复内容</b>：\n"
        f"（当用户发送包含「{keyword}」的消息时，我会回复这个内容）\n\n"
        f"❌ 发送 /cancel 取消。"
    )


@router.message(AddReplyState.waiting_response)
async def reply_add_response(message: Message, state: FSMContext):
    """接收回复内容并保存"""
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("已取消。", reply_markup=get_main_menu_keyboard())

    response = message.text.strip()
    if not response:
        return await message.reply("回复内容不能为空，请重新输入：")

    data = await state.get_data()
    keyword = data["keyword"]
    chat_id = 0 if message.chat.type == "private" else message.chat.id
    is_global = message.chat.type == "private"

    await db.add_auto_reply(keyword, response, chat_id=chat_id, is_global=is_global)
    await state.clear()

    # 构建确认消息
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 继续添加", callback_data="reply:add")
    builder.button(text="📋 查看所有规则", callback_data="reply:list")
    builder.button(text="🔙 返回面板", callback_data="mgmt:reply")
    builder.adjust(2, 1)

    await message.reply(
        f"✅ <b>规则添加成功！</b>\n\n"
        f"🔑 关键词：<b>{keyword}</b>\n"
        f"💬 回复：{response[:60]}{'...' if len(response) > 60 else ''}\n"
        f"📍 范围：{'全局' if is_global else '当前群组'}",
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "reply:list")
async def reply_list(callback: CallbackQuery):
    """查看所有规则"""
    chat_id = callback.message.chat.id if callback.message.chat.type in ("group", "supergroup") else None
    rules = await db.get_auto_replies(chat_id=chat_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 返回", callback_data="mgmt:reply")

    if not rules:
        return await callback.message.edit_text(
            "📋 <b>自动回复规则</b>\n\n当前暂无规则。",
            reply_markup=builder.as_markup()
        )

    lines = ["📋 <b>自动回复规则列表</b>\n"]
    for i, rule in enumerate(rules[:20], 1):
        scope = "🌐 全局" if rule["is_global"] else f"📍 群组{rule['chat_id']}"
        lines.append(f"{i}. [{scope}] <b>{rule['keyword']}</b>")
        lines.append(f"   → {rule['response'][:50]}")
    if len(rules) > 20:
        lines.append(f"\n...还有 {len(rules) - 20} 条规则")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "reply:list_del")
async def reply_list_for_delete(callback: CallbackQuery):
    """列出可删除的规则"""
    chat_id = callback.message.chat.id if callback.message.chat.type in ("group", "supergroup") else None
    rules = await db.get_auto_replies(chat_id=chat_id)

    if not rules:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 返回", callback_data="mgmt:reply")
        return await callback.message.edit_text(
            "📋 <b>删除规则</b>\n\n当前暂无规则可删除。",
            reply_markup=builder.as_markup()
        )

    builder = InlineKeyboardBuilder()
    for rule in rules[:15]:
        label = f"🗑️ {rule['keyword']}"[:40]
        builder.button(text=label, callback_data=f"delreply:{rule['id']}")
    builder.button(text="🔙 返回", callback_data="mgmt:reply")
    builder.adjust(1, repeat=True)

    await callback.message.edit_text(
        "🗑️ <b>选择要删除的规则</b>\n\n"
        "点击下方按钮删除对应规则：",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delreply:"))
async def reply_delete_confirm(callback: CallbackQuery):
    """删除规则确认"""
    rule_id = int(callback.data.split(":")[1])

    # 获取规则信息
    rules = await db.get_auto_replies(chat_id=None)
    rule_info = next((r for r in rules if r["id"] == rule_id), None)

    if not rule_info:
        await callback.answer("规则不存在或已被删除。", show_alert=True)
        return

    await db.delete_auto_reply_by_id(rule_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑️ 继续删除", callback_data="reply:list_del")
    builder.button(text="🔙 返回面板", callback_data="mgmt:reply")

    await callback.message.edit_text(
        f"✅ 已删除规则：<b>{rule_info['keyword']}</b>",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


# ==================== 定时消息管理面板 ====================

@router.callback_query(F.data == "mgmt:schedule")
async def schedule_panel(callback: CallbackQuery):
    """定时消息管理面板"""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ 添加定时消息", callback_data="schedule:add")
    builder.button(text="📋 查看/删除定时消息", callback_data="schedule:list")
    builder.button(text="🔙 返回设置", callback_data="mgmt:back")
    builder.adjust(1, 1, 1)

    await callback.message.edit_text(
        "⏰ <b>定时消息管理</b>\n\n"
        "定时消息会在指定时间自动发送到群组。\n"
        "• 支持 Cron 表达式（如 <code>0 9 * * *</code> 表示每天 9:00）\n"
        "• 添加后需要群组管理员权限才能生效",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "schedule:add")
async def schedule_add_start(callback: CallbackQuery, state: FSMContext):
    """开始添加定时消息 — 选择模式"""
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 固定文案", callback_data="sched_mode:fixed")
    builder.button(text="🎲 随机文案", callback_data="sched_mode:random")
    builder.button(text="❌ 取消", callback_data="mgmt:schedule")
    builder.adjust(2, 1)

    await callback.message.edit_text(
        "⏰ <b>添加定时消息</b> — 第 1 步\n\n"
        "选择文案模式：\n\n"
        "📝 <b>固定文案</b> — 每次发送相同内容\n"
        "🎲 <b>随机文案</b> — 每次从文案库中随机选取\n\n"
        "两种模式都支持在末尾自动拼接固定后缀（如小店地址）",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "sched_mode:fixed")
async def schedule_mode_fixed(callback: CallbackQuery, state: FSMContext):
    """选择固定文案模式"""
    await state.set_state(AddScheduleState.waiting_text)
    await state.update_data(mode="fixed")

    await callback.message.edit_text(
        "⏰ <b>添加定时消息</b> — 第 2 步\n\n"
        "📝 <b>固定文案模式</b>\n\n"
        "请输入每次发送的 <b>固定消息内容</b>：\n\n"
        "❌ 发送 /cancel 取消。",
        reply_markup=InlineKeyboardBuilder().button(
            text="❌ 取消", callback_data="mgmt:schedule"
        ).as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "sched_mode:random")
async def schedule_mode_random(callback: CallbackQuery, state: FSMContext):
    """选择随机文案模式"""
    await state.set_state(AddScheduleState.waiting_text)
    await state.update_data(mode="random")

    await callback.message.edit_text(
        "⏰ <b>添加定时消息</b> — 第 2 步\n\n"
        "🎲 <b>随机文案模式</b>\n\n"
        "请输入 <b>文案库</b>，每行一条：\n\n"
        "格式：每行写一条文案，发送时随机选取一条\n\n"
        "示例：\n"
        "<i>今天的阳光真好☀️\n"
        "生活不止眼前的苟且🌍\n"
        "努力遇见更好的自己💪\n"
        "每天进步一点点📈</i>\n\n"
        "❌ 发送 /cancel 取消。",
        reply_markup=InlineKeyboardBuilder().button(
            text="❌ 取消", callback_data="mgmt:schedule"
        ).as_markup()
    )
    await callback.answer()


@router.message(AddScheduleState.waiting_text)
async def schedule_add_text(message: Message, state: FSMContext):
    """接收文案内容"""
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("已取消。", reply_markup=get_main_menu_keyboard())

    data = await state.get_data()
    mode = data.get("mode", "fixed")
    text = message.text.strip()

    if mode == "random":
        # 把每行解析为文案列表
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return await message.reply("文案不能为空，每行写一条。请重新输入：")
        await state.update_data(random_texts=json.dumps(lines, ensure_ascii=False))
        await state.update_data(text=lines[0])  # 保存第一条作为默认文本
    else:
        if not text:
            return await message.reply("文案不能为空，请重新输入：")
        await state.update_data(text=text, random_texts="")

    await state.set_state(AddScheduleState.waiting_suffix)

    mode_label = "🎲 随机" if mode == "random" else "📝 固定"
    text_preview = text[:50] + "..." if len(text) > 50 else text
    await message.reply(
        f"⏰ <b>第 3 步</b>\n\n"
        f"模式: {mode_label}\n"
        f"文案: {text_preview}\n\n"
        f"请输入 <b>固定后缀</b>（如小店地址、推广链接）\n\n"
        f"每次发送时，后缀会自动拼在文案后面。\n"
        f"不需要后缀就发送 <code>无</code> 或 <code>0</code>\n\n"
        f"示例：<code>🛒 进店看看: https://example.com/shop</code>\n\n"
        f"❌ 发送 /cancel 取消。"
    )


@router.message(AddScheduleState.waiting_suffix)
async def schedule_add_suffix(message: Message, state: FSMContext):
    """接收固定后缀"""
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("已取消。", reply_markup=get_main_menu_keyboard())

    suffix_text = message.text.strip()
    if suffix_text in ("无", "0", "-", "跳过"):
        suffix_text = ""

    await state.update_data(suffix=suffix_text)
    await state.set_state(AddScheduleState.waiting_cron)

    await message.reply(
        "⏰ <b>第 4 步</b>\n\n"
        "请输入 <b>Cron 表达式</b>（定时规则）：\n\n"
        "格式：<code>分 时 日 月 周</code>\n\n"
        "常用示例：\n"
        "• <code>0 9 * * *</code> — 每天 09:00\n"
        "• <code>0 18 * * *</code> — 每天 18:00\n"
        "• <code>30 8 * * 1-5</code> — 工作日 08:30\n"
        "• <code>0 9,18 * * *</code> — 每天 9:00 和 18:00\n"
        "• <code>0 */3 * * *</code> — 每 3 小时\n\n"
        "❌ 发送 /cancel 取消。"
    )


@router.message(AddScheduleState.waiting_cron)
async def schedule_add_cron(message: Message, state: FSMContext, bot: Bot):
    """接收 Cron 表达式 → 选择目标群组"""
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("已取消。", reply_markup=get_main_menu_keyboard())

    cron = message.text.strip()
    parts = cron.split()
    if len(parts) != 5:
        return await message.reply(
            "❌ Cron 格式错误。需要 5 个字段，示例：<code>0 9 * * *</code>\n\n"
            "请重新输入："
        )

    await state.update_data(cron=cron)
    await state.set_state(AddScheduleState.waiting_target)

    # 显示群组列表让用户选择
    groups = await db.get_bot_groups()

    if not groups:
        await state.clear()
        return await message.reply(
            "❌ 暂未加入任何群组。请先把机器人拉入群组后再添加定时消息。"
        )

    builder = InlineKeyboardBuilder()
    for g in groups[:15]:
        label = f"📁 {g['title']}"[:40]
        builder.button(text=label, callback_data=f"sched_target:{g['chat_id']}")
    builder.button(text="❌ 取消", callback_data="mgmt:schedule")
    builder.adjust(1, repeat=True)

    mode = (await state.get_data()).get("mode", "fixed")
    suffix = (await state.get_data()).get("suffix", "")

    await message.reply(
        f"⏰ <b>第 5 步</b> — 选择发送目标群组\n\n"
        f"模式: {'🎲 随机文案' if mode == 'random' else '📝 固定文案'}\n"
        f"Cron: <code>{cron}</code>\n"
        f"后缀: {suffix or '无'}\n\n"
        f"点击下方按钮选择要发送的群组：",
        reply_markup=builder.as_markup()
    )


@router.callback_query(F.data.startswith("sched_target:"))
async def schedule_target_selected(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """选择目标群组 → 保存定时消息"""
    target_chat_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    mode = data.get("mode", "fixed")
    text = data.get("text", "")
    cron = data.get("cron", "")
    random_texts = data.get("random_texts", "")
    suffix = data.get("suffix", "")
    is_random = (mode == "random")

    await db.add_scheduled_message(
        chat_id=target_chat_id,
        text=text,
        cron=cron,
        is_random=is_random,
        random_texts=random_texts,
        suffix=suffix,
    )
    await state.clear()

    # 重新加载定时任务
    from handlers.scheduled_posts import load_scheduled_jobs
    await load_scheduled_jobs(bot)

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ 继续添加", callback_data="schedule:add")
    builder.button(text="📋 查看所有", callback_data="schedule:list")
    builder.button(text="🔙 返回面板", callback_data="mgmt:schedule")
    builder.adjust(2, 1)

    mode_label = "🎲 随机" if is_random else "📝 固定"
    suffix_info = f"\n📎 后缀: {suffix}" if suffix else ""
    await callback.message.edit_text(
        f"✅ <b>定时消息添加成功！</b>\n\n"
        f"模式: {mode_label}\n"
        f"📝 文案: {text[:40]}{'...' if len(text) > 40 else ''}\n"
        f"⏰ 定时: <code>{cron}</code>\n"
        f"📁 群组: {target_chat_id}{suffix_info}",
        reply_markup=builder.as_markup()
    )
    await callback.answer("✅ 添加成功！")


@router.callback_query(F.data == "schedule:list")
async def schedule_list(callback: CallbackQuery):
    """查看定时消息列表"""
    schedules = await db.get_scheduled_messages()

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 返回", callback_data="mgmt:schedule")

    if not schedules:
        return await callback.message.edit_text(
            "📋 <b>定时消息</b>\n\n当前暂无定时消息。",
            reply_markup=builder.as_markup()
        )

    lines = ["📋 <b>定时消息列表</b>\n"]
    for s in schedules[:10]:
        status = "✅" if s["enabled"] else "⏸️"
        lines.append(f"{status} <code>{s['cron']}</code>")
        lines.append(f"   📝 {s['text'][:40]}")
        lines.append(f"   🆔 {s['id']}")

    del_builder = InlineKeyboardBuilder()
    for s in schedules[:10]:
        del_builder.button(text=f"🗑️ 删除 #{s['id']}", callback_data=f"delsched:{s['id']}")
    if schedules:
        del_builder.button(text="🔙 返回", callback_data="mgmt:schedule")
        del_builder.adjust(1, repeat=True)
        await callback.message.edit_text(
            "\n".join(lines) + "\n\n点击下方按钮删除定时消息：",
            reply_markup=del_builder.as_markup()
        )
    await callback.answer()


@router.callback_query(F.data.startswith("delsched:"))
async def schedule_delete(callback: CallbackQuery, bot: Bot):
    """删除定时消息"""
    sched_id = int(callback.data.split(":")[1])
    await db.delete_scheduled_message(sched_id)

    # 重新加载定时任务
    from handlers.scheduled_posts import load_scheduled_jobs
    await load_scheduled_jobs(bot)

    await callback.answer("✅ 已删除定时消息", show_alert=True)

    # 刷新列表
    await schedule_list(callback)


# ==================== 群组设置面板 ====================

@router.callback_query(F.data == "mgmt:settings")
async def group_settings_entry(callback: CallbackQuery, bot: Bot):
    """群组设置入口：私聊中先选群组，群组中直接进入"""
    chat_id = callback.message.chat.id
    # 私聊中：让用户选择群组
    if chat_id >= 0:
        return await select_group_list(callback, bot)
    # 群组中：直接进入
    if not await is_admin(bot, chat_id, callback.from_user.id):
        return await callback.answer("你没有权限执行此操作。", show_alert=True)
    await show_group_settings(callback.message, bot, chat_id)


async def select_group_list(callback_or_message, bot: Bot):
    """显示群组列表供用户选择"""
    groups = await db.get_bot_groups()

    if not groups:
        msg = "📋 <b>群组设置</b>\n\n暂未加入任何群组。\n\n请先把机器人拉入群组并设为管理员。"
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.edit_text(msg)
            await callback_or_message.answer()
        else:
            await callback_or_message.reply(msg)
        return

    builder = InlineKeyboardBuilder()
    for g in groups[:15]:
        label = f"📁 {g['title']}"[:40]
        builder.button(text=label, callback_data=f"gset:select:{g['chat_id']}")
    builder.button(text="🔙 返回", callback_data="mgmt:back")
    builder.adjust(1, repeat=True)

    text = "⚙️ <b>选择要管理的群组</b>\n\n"
    for i, g in enumerate(groups[:15], 1):
        text += f"{i}. {g['title']}\n"

    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.message.edit_text(text, reply_markup=builder.as_markup())
        await callback_or_message.answer()
    else:
        await callback_or_message.reply(text, reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("gset:select:"))
async def group_selected(callback: CallbackQuery, bot: Bot):
    """用户选择了某个群组"""
    target_chat_id = int(callback.data.split(":")[2])
    if not await is_admin(bot, target_chat_id, callback.from_user.id):
        return await callback.answer("你不是该群组的管理员。", show_alert=True)
    await show_group_settings(callback.message, bot, target_chat_id)


async def show_group_settings(message, bot: Bot, target_chat_id: int):
    """显示群组设置面板（可从私聊或群组调用）"""
    setting = await db.get_group_setting(target_chat_id)
    welcome = setting["welcome_msg"] if setting and setting["welcome_msg"] else "默认"
    antispam = setting["antispam"] if setting else settings.ANTISPAM_SECONDS
    block_fwd = setting["block_forward"] if setting else settings.BLOCK_FORWARD

    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ 设置欢迎消息", callback_data=f"gset:welcome:{target_chat_id}")
    builder.button(text=f"🚫 反垃圾: {antispam}s", callback_data=f"gset:antispam:{target_chat_id}")
    builder.button(text=f"📵 禁止转发: {'开' if block_fwd else '关'}", callback_data=f"gset:blockfwd:{target_chat_id}")
    builder.button(text="🔙 返回", callback_data="mgmt:back")
    builder.adjust(1, 1, 1, 1)

    text = (
        f"⚙️ <b>群组设置</b> (ID: {target_chat_id})\n\n"
        f"✏️ 欢迎消息: {welcome[:30]}...\n"
        f"🚫 反垃圾验证: {antispam} 秒\n"
        f"📵 禁止转发: {'✅ 开启' if block_fwd else '❌ 关闭'}\n"
        f"🚫 敏感词: {settings.BANNED_WORDS[:30] or '未设置'}"
    )

    # 如果是编辑已有消息（来自回调），用 edit_text；否则发送新消息
    try:
        await message.edit_text(text, reply_markup=builder.as_markup())
    except TelegramBadRequest:
        msg = await message.reply(text, reply_markup=builder.as_markup())
        return msg  # 返回新消息引用，供后续 edit 使用


@router.callback_query(F.data.startswith("gset:blockfwd:"))
async def toggle_block_forward(callback: CallbackQuery, bot: Bot):
    """切换禁止转发"""
    target_chat_id = int(callback.data.split(":")[2])
    setting = await db.get_group_setting(target_chat_id)
    current = setting["block_forward"] if setting else settings.BLOCK_FORWARD
    new_value = 0 if current else 1
    await db.set_group_setting(target_chat_id, block_forward=new_value)
    await callback.answer(f"已{'开启' if new_value else '关闭'}禁止转发", show_alert=True)
    await show_group_settings(callback.message, bot, target_chat_id)


@router.callback_query(F.data.startswith("gset:antispam:"))
async def cycle_antispam(callback: CallbackQuery, bot: Bot):
    """循环切换反垃圾秒数"""
    target_chat_id = int(callback.data.split(":")[2])
    setting = await db.get_group_setting(target_chat_id)
    current = setting["antispam"] if setting else settings.ANTISPAM_SECONDS
    options = [0, 30, 60, 120, 300]
    idx = options.index(current) if current in options else -1
    new_value = options[(idx + 1) % len(options)]
    await db.set_group_setting(target_chat_id, antispam=new_value)
    await callback.answer(f"反垃圾验证已设为 {new_value} 秒", show_alert=True)
    await show_group_settings(callback.message, bot, target_chat_id)


@router.callback_query(F.data.startswith("gset:welcome:"))
async def set_welcome_start(callback: CallbackQuery, state: FSMContext):
    """开始设置欢迎消息"""
    target_chat_id = int(callback.data.split(":")[2])
    await state.update_data(welcome_chat_id=target_chat_id)
    await state.set_state(SetWelcomeState.waiting_message)
    await callback.message.edit_text(
        "✏️ <b>设置欢迎消息</b>\n\n"
        "请输入新的欢迎消息内容：\n\n"
        "支持占位符：\n"
        "• <code>{first_name}</code> — 用户昵称\n"
        "• <code>{username}</code> — 用户名\n"
        "• <code>{chat_title}</code> — 群组名称\n\n"
        "示例：<i>👋 欢迎 {first_name} 加入 {chat_title}！</i>\n\n"
        "❌ 发送 /cancel 取消。",
        reply_markup=InlineKeyboardBuilder().button(
            text="❌ 取消", callback_data=f"gset:select:{target_chat_id}"
        ).as_markup()
    )
    await callback.answer()


@router.message(SetWelcomeState.waiting_message)
async def welcome_set_message(message: Message, state: FSMContext):
    """接收欢迎消息内容"""
    if message.text == "/cancel":
        await state.clear()
        return await message.reply("已取消。", reply_markup=get_main_menu_keyboard())

    data = await state.get_data()
    target_chat_id = data.get("welcome_chat_id", message.chat.id)

    welcome = message.text.strip()
    await db.set_group_setting(target_chat_id, welcome_msg=welcome)
    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 返回设置", callback_data=f"gset:select:{target_chat_id}")
    await message.reply(
        f"✅ 欢迎消息已更新！\n\n{welcome}",
        reply_markup=builder.as_markup()
    )


# ==================== 主管理面板 ====================

@router.callback_query(F.data == "mgmt:back")
async def management_back(callback: CallbackQuery, state: FSMContext = None):
    """返回主管理面板"""
    if state:
        await state.clear()

    builder = InlineKeyboardBuilder()
    builder.button(text="📝 回复规则管理", callback_data="mgmt:reply")
    builder.button(text="⏰ 定时消息管理", callback_data="mgmt:schedule")
    builder.button(text="⚙️ 群组设置", callback_data="mgmt:settings")
    builder.adjust(1, 1, 1)

    await callback.message.edit_text(
        "⚙️ <b>管理面板</b>\n\n"
        "选择要管理的功能：",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.message(Command("manage"))
async def cmd_manage(message: Message):
    """打开管理面板"""
    await open_management_panel(message)


async def open_management_panel(message: Message):
    """打开管理面板（可被其他模块调用）"""
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 回复规则管理", callback_data="mgmt:reply")
    builder.button(text="⏰ 定时消息管理", callback_data="mgmt:schedule")
    if message.chat.type in ("group", "supergroup"):
        builder.button(text="⚙️ 群组设置", callback_data="mgmt:settings")
    builder.adjust(1, 1, 1)

    await message.reply(
        "⚙️ <b>管理面板</b>\n\n"
        "选择要管理的功能：",
        reply_markup=builder.as_markup()
    )