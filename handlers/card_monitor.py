"""
名片监控
监听 NapCat group_card / group_admin 通知：
- 违规名片审核（链接 / 词库 / 引流话术）
- 名片保护（可选保护名单，被改后自动还原）
- 管理员任免通知
"""

from __future__ import annotations

import logging
import os
import re
from typing import Dict, Optional, Set

logger = logging.getLogger("card_monitor")

# 链接/店铺/扫码特征（关键词匹配）
_LINK_KEYWORD_RE = re.compile(
    r"(https?://|www\.|t\.me/|qq\.com|weixin|wx\.|淘宝|天猫|拼多多|抖音|快手"
    r"|扫码|二维码|vx|加v|加微|微信|扣扣|qq群)",
    re.I,
)

# 裸域名匹配：x.com / x.cn / x.cc / x.top 等
# 左边界：空白/标点/中文字符/行首；右边界：空白/标点/中文字符/行尾
_BARE_DOMAIN_RE = re.compile(
    r"(?:^|(?<=[\s,，;；|｜【\[『\"'(（\u4e00-\u9fa5]))"
    r"([a-zA-Z0-9\u4e00-\u9fa5][-a-zA-Z0-9\u4e00-\u9fa5]*\."
    r"(?:com|cn|cc|top|xyz|net|org|info|io|me|tv|vip|shop|club|site|online|store|app|ink|ltd|group|work|fun|live|pro|icu|tech|art|design|zone|space|market|pub|bid|loan|win|trade|stream|date|click|link|online|life|bar|center|中文网|公司|集团|网络|中国)"
    r"(?:/[a-zA-Z0-9/_.?&=%-]*)?)"
    r"(?:$|(?=[\s,，;；|｜】\]』\"')）\u4e00-\u9fa5]))",
    re.I,
)


def _link_match(card: str) -> bool:
    """检查名片是否包含链接/域名/引流关键词。"""
    return bool(_LINK_KEYWORD_RE.search(card) or _BARE_DOMAIN_RE.search(card))


