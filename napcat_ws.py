"""
NapCat WebSocket 客户端
监听 NapCat 的 WebSocket 推送事件：
1. 群消息缓存（用于精确撤回）
2. 加群申请（入群审核）
3. 图片消息（OCR 广告审核）
4. 名片变更 / 管理员任免（名片监控）
"""

import asyncio
import json
import logging
import aiohttp
import re
import time
from typing import Optional, List, Dict

logger = logging.getLogger("napcat_ws")

# 私聊上下文缓存：user_id -> [{"role":"user"/"assistant","content":text}, ...]
_private_chat_history: Dict[int, List[Dict[str, str]]] = {}
_MAX_PRIVATE_HISTORY = 10  # 每个用户保留最近 10 轮对话

# FAQ 反馈追踪: {(group_id, user_id): {"faq_id": int, "timestamp": float}}
_pending_faq_feedback: Dict[tuple, dict] = {}
_FAQ_FEEDBACK_EXPIRE = 300  # FAQ 反馈窗口 5 分钟

# 全局缓存: group_num -> [(napcat_msg_id, user_qq, content_text, timestamp), ...]
_msg_cache = {}
# 缓存过期时间（秒）
_CACHE_TTL = 300
# 图片审核去重: message_id -> ts
_ocr_seen = {}
# OCR延迟检查队列: message_id -> {data, timestamp, ...}
_pending_ocr_checks: dict = {}
# OCR正在延迟观察的消息ID集合
_ocr_pending_ids: set = set()

# 活动时间跟踪：user_id/group_id -> 最后活跃时间戳（用于周期性清理）
_private_activity_ts: Dict[int, float] = {}
_group_activity_ts: Dict[int, float] = {}
# 最后一次收到 NapCat WS 消息的时间戳（用于检测僵死连接）
_last_ws_msg_ts: float = time.time()
# 内存清理配置
_CLEANUP_INTERVAL = 300  # 每 5 分钟清理一次
_CLEANUP_MAX_IDLE = 1800  # 30 分钟未活跃的缓存条目将被清理

# NapCat 小号 QQ 号（启动时从 config 加载）
_napcat_self_qq = 0

# QQ 系统机器人 QQ 号集合（这些账号的 sender.role 不是 "bot"，需要单独屏蔽）
# Q群管家: 2854196310（自动发欢迎消息、群规提醒等）
SYSTEM_BOT_QQS = {2854196310, 2854196320}

# 群聊 AI 上下文缓存：group_id -> [{"role":"user"/"assistant","content":text}, ...]
_group_chat_history: Dict[int, List[Dict[str, str]]] = {}
_MAX_GROUP_HISTORY = 5

# 自然语言指令正则
_RE_NL_CMD = re.compile(
    r"(?:重新|重|再)\s*(?:检测|检查|扫描|扫|查|看)\s*(?:.*?)(?:广告|违规|垃圾|骚扰)?"
    r"|(?:查|看|检测|检查|扫描)\s*(?:.*?)(?:广告|违规)",
    re.I,
)

# 成员统计意图正则
_RE_STATS_CMD = re.compile(
    r"(?:统计|人数|多少人|有多少|群成员统计|成员统计|群统计)",
    re.I,
)

# ---- 操作日志快捷辅助 ----

def _log_op(*, platform="qq", group_id=0, user_id=0, user_name="",
            action_type="", detail="", operator="", **kw):
    try:
        from handlers.moderation_store import add_operation
        add_operation(
            platform=platform, group_id=group_id, user_id=user_id,
            user_name=user_name, action_type=action_type,
            detail=detail, operator=operator,
        )
    except Exception as e:
        logger.warning(f"[操作日志] 写入失败: {e}")


