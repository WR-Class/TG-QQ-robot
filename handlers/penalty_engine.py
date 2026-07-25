"""
处罚阶梯执行器
警告 → 短禁 → 长禁 → 踢出
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("penalty_engine")


async def apply_penalty(
    *,
    group_id: int,
    user_id: int,
    user_name: str = "",
    reason: str = "",
    napcat=None,
    use_ladder: bool = True,
) -> Dict[str, Any]:
    """
    对用户执行阶梯处罚。
    use_ladder=True 时升级等级；False 时仅按当前配置动作执行一次（不升级）。
    """
    from handlers.moderation_store import (
        escalate_penalty,
        add_violation,
        schedule_unmute,
    )

    if not user_id or not group_id:
        return {"ok": False, "message": "缺少 group_id/user_id"}

    step = escalate_penalty(group_id, user_id, reason=reason) if use_ladder else {
        "action": "mute_short",
        "mute_seconds": 600,
        "label": "禁言10分钟",
        "level": 1,
        "strike_count": 1,
    }

    action = step.get("action") or "warn"
    mute_seconds = int(step.get("mute_seconds") or 0)
    label = step.get("label") or action
    executed = []

    try:
        if napcat and getattr(napcat, "available", False):
            if action == "warn":
                await napcat.send_group_msg(
                    group_id,
                    f"⚠️ [处罚] {user_name or user_id}：{reason or '违规'}\n"
                    f"本次：{label}（第 {step.get('strike_count', 1)} 次）",
                )
                executed.append("warn_notify")
            elif action in ("mute_short", "mute_long") and mute_seconds > 0:
                ok = await napcat.set_group_ban(group_id, user_id, mute_seconds)
                if ok:
                    schedule_unmute(group_id, user_id, mute_seconds, reason=f"阶梯处罚:{label}")
                    executed.append(f"mute_{mute_seconds}s")
                await napcat.send_group_msg(
                    group_id,
                    f"⚠️ [处罚] {user_name or user_id}：{reason or '违规'}\n"
                    f"本次：{label}（第 {step.get('strike_count', 1)} 次）",
                )
                executed.append("notify")
            elif action == "kick":
                # OneBot set_group_kick
                try:
                    ok = await napcat.set_group_kick(group_id, user_id)
                except Exception:
                    # 兼容无封装方法
                    ok = False
                    try:
                        import aiohttp
                        headers = napcat._headers()
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                f"{napcat.base_url}/set_group_kick",
                                headers=headers,
                                json={"group_id": group_id, "user_id": user_id, "reject_add_request": False},
                                timeout=aiohttp.ClientTimeout(total=8),
                            ) as resp:
                                data = await resp.json()
                                ok = data.get("status") == "ok" or data.get("retcode") == 0
                    except Exception as e:
                        logger.warning(f"[处罚] 踢出失败: {e}")
                executed.append("kick" if ok else "kick_failed")
                await napcat.send_group_msg(
                    group_id,
                    f"🚫 [处罚] {user_name or user_id} 因多次违规已被踢出\n原因：{reason or '违规'}",
                )
        add_violation(
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            vtype="penalty",
            score=int(step.get("level") or 0) * 25,
            reason=f"{label}: {reason}",
            content="",
            action=action,
            extra=step,
        )
        return {"ok": True, "step": step, "executed": executed}
    except Exception as e:
        logger.warning(f"[处罚] 执行异常: {e}")
        return {"ok": False, "message": str(e), "step": step}