class CardMonitor:
    def __init__(self):
        try:
            from config import settings
            self.enabled = bool(getattr(settings, "CARD_MONITOR_ENABLED", True))
            self.audit_enabled = bool(getattr(settings, "CARD_AUDIT_ENABLED", True))
            self.link_only = bool(getattr(settings, "CARD_AUDIT_LINK_ONLY", False))
            self.protect_enabled = bool(getattr(settings, "CARD_PROTECT_ENABLED", False))
            self.notify = bool(getattr(settings, "CARD_MONITOR_NOTIFY", True))
            self.admin_notify = bool(getattr(settings, "ADMIN_CHANGE_NOTIFY", True))
            protect_raw = str(getattr(settings, "CARD_PROTECT_LIST", "") or "")
        except Exception:
            self.enabled = self._env_bool("CARD_MONITOR_ENABLED", True)
            self.audit_enabled = self._env_bool("CARD_AUDIT_ENABLED", True)
            self.link_only = self._env_bool("CARD_AUDIT_LINK_ONLY", False)
            self.protect_enabled = self._env_bool("CARD_PROTECT_ENABLED", False)
            self.notify = self._env_bool("CARD_MONITOR_NOTIFY", True)
            self.admin_notify = self._env_bool("ADMIN_CHANGE_NOTIFY", True)
            protect_raw = os.environ.get("CARD_PROTECT_LIST", "")

        # 保护名单: "qq:预设名片" 或 仅 qq（还原为空/旧值）
        # 格式: 123:官方客服,456
        self.protect_map: Dict[int, str] = {}
        for part in re.split(r"[,，\n;；]+", protect_raw):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                qq_s, card = part.split(":", 1)
            elif "：" in part:
                qq_s, card = part.split("：", 1)
            else:
                qq_s, card = part, ""
            if qq_s.strip().isdigit():
                self.protect_map[int(qq_s.strip())] = card.strip()

        # 防止自己改名片触发循环
        self._restore_cooldown: Dict[tuple, float] = {}

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        v = os.environ.get(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def reload(self):
        self.__init__()

    def audit_card(self, card: str) -> dict:
        """
        审核名片。
        返回: {is_bad, reason, mode}
        """
        card = (card or "").strip()
        if not card:
            return {"is_bad": False, "reason": "", "mode": "empty"}

        # 1) 链接/店铺/扫码 — 两种模式都拦
        if _link_match(card):
            # URL 白名单放行
            try:
                from handlers.ad_detector import is_url_whitelisted
                if is_url_whitelisted(card):
                    logger.info(f"[名片监控] URL白名单放行: {card[:60]}")
                    return {"is_bad": False, "reason": "", "mode": "url_whitelisted"}
            except Exception as e:
                logger.debug(f"[名片监控] 处理跳过: {e}")
            return {
                "is_bad": True,
                "reason": "名片含链接/店铺/引流联系方式",
                "mode": "link",
            }

        if self.link_only:
            return {"is_bad": False, "reason": "", "mode": "link_only_pass"}

        if not self.audit_enabled:
            return {"is_bad": False, "reason": "", "mode": "audit_off"}

        # 2) 词库 + 引流关键词
        try:
            from handlers.lexicon_engine import get_lexicon_engine
            lex = get_lexicon_engine()
            if lex.available:
                result = lex.scan(card)
                score = int(result.get("score") or 0)
                cats = {h.get("category") for h in result.get("hits", [])}
                if score >= 40 or cats & {"广告", "引流", "黑产", "色情", "政治", "暴恐", "反动"}:
                    return {
                        "is_bad": True,
                        "reason": "名片含广告/引流/违规话术",
                        "mode": "lexicon",
                    }
        except Exception as e:
            logger.debug(f"[名片监控] 词库扫描跳过: {e}")

        # 3) 简易引流特征：大量数字 + 联系暗示
        if re.search(r"(加我|私我|联系|咨询|接单|代|招|代理)", card) and re.search(r"\d{5,}", card):
            return {
                "is_bad": True,
                "reason": "名片疑似引流广告",
                "mode": "pattern",
            }

        return {"is_bad": False, "reason": "", "mode": "ok"}

    def choose_restore_card(self, user_id: int, card_old: str, card_new: str) -> Optional[str]:
        """
        决定还原目标。
        返回 None 表示不需要还原；返回字符串（可空）表示要 set_group_card。
        """
        # 保护名单优先
        if self.protect_enabled and user_id in self.protect_map:
            preset = self.protect_map[user_id]
            if card_new != preset:
                return preset

        # 违规审核
        audit = self.audit_card(card_new)
        if not audit.get("is_bad"):
            return None

        # 新名片违规：优先还原旧名片；旧名片也违规则清空
        old_audit = self.audit_card(card_old)
        if card_old and not old_audit.get("is_bad"):
            return card_old
        return ""


_monitor: Optional[CardMonitor] = None


def get_card_monitor() -> CardMonitor:
    global _monitor
    if _monitor is None:
        _monitor = CardMonitor()
    return _monitor


async def _restore_card(
    mon: CardMonitor,
    group_id: int,
    user_id: int,
    card_new: str,
    card_old: str = "",
    source: str = "notice",
) -> bool:
    """统一还原逻辑。"""
    import time

    key = (group_id, user_id)
    now = time.time()
    if mon._restore_cooldown.get(key, 0) > now:
        logger.debug(f"[名片监控] 冷却中，跳过 group={group_id} user={user_id}")
        return False

    restore_to = mon.choose_restore_card(user_id, card_old, card_new)
    if restore_to is None:
        return False

    mon._restore_cooldown[key] = now + 15  # 15 秒冷却

    from napcat_bridge import get_napcat_bridge
    napcat = get_napcat_bridge()
    if not napcat.available:
        await napcat.check_available()
    if not napcat.available:
        logger.warning("[名片监控] NapCat 不可用，无法还原名片")
        return False

    ok = await napcat.set_group_card(group_id, user_id, restore_to)
    audit = mon.audit_card(card_new)
    reason = audit.get("reason") or "名片保护还原"
    display_to = restore_to if restore_to else "（清空）"

    logger.info(
        f"[名片监控] source={source} 还原{'成功' if ok else '失败'}: "
        f"user={user_id} {card_new!r} -> {display_to!r} reason={reason}"
    )

    try:
        from handlers.moderation_store import add_violation
        add_violation(
            group_id=group_id, user_id=user_id, user_name=str(user_id),
            vtype="card", score=0, reason=reason,
            content="", action="已还原" if ok else "还原失败",
        )
    except Exception as e:
        logger.debug(f"[名片监控] 记录违规异常: {e}")

    if mon.notify and ok:
        # 不复述违规名片原文，避免二次传播
        msg = (
            f"🛡️ [名片监控] 已还原违规名片\n"
            f"用户：{user_id}\n"
            f"原因：{reason}\n"
            f"处理：已改回安全名片"
        )
        await napcat.send_group_msg(group_id, msg)

    return ok


async def handle_group_card_notice(event: dict) -> bool:
    """
    处理 notice.group_card 事件。
    常见字段: group_id, user_id, card_new, card_old
    """
    try:
        mon = get_card_monitor()
        if not mon.enabled:
            return False

        group_id = int(event.get("group_id") or 0)
        user_id = int(event.get("user_id") or 0)
        card_new = str(event.get("card_new") or event.get("card") or "")
        card_old = str(event.get("card_old") or "")

        if not group_id or not user_id:
            logger.warning(f"[名片监控] 事件字段不完整: {event}")
            return False

        logger.info(
            f"[名片监控] notice group={group_id} user={user_id} "
            f"old={card_old!r} new={card_new!r}"
        )
        return await _restore_card(mon, group_id, user_id, card_new, card_old, source="notice")
    except Exception as e:
        logger.error(f"[名片监控] notice 处理异常: {e}", exc_info=True)
        return False


async def check_sender_card_from_message(event: dict) -> bool:
    """
    从群消息 sender.card 检查名片（兜底方案）。
    很多协议对 group_card 通知不稳定，发言时顺带检查更可靠。
    """
    try:
        mon = get_card_monitor()
        if not mon.enabled or not mon.audit_enabled:
            return False

        group_id = int(event.get("group_id") or 0)
        user_id = int(event.get("user_id") or 0)
        sender = event.get("sender") or {}
        card = str(sender.get("card") or "").strip()
        # 若群名片为空，有些场景用昵称当展示名，不强制审昵称
        if not group_id or not user_id or not card:
            return False

        # 群主跳过（可选：主人自己测试）
        try:
            from config import settings
            owner = str(getattr(settings, "QQ_GROUP_OWNER", "") or "")
            if owner and str(user_id) == owner:
                return False
        except Exception as e:
            logger.debug(f"[名片监控] 读取群主配置异常: {e}")

        audit = mon.audit_card(card)
        if not audit.get("is_bad"):
            return False

        logger.info(
            f"[名片监控] 消息触发审核 group={group_id} user={user_id} "
            f"card={card!r} reason={audit.get('reason')}"
        )
        # 无 card_old 时，违规则清空
        return await _restore_card(mon, group_id, user_id, card, card_old="", source="message")
    except Exception as e:
        logger.error(f"[名片监控] 消息侧审核异常: {e}", exc_info=True)
        return False


async def handle_group_admin_notice(event: dict) -> bool:
    """处理 notice.group_admin 事件。"""
    try:
        mon = get_card_monitor()
        if not mon.enabled or not mon.admin_notify:
            return False

        group_id = int(event.get("group_id") or 0)
        user_id = int(event.get("user_id") or 0)
        sub_type = str(event.get("sub_type") or "")  # set / unset
        if not group_id or not user_id:
            return False

        action = "设为管理员" if sub_type == "set" else "取消管理员" if sub_type == "unset" else f"管理变更({sub_type})"
        logger.info(f"[名片监控] 管理员任免 group={group_id} user={user_id} {action}")

        from napcat_bridge import get_napcat_bridge
        napcat = get_napcat_bridge()
        if not napcat.available:
            await napcat.check_available()
        if napcat.available:
            await napcat.send_group_msg(
                group_id,
                f"ℹ️ [群管理] 用户 {user_id} 已被{action}",
            )
        return True
    except Exception as e:
        logger.error(f"[名片监控] 管理员通知异常: {e}", exc_info=True)
        return False
