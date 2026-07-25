"""
群成员缓存共享模块
供多个 handler 模块独立引用。
"""

import logging
import asyncio
import aiohttp

logger = logging.getLogger("group_member_store")

# 全局群成员缓存: group_num -> {qq: {"card", "nick", "role"}}
_member_cache: dict = {}

# NapCat 小号 QQ 号缓存（启动时初始化，后续直接读取）
_napcat_qq: int = 0
_napcat_qq_set: set = set()


async def refresh_member_cache(group_num: int):
    """
    通过 NapCat OneBot 11 API 刷新群成员列表并缓存。

    Args:
        group_num: 数字群号
    """
    from napcat_bridge import get_napcat_bridge
    bridge = get_napcat_bridge()
    if not bridge or not bridge.available:
        logger.warning(f"[群成员缓存] NapCat 不可用，跳过刷新: group={group_num}")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{bridge.base_url}/get_group_member_list",
                headers=bridge._headers(),
                json={"group_id": group_num, "no_cache": True},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("status") == "ok":
                    _member_cache[group_num] = {}
                    for member in data.get("data", []):
                        uid = member.get("user_id", 0)
                        card = member.get("card", "")
                        nickname = member.get("nickname", "")
                        role = member.get("role", "member")
                        _member_cache[group_num][uid] = {
                            "card": card,
                            "nick": nickname,
                            "role": role,
                        }
                    logger.debug(
                        f"[群成员缓存] 已刷新: group={group_num} "
                        f"members={len(_member_cache[group_num])}"
                    )
                else:
                    logger.warning(f"[群成员缓存] 获取群成员列表失败: {data}")
    except Exception as e:
        logger.warning(f"[群成员缓存] 刷新失败: group={group_num} err={e}")


def get_member_card(group_num: int, qq: int) -> str:
    """
    从缓存获取群成员群名片。

    Args:
        group_num: 数字群号
        qq: 成员 QQ 号

    Returns:
        群名片或昵称，缓存中没有则返回空字符串
    """
    if group_num in _member_cache and qq in _member_cache[group_num]:
        info = _member_cache[group_num][qq]
        return info.get("card") or info.get("nick") or ""
    return ""


def is_group_admin(group_num: int, qq: int) -> bool:
    """
    检查是否是群管理员或群主。

    Args:
        group_num: 数字群号
        qq: 成员 QQ 号

    Returns:
        role 为 "owner" 或 "admin" 时返回 True
    """
    if group_num in _member_cache and qq in _member_cache[group_num]:
        role = _member_cache[group_num][qq].get("role", "")
        return role in ("owner", "admin")
    return False


async def init_napcat_qq():
    """
    异步初始化 NapCat 小号 QQ 号。
    通过 get_login_info API 获取 NapCat 登录的 QQ 号并缓存到 _napcat_qq_set。
    应在启动时调用一次。
    """
    global _napcat_qq, _napcat_qq_set
    from napcat_bridge import get_napcat_bridge
    bridge = get_napcat_bridge()
    if not bridge:
        logger.warning("[群成员缓存] NapCat bridge 不可用，跳过 QQ 号获取")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{bridge.base_url}/get_login_info",
                headers=bridge._headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                if data.get("status") == "ok":
                    _napcat_qq = int(data.get("data", {}).get("user_id", 0) or 0)
                    _napcat_qq_set.add(_napcat_qq)
                    logger.info(f"[群成员缓存] NapCat 小号 QQ: {_napcat_qq}")
                else:
                    logger.warning(f"[群成员缓存] 获取 login_info 失败: {data}")
    except Exception as e:
        logger.warning(f"[群成员缓存] 获取 NapCat QQ 号失败: {e}")


def get_napcat_qq_set() -> set:
    """
    返回需要跳过的 QQ 号集合（NapCat 小号自身）。

    内部缓存了 NapCat 小号的 QQ 号，需先调用 init_napcat_qq() 初始化。
    用于过滤自身发送的告警消息，避免循环检测。

    Returns:
        包含 NapCat 小号 QQ 号的 set（已初始化）
    """
    return _napcat_qq_set
