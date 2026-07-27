"""
TGJQR Bot - Telegram 群组管理与自动回复机器人
基于 aiogram 3.x 异步框架
"""

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message

from config import settings
from models.database import db
from handlers import group_manager, auto_reply, scheduled_posts, ai_chat, menu, visual_mgmt, ad_detector


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
# 抑制 httpx/apscheduler 的 INFO 级请求日志刷屏
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# --- 核心命令直接注册到主 Dispatcher（避免子 Router 传播问题） ---

async def _register_main_commands(dp: Dispatcher):
    """将核心命令和 AI 回复直接注册到主 Dispatcher"""

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        text = (
            "👋 你好！我是 TGJQR 群管机器人。\n\n"
            "请从下方菜单选择功能，或直接发送命令：\n\n"
            "🤖 <b>AI对话</b> — 调用 AI 智能回答\n"
            "📋 <b>回复规则</b> — 查看自动回复规则\n"
            "🤖 <b>AI状态</b> — 查看 AI 配置\n"
            "🆘 <b>帮助</b> — 查看所有命令\n\n"
            "💡 把我拉入群组并设为管理员，即可使用群管功能。"
        )
        await message.reply(text, reply_markup=menu.get_main_menu_keyboard())

    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        await cmd_start(message)

    @dp.message(Command("ai_status"))
    async def cmd_ai_status(message: Message):
        status = "✅ 已启用" if settings.AI_ENABLED else "❌ 已禁用"
        model = settings.AI_MODEL
        base_url = settings.AI_BASE_URL or "默认 (OpenAI 官方)"
        trigger = settings.AI_GROUP_TRIGGER
        text = (
            f"🤖 AI 客服状态:\n"
            f"状态: {status}\n"
            f"模型: {model}\n"
            f"API 地址: {base_url}\n"
            f"群组触发: {trigger}\n"
            f"私聊自动: {'开' if settings.AI_PRIVATE_AUTO else '关'}"
        )
        await message.reply(text)

    @dp.message(Command("ai"))
    async def cmd_ai(message: Message):
        if not settings.AI_ENABLED:
            return await message.reply("❌ AI 客服功能未启用。")
        text = message.text.split(maxsplit=1)
        if len(text) < 2:
            return await message.reply("用法: /ai 你的问题")
        question = text[1]
        await message.reply("🤖 思考中...")
        user_msg = {"role": "user", "content": question}
        reply = await ai_chat.chat_with_ai(message.from_user.id, [user_msg])
        if reply:
            await message.reply(f"🤖 {reply}")

    # --- 群组 @mention AI 回复 ---
    @dp.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def group_ai_reply(message: Message, bot: Bot):
        """群组中 @机器人 或回复机器人消息时触发 AI + AI 广告检测"""
        logger.info(f"[群组消息] from={message.from_user.id} text={message.text[:30]} chat={message.chat.id}")

        # === 广告检测（对所有群组消息生效）===
        text_for_check = message.text or ""
        if len(text_for_check) >= 5:
            try:
                from handlers.ad_detector import check_and_handle_ad
                handled = await check_and_handle_ad(message, bot)
                if handled:
                    return  # 广告已被拦截，不再处理
            except Exception as e:
                logger.error(f"[广告检测] 调用异常: {e}")

        if not settings.AI_ENABLED:
            return
        text = message.text
        bot_info = await bot.me()
        bot_username = bot_info.username

        triggered = False
        trigger_mode = settings.AI_GROUP_TRIGGER

        if trigger_mode == "mention":
            if bot_username and f"@{bot_username}" in text:
                triggered = True
                text = text.replace(f"@{bot_username}", "").strip()
        elif trigger_mode == "reply":
            if message.reply_to_message and message.reply_to_message.from_user.id == bot_info.id:
                triggered = True
        elif trigger_mode == "all":
            triggered = True

        if not triggered:
            return

        logger.info(f"[AI群组回复] triggered={triggered} question={text[:30]}")
        if not text:
            return await message.reply("🤖 你好！请问有什么问题？")

        try:
            await message.reply("🤖 思考中...")
        except Exception as e:
            logger.error(f"[AI群组回复] 发送思考中失败: {e}")
            return

        user_msg = {"role": "user", "content": text}
        reply = await ai_chat.chat_with_ai(message.from_user.id, [user_msg])
        if reply:
            try:
                await message.reply(f"🤖 {reply}")
                logger.info(f"[AI群组回复] 回复成功: {reply[:30]}")
            except Exception as e:
                logger.error(f"[AI群组回复] 发送回复失败: {e}")
        else:
            try:
                await message.reply("🤖 抱歉，暂时无法回答你的问题。")
            except Exception as e:
                logger.error(f"[AI群组回复] 发送失败: {e}")

    # --- 私聊 AI 自动回复 ---
    @dp.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
    async def private_ai_reply(message: Message):
        """私聊中非命令消息自动调用 AI"""
        if not settings.AI_ENABLED or not settings.AI_PRIVATE_AUTO:
            return
        if not message.text:
            return
        user_msg = {"role": "user", "content": message.text}
        reply = await ai_chat.chat_with_ai(message.from_user.id, [user_msg])
        if reply:
            await message.reply(reply)