class NapCatWSClient:
    """NapCat WebSocket 正向连接客户端"""

    def __init__(self, ws_url: str, access_token: str = ""):
        self.ws_url = ws_url
        self.access_token = access_token
        self._running = False
        self._session = None
        self._ws = None

    async def start(self):
        """启动 WebSocket 连接"""
        self._running = True
        self._session = aiohttp.ClientSession()
        _reconnect_delay = 2  # 初始重连延迟
        while self._running:
            try:
                url = self.ws_url
                if self.access_token:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}access_token={self.access_token}"
                # 隐藏 URL 中的 access_token 参数
                safe_url = url
                if self.access_token:
                    safe_url = url.replace(f"access_token={self.access_token}", "access_token=***")
                logger.info(f"[NapCat WS] 连接中: {safe_url[:80]}...")
                async with self._session.ws_connect(url) as ws:
                    self._ws = ws
                    logger.info("[NapCat WS] 已连接")
                    _reconnect_delay = 2  # 连接成功，重置退避
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=60)
                            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                             aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                logger.warning(f"[NapCat WS] 收到关闭帧: type={msg.type}({msg.type.name})，准备重连")
                                break
                            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                                await self._on_message(msg)
                            # 非消息帧（如 PING/PONG）忽略
                            continue
                        except asyncio.TimeoutError:
                            # 60秒无任何帧，连接已死
                            logger.warning(
                                f"[NapCat WS] 60s 未收到任何帧，连接已死，断开重连"
                            )
                            break
                # 内层 break 后到这里，等待后重连
                await asyncio.sleep(_reconnect_delay)
                _reconnect_delay = min(_reconnect_delay * 2, 30)  # 指数退避，最大30s
            except asyncio.CancelledError:
                logger.info("[NapCat WS] 任务被取消，停止")
                break
            except Exception as e:
                logger.warning(f"[NapCat WS] 连接异常: {e}, 5秒后重连...")
                await asyncio.sleep(5)
        if self._session:
            await self._session.close()

    def stop(self):
        self._running = False

    async def _on_message(self, ws_msg):
        """处理 WebSocket 消息"""
        try:
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(ws_msg.data)
            elif ws_msg.type == aiohttp.WSMsgType.BINARY:
                data = json.loads(ws_msg.data.decode("utf-8"))
            else:
                return

            post_type = data.get("post_type", "")
            message_type = data.get("message_type", "")
            # 更新最后收到 WS 消息的时间戳（仅非心跳事件，用于僵死连接检测）
            # meta_event 是心跳/生命周期事件，不反映 QQ 连接是否真正活跃
            if post_type != "meta_event":
                global _last_ws_msg_ts
                _last_ws_msg_ts = time.time()
            # 跳过心跳/元事件，其余消息类打日志便于排查
            if post_type and post_type not in ("meta_event",):
                if post_type == "message" or message_type:
                    logger.info(
                        f"[NapCat WS] 事件 post={post_type} type={message_type} "
                        f"user={data.get('user_id')} group={data.get('group_id')}"
                    )

            # 群消息：缓存 + 名片检查 + 图片 OCR
            if post_type == "message" and message_type == "group":
                # 过滤其他机器人的消息（sender.role == "bot" 或已知系统机器人QQ号）
                _grp_sender = data.get("sender") or {}
                _grp_user_id = data.get("user_id", 0)
                if str(_grp_sender.get("role", "")).lower() == "bot" or _grp_user_id in SYSTEM_BOT_QQS:
                    return
                is_at_msg = await self._handle_group_message(data)
                # @消息不再走名片检查和OCR检测
                if not is_at_msg:
                    await self._handle_sender_card_check(data)
                    await self._handle_group_image_ocr(data)
                return

            # 私聊：群主误判/放行等指令（兼容 private / friend）
            if post_type == "message" and message_type in ("private", "friend"):
                await self._handle_private_message(data)
                return
            # 部分协议用 notice 或直接无 message_type
            if post_type == "message" and not data.get("group_id") and data.get("user_id"):
                await self._handle_private_message(data)
                return

            # 加群申请
            if post_type == "request" and data.get("request_type") == "group":
                await self._handle_group_request(data)
                return

            # 群通知：名片变更 / 管理员任免 / 入群欢迎
            if post_type == "notice":
                notice_type = data.get("notice_type", "")
                if notice_type == "group_card":
                    await self._handle_group_card(data)
                    return
                if notice_type == "group_admin":
                    await self._handle_group_admin(data)
                    return
                if notice_type in ("group_increase", "group_member_increase"):
                    await self._handle_group_increase(data)
                    return

        except Exception as e:
            logger.warning(f"[NapCat WS] 解析消息异常: {e}", exc_info=True)

    async def _handle_group_message(self, data: dict) -> bool:
        """处理群消息事件（缓存 + @指令 + AI 回复）。返回 True 表示是@NapCat小号的消息。"""
        global _napcat_self_qq
        group_id = data.get("group_id", 0)
        message_id = data.get("message_id", 0)
        # 更新群活跃时间戳（用于内存清理）
        _group_activity_ts[group_id] = time.time()
        user_id = data.get("user_id", 0)
        raw_msg = data.get("raw_message", "") or ""
        msg_time = time.time()  # 统一用本地时间，避免服务器时间偏差

        text = ""
        segs = data.get("message", [])
        if isinstance(segs, list):
            for seg in segs:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    text += seg.get("data", {}).get("text", "")
        if not text:
            text = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg)

        # ===== 过滤其他机器人的消息（不做检测、不回复、不缓存）=====
        sender_raw = data.get("sender") or {}
        if str(sender_raw.get("role", "")).lower() == "bot" or user_id in SYSTEM_BOT_QQS:
            return False

        # ===== 自动学习：首次发现群时创建配置 =====
        if group_id:
            try:
                from handlers.moderation_store import ensure_group_config
                ensure_group_config(int(group_id))
            except Exception as e:
                logger.debug(f"[NapCat WS] 自动学习群配置失败: {e}")

        # ===== @NapCat 小号检测 =====
        # 1. 获取/缓存 NapCat 小号 QQ 号
        if not _napcat_self_qq:
            try:
                from napcat_bridge import get_napcat_bridge
                napcat = get_napcat_bridge()
                if not napcat.available:
                    await napcat.check_available()
                if napcat.available:
                    import aiohttp
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                        async with session.get(
                            f"{napcat.base_url}/get_login_info",
                            headers=napcat._headers(),
                        ) as resp:
                            if resp.status == 200:
                                info = await resp.json()
                                if info.get("status") == "ok":
                                    _napcat_self_qq = int(info.get("data", {}).get("user_id", 0) or 0)
                                    logger.info(f"[NapCat WS] 获取小号 QQ: {_napcat_self_qq}")
            except Exception as e:
                logger.warning(f"[NapCat WS] 获取小号 QQ 失败: {e}")

        # 2. 检查消息中是否 @ 了 NapCat 小号
        at_self = False
        if _napcat_self_qq:
            at_pattern = f"[CQ:at,qq={_napcat_self_qq}]"
            if at_pattern in raw_msg:
                at_self = True
            else:
                # 也检查 message 数组中的 at segment
                if isinstance(segs, list):
                    for seg in segs:
                        if isinstance(seg, dict) and seg.get("type") == "at":
                            if str(seg.get("data", {}).get("qq", "")) == str(_napcat_self_qq):
                                at_self = True
                                break

        # 提取纯净文本（去掉所有 CQ 码）
        pure_text = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg).strip()
        sender = data.get("sender") or {}
        nick = sender.get("card") or sender.get("nickname") or str(user_id)

        # ===== 斜杠指令：无需 @ 小号，消息以 / 开头即触发 =====
        cmd_handled = False
        if pure_text.startswith("/"):
            try:
                from handlers.qq_group_manager import handle_group_command
                cmd_handled = await handle_group_command(pure_text, user_id, group_id, nick)
                if cmd_handled:
                    logger.info(f"[NapCat WS] 斜杠指令已处理: {pure_text[:60]}")
            except Exception as e:
                logger.warning(f"[NapCat WS] 斜杠指令异常: {e}")

        # ===== 消息缓存（无论是否 @ 都执行）=====
        if group_id not in _msg_cache:
            _msg_cache[group_id] = []
        entry = {
            "msg_id": message_id,
            "user_id": user_id,
            "text": text.strip(),
            "time": msg_time,
        }
        _msg_cache[group_id].append(entry)
        logger.debug(
            f"[NapCat WS] 缓存消息: group={group_id} msg_id={message_id} "
            f"user={user_id} text={text[:30]}"
        )
        await self._cleanup_cache(group_id)

        # ===== 广告检测优先于 FAQ（非斜杠指令消息）=====
        is_ad = False
        if not cmd_handled and text.strip():
            is_ad = await self._check_text_ad(data, text.strip(), group_id, user_id, message_id)

        # ===== FAQ 自动问答匹配（非广告、非斜杠指令消息）=====
        faq_handled = False
        if not is_ad and not cmd_handled and text.strip():
            try:
                from handlers.semantic_faq import match_faq_async

                faq_hit = await match_faq_async(text.strip(), group_id)
                if faq_hit:
                    from napcat_bridge import get_napcat_bridge

                    napcat = get_napcat_bridge()
                    if not napcat.available:
                        await napcat.check_available()
                    if napcat.available:
                        reply = faq_hit["answer"]
                        # 追加 FAQ 反馈提示
                        faq_id = faq_hit.get("id", 0)
                        reply_with_fb = f"{reply}\n\n💬 回复「👍有用」或「👎无用」来反馈此回答"
                        await napcat.send_group_msg(group_id, reply_with_fb)
                        # 记录待反馈状态（触发 FAQ 的用户可以在 5 分钟内反馈）
                        if faq_id:
                            _pending_faq_feedback[(group_id, user_id)] = {
                                "faq_id": faq_id,
                                "timestamp": time.time(),
                            }
                        logger.info(
                            f"[NapCat WS] FAQ 回复: group={group_id} "
                            f"keyword={faq_hit['keyword']} reply={reply[:50]}"
                        )
                        _log_op(
                            platform="qq", group_id=group_id, user_id=user_id,
                            user_name=nick, action_type="faq",
                            detail=f"FAQ自动回复: {faq_hit['keyword']} -> {reply[:60]}",
                            operator="auto_faq",
                        )
                    faq_handled = True
            except Exception as e:
                logger.warning(f"[NapCat WS] FAQ 匹配异常: {e}")

        # ===== FAQ 反馈检测 =====
        fb_key = (group_id, user_id)
        if fb_key in _pending_faq_feedback:
            fb_data = _pending_faq_feedback[fb_key]
            if time.time() - fb_data["timestamp"] > _FAQ_FEEDBACK_EXPIRE:
                del _pending_faq_feedback[fb_key]
            else:
                t = text.strip()
                if any(kw in t for kw in ("👍有用", "有用", "👍", "好的", "解决了", "正确")):
                    try:
                        from handlers.moderation_store import add_faq_feedback
                        add_faq_feedback(
                            faq_id=fb_data["faq_id"],
                            feedback="useful",
                            user_id=user_id, user_name=nick, group_id=group_id,
                        )
                        logger.info(f"[NapCat WS] FAQ反馈 useful: faq_id={fb_data['faq_id']} user={user_id}")
                    except Exception as e:
                        logger.warning(f"[NapCat WS] FAQ useful 反馈记录失败: {e}")
                    del _pending_faq_feedback[fb_key]
                elif any(kw in t for kw in ("👎无用", "无用", "👎", "不对", "错误", "没帮助")):
                    try:
                        from handlers.moderation_store import add_faq_feedback
                        add_faq_feedback(
                            faq_id=fb_data["faq_id"],
                            feedback="useless",
                            user_id=user_id, user_name=nick, group_id=group_id,
                        )
                        logger.info(f"[NapCat WS] FAQ反馈 useless: faq_id={fb_data['faq_id']} user={user_id}")
                    except Exception as e:
                        logger.warning(f"[NapCat WS] FAQ useless 反馈记录失败: {e}")
                    del _pending_faq_feedback[fb_key]

        # 3. 如果 @ 了小号，处理 AI 聊天 / 自然语言指令 / 统计意图（广告消息不回复）
        if at_self and not cmd_handled and not is_ad:
            try:
                nl_reply = await self._handle_nl_command_in_ws(pure_text, user_id, group_id)
                if nl_reply:
                    from napcat_bridge import get_napcat_bridge
                    napcat = get_napcat_bridge()
                    if not napcat.available:
                        await napcat.check_available()
                    if napcat.available:
                        await napcat.send_group_msg(group_id, nl_reply)
                        logger.info(f"[NapCat WS] @自然语言指令已回复: {nl_reply[:50]}")
                    cmd_handled = True
            except Exception as e:
                logger.warning(f"[NapCat WS] @消息自然语言指令异常: {e}")

            # 5.5 检测成员统计意图（例如 "群成员统计 现在有多少人 不计算机器人"）
            if not cmd_handled and _RE_STATS_CMD.search(pure_text):
                try:
                    from handlers.qq_group_manager import handle_group_command
                    # 调用 /统计 命令来处理
                    stats_handled = await handle_group_command("/统计", user_id, group_id, nick)
                    if stats_handled:
                        cmd_handled = True
                        logger.info(f"[NapCat WS] @成员统计已回复")
                except Exception as e:
                    logger.warning(f"[NapCat WS] @消息统计意图异常: {e}")

            # 6. 不是指令，走 AI 聊天回复
            if not cmd_handled:
                try:
                    ai_reply = await self._handle_group_ai_chat(pure_text, group_id)
                    if ai_reply:
                        from napcat_bridge import get_napcat_bridge
                        napcat = get_napcat_bridge()
                        if not napcat.available:
                            await napcat.check_available()
                        if napcat.available:
                            await napcat.send_group_msg(group_id, ai_reply)
                            logger.info(f"[NapCat WS] @AI回复: {ai_reply[:50]}")
                except Exception as e:
                    logger.warning(f"[NapCat WS] @AI聊天异常: {e}")

        # @消息处理完成后返回 True，不再走后续的名片检查和 OCR
        return bool(at_self)

    async def _handle_nl_command_in_ws(self, text: str, sender_qq: int, group_num: int) -> str | None:
        """处理自然语言指令（NapCat WS 版本），返回回复文本，非指令返回 None"""
        if not _RE_NL_CMD.search(text):
            return None

        # 从消息中提取待检测内容（去掉指令前缀后的剩余部分）
        cleaned = _RE_NL_CMD.sub("", text).strip()

        # ===== 场景1：要求扫描群消息历史 =====
        want_group_scan = any(kw in text for kw in ["群里", "群里的", "群内", "群中", "刚刚", "最近", "消息"])
        if want_group_scan:
            return await self._scan_group_messages_in_ws(group_num)

        # ===== 场景2：检测指定文本内容 =====
        if not cleaned:
            return (
                "[群管] 请提供要检测的文本内容，例如：\n"
                "  @我 重新检测 兼职日结加微信xxx"
            )

        logger.info(f"[NapCat WS] 自然语言指令: 检测内容={cleaned[:80]}")
        try:
            # 黑名单关键词检测
            from handlers.moderation_store import match_blacklist_words, match_whitelist_words
            blacklisted = match_blacklist_words(cleaned, group_num)
            whitelisted = match_whitelist_words(cleaned, group_num)

            if whitelisted:
                return (
                    f"[群管] 广告检测结果\n"
                    f"  内容: {cleaned[:60]}\n"
                    f"  判定: 安全 (命中白名单关键词)"
                )
            if blacklisted:
                return (
                    f"[群管] 广告检测结果\n"
                    f"  内容: {cleaned[:60]}\n"
                    f"  判定: 广告 (命中黑名单关键词: {'/'.join(blacklisted[:3])})"
                )
            # 简单规则检测
            ad_hints = ["兼职", "日结", "刷单", "代刷", "跑分", "加微信", "加vx",
                        "加v", "引流", "躺赚", "暴富", "稳赚", "代理", "拉新",
                        "免费.*送", "0元购", "赚钱"]
            matched_hints = [h for h in ad_hints if re.search(h, cleaned)]
            if matched_hints:
                return (
                    f"[群管] 广告检测结果\n"
                    f"  内容: {cleaned[:60]}\n"
                    f"  判定: 疑似广告 (命中规则: {'/'.join(matched_hints[:3])})"
                )
            return (
                f"[群管] 广告检测结果\n"
                f"  内容: {cleaned[:60]}\n"
                f"  判定: 非广告 (未触发检测规则)"
            )
        except Exception as e:
            logger.error(f"[NapCat WS] 自然语言指令异常: {e}")
            return f"[群管] 检测失败: {e}"

    async def _scan_group_messages_in_ws(self, group_num: int) -> str:
        """扫描群消息缓存，检查是否有广告内容（NapCat WS 版本）"""
        try:
            from handlers.ad_detector import ai_detect_ad
        except ImportError:
            return "[群管] 广告检测模块不可用"

        try:
            from handlers.moderation_store import match_blacklist_words
        except ImportError:
            match_blacklist_words = None

        entries = _msg_cache.get(group_num, [])
        if not entries:
            return (
                "[群管] 群消息广告检测结果\n"
                "  群消息缓存为空，暂无可扫描的消息\n"
                "  提示: Bot 上线后会缓存群消息，请稍后再试\n"
                "  或者 @我 + 具体文本进行检测"
            )

        now = time.time()
        scan_window = 300  # 扫描最近 5 分钟的缓存
        recent = [e for e in entries if now - float(e.get("time", 0) or 0) < scan_window]
        if not recent:
            recent = entries[-20:]  # 回退到最近 20 条

        ads_found = []
        scanned = 0
        for entry in reversed(recent):
            e_text = (entry.get("text") or "").strip()
            e_user = entry.get("user_id", 0)
            if not e_text or len(e_text) < 5:
                continue
            # 跳过自身消息（NapCat 小号）
            if _napcat_self_qq and e_user == _napcat_self_qq:
                continue
            scanned += 1

            # 先查黑名单
            if match_blacklist_words:
                bl = match_blacklist_words(e_text, group_num)
                if bl:
                    ads_found.append(f"  QQ:{e_user} -> 命中黑名单: {bl[0]}")
                    continue

            # AI 检测
            result = await ai_detect_ad(e_text, "", "", group_id=group_num)
            score = result.get("score", 0)
            if score >= 50:
                preview = e_text[:30]
                ads_found.append(f"  QQ:{e_user} -> 评分{score}: {preview}")

        if ads_found:
            lines = ["[群管] 群消息广告检测结果\n"]
            lines.append(f"  共扫描 {scanned} 条消息，发现 {len(ads_found)} 条可疑:")
            lines.extend(ads_found[:10])
            if len(ads_found) > 10:
                lines.append(f"  ... 及另外 {len(ads_found) - 10} 条")
            return "\n".join(lines)
        else:
            return (
                f"[群管] 群消息广告检测结果\n"
                f"  共扫描 {scanned} 条消息，未发现广告内容"
            )

    async def _handle_group_ai_chat(self, text: str, group_id: int) -> str:
        """群聊 AI 聊天回复（@NapCat 小号时触发）"""
        try:
            from config import settings

            api_key = str(getattr(settings, "AI_API_KEY", "") or "")
            if not api_key:
                return "[群管] AI 功能未配置 API Key"

            import datetime
            now = datetime.datetime.now()
            weekday_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            wd = weekday_en[now.weekday()]

            system_prompt = (
                f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M:%S')}（北京时间）{wd}\n"
                f"你是群管理助手。用中文简洁回答。\n"
                f"【重要规则】\n"
                f"1. 你具备联网搜索、获取网页内容和查询天气的能力，可以获取实时信息。\n"
                f"2. 当用户询问任何城市的天气（如'北京天气''明天青岛天气'）时，必须使用 get_weather 工具查询，禁止用 web_search 查天气。\n"
                f"3. 搜索实时信息（如新闻、热点）时，必须使用当前具体日期（{now.strftime('%Y年%m月%d日')}）替换「今天」「昨天」等模糊词汇作为搜索关键词，否则搜不到结果。\n"
                f"4. 当你调用了工具并收到结果后，必须基于工具返回的内容回答用户问题。\n"
                f"5. 绝对不要说「我无法提供实时信息」「我的知识库截止于」「作为AI我无法」之类的话——因为你已经获取到了实时数据。\n"
                f"6. 如果搜索结果为空，说明搜索词不合适，主动告诉用户并换种说法重试。\n"
                f"7. 日常闲聊正常交流，群管理问题引导用户用指令。\n"
                f"8. 回复要简短精炼，不要长篇大论。"
            )

            # 使用共享 AI 工具调用模块
            from handlers.ai_tools import ai_chat_with_tools

            # 构建消息列表（带群聊上下文）
            history = _group_chat_history.setdefault(group_id, [])
            chat_messages = list(history) + [{"role": "user", "content": text}]

            result = await ai_chat_with_tools(
                chat_messages,
                system_prompt=system_prompt,
                max_tool_rounds=2,
            )

            reply = result.get("content", "").strip()

            # 缓存群聊上下文
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            if len(history) > _MAX_GROUP_HISTORY * 2:
                _group_chat_history[group_id] = history[-_MAX_GROUP_HISTORY * 2:]

            return reply

        except Exception as e:
            logger.warning(f"[NapCat WS] 群聊 AI 失败: {e}")
            return ""

    async def _try_local_query(self, text: str) -> Optional[str]:
        """
        尝试用本地数据回答常见问题，避免浪费 AI API。
        返回 None 表示无法处理，需走 AI。
        """
        t = text.strip()
        # 时间/日期类
        if any(kw in t for kw in ("几点了", "现在时间", "当前时间", "现在几点", "今天几号", "今天星期", "日期")):
            import datetime
            now = datetime.datetime.now()
            weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            wd = weekday_cn[now.weekday()]
            return f"现在是 {now.strftime('%Y年%m月%d日')} {wd} {now.strftime('%H:%M:%S')}（北京时间）"
        # 违规统计类
        if any(kw in t for kw in ("违规统计", "今天统计", "今日统计", "群里今天", "群统计", "消息数", "消息数量")):
            try:
                from handlers.moderation_store import violation_stats
                st = violation_stats(hours=24)
            except Exception:
                return None
            total = st.get("total", 0)
            if total == 0:
                base = "今天群里暂无违规记录，一片祥和 🎉"
            else:
                by_type_parts = [f"{x.get('vtype')}: {x.get('cnt')}次" for x in (st.get("by_type") or [])[:5]]
                top_reason_parts = [f"「{x.get('reason','-')[:30]}」×{x.get('cnt')}" for x in (st.get("top_reasons") or [])[:3]]
                parts = [f"今日违规总量：{total} 条"]
                if by_type_parts:
                    parts.append("类型：" + "  ".join(by_type_parts))
                if top_reason_parts:
                    parts.append("高频原因：" + "  ".join(top_reason_parts))
                base = "\n".join(parts)
            return (
                f"📊 **群违规统计**\n"
                f"{base}\n\n"
                f"注意：我只统计被检测到的违规/广告，不是总消息数（QQ不对外暴露接口）。"
            )
        return None

    async def _execute_weather(self, city: str, date: str = "") -> str:
        """工具函数：查询天气。支持实时和未来1-3天预报。返回纯文本结果。"""
        import urllib.parse
        from datetime import datetime

        city = (city or "").strip()
        if not city:
            return "error: 城市名为空"

        # 解析日期参数
        date_str = (date or "").strip()
        date_offset = 0  # 0=今天, 1=明天, ...
        date_label = "今天"

        if not date_str or date_str == "今天":
            date_offset = 0
            date_label = "今天"
        else:
            date_map = {"今天": 0, "明天": 1, "后天": 2, "3天后": 3}
            if date_str in date_map:
                date_offset = date_map[date_str]
                date_label = date_str
            else:
                # 尝试解析具体日期格式
                parsed = False
                for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
                    try:
                        target = datetime.strptime(date_str, fmt)
                        target = target.replace(hour=0, minute=0, second=0, microsecond=0)
                        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                        delta = (target - today).days
                        if delta < 0:
                            return f"error: {date_str} 是过去的日期，无法查询历史天气"
                        if delta > 3:
                            return f"error: {date_str} 超出3天预报范围，wttr.in 仅支持未来3天"
                        date_offset = delta
                        label_map = {0: "今天", 1: "明天", 2: "后天", 3: "3天后"}
                        date_label = label_map.get(delta, date_str)
                        parsed = True
                        break
                    except ValueError:
                        continue
                if not parsed:
                    # 兜底：检查是否包含中文关键词
                    for k, v in date_map.items():
                        if k in date_str:
                            date_offset = v
                            date_label = k
                            parsed = True
                            break
                    if not parsed:
                        return f"error: 不支持的日期参数 '{date}'，请使用：今天、明天、后天、3天后，或具体日期如 2026-07-26"

        try:
            encoded = urllib.parse.quote(city)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(
                    f"https://wttr.in/{encoded}?format=j1&lang=zh"
                ) as resp:
                    if resp.status != 200:
                        return f"error: 天气API返回 {resp.status}"
                    data = await resp.json()

            area = data.get("nearest_area") or [{}]
            area_name = (area[0].get("areaName") or [{}])[0].get("value", city)

            if date_offset == 0:
                # 实时天气
                cc = data.get("current_condition") or [{}]
                cc = cc[0]
                temp = cc.get("temp_C", "?")
                feels = cc.get("FeelsLikeC", "?")
                humidity = cc.get("humidity", "?")
                wind = cc.get("windspeedKmph", "?")
                wind_dir = cc.get("winddir16Point", "?")
                desc = (cc.get("weatherDesc") or [{}])[0].get("value", "?")
                visibility = cc.get("visibility", "?")
                pressure = cc.get("pressure", "?")
                return (
                    f"【{area_name} {date_label}天气】\n"
                    f"天气：{desc}\n"
                    f"气温：{temp}°C（体感 {feels}°C）\n"
                    f"湿度：{humidity}%\n"
                    f"风速：{wind_dir} {wind}km/h\n"
                    f"能见度：{visibility}km\n"
                    f"气压：{pressure}hPa"
                )
            else:
                # 预报天气
                forecasts = data.get("weather") or []
                if date_offset >= len(forecasts):
                    return f"error: 暂不支持查询{date_label}的天气，仅支持未来3天内的预报"
                fc = forecasts[date_offset]
                avg_temp = fc.get("avgtempC", "?")
                max_temp = fc.get("maxtempC", "?")
                min_temp = fc.get("mintempC", "?")

                # 取白天气段（06:00-18:00）的天气描述
                hourly = fc.get("hourly") or []
                day_desc = "?"
                day_humidity = "?"
                day_wind = "?"
                day_wind_dir = "?"
                for h in hourly:
                    hour = int(h.get("time", "0"))
                    if 600 <= hour <= 1200:
                        desc_val = (h.get("weatherDesc") or [{}])[0].get("value", "")
                        if desc_val and desc_val != "?":
                            day_desc = desc_val.strip()
                            day_humidity = h.get("humidity", day_humidity)
                            day_wind = h.get("windspeedKmph", day_wind)
                            day_wind_dir = h.get("winddir16Point", day_wind_dir)
                        break
                # 如果上午没有，取任意一个
                if day_desc == "?":
                    for h in hourly:
                        desc_val = (h.get("weatherDesc") or [{}])[0].get("value", "")
                        if desc_val and desc_val != "?":
                            day_desc = desc_val.strip()
                            day_humidity = h.get("humidity", day_humidity)
                            day_wind = h.get("windspeedKmph", day_wind)
                            day_wind_dir = h.get("winddir16Point", day_wind_dir)
                            break

                # 降雨概率（取最大值）
                rain_chances = [int(h.get("chanceofrain", 0)) for h in hourly if h.get("chanceofrain")]
                max_rain = max(rain_chances) if rain_chances else 0

                fc_date = fc.get("date", "")
                return (
                    f"【{area_name} {date_label}（{fc_date}）天气预报】\n"
                    f"天气：{day_desc}\n"
                    f"气温：{min_temp}°C ~ {max_temp}°C（平均 {avg_temp}°C）\n"
                    f"湿度：{day_humidity}%\n"
                    f"风速：{day_wind_dir} {day_wind}km/h\n"
                    f"降雨概率：最高 {max_rain}%"
                )
        except Exception as e:
            return f"error: 天气查询失败: {e}"

    async def _fetch_url_text(self, url: str, timeout: int = 12, accept_202: bool = False) -> str:
        """通用URL抓取，返回原始HTML文本。accept_202=True 时也接受202状态码（DDG反爬）。"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200 and not (accept_202 and resp.status == 202):
                        return f"error: HTTP {resp.status}"
                    content = await resp.text()
                    return content
        except asyncio.TimeoutError:
            return "error: 请求超时"
        except Exception as e:
            return f"error: 请求失败: {e}"

    async def _execute_web_search(self, query: str) -> str:
        """
        工具函数：联网搜索。
        优先使用 Bing（中文搜索质量好），失败则降级 DuckDuckGo。
        返回纯文本结果摘要。
        """
        import urllib.parse, html as html_mod

        query = (query or "").strip()
        if not query:
            return "error: 搜索词为空"

        encoded = urllib.parse.quote(query[:300])

        # 定义解析函数：从 HTML 提取搜索结果
        def _parse_bing(html_text: str) -> list:
            """解析 Bing 搜索结果 (class=b_algo)"""
            items = []
            # 每个结果块
            for block in re.finditer(
                r'<li[^>]*class="b_algo"[^>]*>(.*?)</li>',
                html_text,
                re.DOTALL,
            ):
                block_html = block.group(1)
                # 提取链接和标题
                link_m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block_html, re.DOTALL)
                if not link_m:
                    continue
                url = html_mod.unescape(link_m.group(1) or "")
                title = html_mod.unescape(re.sub(r"<[^>]+>", "", link_m.group(2))).strip()
                if not title:
                    continue
                # 提取摘要
                snippet = ""
                cap_m = re.search(r'<p[^>]*>(.*?)</p>', block_html, re.DOTALL)
                if cap_m:
                    snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", cap_m.group(1))).strip()
                entry = f"📌 {title}"
                if snippet:
                    entry += f"\n   {snippet[:250]}"
                entry += f"\n   🔗 {url[:150]}"
                items.append(entry)
                if len(items) >= 6:
                    break
            return items

        def _parse_ddg(html_text: str) -> list:
            """解析 DuckDuckGo HTML 搜索结果"""
            items = []
            blocks = re.findall(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
                r'\s*<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                html_text,
                re.DOTALL,
            )
            for url, title_html, snippet_html in blocks:
                url = html_mod.unescape(url or "")
                if "uddg=" in url:
                    from urllib.parse import parse_qs, urlparse
                    parsed = urlparse(url)
                    qs = parse_qs(parsed.query)
                    url = qs.get("uddg", [url])[0]
                title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
                snippet = html_mod.unescape(re.sub(r"<[^>]+>", "", snippet_html)).strip()
                if title:
                    entry = f"📌 {title}"
                    if snippet:
                        entry += f"\n   {snippet[:200]}"
                    entry += f"\n   🔗 {url[:150]}"
                    items.append(entry)
                if len(items) >= 6:
                    break
            # 兜底找所有 result__a 链接
            if not items:
                all_links = re.findall(
                    r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                    html_text,
                    re.DOTALL,
                )
                for url, title_html in all_links[:6]:
                    url = html_mod.unescape(url or "")
                    if "uddg=" in url:
                        from urllib.parse import parse_qs, urlparse
                        parsed = urlparse(url)
                        qs = parse_qs(parsed.query)
                        url = qs.get("uddg", [url])[0]
                    title = html_mod.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
                    if title:
                        items.append(f"📌 {title}\n   🔗 {url[:150]}")
            return items

        try:
            # 策略 1: Bing cn 搜索（对中文内容质量最好）
            bing_url = f"https://cn.bing.com/search?q={encoded}&setlang=zh-cn"
            bing_html = await self._fetch_url_text(bing_url, timeout=12)
            if not bing_html.startswith("error:"):
                results = _parse_bing(bing_html)
                if results:
                    return "📡 联网搜索结果：\n\n" + "\n\n".join(results)
                # Bing返回了页面但没解析到结果 → 可能页面结构变了
                logger.info(f"[AI工具] Bing 无结果，尝试 DDG 兜底")

            # 策略 2: DuckDuckGo HTML 搜索（接受202状态码）
            ddg_encoded = urllib.parse.quote(query[:200])
            ddg_html = await self._fetch_url_text(
                f"https://html.duckduckgo.com/html/?q={ddg_encoded}&kl=cn-chinese",
                timeout=15,
                accept_202=True,
            )
            if not ddg_html.startswith("error:"):
                results = _parse_ddg(ddg_html)
                if results:
                    return "📡 联网搜索结果：\n\n" + "\n\n".join(results)

            # 两个搜索引擎都失败
            logger.warning(f"[AI工具] 搜索均无结果 query={query[:60]}")
            return "未找到相关结果，可以尝试换一种说法搜索"
        except Exception as e:
            logger.warning(f"[AI工具] 搜索异常: {e}", exc_info=True)
            return f"error: 搜索失败: {e}"

    async def _execute_web_fetch(self, url: str) -> str:
        """工具函数：获取指定网页的文本内容。返回纯文本。"""
        import html as html_mod

        url = (url or "").strip()
        if not url:
            return "error: URL为空"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            html = await self._fetch_url_text(url, timeout=15)
            if html.startswith("error:"):
                return html
            # 提取正文：去掉 script, style, 标签
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = html_mod.unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            # 截取前后重要部分
            if len(text) > 3000:
                text = text[:3000] + "\n\n...(内容较长，已截取前3000字符)"
            if len(text) < 50:
                return f"该页面内容较少或需要登录才能查看: {text[:200]}"
            return f"📄 网页内容（{url[:100]}）:\n\n{text}"
        except Exception as e:
            return f"error: 获取网页失败: {e}"

    async def _handle_private_message(self, data: dict):
        """群主私聊指令（误判/放行/统计等）"""
        try:
            user_id = int(data.get("user_id") or 0)
            if not user_id:
                return
            # 更新私聊活跃时间戳（用于内存清理）
            _private_activity_ts[user_id] = time.time()
            # 提取文本
            text = ""
            raw = data.get("raw_message") or ""
            segs = data.get("message") or []
            if isinstance(segs, list):
                for seg in segs:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        text += (seg.get("data") or {}).get("text", "")
                    elif isinstance(seg, str):
                        text += seg
            if not text:
                text = re.sub(r"\[CQ:[^\]]+\]", "", raw)
            text = (text or "").strip()
            logger.info(f"[NapCat WS] 收到私聊 user={user_id} text={text[:80]!r}")
            if not text:
                return

            from handlers.owner_commands import handle_owner_private_text
            reply = await handle_owner_private_text(user_id, text)
            if not reply:
                logger.info(f"[NapCat WS] 私聊未匹配指令或非群主 user={user_id}，走 AI 闲聊")

                # 先尝试本地数据查询
                local_reply = await self._try_local_query(text)
                if local_reply:
                    reply = local_reply
                else:
                    # 直接走 AI function calling，不再预搜索
                    try:
                        from openai import AsyncOpenAI
                        from config import settings

                        client = AsyncOpenAI(
                            api_key=str(getattr(settings, "AI_API_KEY", "") or ""),
                            base_url=str(getattr(settings, "AI_BASE_URL", "") or ""),
                        )
                        model = str(getattr(settings, "AI_MODEL", "agnes-2.0-flash") or "agnes-2.0-flash")
                        if not client.api_key:
                            reply = (
                                "收到消息。私聊指令：\n"
                                "帮助 — 查看全部指令\n"
                                "误判 — 最近违规加白名单\n"
                                "统计 — 今日违规摘要"
                            )
                        else:
                            import datetime, time as time_mod
                            now = datetime.datetime.now()
                            # 获取今日违规统计
                            try:
                                from handlers.moderation_store import violation_stats
                                st = violation_stats(hours=24)
                            except Exception:
                                st = None

                            # 预计算统计字符串（避免f-string嵌套引号冲突）
                            if st:
                                total_str = str(st.get("total", "未知"))
                                type_parts = []
                                for x in (st.get("by_type") or [])[:5]:
                                    type_parts.append(f"{x.get('vtype','?')}:{x.get('cnt',0)}次")
                                type_str = "  ".join(type_parts) if type_parts else "暂无"
                                reason_parts = []
                                for x in (st.get("top_reasons") or [])[:3]:
                                    r = (x.get("reason") or "-")[:30]
                                    reason_parts.append(f"「{r}」×{x.get('cnt',0)}")
                                reason_str = "  ".join(reason_parts) if reason_parts else "暂无"
                            else:
                                total_str = "未知"
                                type_str = "暂无"
                                reason_str = "暂无"

                            # 系统 prompt：简洁，强约束，工具结果必须使用
                            system_prompt = (
                                f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M:%S')}（北京时间）{now.strftime('%A')}\n"
                                f"你是群管理助手。用中文简洁回答。\n"
                                f"【重要规则】\n"
                                f"1. 你具备联网搜索、获取网页内容和查询天气的能力，可以获取实时信息。\n"
                                f"2. 当用户询问任何城市的天气（如'北京天气''明天青岛天气'）时，必须使用 get_weather 工具查询，禁止用 web_search 查天气。\n"
                                f"3. 搜索实时信息（如新闻、热点）时，必须使用当前具体日期（{now.strftime('%Y年%m月%d日')}）替换「今天」「昨天」等模糊词汇作为搜索关键词，否则搜不到结果。\n"
                                f"4. 当你调用了工具并收到结果后，必须基于工具返回的内容回答用户问题。\n"
                                f"5. 绝对不要说「我无法提供实时信息」「我的知识库截止于」「作为AI我无法」之类的话——因为你已经获取到了实时数据。\n"
                                f"6. 如果搜索结果为空，说明搜索词不合适，主动告诉用户并换种说法重试。\n"
                                f"7. 日常闲聊正常交流，群管理问题引导用户用指令。"
                            )

                            # 使用共享 AI 工具调用模块
                            from handlers.ai_tools import ai_chat_with_tools

                            chat_messages = list(_private_chat_history.get(user_id, [])) + [{"role": "user", "content": text}]

                            result = await ai_chat_with_tools(
                                chat_messages,
                                system_prompt=system_prompt,
                                max_tool_rounds=2,
                            )

                            reply = result.get("content", "").strip()

                            # 缓存对话上下文（不含工具调用细节，只存文本对）
                            history = _private_chat_history.setdefault(user_id, [])
                            history.append({"role": "user", "content": text})
                            history.append({"role": "assistant", "content": reply})
                            if len(history) > _MAX_PRIVATE_HISTORY * 2:
                                _private_chat_history[user_id] = history[-_MAX_PRIVATE_HISTORY * 2:]
                    except Exception as e:
                        logger.warning(f"[NapCat WS] AI 闲聊失败: {e}")
                        reply = (
                            "收到消息。私聊指令：\n"
                            "帮助 — 查看全部指令\n"
                            "误判 — 最近违规加白名单\n"
                            "统计 — 今日违规摘要"
                        )
            from napcat_bridge import get_napcat_bridge
            napcat = get_napcat_bridge()
            if not napcat.available:
                await napcat.check_available()
            if napcat.available:
                ok = await napcat.send_private_msg(user_id, reply)
                logger.info(f"[NapCat WS] 群主指令已回复 user={user_id} ok={ok}")
            else:
                logger.warning(f"[NapCat WS] NapCat 不可用，无法回复私聊 user={user_id}")
        except Exception as e:
            logger.warning(f"[NapCat WS] 私聊指令异常: {e}", exc_info=True)

    async def _handle_group_increase(self, data: dict):
        """新成员入群欢迎语"""
        try:
            group_id = int(data.get("group_id") or 0)
            user_id = int(data.get("user_id") or 0)
            if not group_id:
                return
            # 自动学习：新成员入群说明群活跃，确保配置存在
            try:
                from handlers.moderation_store import ensure_group_config
                ensure_group_config(group_id)
            except Exception:
                pass
            from handlers.moderation_store import get_group_config
            gcfg = get_group_config(group_id).get("config") or {}
            if not gcfg.get("welcome_enabled"):
                return
            welcome = str(gcfg.get("welcome_text") or "").strip()
            if not welcome:
                return
            # 可替换变量
            welcome = welcome.replace("{qq}", str(user_id)).replace("{group}", str(group_id))
            from napcat_bridge import get_napcat_bridge
            napcat = get_napcat_bridge()
            if not napcat.available:
                await napcat.check_available()
            if napcat.available:
                await napcat.send_group_msg(group_id, welcome)
                logger.info(f"[NapCat WS] 入群欢迎 group={group_id} user={user_id}")
        except Exception as e:
            logger.debug(f"[NapCat WS] 入群欢迎跳过: {e}")

    async def _handle_group_request(self, data: dict):
        """处理加群申请"""
        try:
            from handlers.join_audit import handle_group_add_request
            await handle_group_add_request(data)
        except Exception as e:
            logger.error(f"[NapCat WS] 入群审核异常: {e}", exc_info=True)

    async def _check_text_ad(self, data: dict, text: str, group_id: int, user_id: int, message_id: int) -> bool:
        """文本广告实时检测 + 置信度分层 + 撤回/提醒 + 通知
        返回: True=已检测为广告并处理, False=非广告"""
        try:
            # 过滤自身消息
            if _napcat_self_qq and user_id == _napcat_self_qq:
                return False
            # 过滤机器人告警消息
            if any(kw in text for kw in ["广告检测告警", "广告已撤回", "[群管]", "[防刷屏]"]):
                return False
            # 最小检测长度
            if len(text) < 6:
                return False

            # 管理员/群主跳过
            try:
                from group_member_store import is_group_admin
                if is_group_admin(group_id, user_id):
                    return False
            except Exception:
                pass
            try:
                from config import settings
                owner = str(getattr(settings, "QQ_GROUP_OWNER", "") or "")
                if owner and str(user_id) == owner:
                    return False
            except Exception:
                pass

            # URL 白名单放行
            try:
                from handlers.ad_detector import is_url_whitelisted
                if is_url_whitelisted(text):
                    logger.info(f"[文本广告] URL白名单放行: user={user_id} text={text[:60]}")
                    return False
            except Exception:
                pass

            # 群配置阈值
            ad_warn_score = 50
            ad_recall_score = 70
            ad_mute_minutes = 0
            try:
                from handlers.moderation_store import get_group_config, match_whitelist_words
                gcfg = get_group_config(group_id).get("config") or {}
                if gcfg.get("enabled") is False or gcfg.get("ad_enabled") is False:
                    return False
                ad_warn_score = int(gcfg.get("ad_warn_score", 50) or 50)
                ad_recall_score = int(gcfg.get("ad_recall_score", 70) or 70)
                ad_mute_minutes = int(gcfg.get("ad_mute_minutes", 0) or 0)
            except Exception:
                match_whitelist_words = None  # type: ignore

            # 白名单词放行
            try:
                from handlers.moderation_store import match_whitelist_words as _mw
                wl = _mw(text, group_id)
                if wl:
                    logger.info(f"[文本广告] 白名单词放行: {wl[:3]} user={user_id}")
                    return False
            except Exception:
                pass

            # 获取发送者昵称
            sender = data.get("sender") or {}
            nick = sender.get("card") or sender.get("nickname") or str(user_id)

            # 调用广告检测
            from handlers.ad_detector import ai_detect_ad
            result = await ai_detect_ad(text, nick, "", group_id=group_id)
            score = result.get("score", 0)
            reason = result.get("reason", "")
            detect_source = result.get("source") or "unknown"

            logger.info(
                f"[文本广告] group={group_id} user={user_id}({nick}) "
                f"score={score} source={detect_source} reason={reason[:60]}"
            )

            if score < ad_warn_score:
                return False  # 未达到警告阈值，放行

            from handlers.ad_detector import summarize_ad_reason
            short_reason = summarize_ad_reason(reason, is_image=False)

            is_high_conf = score >= ad_recall_score

            # 低置信（ad_warn_score <= score < ad_recall_score）：仅私聊通知群主，不撤回
            if not is_high_conf:
                try:
                    from handlers.moderation_store import add_violation
                    add_violation(
                        group_id=group_id, user_id=user_id, user_name=nick,
                        vtype="text_ad", score=score,
                        reason=f"[{detect_source}] {short_reason}",
                        content=text[:200],
                        action="仅提醒",
                        extra={"source": detect_source, "confidence": "低置信"},
                    )
                except Exception:
                    pass
                _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                        action_type="alert", detail=f"文本广告低置信({score}分): {short_reason[:60]}")
                try:
                    from config import settings
                    from napcat_bridge import get_napcat_bridge
                    napcat = get_napcat_bridge()
                    if not napcat.available:
                        await napcat.check_available()
                    notify_qq = str(getattr(settings, "QQ_AD_NOTIFY_QQ", "") or "")
                    if napcat.available and notify_qq.isdigit():
                        await napcat.send_private_msg(
                            int(notify_qq),
                            f"⚠️ 文本广告提醒(未撤回)\n"
                            f"群:{group_id}\n用户:{nick}({user_id})\n"
                            f"评分:{score}/100 (低置信)\n来源:{detect_source}\n"
                            f"原因:{short_reason}\n内容:{text[:150]}\n\n"
                            f"回复「误判」可加白名单",
                        )
                except Exception:
                    pass
                # 低置信度仅提醒，不视为广告（消息仍在群里，FAQ等后续流程继续）
                return False

            # 高置信（score >= ad_recall_score）：撤回 + 群内告警 + 私聊通知
            from napcat_bridge import get_napcat_bridge
            napcat = get_napcat_bridge()
            if not napcat.available:
                await napcat.check_available()
            if not napcat.available:
                return True

            deleted = await napcat.delete_group_msg(group_id, message_id)

            # 禁言（如果配置了禁言时长）
            muted = False
            if ad_mute_minutes > 0 and deleted:
                try:
                    mute_ok = await napcat.set_group_ban(group_id, user_id, ad_mute_minutes * 60)
                    if mute_ok:
                        muted = True
                        logger.info(f"[文本广告] 已禁言 user={user_id} {ad_mute_minutes}分钟")
                except Exception as e:
                    logger.warning(f"[文本广告] 禁言失败: {e}")

            try:
                from handlers.moderation_store import add_violation
                add_violation(
                    group_id=group_id, user_id=user_id, user_name=nick,
                    vtype="text_ad", score=score,
                    reason=f"[{detect_source}] {short_reason}",
                    content=text[:200],
                    action=f"已撤回{'+禁言' if muted else ''}" if deleted else "撤回失败",
                    extra={"source": detect_source, "confidence": "高置信"},
                )
            except Exception:
                pass
            _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                    action_type="recall", detail=f"文本广告高置信({score}分): {short_reason[:60]}")
            if muted:
                _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                        action_type="mute", detail=f"文本广告禁言{ad_mute_minutes}分钟({score}分)")

            # 群内告警（撤回成功时才发）
            if deleted:
                warn_parts = [
                    f"⚠️ 广告已撤回",
                    f"用户：{nick}(QQ:{user_id})",
                    f"评分：{score}/100",
                    f"📢 请遵守群规，本群🚫广告，只为友好交流",
                ]
                if muted:
                    warn_parts.insert(2, f"已禁言：{ad_mute_minutes}分钟")
                warn = "\n".join(warn_parts)
                await napcat.send_group_msg(group_id, warn)

            # 私聊通知群主
            try:
                from config import settings
                notify_qq = str(getattr(settings, "QQ_AD_NOTIFY_QQ", "") or "")
                if notify_qq.isdigit():
                    await napcat.send_private_msg(
                        int(notify_qq),
                        f"🚫 文本广告告警\n"
                        f"群:{group_id}\n用户:{nick}({user_id})\n"
                        f"评分:{score}/100 (高置信)\n来源:{detect_source}\n"
                        f"原因:{short_reason}\n"
                        f"状态:{'已撤回' if deleted else '未能撤回'}\n"
                        f"内容：{text[:150]}",
                    )
            except Exception:
                pass

        except Exception as e:
            logger.warning(f"[文本广告] 检测异常: {e}", exc_info=True)
            return False

        # 高置信检测路径正常结束
        return True

    async def _handle_group_image_ocr(self, data: dict):
        """群图片 OCR 广告审核"""
        try:
            import os
            if str(os.environ.get("OCR_ENABLED", "true")).lower() in ("0", "false", "no", "off"):
                return

            message_id = int(data.get("message_id") or 0)
            group_id = int(data.get("group_id") or 0)
            user_id = int(data.get("user_id") or 0)
            if not message_id or not group_id or not user_id:
                return

            # 去重
            now = time.time()
            if message_id in _ocr_seen and now - _ocr_seen[message_id] < 120:
                return
            _ocr_seen[message_id] = now
            # 清理旧去重记录
            if len(_ocr_seen) > 500:
                cutoff = now - 300
                for k in list(_ocr_seen.keys()):
                    if _ocr_seen[k] < cutoff:
                        del _ocr_seen[k]

            # 管理员跳过
            try:
                from group_member_store import is_group_admin
                if is_group_admin(group_id, user_id):
                    return
            except Exception:
                pass

            # 群主跳过
            try:
                from config import settings
                owner = str(getattr(settings, "QQ_GROUP_OWNER", "") or "")
                if owner and str(user_id) == owner:
                    return
            except Exception:
                pass

            from handlers.ocr_audit import get_ocr_auditor, extract_image_urls_from_message
            urls = extract_image_urls_from_message(data)
            if not urls:
                return

            # 附加文本
            extra = ""
            segs = data.get("message") or []
            if isinstance(segs, list):
                for seg in segs:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        extra += seg.get("data", {}).get("text", "")

            # 附加文本中的 URL 白名单放行
            try:
                from handlers.ad_detector import is_url_whitelisted
                if extra and is_url_whitelisted(extra):
                    logger.info(f"[OCR] URL白名单放行: user={user_id} extra={extra[:60]}")
                    return
            except Exception:
                pass

            auditor = get_ocr_auditor()
            if not auditor.enabled:
                return

            sender = data.get("sender") or {}
            nick = sender.get("card") or sender.get("nickname") or str(user_id)

            # 群配置阈值
            ad_warn_score = 50
            ad_recall_score = 70
            delay_observe_sec = 30
            try:
                from handlers.moderation_store import get_group_config, match_whitelist_words
                gcfg = get_group_config(group_id).get("config") or {}
                if gcfg.get("enabled") is False or gcfg.get("ocr_enabled") is False:
                    return
                ad_warn_score = int(gcfg.get("ad_warn_score", gcfg.get("ad_mute_score", 50)) or 50)
                ad_recall_score = int(gcfg.get("ad_recall_score", 70) or 70)
                delay_observe_sec = int(gcfg.get("delay_observe_sec", 30) or 30)
            except Exception:
                match_whitelist_words = None  # type: ignore

            # 只审第一张图，避免刷爆 API
            result = await auditor.audit_image(urls[0], username=nick, extra_text=extra, group_id=group_id)
            score = int(result.get("score") or 0)
            reason = result.get("reason") or ""
            ocr_text = result.get("ocr_text") or ""
            detect_source = result.get("source") or "ocr"

            # 白名单词放行
            try:
                from handlers.moderation_store import match_whitelist_words as _mw
                wl = _mw((ocr_text or "") + " " + (extra or ""), group_id)
                if wl:
                    logger.info(f"[OCR] 白名单词放行: {wl[:3]} user={user_id}")
                    return
            except Exception:
                pass

            logger.info(
                f"[OCR] group={group_id} user={user_id} score={score} "
                f"ocr={ocr_text[:40]!r} reason={reason[:60]}"
            )

            if score < ad_warn_score:
                return

            # 延迟观察判断
            from handlers.ad_detector import summarize_ad_reason
            short_reason = summarize_ad_reason(reason, ocr_text=ocr_text, is_image=True)
            is_marketing = bool(re.search(
                r"(赚钱|兼职|日结|月入|躺赚|暴富|刷单|代理|跑分|招|代刷|免费领|0元购|一键|加群|扫码)",
                ocr_text
            ))
            is_sharing = bool(re.search(
                r"(分享|推荐|测评|评测|安利|好用的|便宜的|优惠活动|白嫖|薅羊毛)",
                ocr_text
            ))
            is_high_conf = score >= ad_recall_score

            # 延迟：临界分 或 分享类无营销词（即使高分）
            should_delay = (
                (not is_high_conf and not is_marketing)
                or (is_sharing and not is_marketing)
            )

            if should_delay:
                _pending_ocr_checks[message_id] = {
                    "data": data,
                    "timestamp": time.time(),
                    "score": score,
                    "reason": reason,
                    "ocr_text": ocr_text,
                    "nick": nick,
                    "short_reason": short_reason,
                    "source": detect_source,
                    "is_high_conf": is_high_conf,
                }
                _ocr_pending_ids.add(message_id)
                logger.info(
                    f"[OCR] 延迟观察: group={group_id} user={user_id} "
                    f"score={score}, {delay_observe_sec}秒后根据上下文判断"
                )
                asyncio.create_task(_delayed_ocr_check(message_id, delay=delay_observe_sec))
                return

            # 低置信：仅私聊提醒，不撤回
            if not is_high_conf:
                try:
                    from handlers.moderation_store import add_violation
                    add_violation(
                        group_id=group_id, user_id=user_id, user_name=nick,
                        vtype="ocr", score=score,
                        reason=f"[{detect_source}] {short_reason}",
                        content=(ocr_text or "")[:200],
                        action="仅提醒",
                        extra={"source": detect_source, "confidence": "低置信"},
                    )
                except Exception:
                    pass
                _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                        action_type="alert", detail=f"图片OCR低置信({score}分): {short_reason[:60]}")
                try:
                    from config import settings
                    from napcat_bridge import get_napcat_bridge
                    napcat = get_napcat_bridge()
                    if not napcat.available:
                        await napcat.check_available()
                    notify_qq = str(getattr(settings, "QQ_AD_NOTIFY_QQ", "") or "")
                    if napcat.available and notify_qq.isdigit():
                        await napcat.send_private_msg(
                            int(notify_qq),
                            f"⚠️ 图片广告提醒(未撤回)\n群:{group_id}\n用户:{nick}({user_id})\n"
                            f"评分:{score}/100 (低置信)\n来源:{detect_source}\n"
                            f"原因:{short_reason}\n图片文字:{(ocr_text or '')[:150]}\n\n"
                            f"回复「误判」可加白名单",
                        )
                except Exception as e:
                    logger.debug(f"[OCR] 低置信提醒失败: {e}")
                return

            # 高置信 → 立即撤回
            from napcat_bridge import get_napcat_bridge
            napcat = get_napcat_bridge()
            if not napcat.available:
                await napcat.check_available()
            if not napcat.available:
                return
            deleted = await napcat.delete_group_msg(group_id, message_id)
            try:
                from handlers.moderation_store import add_violation
                add_violation(
                    group_id=group_id, user_id=user_id, user_name=nick,
                    vtype="ocr", score=score,
                    reason=f"[{detect_source}] {short_reason}",
                    content=(ocr_text or "")[:200],
                    action="已撤回" if deleted else "撤回失败",
                    extra={"source": detect_source, "confidence": "高置信"},
                )
            except Exception:
                pass
            _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                    action_type="recall", detail=f"图片OCR高置信({score}分): {short_reason[:60]}")
            warn = (
                f"⚠️ 图片广告已处理\n"
                f"用户：{nick}(QQ:{user_id})\n"
                f"原因：{short_reason}\n"
                f"📢 请遵守群规，本群🚫广告"
            )
            await napcat.send_group_msg(group_id, warn)

            try:
                from config import settings
                notify_qq = str(getattr(settings, "QQ_AD_NOTIFY_QQ", "") or "")
                if notify_qq.isdigit():
                    ocr_preview = ocr_text[:150] if len(ocr_text) > 150 else ocr_text
                    await napcat.send_private_msg(
                        int(notify_qq),
                        f"🚫 图片广告告警\n群:{group_id}\n用户:{nick}({user_id})\n"
                        f"评分:{score}/100 (高置信)\n来源:{detect_source}\n"
                        f"原因:{short_reason}\n"
                        f"状态:{'已撤回' if deleted else '撤回失败'}\n"
                        f"图片文字：{ocr_preview}\n\n"
                        f"回复「误判」可加白名单",
                    )
            except Exception as e:
                logger.debug(f"[OCR] 通知群主失败: {e}")

        except Exception as e:
            logger.warning(f"[NapCat WS] OCR 审核异常: {e}")

    async def _handle_group_card(self, data: dict):
        """处理名片变更"""
        try:
            from handlers.card_monitor import handle_group_card_notice
            await handle_group_card_notice(data)
        except Exception as e:
            logger.error(f"[NapCat WS] 名片监控异常: {e}", exc_info=True)

    async def _handle_group_admin(self, data: dict):
        """处理管理员任免"""
        try:
            from handlers.card_monitor import handle_group_admin_notice
            await handle_group_admin_notice(data)
        except Exception as e:
            logger.error(f"[NapCat WS] 管理员任免通知异常: {e}", exc_info=True)

    async def _handle_sender_card_check(self, data: dict):
        """群消息发言时检查 sender.card（名片通知兜底）"""
        try:
            from handlers.card_monitor import check_sender_card_from_message
            await check_sender_card_from_message(data)
        except Exception as e:
            logger.warning(f"[NapCat WS] 发言名片检查异常: {e}")

    async def _cleanup_cache(self, group_id: int):
        """清理过期的消息缓存"""
        if group_id not in _msg_cache:
            return
        now = time.time()
        _msg_cache[group_id] = [
            e for e in _msg_cache[group_id]
            if now - e["time"] < _CACHE_TTL
        ]
        if len(_msg_cache[group_id]) > 100:
            _msg_cache[group_id] = _msg_cache[group_id][-100:]


async def _delayed_ocr_check(message_id: int, delay: int = 30):
    """
    OCR图片延迟检查。
    等待 delay 秒后，检查后续对话是否有上下文互动。
    """
    try:
        await asyncio.sleep(delay)
        item = _pending_ocr_checks.pop(message_id, None)
        _ocr_pending_ids.discard(message_id)  # 检查完成，移出pending集合
        if not item:
            return

        data = item["data"]
        group_id = int(data.get("group_id") or 0)
        user_id = int(data.get("user_id") or 0)
        nick = item["nick"]
        score = item["score"]
        reason = item["reason"]
        ocr_text = item["ocr_text"]
        short_reason = item["short_reason"]
        detect_source = item.get("source") or "ocr"

        # 获取群配置阈值
        ad_recall_score = 70
        try:
            from handlers.moderation_store import get_group_config
            gcfg = get_group_config(group_id).get("config") or {}
            ad_recall_score = int(gcfg.get("ad_recall_score", 70) or 70)
        except Exception as e:
            logger.warning(f"[NapCat WS] 关闭旧连接异常: {e}")
            pass

        # 检查后续对话中是否有其他用户提及图片相关内容
        now = time.time()
        entries = _msg_cache.get(group_id, [])
        others_replied = False
        checked_count = 0

        logger.info(
            f"[OCR] 延迟检查开始: group={group_id} user={user_id} "
            f"缓存条目数={len(entries)}"
        )

        for e in reversed(entries[-30:]):
            try:
                age = now - float(e.get("time") or 0)
                msg_user = int(e.get("user_id") or 0)
                msg_text = (e.get("text") or "").strip()
                checked_count += 1
                if age > delay + 5:
                    continue  # 超过窗口的消息跳过，但不break（时间可能不严格有序）
                if not msg_text:
                    continue
                if msg_user == user_id:
                    continue
                # 其他用户回复非广告内容 → 正常互动
                if len(msg_text) < 60:
                    others_replied = True
                    logger.info(
                        f"[OCR] 检测到互动: user={msg_user} text={msg_text[:30]!r} "
                        f"age={age:.1f}s"
                    )
                    break
            except Exception:
                continue

        logger.info(
            f"[OCR] 延迟检查完成: group={group_id} checked={checked_count} "
            f"others_replied={others_replied}"
        )

        if others_replied:
            logger.info(
                f"[OCR] 延迟放行: group={group_id} user={user_id} "
                f"原因=其他用户有互动"
            )
            return

        # 无人互动 → 根据置信度决定处理方式
        # 低置信（score < ad_recall_score）：仅通知管理员，不撤回
        # 高置信（score >= ad_recall_score）：撤回 + 通知
        is_high_conf = score >= ad_recall_score

        if not is_high_conf:
            # 低置信 → 仅私聊通知群主，不撤回
            logger.info(
                f"[OCR] 延迟低置信(未撤回): group={group_id} user={user_id} "
                f"score={score}"
            )
            try:
                from handlers.moderation_store import add_violation
                add_violation(
                    group_id=group_id, user_id=user_id, user_name=nick,
                    vtype="ocr", score=score,
                    reason=f"[{detect_source}] {short_reason}",
                    content=(ocr_text or "")[:200],
                    action="仅提醒(延迟)",
                    extra={"source": detect_source, "confidence": "延迟低置信"},
                )
            except Exception:
                pass
            try:
                from config import settings
                notify_qq = str(getattr(settings, "QQ_AD_NOTIFY_QQ", "") or "")
                if notify_qq.isdigit():
                    from napcat_bridge import get_napcat_bridge
                    napcat = get_napcat_bridge()
                    if not napcat.available:
                        await napcat.check_available()
                    if napcat.available:
                        await napcat.send_private_msg(
                            int(notify_qq),
                            f"⚠️ 图片广告提醒(延迟观察后未撤回)\n"
                            f"群:{group_id}\n用户:{nick}({user_id})\n"
                            f"评分:{score}/100 (低置信)\n来源:{detect_source}\n"
                            f"原因:{short_reason}\n图片文字:{(ocr_text or '')[:150]}\n\n"
                            f"回复「误判」可加白名单",
                        )
            except Exception as e:
                logger.debug(f"[OCR] 延迟低置信通知失败: {e}")
            _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                    action_type="alert", detail=f"图片OCR延迟低置信({score}分): {short_reason[:60]}")
            return

        # 高置信 → 撤回 + 告警
        logger.info(
            f"[OCR] 延迟撤回: group={group_id} user={user_id} "
            f"score={score}"
        )

        from napcat_bridge import get_napcat_bridge
        napcat = get_napcat_bridge()
        if not napcat.available:
            await napcat.check_available()
        if not napcat.available:
            return

        detect_source = item.get("source") or "ocr"
        deleted = await napcat.delete_group_msg(group_id, message_id)
        try:
            from handlers.moderation_store import add_violation
            add_violation(
                group_id=group_id, user_id=user_id, user_name=nick,
                vtype="ocr", score=score,
                reason=f"[{detect_source}] {short_reason}",
                content=(ocr_text or "")[:200],
                action="已撤回" if deleted else "撤回失败",
                extra={"source": detect_source, "confidence": "延迟后撤回"},
            )
        except Exception:
            pass
        _log_op(group_id=group_id, user_id=user_id, user_name=nick,
                action_type="recall", detail=f"图片OCR延迟高置信({score}分): {short_reason[:60]}")
        warn = (
            f"⚠️ 图片广告已处理\n"
            f"用户：{nick}(QQ:{user_id})\n"
            f"原因：{short_reason}\n"
            f"📢 请遵守群规，本群🚫广告"
        )
        await napcat.send_group_msg(group_id, warn)

        try:
            from config import settings
            notify_qq = str(getattr(settings, "QQ_AD_NOTIFY_QQ", "") or "")
            if notify_qq.isdigit():
                ocr_preview = ocr_text[:150] if len(ocr_text) > 150 else ocr_text
                await napcat.send_private_msg(
                    int(notify_qq),
                    f"🚫 图片广告告警(延迟)\n群:{group_id}\n用户:{nick}({user_id})\n"
                    f"评分:{score}/100\n来源:{detect_source}\n"
                    f"原因:{short_reason}\n"
                    f"状态:{'已撤回' if deleted else '撤回失败'}\n"
                    f"图片文字：{ocr_preview}\n\n"
                    f"回复「误判」可加白名单",
                )
        except Exception as e:
            logger.debug(f"[OCR] 通知群主失败: {e}")

    except Exception as e:
        logger.error(f"[OCR] 延迟检查异常: {e}", exc_info=True)


def guess_group_by_content(content: str, window_sec: float = 20.0) -> int:
    """
    用官方机器人消息内容匹配 NapCat 最近消息，猜测数字群号。
    仅当唯一群命中时返回，避免误学。
    """
    frag = (content or "").strip()
    if len(frag) < 4:
        return 0
    now = time.time()
    hits = []
    for gid, entries in list(_msg_cache.items()):
        for e in reversed(entries[-30:]):
            try:
                if now - float(e.get("time") or 0) > window_sec:
                    break
                t = (e.get("text") or "").strip()
                if not t:
                    continue
                if frag in t or t in frag or (len(frag) >= 8 and frag[:20] in t):
                    hits.append(int(gid))
                    break
            except Exception:
                continue
    uniq = list(set(hits))
    if len(uniq) == 1:
        return uniq[0]
    return 0


def find_napcat_msg_id(group_num: int, content_fragment: str, max_age: int = 60) -> int:
    """从缓存中查找匹配的 NapCat 消息 ID"""
    if group_num not in _msg_cache:
        return 0

    now = time.time()
    best_match = None
    best_age = 999999

    for entry in reversed(_msg_cache[group_num]):
        age = now - entry["time"]
        if age > max_age:
            continue
        clean_cache = re.sub(r"\s+", "", entry.get("text", ""))
        clean_fragment = re.sub(r"\s+", "", content_fragment)
        if clean_fragment and clean_cache and clean_fragment[:20] in clean_cache:
            if age < best_age:
                best_match = entry
                best_age = age

    if best_match:
        return best_match["msg_id"]
    return 0


def check_context_relevance(group_num: int, text: str, sender_qq: int = 0, window_sec: float = 120.0) -> bool:
    """
    检查当前消息是否与群内近期对话上下文相关联。
    
    判断逻辑：从消息中提取关键名词/主题词，检查前 window_sec 秒内
    其他用户的消息中是否包含相同主题词。如果有人提到过相关话题，
    认为当前消息是对话延续而非广告。
    
    返回 True 表示有上下文关联。
    """
    if not group_num or group_num not in _msg_cache:
        return False
    
    # 从当前消息中提取主题词（至少2个字的中文词/英文词）
    import re
    tokens = set()
    # 中文词：2-6字
    for m in re.finditer(r"[\u4e00-\u9fff]{2,6}", text):
        tokens.add(m.group())
    # 英文词：至少4字母
    for m in re.finditer(r"[a-zA-Z]{4,}", text):
        tokens.add(m.group().lower())
    # 数字串（论坛名、版本号等）
    for m in re.finditer(r"[a-zA-Z]+\d+|\d+[a-zA-Z]+", text):
        tokens.add(m.group().lower())
    
    if not tokens:
        return False
    
    now = time.time()
    entries = _msg_cache.get(group_num, [])
    
    # 检查 window_sec 内其他用户的消息
    matched_others = 0
    for e in reversed(entries[-50:]):
        try:
            age = now - float(e.get("time") or 0)
            if age > window_sec:
                break
            # 排除自己
            if sender_qq and int(e.get("user_id") or 0) == sender_qq:
                continue
            prev_text = (e.get("text") or "").strip()
            if not prev_text:
                continue
            # 检查是否有共同主题词
            overlap = 0
            for token in tokens:
                if token in prev_text:
                    overlap += 1
            if overlap >= 1:
                matched_others += 1
        except Exception:
            continue
    
    # 至少有1个其他用户提到过相关话题，认为有关联
    if matched_others >= 1:
        logger.info(
            f"[上下文关联] group={group_num} 检测到上下文关联 "
            f"(matched={matched_others} others in {window_sec}s)"
        )
        return True
    return False


# 全局实例
_ws_client: NapCatWSClient = None


async def _periodic_memory_cleanup():
    """
    周期性内存清理任务：每 5 分钟执行一次。
    清理以下无界增长的缓存：
    1. _private_chat_history / _group_chat_history - 移除 30 分钟未活跃的条目
    2. _pending_faq_feedback - 移除过期的反馈条目（超过 5 分钟）
    3. _msg_cache - 移除过期群的消息缓存
    4. _pending_ocr_checks / _ocr_pending_ids - 移除 OCR 处理超时的条目
    5. _private_activity_ts / _group_activity_ts - 移除已清理的活跃时间记录
    """
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        try:
            now = time.time()
            cutoff = now - _CLEANUP_MAX_IDLE
            cleaned = {"private": 0, "group": 0, "faq": 0, "msg_cache": 0, "ocr": 0}

            # 1. 清理私聊历史（30 分钟未活跃的用户）
            stale_users = [
                uid for uid, ts in _private_activity_ts.items()
                if ts < cutoff
            ]
            for uid in stale_users:
                _private_chat_history.pop(uid, None)
                _private_activity_ts.pop(uid, None)
                cleaned["private"] += 1

            # 2. 清理群聊历史（30 分钟未活跃的群）
            stale_groups = [
                gid for gid, ts in _group_activity_ts.items()
                if ts < cutoff
            ]
            for gid in stale_groups:
                _group_chat_history.pop(gid, None)
                _group_activity_ts.pop(gid, None)
                cleaned["group"] += 1

            # 3. 清理过期的 FAQ 反馈条目
            stale_faq = [
                key for key, val in _pending_faq_feedback.items()
                if now - val.get("timestamp", 0) > _FAQ_FEEDBACK_EXPIRE
            ]
            for key in stale_faq:
                del _pending_faq_feedback[key]
                cleaned["faq"] += 1

            # 4. 清理 _msg_cache 中过期的群缓存
            stale_msg_groups = [
                gid for gid, entries in _msg_cache.items()
                if not entries or now - entries[-1].get("time", 0) > _CACHE_TTL * 2
            ]
            for gid in stale_msg_groups:
                _msg_cache.pop(gid, None)
                cleaned["msg_cache"] += 1

            # 5. 清理 OCR 延迟检查超时条目（超过 5 分钟未完成）
            stale_ocr = [
                mid for mid, item in _pending_ocr_checks.items()
                if now - item.get("timestamp", 0) > 300
            ]
            for mid in stale_ocr:
                _pending_ocr_checks.pop(mid, None)
                _ocr_pending_ids.discard(mid)
                cleaned["ocr"] += 1

            # 6. 清理 _ocr_seen 中超过 500 条的旧记录
            if len(_ocr_seen) > 500:
                cutoff_ocr = now - 120
                for k in list(_ocr_seen.keys()):
                    if _ocr_seen[k] < cutoff_ocr:
                        del _ocr_seen[k]

            total_cleaned = sum(cleaned.values())
            if total_cleaned > 0:
                logger.info(
                    f"[内存清理] 清理完成: 私聊历史={cleaned['private']} "
                    f"群聊历史={cleaned['group']} FAQ反馈={cleaned['faq']} "
                    f"消息缓存={cleaned['msg_cache']} OCR超时={cleaned['ocr']}"
                )
        except Exception as e:
            logger.warning(f"[内存清理] 异常: {e}")


def get_last_ws_msg_ts() -> float:
    """获取最后一次收到 NapCat WS 消息的时间戳（用于僵死连接检测）"""
    return _last_ws_msg_ts


async def start_napcat_ws():
    """启动 NapCat WebSocket 客户端（后台任务）"""
    global _ws_client
    from config import settings

    # 优先使用专用 WS 地址；默认 NapCat WebSocket 端口 30102
    ws_url = getattr(settings, "NAPCAT_WS_URL", "") or ""
    if not ws_url:
        ws_url = "ws://napcat:30102"
    token = getattr(settings, "NAPCAT_ACCESS_TOKEN", "") or ""
    if not token:
        logger.warning("[NapCat WS] NAPCAT_ACCESS_TOKEN 未配置，NapCat WebSocket 连接可能被拒绝")
        logger.warning("[NapCat WS] 请在 .env 中设置 NAPCAT_ACCESS_TOKEN 或在 config.py 中配置")

    _ws_client = NapCatWSClient(ws_url, token)
    asyncio.create_task(_ws_client.start())
    logger.info(f"[NapCat WS] 后台任务已启动: {ws_url}")
    # 启动周期性内存清理任务
    asyncio.create_task(_periodic_memory_cleanup())
    logger.info(f"[内存清理] 周期性清理任务已启动（间隔{_CLEANUP_INTERVAL}s，最大空闲{_CLEANUP_MAX_IDLE}s）")
    await asyncio.sleep(2)

    # 启动后同步 NapCat 群列表到 group_configs（自动学习）
    try:
        from napcat_bridge import get_napcat_bridge
        bridge = get_napcat_bridge()
        if bridge.available or await bridge.check_available():
            groups = await bridge.get_group_list()
            if groups:
                from handlers.moderation_store import ensure_group_config
                synced = 0
                for g in groups:
                    gid = g.get("group_id") or 0
                    gname = g.get("group_name") or g.get("group_name", "")
                    if gid:
                        ensure_group_config(int(gid), group_name=gname)
                        synced += 1
                logger.info(f"[NapCat WS] 群配置自动同步完成: {synced} 个群")
    except Exception as e:
        logger.debug(f"[NapCat WS] 群配置自动同步跳过: {e}")
