"""
群主/通知 QQ 私聊指令
- 误判 / 放行 关键词：加入白名单词
- 禁言QQ 分钟 / 踢QQ：快速处理
- 统计：近24h 违规摘要
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger("owner_commands")


def _is_owner_qq(user_id: int) -> bool:
    try:
        from config import settings
        import os

        owners = set()
        for key in ("QQ_AD_NOTIFY_QQ", "QQ_GROUP_OWNER"):
            # 优先读环境变量（.env 热更新），再回退 settings
            raw = str(os.environ.get(key) or getattr(settings, key, "") or "")
            for part in re.split(r"[,，\s]+", raw):
                part = part.strip()
                if part.isdigit():
                    owners.add(int(part))
        ok = int(user_id) in owners
        if not ok:
            logger.info(f"[群主指令] 非授权QQ: {user_id} owners={owners}")
        return ok
    except Exception as e:
        logger.warning(f"[群主指令] 校验群主失败: {e}")
        return False


def _extract_keyword_from_violation(v: dict) -> str:
    """从违规记录提取适合做白名单的关键词。"""
    content = str(v.get("content") or "").strip()
    if content:
        # URL 优先
        m = re.search(r"https?://[^\s]+", content)
        if m:
            return m.group(0)[:80]
        # 否则取前 20 字（去掉空白）
        compact = re.sub(r"\s+", "", content)
        if len(compact) >= 2:
            return compact[:20]
    reason = str(v.get("reason") or "")
    # 词库命中 类别:词
    m = re.search(r"[:：]([^\s、,，]{2,20})", reason)
    if m:
        return m.group(1)
    return ""


async def handle_owner_private_text(user_id: int, text: str) -> Optional[str]:
    """
    处理群主私聊指令。
    返回回复文案；None 表示不是指令或不处理。
    """
    text = (text or "").strip()
    if not text or not _is_owner_qq(user_id):
        return None

    from handlers.moderation_store import (
        add_access,
        get_recent_violation,
        violation_stats,
        add_violation,
    )

    # 误判：把最近一条违规的原文/关键词加白
    if text in ("误判", "误杀", "放行", "不是广告", "申诉"):
        v = get_recent_violation(within_sec=7200)
        if not v:
            return "近2小时没有违规记录，无法标记误判。\n可直接发：放行 关键词"
        kw = _extract_keyword_from_violation(v)
        if not kw:
            return (
                f"最近违规：用户{v.get('user_id')} score={v.get('score')}\n"
                f"原因：{v.get('reason')}\n"
                f"未能自动提取关键词，请发：放行 关键词"
            )
        add_access(
            scope="whitelist",
            target_type="word",
            target_id=kw,
            group_id=0,  # 全局白名单：误判放行对所有群生效
            note=f"误判申诉 from {user_id} (原群{v.get('group_id')})",
        )
        try:
            add_violation(
                group_id=int(v.get("group_id") or 0),
                user_id=int(v.get("user_id") or 0),
                user_name=str(v.get("user_name") or ""),
                vtype="appeal",
                score=0,
                reason=f"误判放行关键词:{kw}",
                content=kw,
                action="已加全局白名单",
            )
        except Exception:
            pass
        return (
            f"✅ 已标记误判并加入全局白名单\n"
            f"关键词：{kw}\n"
            f"原用户：{v.get('user_name')}({v.get('user_id')})\n"
            f"原原因：{v.get('reason')}\n"
            f"⚠️ 此词已对所有群生效"
        )

    # 放行 关键词
    m = re.match(r"^(?:放行|白名单|加白)\s+(.+)$", text, re.I)
    if m:
        kw = m.group(1).strip()[:80]
        if len(kw) < 2:
            return "关键词太短，至少2个字符"
        add_access(
            scope="whitelist",
            target_type="word",
            target_id=kw,
            group_id=0,
            note=f"手动放行 from {user_id}",
        )
        return f"✅ 已加入全局白名单词：{kw}"

    # 黑名单 关键词
    m = re.match(r"^(?:拉黑词|黑名单词)\s+(.+)$", text, re.I)
    if m:
        kw = m.group(1).strip()[:80]
        if len(kw) < 2:
            return "关键词太短"
        add_access(
            scope="blacklist",
            target_type="word",
            target_id=kw,
            group_id=0,
            note=f"手动拉黑词 from {user_id}",
        )
        return f"✅ 已加入全局黑名单词：{kw}"

    # 禁言 QQ 分钟
    m = re.match(r"^禁言\s*(\d{5,12})\s+(\d{1,4})$", text)
    if m:
        qq = int(m.group(1))
        minutes = int(m.group(2))
        v = get_recent_violation(user_id=qq, within_sec=86400)
        group_id = int((v or {}).get("group_id") or 0)
        if not group_id:
            return "找不到该用户最近违规所在群，请在管理后台操作禁言。"
        from napcat_bridge import get_napcat_bridge

        napcat = get_napcat_bridge()
        if not napcat.available:
            await napcat.check_available()
        if not napcat.available:
            return "NapCat 不可用，无法禁言"
        ok = await napcat.set_group_ban(group_id, qq, minutes * 60)
        return f"{'✅' if ok else '❌'} 禁言 {qq} {minutes}分钟 @群{group_id}"

    # 踢 QQ
    m = re.match(r"^踢\s*(\d{5,12})$", text)
    if m:
        qq = int(m.group(1))
        v = get_recent_violation(user_id=qq, within_sec=86400)
        group_id = int((v or {}).get("group_id") or 0)
        if not group_id:
            return "找不到该用户最近违规所在群。"
        from napcat_bridge import get_napcat_bridge

        napcat = get_napcat_bridge()
        if not napcat.available:
            await napcat.check_available()
        if not napcat.available:
            return "NapCat 不可用，无法踢人"
        ok = await napcat.kick_group_member(group_id, qq)
        return f"{'✅' if ok else '❌'} 踢出 {qq} @群{group_id}"

    # 统计
    if text in ("统计", "今日统计", "违规统计"):
        st = violation_stats(hours=24)
        lines = [f"📊 近24小时违规统计：共 {st.get('total', 0)} 条"]
        for it in (st.get("by_type") or [])[:8]:
            lines.append(f"  · {it.get('vtype')}: {it.get('cnt')}")
        if st.get("top_reasons"):
            lines.append("高频原因：")
            for it in st["top_reasons"][:5]:
                lines.append(f"  · {it.get('cnt')}x {(it.get('reason') or '')[:40]}")
        return "\n".join(lines)

    # 帮助
    if text in ("帮助", "指令", "help", "?"):
        return (
            "🛠️ 群主私聊指令\n"
            "误判 — 把最近一条违规原文加入白名单\n"
            "放行 关键词 — 全局白名单词\n"
            "拉黑词 关键词 — 全局黑名单词\n"
            "禁言QQ 分钟 — 如：禁言123456 10\n"
            "踢QQ — 如：踢123456\n"
            "统计 — 近24h违规摘要"
        )

    return None