async def main():
    """主入口"""
    logger.info("正在启动 TGJQR Bot...")

    # 检查必要配置
    if not settings.BOT_TOKEN:
        logger.error("错误: BOT_TOKEN 未设置。请在 .env 文件中配置 BOT_TOKEN。")
        sys.exit(1)

    # 初始化数据库
    await db.connect()
    logger.info("数据库连接成功。")

    # 初始化 Bot（含代理支持）
    default_props = DefaultBotProperties(parse_mode=ParseMode.HTML)

    if settings.PROXY_URL:
        from aiogram.client.session.aiohttp import AiohttpSession
        session = AiohttpSession(proxy=settings.PROXY_URL)
        bot = Bot(token=settings.BOT_TOKEN, default=default_props, session=session)
        # 隐藏代理 URL 中的用户名密码部分
        try:
            from urllib.parse import urlparse
            parsed = urlparse(settings.PROXY_URL)
            if parsed.username or parsed.password:
                safe_url = parsed._replace(netloc=f"{parsed.hostname}:{parsed.port or ''}").geturl()
            else:
                safe_url = settings.PROXY_URL
        except Exception:
            safe_url = "(已设置)"
        logger.info(f"已启用代理: {safe_url}")
    else:
        bot = Bot(token=settings.BOT_TOKEN, default=default_props)

    # 初始化 Dispatcher
    dp = Dispatcher()

    # 先注册核心命令到主 Dispatcher（优先级最高）
    await _register_main_commands(dp)

    # 注册子路由（顺序决定优先级）
    dp.include_router(group_manager.router)
    dp.include_router(ad_detector.router)   # AI 广告检测（解封回调）
    dp.include_router(menu.router)        # 菜单按钮优先匹配
    dp.include_router(visual_mgmt.router) # 可视化管理系统
    dp.include_router(auto_reply.router)  # 自动回复
    dp.include_router(ai_chat.router)     # AI 客服兜底
    dp.include_router(scheduled_posts.router)

    # 初始化定时任务调度器
    scheduled_posts.init_scheduler(bot)
    await scheduled_posts.load_scheduled_jobs(bot)

    # 启动 Web 管理后台（与 Bot 并行运行）
    web_task = None
    try:
        import uvicorn
        from web_server import app
        web_port = int(os.environ.get("WEB_PORT", "8080"))
        config = uvicorn.Config(app, host="0.0.0.0", port=web_port, log_level="warning")
        server = uvicorn.Server(config)
        web_task = asyncio.create_task(server.serve())
        logger.info(f"Web 管理后台已启动: http://0.0.0.0:{web_port}")
    except Exception as e:
        logger.warning(f"Web 管理后台启动失败（非致命）: {e}")

    # 启动轮询
    logger.info("Bot 已启动，开始接收消息...")

    # 启动 NapCat WebSocket 后台任务（QQ 群管 + @指令 + AI 聊天）
    napcat_ws_task = None
    try:
        from napcat_ws import start_napcat_ws
        await start_napcat_ws()
        logger.info("[NapCat WS] 已集成到 tgjqr-bot 容器")
    except Exception as e:
        logger.warning(f"NapCat WS 启动失败（非致命）: {e}")

    # 启动健康监控循环（自动续命 + Prometheus 指标）
    try:
        from handlers.health_monitor import health_loop
        asyncio.create_task(health_loop(interval=60))
        logger.info("[健康监控] 循环已启动（间隔60s，含日志监控+自动续命）")
    except Exception as e:
        logger.warning(f"健康监控启动失败（非致命）: {e}")

    try:
        await dp.start_polling(bot)
    finally:
        if web_task:
            web_task.cancel()
        await db.close()
        await bot.session.close()
        logger.info("Bot 已停止。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
