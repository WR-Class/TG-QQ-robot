"""
定时消息推送模块
功能：基于 APScheduler 的定时任务，支持 Cron 表达式
"""

import asyncio
import hashlib
import logging
import json
import random
from datetime import datetime

from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings, get_scheduled_messages
from models.database import db
from handlers.group_manager import is_admin, is_super_admin

router = Router()
logger = logging.getLogger(__name__)

# 全局调度器
scheduler: AsyncIOScheduler | None = None


def init_scheduler(bot: Bot):
    """初始化 APScheduler 定时任务调度器"""
    global scheduler
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
    scheduler = AsyncIOScheduler()
    scheduler.start()
    logger.info("APScheduler 已启动")
    return scheduler


async def ai_generate_one_quote() -> str:
    """调用 AI 实时生成一条励志语录"""
    if not settings.AI_ENABLED:
        return ""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=settings.AI_API_KEY,
            base_url=settings.AI_BASE_URL if settings.AI_BASE_URL else None,
        )
        response = await client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=[
                {"role": "system", "content": "你是一个励志语录创作者。只输出一条语录本身，不要任何其他文字、标点前缀、序号。"},
                {"role": "user", "content": "随机生成一条简短的中文励志语录，10-25个字，积极向上温暖治愈，不颓废不消极。只输出一条。"},
            ],
            temperature=1.2,
            max_tokens=100,
        )
        content = (response.choices[0].message.content or "").strip()
        # 清理可能的前缀
        for prefix in ["「", "\"", "'", "1.", "1、", "1)", "-"]:
            if content.startswith(prefix):
                content = content[len(prefix):].strip()
        if 5 <= len(content) <= 50:
            return content
        return ""
    except Exception as e:
        logger.error(f"[AI生成语录] 失败: {e}")
        return ""


async def load_scheduled_jobs(bot: Bot):
    """从数据库加载定时任务"""
    if not scheduler:
        return

    # 先移除旧任务
    for job in scheduler.get_jobs():
        job.remove()

    # 加载环境变量中的任务
    env_jobs = get_scheduled_messages()
    for job_cfg in env_jobs:
        _add_cron_job(bot, job_cfg.get("chat_id"), job_cfg.get("text"), job_cfg.get("cron"))

    # 加载数据库中的任务
    db_jobs = await db.get_scheduled_messages(enabled_only=True)
    for job in db_jobs:
        _add_cron_job_db(bot, job)

    logger.info(f"已加载 {len(env_jobs) + len(db_jobs)} 个定时任务")


def _add_cron_job(bot: Bot, chat_id: int, text: str, cron: str, job_id: str = None,
                  is_random: bool = False, random_texts: str = "", suffix: str = "",
                  use_ai: bool = False):
    """添加 Cron 定时任务（环境变量配置）"""
    if not scheduler or not chat_id or not cron:
        return

    job_id = job_id or f"env_{chat_id}_{hashlib.md5(text.encode()).hexdigest()[:8]}"

    async def send_job():
        try:
            final_text = ""

            # AI 实时生成模式：每次发送时调 AI 生成一条
            if use_ai:
                ai_text = await ai_generate_one_quote()
                if ai_text:
                    final_text = ai_text
                    logger.info(f"[定时推送] AI生成语录: {final_text}")

            # AI 失败或未开启时，从本地文案库随机选取兜底
            if not final_text and is_random and random_texts:
                try:
                    texts_list = json.loads(random_texts)
                    if isinstance(texts_list, list) and texts_list:
                        final_text = random.choice(texts_list)
                except (json.JSONDecodeError, TypeError):
                    pass

            # 固定文案兜底
            if not final_text:
                final_text = text

            # 拼接固定后缀（如小店地址）
            if suffix:
                final_text = f"{final_text}\n\n{suffix}"

            await bot.send_message(chat_id, final_text)
            logger.info(f"[定时推送] 已发送到 {chat_id}: {final_text[:30]}...")
        except TelegramBadRequest as e:
            logger.error(f"[定时推送] 发送失败 {chat_id}: {e.message}")
        except Exception as e:
            logger.error(f"[定时推送] 异常 {chat_id}: {e}")

    try:
        # 解析 cron 表达式 (minute hour day month day_of_week)
        parts = cron.split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        else:
            logger.warning(f"Cron 表达式格式错误: {cron}")
            return

        scheduler.add_job(send_job, trigger, id=job_id, replace_existing=True)
        logger.info(f"添加定时任务 {job_id}: cron={cron}")
    except Exception as e:
        logger.error(f"添加定时任务失败: {e}")


