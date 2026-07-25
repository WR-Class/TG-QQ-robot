"""
防刷屏检测
滑动窗口统计用户在群内的消息频率，超限自动禁言。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger("anti_flood")

# 默认阈值（0 表示关闭该档）
DEFAULT_RATE_PER_SECOND = 5
DEFAULT_RATE_PER_MINUTE = 20
DEFAULT_RATE_PER_HOUR = 240
DEFAULT_MUTE_MINUTES = 10
DEFAULT_REPEAT_WINDOW = 120  # 秒
DEFAULT_REPEAT_LIMIT = 3     # 同一内容重复次数


class AntiFloodGuard:
    def __init__(
        self,
        rate_per_second: int = DEFAULT_RATE_PER_SECOND,
        rate_per_minute: int = DEFAULT_RATE_PER_MINUTE,
        rate_per_hour: int = DEFAULT_RATE_PER_HOUR,
        mute_minutes: int = DEFAULT_MUTE_MINUTES,
        repeat_window: int = DEFAULT_REPEAT_WINDOW,
        repeat_limit: int = DEFAULT_REPEAT_LIMIT,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.rate_per_second = rate_per_second
        self.rate_per_minute = rate_per_minute
        self.rate_per_hour = rate_per_hour
        self.mute_minutes = mute_minutes
        self.repeat_window = repeat_window
        self.repeat_limit = repeat_limit

        # key: (group_id, user_id) -> timestamps
        self._ts: Dict[Tuple[int, int], Deque[float]] = defaultdict(deque)
        # key: (group_id, user_id) -> [(ts, content_hash)]
        self._repeats: Dict[Tuple[int, int], Deque[Tuple[float, str]]] = defaultdict(deque)
        # 禁言冷却：命中后一段时间内不再重复处理
        self._muted_until: Dict[Tuple[int, int], float] = {}

    def configure(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k) and v is not None:
                setattr(self, k, v)

    def _base_from_db(self) -> dict:
        """读取 group_id=0 作为全局默认覆盖。"""
        base = {
            "enabled": self.enabled,
            "rate_per_second": self.rate_per_second,
            "rate_per_minute": self.rate_per_minute,
            "rate_per_hour": self.rate_per_hour,
            "mute_minutes": self.mute_minutes,
            "repeat_window": self.repeat_window,
            "repeat_limit": self.repeat_limit,
        }
        try:
            from handlers.moderation_store import get_group_config
            g0 = get_group_config(0)
            if not g0.get("is_default"):
                cfg = g0.get("config") or {}
                if "flood_enabled" in cfg:
                    base["enabled"] = bool(cfg["flood_enabled"])
                if "flood_per_second" in cfg:
                    base["rate_per_second"] = int(cfg["flood_per_second"])
                if "flood_per_minute" in cfg:
                    base["rate_per_minute"] = int(cfg["flood_per_minute"])
                if "flood_per_hour" in cfg:
                    base["rate_per_hour"] = int(cfg["flood_per_hour"])
                if "flood_mute_minutes" in cfg:
                    base["mute_minutes"] = int(cfg["flood_mute_minutes"])
                if "flood_repeat_window" in cfg:
                    base["repeat_window"] = int(cfg["flood_repeat_window"])
                if "flood_repeat_limit" in cfg:
                    base["repeat_limit"] = int(cfg["flood_repeat_limit"])
        except Exception as e:
            logger.debug(f"[AntiFlood] DB 读取异常: {e}")
        return base

    def _group_limits(self, group_id: int) -> dict:
        """读取群独立阈值，失败则用全局默认。"""
        limits = self._base_from_db()
        try:
            from handlers.moderation_store import get_group_config
            cfg = get_group_config(int(group_id)).get("config") or {}
            if cfg.get("flood_enabled") is False or cfg.get("enabled") is False:
                limits["enabled"] = False
            if "flood_per_second" in cfg:
                limits["rate_per_second"] = int(cfg["flood_per_second"])
            if "flood_per_minute" in cfg:
                limits["rate_per_minute"] = int(cfg["flood_per_minute"])
            if "flood_per_hour" in cfg:
                limits["rate_per_hour"] = int(cfg["flood_per_hour"])
            if "flood_mute_minutes" in cfg:
                limits["mute_minutes"] = int(cfg["flood_mute_minutes"])
            if "flood_repeat_window" in cfg:
                limits["repeat_window"] = int(cfg["flood_repeat_window"])
            if "flood_repeat_limit" in cfg:
                limits["repeat_limit"] = int(cfg["flood_repeat_limit"])
        except Exception as e:
            logger.debug(f"[AntiFlood] DB 读取 flood 配置异常: {e}")

    def snapshot(self) -> dict:
        """供 Web 面板展示当前全局配置与活跃统计。"""
        now = time.time()
        active_users = 0
        for q in self._ts.values():
            if any(now - t <= 60 for t in q):
                active_users += 1
        base = self._base_from_db()
        return {
            "enabled": base["enabled"],
            "rate_per_second": base["rate_per_second"],
            "rate_per_minute": base["rate_per_minute"],
            "rate_per_hour": base["rate_per_hour"],
            "mute_minutes": base["mute_minutes"],
            "repeat_window": base["repeat_window"],
            "repeat_limit": base["repeat_limit"],
            "tracked_keys": len(self._ts),
            "active_users_1m": active_users,
            "muted_cooling": sum(1 for u in self._muted_until.values() if u > now),
        }

    def check(
        self,
        group_id: int,
        user_id: int,
        content: str = "",
        is_admin: bool = False,
    ) -> Optional[dict]:
        """
        检查是否刷屏。
        返回 None 表示正常；返回 dict 表示触发。
        """
        if is_admin or not group_id or not user_id:
            return None

        limits = self._group_limits(group_id)
        if not limits.get("enabled", True):
            return None

        rps = int(limits["rate_per_second"])
        rpm = int(limits["rate_per_minute"])
        rph = int(limits["rate_per_hour"])
        mute_minutes = int(limits["mute_minutes"])
        repeat_window = int(limits["repeat_window"])
        repeat_limit = int(limits["repeat_limit"])

        now = time.time()
        key = (int(group_id), int(user_id))

        # 冷却期内直接跳过
        until = self._muted_until.get(key, 0)
        if until > now:
            return None

        # 清理并记录时间戳
        q = self._ts[key]
        q.append(now)
        while q and now - q[0] > 3600:
            q.popleft()

        c1 = sum(1 for t in q if now - t <= 1)
        c60 = sum(1 for t in q if now - t <= 60)
        c3600 = len(q)

        # 速率检查
        if rps > 0 and c1 > rps:
            return self._trigger(key, now, "rate", f"1秒内发送 {c1} 条(限{rps})", c1, c60, c3600, mute_minutes)
        if rpm > 0 and c60 > rpm:
            return self._trigger(key, now, "rate", f"1分钟内发送 {c60} 条(限{rpm})", c1, c60, c3600, mute_minutes)
        if rph > 0 and c3600 > rph:
            return self._trigger(key, now, "rate", f"1小时内发送 {c3600} 条(限{rph})", c1, c60, c3600, mute_minutes)

        # 重复消息检查
        content = (content or "").strip()
        if content and repeat_limit > 0:
            if len(content) >= 4:
                rq = self._repeats[key]
                h = content[:200]
                rq.append((now, h))
                while rq and now - rq[0][0] > repeat_window:
                    rq.popleft()
                same = sum(1 for t, c in rq if c == h)
                if same >= repeat_limit:
                    return self._trigger(
                        key, now, "repeat",
                        f"{repeat_window}秒内重复发送 {same} 次相同内容",
                        c1, c60, c3600, mute_minutes,
                    )

        return None

    def _trigger(self, key, now, typ, reason, c1, c60, c3600, mute_minutes=None) -> dict:
        minutes = self.mute_minutes if mute_minutes is None else mute_minutes
        mute_sec = max(60, int(minutes) * 60)
        self._muted_until[key] = now + mute_sec
        # 清理窗口，避免连续触发
        self._ts[key].clear()
        self._repeats[key].clear()
        logger.warning(
            f"[防刷屏] group={key[0]} user={key[1]} type={typ} reason={reason}"
        )
        return {
            "type": typ,
            "reason": reason,
            "mute_seconds": mute_sec,
            "counts": {"per_second": c1, "per_minute": c60, "per_hour": c3600},
        }


_guard: Optional[AntiFloodGuard] = None


def get_anti_flood() -> AntiFloodGuard:
    global _guard
    if _guard is None:
        _guard = AntiFloodGuard()
    return _guard
