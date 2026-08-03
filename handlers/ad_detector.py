"""
AI 广告/垃圾消息检测模块
功能：调用 AI 分析消息内容，判断是否为广告并评分，自动禁言/封禁
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from models.database import db

logger = logging.getLogger(__name__)
router = Router()

# 广告检测评分阈值
SCORE_WARN = 50      # 50-69: 警告（仅通知管理员）
SCORE_MUTE = 70      # 70-89: 禁言
SCORE_BAN = 90       # 90+: 封禁
CHECK_COOLDOWN = 30   # 命中后静默检查冷却（秒）

# 广告检测规则：每条规则只给基础分，需要组合才触发
# 招揽话术类（单独出现不加太多分）
AD_RULES = [
    # --- 招揽/收益话术（高权重）---
    {"pattern": r"(日[入结]|月入|时薪|天入)\s*[\d万亿千百零一两二三四五六七八九十]+", "score": 35, "reason": "高收益话术"},
    {"pattern": r"(收米|躺赚|暴富|一夜暴富|稳赚不赔|一单一结|日结.*[刷兼]|兼职.*日结)", "score": 45, "reason": "高收益话术"},
    {"pattern": r"(免费送|免费领|0元|零元购|白嫖|免费.*[领送取])", "score": 30, "reason": "免费诱饵"},
    {"pattern": r"(投资|理财|基金|股票|币圈).{0,10}(回报|收益|翻倍|保证金)", "score": 35, "reason": "投资话术"},
    {"pattern": r"(刷单|兼职|代付|跑分|租借|出借|租号|出号)(微信|支付宝|银行卡|账号|卡|码)", "score": 50, "reason": "黑产话术"},
    {"pattern": r"(抖音|快手|小红书).{0,10}(日结|兼职|赚钱|副业|粉丝|点赞)", "score": 40, "reason": "平台兼职话术"},
    
    # --- 引流行为（权重较高）---
    {"pattern": r"(加[我我]|联系我|私聊我|戳我).{0,5}[@＠]\w{2,}", "score": 40, "reason": "@用户名引流"},
    {"pattern": r"t\.me/\w{3,}", "score": 35, "reason": "TG链接引流"},
    {"pattern": r"(进群|加群|拉群|扫码|二维码|扫码进群)", "score": 30, "reason": "引导加群/扫码"},
    {"pattern": r"(免费|领取|赠送|送你|福利).{0,10}(链接|网址|点击|扫码)", "score": 35, "reason": "诱导点击"},
    {"pattern": r"[@＠]\w{3,}\s*[@＠]\w{3,}", "score": 35, "reason": "连续@多人"},
    
    # --- 商业推广 ---
    {"pattern": r"(代购|买[卖]|出[售转]|低价|折扣|优惠|促销|特价|白菜价)", "score": 25, "reason": "商业推广"},
    {"pattern": r"(接单|派单|做任务|赚外快|副业|带单|招代理|代理)", "score": 30, "reason": "疑似兼职"},
    {"pattern": r"(VX|vx|威信|微[信号]|加v|加微|薇信)", "score": 25, "reason": "疑似微信引流"},
    {"pattern": r"https?://[^\s]{5,}", "score": 20, "reason": "外部链接"},
]

# 推广性简介关键词（配合无意义消息时加分）
BIO_SPAM_KEYWORDS = [
    "免费提链", "免费推广", "接单", "代发", "引流",
    "加群", "拉新", "收徒", "带单", "出单",
]

# AI 语义分析配置（从 settings 加载）
AI_ENABLED = settings.AI_ENABLED
AI_TIMEOUT = 15  # 秒


# 技术/开源分享域名：文本侧直接放行
_TECH_SHARE_URL_RE = re.compile(
    r"(github\.com|gitee\.com|gitcode\.com|raw\.githubusercontent\.com|"
    r"gitlab\.com|npmjs\.com|pypi\.org|huggingface\.co|docker\.io|hub\.docker)",
    re.I,
)
# 明显营销词（技术域名旁出现时仍可能是广告）
_TECH_MARKETING_RE = re.compile(
    r"(赚钱|兼职|日结|月入|躺赚|暴富|刷单|代理|跑分|加v|加微|扫码进群|免费领|0元购)",
    re.I,
)

# ===== URL 白名单：这些链接不会被判定为广告 =====
_URL_WHITELIST = re.compile(
    r"(ai\.hhhl\.cc|ai2\.hhhl\.cc|216\.195\.211\.206|cndd\.cc\.cd)",
    re.I,
)


def is_url_whitelisted(text: str) -> bool:
    """检查文本中是否包含白名单 URL，命中则直接放行。"""
    if not text:
        return False
    return bool(_URL_WHITELIST.search(text))


def _is_tech_share_text(text: str) -> bool:
    """GitHub/Gitee 等开源项目分享，或技术资源链接。"""
    if not text:
        return False
    if not _TECH_SHARE_URL_RE.search(text):
        return False
    if _TECH_MARKETING_RE.search(text):
        return False
    return True


async def ai_detect_ad(text: str, username: str = "", user_bio: str = "", group_id: int = 0) -> dict:
    """
    混合检测：规则快速拦截明显广告 + AI 语义分析处理复杂情况
    返回: {"score": 0-100, "reason": "...", "is_ad": bool}

    group_id: 群号，用于查询群级/全局白名单词库（管理员通过「放行」命令添加的词）
    """
    if not text:
        return {"score": 0, "reason": "", "is_ad": False}

    # ===== 白名单词库放行（管理员通过「放行/误判」命令添加的词）=====
    try:
        from handlers.moderation_store import match_whitelist_words
        wl_hits = match_whitelist_words(text, group_id=group_id)
        if wl_hits:
            logger.info(
                f"[广告检测] 白名单词库命中放行: {wl_hits} | text={text[:60]!r}"
            )
            return {
                "score": 0,
                "reason": f"白名单词库命中({'+'.join(wl_hits[:3])})",
                "is_ad": False,
                "source": "whitelist_word",
                "sources": ["whitelist_word"],
            }
    except Exception as e:
        logger.debug(f"[广告检测] 白名单词库查询跳过: {e}")

    # 开源/技术平台链接分享直接放行
    if _is_tech_share_text(text):
        logger.info(f"[广告检测] 技术/开源分享放行: {text[:80]!r}")
        return {
            "score": 0,
            "reason": "技术/开源项目分享",
            "is_ad": False,
            "source": "tech_share",
            "sources": ["tech_share"],
        }

    # 白名单 URL 直接放行
    if is_url_whitelisted(text):
        logger.info(f"[广告检测] URL白名单放行: {text[:80]!r}")
        return {
            "score": 0,
            "reason": "白名单链接",
            "is_ad": False,
            "source": "url_whitelist",
            "sources": ["url_whitelist"],
        }

    text_lower = text.lower()
    total_score = 0
    matched_rules = []
    lex_only = False
    sources = []

    # 第零阶段：Aho-Corasick 词库初筛（大规模关键词）
    try:
        from handlers.lexicon_engine import get_lexicon_engine
        lex = get_lexicon_engine()
        if lex.available:
            lex_result = lex.scan(text)
            if lex_result.get("is_hit"):
                total_score += lex_result.get("score", 0)
                matched_rules.append({
                    "score": lex_result.get("score", 0),
                    "reason": lex_result.get("reason", "词库命中"),
                })
                hits = lex_result.get("hits") or []
                if hits:
                    logger.info(
                        f"[广告检测] 词库命中: "
                        + "、".join(f"{h.get('category')}:{h.get('word')}" for h in hits[:5])
                    )
                lex_only = True
                sources.append("词库")
    except Exception as e:
        logger.debug(f"词库扫描跳过: {e}")

    # 第一阶段：规则引擎快速检测
    for rule in AD_RULES:
        match = re.search(rule["pattern"], text_lower)
        if match:
            matched_rules.append(rule)
            total_score += rule["score"]
            lex_only = False
            if "规则" not in sources:
                sources.append("规则")

    # 简介推广性检测（当消息较短/无意义时）
    if len(text) < 15 and user_bio:
        bio_lower = user_bio.lower()
        for kw in BIO_SPAM_KEYWORDS:
            if kw in bio_lower:
                total_score += 30
                matched_rules.append({"score": 30, "reason": f"简介含推广词「{kw}」"})
                lex_only = False
                if "简介" not in sources:
                    sources.append("简介")
                break

    # 组合加分：高收益话术 + 引流行为 = 明确广告
    has_income = any(r["score"] >= 25 and ("话术" in r["reason"] or "词库" in r["reason"] or "黑产" in r["reason"] or "兼职" in r["reason"] or "投资" in r["reason"]) for r in matched_rules)
    has_redirect = any("引流" in r["reason"] or "链接" in r["reason"] or "加群" in r["reason"] or "诱导" in r["reason"] or "扫码" in r["reason"] or "推广" in r["reason"] or "微信" in r["reason"] for r in matched_rules)
    if has_income and has_redirect:
        total_score = max(total_score, 85)
        matched_rules.append({"score": 0, "reason": "高收益+引流组合"})
        lex_only = False

    if len(matched_rules) >= 3:
        total_score = min(100, total_score + 15)
    total_score = min(100, total_score)

    def _pack(score: int, reason: str, is_ad: bool, src: list) -> dict:
        return {
            "score": score,
            "reason": reason,
            "is_ad": is_ad,
            "source": "+".join(src) if src else "unknown",
            "sources": src,
        }

    # 仅词库命中：交给 AI 或降权，避免日常聊天误伤
    if lex_only and total_score >= SCORE_MUTE:
        ai_result = await _ai_analyze_with_retry(text, username, user_bio)
        if ai_result is not None:
            ai_score = int(ai_result.get("score") or 0)
            if ai_score < 50:
                logger.info(
                    f"[广告检测] 纯词库高分但AI判低分: lex={total_score} ai={ai_score} "
                    f"text={text[:40]!r}"
                )
                return _pack(
                    ai_score,
                    ai_result.get("reason") or "日常聊天/非广告",
                    False,
                    ["词库", "AI"],
                )
            return _pack(
                min(total_score, max(ai_score, 50)),
                ai_result.get("reason") or "词库+AI",
                min(total_score, max(ai_score, 50)) >= SCORE_MUTE,
                ["词库", "AI"],
            )
        # AI不可用时：高分(score>=80)直接拦截，中分降权避免误伤
        fallback_score = min(total_score, 45) if total_score < 80 else total_score
        logger.info(f"[广告检测] 纯词库高分且无AI，降权放行: {total_score}->{fallback_score} text={text[:40]!r}")
        return _pack(
            fallback_score,
            matched_rules[0]["reason"] if matched_rules else "词库命中",
            fallback_score >= SCORE_MUTE,
            ["词库"],
        )

    # 规则明确命中高分：快速拦截
    if total_score >= SCORE_MUTE:
        reasons = [r["reason"] for r in matched_rules[:3]]
        if len(matched_rules) > 3:
            reasons.append(f"等{len(matched_rules)}项命中")
        return _pack(total_score, "、".join(reasons), True, sources or ["规则"])

    # AI 语义分析
    if total_score > 0 or len(text) > 20:
        ai_result = await _ai_analyze_with_retry(text, username, user_bio)
        if ai_result:
            ai_score = ai_result.get("score", 0)
            ai_reason = ai_result.get("reason", "")
            ai_src = (sources or []) + (["AI"] if "AI" not in sources else [])
            if ai_score > total_score:
                return _pack(
                    int(ai_score),
                    ai_reason or "",
                    int(ai_score) >= SCORE_MUTE,
                    ai_src if sources else ["AI"],
                )
            elif ai_score > 0 and total_score > 0:
                # AI 判定正常聊天(score<=20)时，词库/规则命中大幅降权，避免误伤
                if ai_score <= 20:
                    merged_score = min(total_score, max(ai_score, 25))
                    logger.info(
                        f"[广告检测] AI判正常聊天降权: rules={total_score} ai={ai_score} "
                        f"merged={merged_score} text={text[:40]!r}"
                    )
                else:
                    merged_score = min(100, total_score + ai_score // 2)
                merged_reason = "、".join(filter(None, [
                    "、".join([r["reason"] for r in matched_rules[:2]]),
                    ai_reason,
                ]))
                return _pack(merged_score, merged_reason, merged_score >= SCORE_MUTE, ai_src)

    if matched_rules:
        reasons = [r["reason"] for r in matched_rules[:3]]
        if len(matched_rules) > 3:
            reasons.append(f"等{len(matched_rules)}项命中")
        max_reason = "、".join(reasons)
    else:
        max_reason = ""

    return _pack(total_score, max_reason, total_score >= SCORE_MUTE, sources or [])


def summarize_ad_reason(reason: str = "", ocr_text: str = "", is_image: bool = False) -> str:
    """
    将详细命中原因压缩为群内可展示的简短总结。
    不复述广告原文，避免二次传播。
    """
    blob = f"{reason or ''} {ocr_text or ''}".lower()

    # 按严重程度优先匹配
    rules = [
        (("刷单", "跑分", "租号", "出号", "卡商", "接码", "黑产", "洗钱"), "消息包含黑产/诈骗话术"),
        (("兼职", "日结", "月入", "天入", "时薪", "副业", "躺赚", "稳赚", "一单一结", "代刷"), "消息包含典型的网络兼职诈骗话术"),
        (("投资", "理财", "基金", "币圈", "翻倍", "回报"), "消息包含高收益投资诱导话术"),
        (("引流", "加v", "加微", "私聊", "t.me", "二维码", "扫码", "进群", "加群"), "消息含引流/诱导加联系方式内容"),
        (("免费领", "免费送", "白嫖", "0元", "诱饵"), "消息含诱导点击/免费诱饵话术"),
        (("色情", "黄", "约炮"), "消息包含违规低俗内容"),
        (("政治", "反动", "暴恐"), "消息包含严重违规内容"),
        (("商业推广", "代购", "促销", "特价"), "消息包含商业推广内容"),
        (("词库", "广告", "高收益", "链接"), "消息包含广告/推广话术"),
    ]
    for keys, summary in rules:
        if any(k in blob for k in keys):
            prefix = "图片" if is_image else "消息"
            # 统一用「消息/图片」开头，避免重复
            if summary.startswith("消息"):
                return summary if not is_image else summary.replace("消息", "图片", 1)
            return f"{prefix}{summary}"

    if is_image:
        return "图片包含疑似广告/诈骗内容"
    return "消息包含疑似广告/推广内容"


async def _ai_analyze_with_retry(text: str, username: str, user_bio: str, max_retries: int = 3) -> dict | None:
    """带重试机制的 AI 语义分析，在独立线程中运行避免阻塞 bot 事件循环"""
    if not AI_ENABLED:
        return None

    prompt = (
        "你是一个广告/垃圾消息检测专家。分析以下消息，判断是否为广告或垃圾消息。\n\n"
        f"消息内容：{text}\n"
        f"用户名：{username or '无'}\n"
        f"用户简介：{user_bio or '无'}\n\n"
        '请严格按以下JSON格式回复，不要输出其他内容：\n'
        '{"score": 0-100, "reason": "一句话类型总结，不要复述原文"}\n'
        "评分标准：\n"
        "- 0-30：正常消息\n"
        "- 31-49：疑似广告，但不确定\n"
        "- 50-69：较大概率是广告\n"
        "- 70-89：明确是广告\n"
        "- 90-100：恶劣广告/诈骗\n\n"
        "reason 要求：\n"
        "- 只用简短类型描述，例如「网络兼职诈骗话术」「引流加联系方式」「高收益投资诱导」\n"
        "- 禁止复述消息原文、禁止写出具体收益数字/联系方式/链接\n\n"
        "重要判断原则：\n"
        "- 如果消息是在【讨论】或【描述】某种技术/行为（包括黑产技术），而不是在【推广】或【招揽】，应评低分（0-30）\n"
        "- 只有明确包含【推销话术】（如收益承诺、引导加群/私聊、诱导点击链接等）才评高分\n"
        "- 正常的技术讨论、经验分享、问题解答不应被误判为广告\n"
        "- API key分享、token分享、激活码/兑换码分享、技术资源链接（如GitHub、开源项目）属于【技术福利分享】，应评低分（0-20）\n"
        "- 截图中包含URL + API key（如 sk-xxx 格式）是技术资源分享，不是广告\n"
    )

    def _sync_call():
        """在独立线程中同步调用 OpenAI，返回 (content, reasoning_content)"""
        import httpx
        from openai import OpenAI
        ad_key = settings.AD_AI_API_KEY or settings.AI_API_KEY
        proxy_url = settings.PROXY_URL or None
        http_client = httpx.Client(proxy=proxy_url, timeout=AI_TIMEOUT) if proxy_url else None
        client = OpenAI(
            api_key=ad_key,
            base_url=settings.AI_BASE_URL if settings.AI_BASE_URL else None,
            http_client=http_client,
        )
        response = client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=[
                {"role": "system", "content": "你是广告检测AI。只返回JSON格式{\"score\":数字,\"reason\":\"理由\"}，不要其他文字，不要markdown代码块。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=500,
        )
        msg = response.choices[0].message
        content = (msg.content or "").strip()
        # 去除 markdown 代码块包裹（```json ... ```）
        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)
            content = content.strip()
        # agnes-2.0-flash 等推理模型可能将输出放在 reasoning_content 中
        reasoning_content = ""
        try:
            reasoning_content = (getattr(msg, "reasoning_content", None) or "").strip()
        except Exception:
            pass
        return content, reasoning_content

    for attempt in range(max_retries):
        try:
            content, reasoning_content = await asyncio.wait_for(
                asyncio.to_thread(_sync_call),
                timeout=AI_TIMEOUT,
            )
            logger.info(f"[AI广告检测] 原始返回: [{content[:100]}]")

            if not content:
                # content 为空时，从 reasoning_content 中提取评分
                if reasoning_content:
                    logger.info(f"[AI广告检测] content为空, 从reasoning_content提取: [{reasoning_content[:100]}]")
                    # 从 reasoning_content 中提取 JSON
                    json_match = re.search(r'\{[^}]+\}', reasoning_content)
                    if json_match:
                        try:
                            result = json.loads(json_match.group())
                            score = max(0, min(100, int(float(result.get("score", 0)))))
                            reason = result.get("reason", "")
                            return {"score": score, "reason": reason, "is_ad": score >= SCORE_MUTE}
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.warning(f"[AI广告检测] reasoning_content JSON解析失败: {e}")
                    # 从 reasoning_content 中提取纯数字评分
                    score_match = re.search(r'score["\']?\s*[:=]\s*(\d+)', reasoning_content, re.IGNORECASE)
                    if score_match:
                        score = max(0, min(100, int(score_match.group(1))))
                        return {"score": score, "reason": "AI推理提取(无格式化输出)", "is_ad": score >= SCORE_MUTE}
                logger.warning(f"[AI广告检测] 返回空, 尝试 {attempt+1}/{max_retries}")
                await asyncio.sleep(0.3 * (attempt + 1))
                continue

            # 尝试解析 JSON
            json_match = re.search(r'\{[^}]+\}', content)
            if json_match:
                result = json.loads(json_match.group())
                score = max(0, min(100, int(float(result.get("score", 0)))))
                reason = result.get("reason", "")
                return {"score": score, "reason": reason, "is_ad": score >= SCORE_MUTE}

            # 尝试纯文本格式：第一行数字，第二行理由
            lines = content.split("\n", 1)
            score_str = re.sub(r'[^\d]', '', lines[0].strip())
            if score_str:
                score = max(0, min(100, int(score_str)))
                reason = lines[1].strip() if len(lines) > 1 else ""
                return {"score": score, "reason": reason, "is_ad": score >= SCORE_MUTE}

            logger.warning(f"[AI广告检测] 无法解析: {content[:100]}")

        except asyncio.TimeoutError:
            logger.warning(f"[AI广告检测] 超时, 尝试 {attempt+1}/{max_retries}")
        except Exception as e:
            logger.warning(f"[AI广告检测] 异常: {e}, 尝试 {attempt+1}/{max_retries}")

        if attempt < max_retries - 1:
            await asyncio.sleep(0.3 * (attempt + 1))

    logger.warning("[AI广告检测] 所有重试均失败，回退到规则引擎")
    return None


async def get_user_bio(bot: Bot, user_id: int) -> str:
    """获取用户简介"""
    try:
        chat = await bot.get_chat(user_id)
        return chat.bio or ""
    except Exception:
        return ""


# 记录最近检测过的用户，避免重复检测
_recent_checks = {}

async def check_and_handle_ad(message: Message, bot: Bot) -> bool:
    """
    检测消息是否为广告，如果是则自动处理
    返回 True 表示消息已被处理（应阻止后续流程）
    """
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text or message.caption or ""
    username = message.from_user.username or ""
    user_full = message.from_user.first_name or ""

    # 管理员不受检测
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in ("administrator", "creator"):
            return False
    except Exception:
        logger.debug(f"[广告检测] 查询管理员状态失败 user={user_id}")

    # 冷却检查
    cache_key = f"{chat_id}:{user_id}"
    now = asyncio.get_event_loop().time()
    if cache_key in _recent_checks and now - _recent_checks[cache_key] < CHECK_COOLDOWN:
        return False

    # 短消息快速预筛：太短的正常聊天跳过（但保留检测敏感词的权利）
    if len(text) < 5:
        return False

    # 获取用户简介（辅助判断）
    bio = await get_user_bio(bot, user_id)

    # 调用 AI 检测
    result = await ai_detect_ad(text, username, bio, group_id=chat_id)
    _recent_checks[cache_key] = now

    score = result["score"]
    reason = result["reason"]

    if score < SCORE_WARN:
        return False  # 正常消息

    # --- 广告/垃圾消息处理 ---
    logger.info(f"[广告检测] user={user_id}({user_full}) score={score} reason={reason}")

    # 构建通知（带解封按钮）
    builder = InlineKeyboardBuilder()
    builder.button(text="🔓 解除禁言", callback_data=f"ad_unmute:{user_id}:{chat_id}")
    if score >= SCORE_BAN:
        builder.button(text="✅ 解除封禁", callback_data=f"ad_unban:{user_id}:{chat_id}")
    builder.adjust(1, 1)

    score_label = f"🔴 {score}分" if score >= SCORE_BAN else f"🟠 {score}分"

    notify_text = (
        f"🚫 <b>AI 广告检测拦截</b>\n\n"
        f"用户：{user_full}\n"
        f"评分：{score_label}\n"
        f"原因：{reason}"
    )

    if score >= SCORE_BAN:
        # 封禁
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"[广告检测] 删除广告消息失败: {e}")
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except Exception as e:
            logger.error(f"[广告检测] 封禁失败: {e}")
        action_text = "用户已被<b>封禁</b>"
    elif score >= SCORE_MUTE:
        # 禁言 24 小时
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"[广告检测] 删除广告消息失败: {e}")
        try:
            mute_until = int(datetime.now(timezone.utc).timestamp()) + 86400
            await bot.restrict_chat_member(
                chat_id, user_id,
                until_date=mute_until,
                can_send_messages=False,
            )
        except Exception as e:
            logger.error(f"[广告检测] 禁言失败: {e}")
        action_text = "用户已被<b>禁言24小时</b>"
    else:
        # 警告：不删除消息，只通知管理员
        action_text = "⚠️ <b>疑似广告</b>，已通知管理员"

    # 发送通知到群组
    try:
        await bot.send_message(
            chat_id,
            f"{notify_text}\n\n⚙️ 处理：{action_text}",
            reply_markup=builder.as_markup(),
        )
    except Exception as e:
        logger.error(f"[广告检测] 发送通知失败: {e}")

    return True


# --- 解封/解禁回调 ---

@router.callback_query(F.data.startswith("ad_unmute:"))
async def ad_unmute(callback: CallbackQuery, bot: Bot):
    """解除禁言"""
    parts = callback.data.split(":")
    if len(parts) < 3:
        return await callback.answer("数据格式错误", show_alert=True)
    user_id = int(parts[1])
    chat_id = int(parts[2])

    if not await is_admin_unsafe(bot, chat_id, callback.from_user.id):
        return await callback.answer("只有管理员可以操作", show_alert=True)

    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
        )
        await callback.answer("✅ 已解除禁言", show_alert=True)
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ <b>管理员已解除禁言</b>",
            reply_markup=None,
        )
    except Exception as e:
        await callback.answer(f"解除失败: {e}", show_alert=True)


@router.callback_query(F.data.startswith("ad_unban:"))
async def ad_unban(callback: CallbackQuery, bot: Bot):
    """解除封禁"""
    parts = callback.data.split(":")
    if len(parts) < 3:
        return await callback.answer("数据格式错误", show_alert=True)
    user_id = int(parts[1])
    chat_id = int(parts[2])

    if not await is_admin_unsafe(bot, chat_id, callback.from_user.id):
        return await callback.answer("只有管理员可以操作", show_alert=True)

    try:
        await bot.unban_chat_member(chat_id, user_id)
        await callback.answer("✅ 已解除封禁", show_alert=True)
        await callback.message.edit_text(
            f"{callback.message.text}\n\n✅ <b>管理员已解除封禁</b>",
            reply_markup=None,
        )
    except Exception as e:
        await callback.answer(f"解除失败: {e}", show_alert=True)


async def is_admin_unsafe(bot: Bot, chat_id: int, user_id: int) -> bool:
    """检查是否为管理员（不抛异常）"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False