def _add_cron_job_db(bot: Bot, job: dict):
    """添加数据库中的定时任务"""
    job_id = f"db_{job['id']}"
    _add_cron_job(
        bot, job["chat_id"], job["text"], job["cron"], job_id,
        is_random=bool(job.get("is_random", 0)),
        random_texts=job.get("random_texts", ""),
        suffix=job.get("suffix", ""),
        use_ai=bool(job.get("use_ai", 0)),
    )


# --- 管理命令 ---

@router.message(Command("addschedule"))
async def cmd_add_schedule(message: Message, bot: Bot):
    """添加定时消息 /addschedule chat_id cron表达式 | 消息内容"""
    if not is_super_admin(message.from_user.id):
        if message.chat.type in ("group", "supergroup"):
            if not await is_admin(bot, message.chat.id, message.from_user.id):
                return await message.reply("❌ 你没有权限执行此操作。")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply(
            "用法: /addschedule chat_id cron表达式 | 消息内容\n"
            "示例: /addschedule -1001234567890 0 9 * * * | 早安，各位！\n"
            "cron 格式: 分 时 日 月 星期"
        )

    parts = text[1].split("|", 1)
    if len(parts) != 2:
        return await message.reply("格式错误。请使用: /addschedule chat_id cron | 消息内容")

    left = parts[0].strip().rsplit(" ", 1)
    if len(left) != 2:
        return await message.reply("格式错误。chat_id 和 cron 之间需要用空格分隔。")

    try:
        chat_id = int(left[0])
    except ValueError:
        return await message.reply("chat_id 必须是数字。")

    cron = left[1]
    msg_text = parts[1].strip()

    await db.add_scheduled_message(chat_id, msg_text, cron)
    await message.reply(f"✅ 已添加定时消息到 {chat_id}，cron={cron}")

    # 重新加载任务
    await load_scheduled_jobs(bot)


@router.message(Command("delschedule"))
async def cmd_del_schedule(message: Message, bot: Bot):
    """删除定时消息 /delschedule 任务ID"""
    if not is_super_admin(message.from_user.id):
        if message.chat.type in ("group", "supergroup"):
            if not await is_admin(bot, message.chat.id, message.from_user.id):
                return await message.reply("❌ 你没有权限执行此操作。")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("用法: /delschedule 任务ID")

    try:
        msg_id = int(text[1].strip())
    except ValueError:
        return await message.reply("任务ID必须是数字。")

    await db.delete_scheduled_message(msg_id)
    await message.reply(f"✅ 已删除定时消息 ID={msg_id}")
    await load_scheduled_jobs(bot)


@router.message(Command("listschedule"))
async def cmd_list_schedule(message: Message):
    """列出所有定时消息 /listschedule"""
    jobs = await db.get_scheduled_messages(enabled_only=False)
    if not jobs:
        return await message.reply("暂无定时消息任务。")

    lines = ["📅 定时消息任务列表:"]
    for job in jobs:
        status = "✅" if job["enabled"] else "❌"
        lines.append(
            f"{status} ID:{job['id']} 群组:{job['chat_id']} "
            f"cron:{job['cron']} 内容:{job['text'][:30]}..."
        )

    await message.reply("\n".join(lines))


@router.message(Command("testschedule"))
async def cmd_test_schedule(message: Message, bot: Bot):
    """立即测试发送定时消息 /testschedule 任务ID"""
    if not is_super_admin(message.from_user.id):
        if message.chat.type in ("group", "supergroup"):
            if not await is_admin(bot, message.chat.id, message.from_user.id):
                return await message.reply("❌ 你没有权限执行此操作。")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("用法: /testschedule 任务ID")

    try:
        msg_id = int(text[1].strip())
    except ValueError:
        return await message.reply("任务ID必须是数字。")

    # 从数据库查找
    all_jobs = await db.get_scheduled_messages(enabled_only=False)
    target = next((j for j in all_jobs if j["id"] == msg_id), None)
    if not target:
        return await message.reply("❌ 找不到该任务。")

    try:
        await bot.send_message(target["chat_id"], target["text"])
        await message.reply(f"✅ 测试消息已发送到 {target['chat_id']}。")
    except TelegramBadRequest as e:
        await message.reply(f"❌ 发送失败: {e.message}")


@router.message(Command("reloadschedule"))
async def cmd_reload_schedule(message: Message, bot: Bot):
    """重新加载所有定时任务 /reloadschedule"""
    if not is_super_admin(message.from_user.id):
        if message.chat.type in ("group", "supergroup"):
            if not await is_admin(bot, message.chat.id, message.from_user.id):
                return await message.reply("❌ 你没有权限执行此操作。")

    await load_scheduled_jobs(bot)
    job_count = len(scheduler.get_jobs()) if scheduler else 0
    await message.reply(f"✅ 已重新加载 {job_count} 个定时任务。")
