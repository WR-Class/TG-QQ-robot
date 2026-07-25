"""
定时解禁后台任务
轮询 scheduled_unmutes 表，到期后调用 NapCat 解除禁言。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("unmute_worker")

_running = False


async def process_due_unmutes() -> int:
    """处理到期解禁，返回处理条数。"""
    from handlers.moderation_store import fetch_due_unmutes, mark_unmute_done
    from napcat_bridge import get_napcat_bridge

    rows = fetch_due_unmutes()
    if not rows:
        return 0

    napcat = get_napcat_bridge()
    if not napcat.available:
        await napcat.check_available()
    if not napcat.available:
        logger.warning("[定时解禁] NapCat 不可用，稍后重试")
        return 0

    done = 0
    for row in rows:
        rid = row["id"]
        group_id = int(row["group_id"])
        user_id = int(row["user_id"])
        try:
            ok = await napcat.set_group_ban(group_id, user_id, 0)
            if ok:
                mark_unmute_done(rid, "done")
                done += 1
                logger.info(f"[定时解禁] 已解禁 group={group_id} user={user_id}")
            else:
                logger.warning(f"[定时解禁] 解禁失败 group={group_id} user={user_id}")
        except Exception as e:
            logger.error(f"[定时解禁] 异常: {e}")
    return done


async def unmute_loop(interval: int = 20):
    """后台循环。"""
    global _running
    if _running:
        return
    _running = True
    logger.info(f"[定时解禁] 后台任务启动，间隔 {interval}s")
    try:
        while _running:
            try:
                n = await process_due_unmutes()
                if n:
                    logger.info(f"[定时解禁] 本轮处理 {n} 条")
            except Exception as e:
                logger.warning(f"[定时解禁] 轮询异常: {e}")
            await asyncio.sleep(interval)
    finally:
        _running = False


def start_unmute_worker():
    """在当前事件循环中启动后台任务。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(unmute_loop())
        logger.info("[定时解禁] 已提交后台任务")
    except RuntimeError:
        logger.warning("[定时解禁] 无运行中的事件循环，跳过启动")
