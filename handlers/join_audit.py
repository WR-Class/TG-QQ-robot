"""
入群审核
监听 NapCat 加群申请事件，按关键词/词库自动通过或拒绝。
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

logger = logging.getLogger("join_audit")


def _split_words(raw: str) -> List[str]:
    if not raw:
        return []
    parts = re.split(r"[,，\n|;；]+", raw)
    return [p.strip() for p in parts if p.strip()]


class JoinAuditor:
    def __init__(self):
        try:
            from config import settings
            self.enabled = bool(getattr(settings, "JOIN_AUDIT_ENABLED", True))
            self.default_action = str(getattr(settings, "JOIN_AUDIT_DEFAULT", "approve") or "approve").lower()
            self.approve_words = _split_words(getattr(settings, "JOIN_AUDIT_APPROVE_WORDS", "") or "")
            self.reject_words = _split_words(
                getattr(settings, "JOIN_AUDIT_REJECT_WORDS", "")
                or "广告,兼职,日结,刷单,代刷,跑分,私聊,加微信,加v,引流"
            )
            self.use_lexicon = bool(getattr(settings, "JOIN_AUDIT_USE_LEXICON", True))
            self.notify_group = bool(getattr(settings, "JOIN_AUDIT_NOTIFY_GROUP", True))
            self.reject_reason = str(getattr(settings, "JOIN_AUDIT_REJECT_REASON", "申请信息未通过审核") or "申请信息未通过审核")
        except Exception:
            self.enabled = self._env_bool("JOIN_AUDIT_ENABLED", True)
            self.default_action = (os.environ.get("JOIN_AUDIT_DEFAULT", "approve") or "approve").lower()
            self.approve_words = _split_words(os.environ.get("JOIN_AUDIT_APPROVE_WORDS", ""))
            self.reject_words = _split_words(
                os.environ.get(
                    "JOIN_AUDIT_REJECT_WORDS",
                    "广告,兼职,日结,刷单,代刷,跑分,私聊,加微信,加v,引流",
                )
            )
            self.use_lexicon = self._env_bool("JOIN_AUDIT_USE_LEXICON", True)
            self.notify_group = self._env_bool("JOIN_AUDIT_NOTIFY_GROUP", True)
            self.reject_reason = os.environ.get("JOIN_AUDIT_REJECT_REASON", "申请信息未通过审核")

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        v = os.environ.get(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def reload(self):
        self.__init__()

    def decide(self, comment: str, user_id: int = 0, group_id: int = 0) -> dict:
        if not self.enabled:
            return {"action": "manual", "reason": "入群审核未启用", "matched": ""}

        text = (comment or "").strip()
        text_l = text.lower()

        for w in self.reject_words:
            if w.lower() in text_l:
                return {"action": "reject", "reason": f"命中拒绝词「{w}」", "matched": w}

        if self.use_lexicon and text:
            try:
                from handlers.lexicon_engine import get_lexicon_engine
                lex = get_lexicon_engine()
                if lex.available:
                    result = lex.scan(text)
                    score = result.get("score", 0)
                    if score >= 50 or any(
                        h.get("category") in ("黑产", "色情", "政治", "暴恐", "反动", "广告", "引流")
                        for h in result.get("hits", [])
                    ):
                        return {
                            "action": "reject",
                            "reason": result.get("reason") or f"词库评分 {score}",
                            "matched": ",".join(h.get("word", "") for h in result.get("hits", [])[:3]),
                        }
            except Exception as e:
                logger.debug(f"[入群审核] 词库扫描跳过: {e}")

        for w in self.approve_words:
            if w.lower() in text_l:
                return {"action": "approve", "reason": f"命中通过词「{w}」", "matched": w}

        action = self.default_action if self.default_action in ("approve", "reject", "manual") else "approve"
        return {"action": action, "reason": f"默认动作: {action}", "matched": ""}


_auditor: Optional[JoinAuditor] = None


def get_join_auditor() -> JoinAuditor:
    global _auditor
    if _auditor is None:
        _auditor = JoinAuditor()
    return _auditor


async def handle_group_add_request(event: dict) -> bool:
    """处理 OneBot request.group 事件。"""
    try:
        flag = str(event.get("flag") or "")
        sub_type = str(event.get("sub_type") or "add")
        group_id = int(event.get("group_id") or 0)
        user_id = int(event.get("user_id") or 0)
        comment = str(event.get("comment") or event.get("message") or "")

        if not flag or not group_id:
            logger.warning(f"[入群审核] 事件字段不完整: {event}")
            return False

        auditor = get_join_auditor()
        decision = auditor.decide(comment, user_id=user_id, group_id=group_id)
        action = decision["action"]
        reason = decision["reason"]

        logger.info(
            f"[入群审核] group={group_id} user={user_id} action={action} "
            f"comment={comment[:50]!r} reason={reason}"
        )

        from napcat_bridge import get_napcat_bridge
        napcat = get_napcat_bridge()
        if not napcat.available:
            await napcat.check_available()

        ok = True
        if action == "approve":
            ok = await napcat.set_group_add_request(flag, sub_type=sub_type, approve=True)
        elif action == "reject":
            ok = await napcat.set_group_add_request(
                flag, sub_type=sub_type, approve=False, reason=auditor.reject_reason
            )

        try:
            from handlers.moderation_store import add_violation
            add_violation(
                group_id=group_id, user_id=user_id, user_name=str(user_id),
                vtype="join", score=0, reason=reason,
                content=(comment or "")[:100],
                action={"approve": "通过", "reject": "拒绝", "manual": "待审"}.get(action, action),
            )
        except Exception:
            pass

        if auditor.notify_group and napcat.available:
            status_map = {"approve": "已自动通过", "reject": "已自动拒绝", "manual": "待人工审核"}
            # 不复述完整验证信息中的广告原文，截断展示
            safe_comment = (comment or "（无）")[:40]
            msg = (
                f"🛡️ [入群审核] {status_map.get(action, action)}\n"
                f"申请人：{user_id}\n"
                f"验证信息：{safe_comment}\n"
                f"原因：{reason}"
            )
            await napcat.send_group_msg(group_id, msg)

        return ok
    except Exception as e:
        logger.error(f"[入群审核] 处理异常: {e}", exc_info=True)
        return False
