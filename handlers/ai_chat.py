"""
AI 客服模块
支持 OpenAI 兼容 API（可自定义 Base URL 和 API Key）
触发方式：
  - 私聊：关键词未匹配时自动调用 AI（可关闭）
  - 群组：@机器人 / 回复机器人消息 / 所有消息（可配置）
  
工具调用：
  - web_search(query: str) — 联网搜索，AI 自行决策何时调用
"""

import json
import logging
import re
from typing import List, Callable
from datetime import datetime, timezone, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from openai import AsyncOpenAI

from config import settings
from models.database import db

router = Router()
logger = logging.getLogger(__name__)

_ai_client: AsyncOpenAI | None = None

# ===== 工具定义 =====

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取实时信息，如天气、新闻、百科、价格等",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，简明扼要"
                }
            },
            "required": ["query"]
        }
    }
}

TOOLS = [WEB_SEARCH_TOOL]

# 天气关键词集合（用于判断是否为天气类搜索）
_WEATHER_KW = frozenset({"天气", "weather", "温度", "气温", "天気", "℃", "°c",
                         "天气预报", "weather forecast", "晴", "雨", "雪", "阴",
                         "风力", "湿度"})

# 温度数据正则（精确匹配，避免误提取）
# 优先级：完整范围 > 高温 > 低温
_TEMP_PATTERN = re.compile(
    # 格式1: "23~29℃" 或 "23-29°C"
    r'(?:(?:气温|温度|约|在)?\s*)'
    r'(\d{1,2})\s*[~\-–—]\s*(\d{1,2})\s*[°℃]\s*[cC]?'
    r'|'  # 格式2: "最高气温 33℃"
    r'最[高大][温气温约]*\s*(\d{1,2})\s*[°℃]\s*[cC]?'
    r'|'  # 格式3: "最低气温 25℃"
    r'最[低小][温气温约]*\s*(\d{1,2})\s*[°℃]\s*[cC]?'
    r'|'  # 格式4: "33℃" 单独出现（必须临近天气关键词）
    r'(?:(?:气温|温度|约|在|仅|只有|高达|升至|降至)\s*)'
    r'(\d{1,2})\s*[°℃]\s*[cC]?'
)


def get_ai_client() -> AsyncOpenAI:
    global _ai_client
    if _ai_client is None:
        kwargs = {"api_key": settings.AI_API_KEY}
        if settings.AI_BASE_URL:
            kwargs["base_url"] = settings.AI_BASE_URL
        _ai_client = AsyncOpenAI(**kwargs)
    return _ai_client


# ---------- 天气搜索优化 ----------

def _is_weather_query(query: str) -> bool:
    """判断是否为天气类搜索"""
    q = query.lower()
    return any(kw in q for kw in _WEATHER_KW)


def _improve_weather_query(query: str) -> str:
    """优化天气类查询词，提高搜索结果命中率。
    例如 '明天青岛天气' → '青岛天气预报 明天 气温'"""
    if not _is_weather_query(query):
        return query

    # 如果已含"预报"关键词就不重复追加
    if "预报" in query or "forecast" in query.lower():
        return query

    # 提取城市名优化查询
    m = re.search(r'([\u4e00-\u9fff]{2,6}(?:市|区|县)?)'
                  r'(今天|明天|后天|昨日|今日|明日|后天)?\s*'
                  r'(天气|气温|温度)', query)
    if m:
        city = m.group(1)
        time_part = m.group(2) or ""
        return f"{city}天气预报 {time_part} 气温".strip()

    return query + " 天气预报"


def _maybe_extract_temperatures(texts: list[str]) -> list[str]:
    """从原始文本中尝试提取温度数值，返回形如 ['23~29°C', '33°C'] 的列表"""

    def is_weather_context(text: str) -> bool:
        """判断文本是否包含天气上下文"""
        return any(kw in text.lower() for kw in [
            "天气", "气温", "温度", "预报", "℃", "°c", "°c",
            "晴", "多云", "阴", "雨", "雪", "风", "湿度",
            "weather", "forecast", "sunny", "cloudy", "rain",
            "最高", "最低", "高温", "低温",
        ])

    def format_temp(groups: tuple, source_text: str) -> str | None:
        """将正则 groups 格式化为温度字符串"""
        parts = [g for g in groups if g]
        if not parts:
            return None
        nums = [int(g) for g in parts]

        # 范围格式：两个数字
        if len(parts) >= 2:
            low, high = nums[0], nums[1]
            if low > high:
                low, high = high, low
            if high - low > 20:
                return None
            # 夏季（5-9月）合理范围 10~45°C
            if high > 45 or low < -10:
                return None
            return f"{low}~{high}°C"

        # 单个数字
        val = nums[0]
        # 单温低于 -20°C 或高于 50°C 不合理
        if val < -20 or val > 50:
            return None
        return f"{val}°C"

    found = []
    for t in texts:
        if not is_weather_context(t):
            continue
        for m in _TEMP_PATTERN.finditer(t):
            val = format_temp(m.groups(), t)
            if val and val not in found:
                found.append(val)

    # 按优先级排序：范围优先
    ranges = [v for v in found if "~" in v]
    singles = [v for v in found if "~" not in v]

    result = list(dict.fromkeys(ranges + singles))  # 去重保持顺序
    return result[:3]


async def _search_searxng(query: str, language: str = "zh-CN") -> list[dict]:
    """底层 SearXNG 搜索，返回原始结果列表"""
    import httpx
    params = {
        "q": query,
        "format": "json",
        "language": language,
        "categories": "general",
        "pageno": 1,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{settings.SEARXNG_BASE_URL}/search", params=params)
        logger.info(f"[SearXNG] 请求 '{query}' 状态码={resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        logger.info(f"[SearXNG] 结果数={len(results)}")
        return results


def _format_search_results(results: list[dict], max_items: int = 10) -> str:
    """将搜索结果格式化为 markdown 字符串"""
    lines = []
    for r in results[:max_items]:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()
        engine = r.get("engine", "")
        if title and url:
            tag = f"[{engine}]" if engine else ""
            lines.append(f"- {tag}[{title}]({url})\n  {content[:300]}")
    if not lines:
        return "未找到相关结果"
    return "## 搜索结果\n\n" + "\n\n".join(lines)


# ---------- 主动天气提取器 ----------

_WEATHER_SITES = {"中国天气网", "weather.com.cn", "天气网", "天气预报",
                  "墨迹天气", "mojiweather", "2345天气预报",
                  "AccuWeather", "weather"}


def _is_weather_result(r: dict) -> bool:
    """判断一条搜索结果是否与天气数据相关"""
    title = (r.get("title") or "").lower()
    content = (r.get("content") or "").lower()
    combined = title + " " + content
    # 只要有温度数字就算天气结果
    if re.search(r'\d{1,2}\s*[°℃]', combined):
        return True
    # 标题包含天气关键词
    if any(kw in combined for kw in ["预报", "天气", "气温", "温度", "℃", "°c"]):
        return True
    # 来自天气类网站
    site = (r.get("parsed_url") or [""])[1] or ""
    if any(ws in site for ws in _WEATHER_SITES):
        return True
    return False


async def _smart_weather_search(query: str) -> str:
    """智能化天气搜索：提取关键天气数据，返回结构化摘要给 AI"""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone(timedelta(hours=8)))
    today_str = now.strftime("%Y年%m月%d日")
    tomorrow = now + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y年%m月%d日")

    improved_query = _improve_weather_query(query)
    logger.info(f"[天气] 原始查询: '{query}', 优化后: '{improved_query}', 今天: {today_str}")

    # 尝试提取城市名
    city_match = re.search(r'([\u4e00-\u9fff]{2,6}(?:市|区|县)?)\s*(?:今天|明天|后天|昨日|今日|明日|后天)?\s*(?:天气|气温|温度|weather)', query)
    city_name = city_match.group(1) if city_match else ""

    # 构造多组备选查询
    is_tomorrow = any(kw in query for kw in ["明天", "明日", "tomorrow"])
    is_day_after = any(kw in query for kw in ["后天"])
    day_label = "后天" if is_day_after else ("明天" if is_tomorrow else "今天")

    alt_queries = [improved_query, query]
    if is_tomorrow and city_name:
        alt_queries.append(f"{city_name} {tomorrow_str} 天气预报")
    elif is_day_after and city_name:
        day_after = now + timedelta(days=2)
        alt_queries.append(f"{city_name} {day_after.strftime('%Y年%m月%d日')} 天气预报")
    else:
        alt_queries.append(f"{query} {today_str}")

    # 收集所有天气相关结果
    all_weather_results = []
    seen_urls = set()
    for aq in alt_queries:
        if len(all_weather_results) >= 15:
            break
        try:
            results = await _search_searxng(aq)
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls and _is_weather_result(r):
                    seen_urls.add(url)
                    all_weather_results.append(r)
        except Exception:
            continue

    logger.info(f"[天气] 累计天气相关结果: {len(all_weather_results)}")

    # ===== 核心提取：从搜索结果中解析温度 =====
    # 按可信度排序：天气官网（weather.com.cn, tianqi.com 等）排前
    def _site_score(r: dict) -> int:
        url = r.get("url", "").lower()
        title = (r.get("title") or "").lower()
        score = 0
        if any(s in url for s in ["weather.com.cn", "tianqi.com", "tianqiy.com", "weather"]):
            score += 10
        if "天气预报" in title or "天气查询" in title:
            score += 5
        if "旅游" in title or "攻略" in title or "景点" in title or "百科" in title:
            score -= 10
        return -score

    all_weather_results.sort(key=_site_score)

    # 从 top 结果提取温度
    raw_texts = []
    for r in all_weather_results[:20]:
        raw_texts.append(r.get("title", "") + " " + (r.get("content", "") or ""))

    temp_values = _maybe_extract_temperatures(raw_texts)

    # 取最合理的温度范围（优先天气官网的）
    best_temp = ""
    if temp_values:
        # 优先选范围（如 23~29°C）
        ranges = [t for t in temp_values if "~" in t]
        if ranges:
            best_temp = ranges[0]
        else:
            # 没有范围则用第一个单温（通常是最高温）
            best_temp = temp_values[0]

    # 尝试从内容中提取天气状况（晴/多云/雨等）
    weather_condition = ""
    condition_kw = ["晴", "多云", "阴", "小雨", "中雨", "大雨", "雷阵雨",
                    "阵雨", "暴雨", "雪", "小雪", "中雪", "大雪", "雾", "霾",
                    "sunny", "cloudy", "rain", "clear"]
    for t in raw_texts:
        t_lower = t.lower()
        for kw in condition_kw:
            if kw in t_lower:
                weather_condition = kw
                break
        if weather_condition:
            break

    # 尝试提取风力
    wind_info = ""
    wind_match = re.search(r'([南南北北东东西西]+?风\s*\d+\s*[级级]?)', " ".join(raw_texts))
    if wind_match:
        wind_info = wind_match.group(1)

    # ===== 构建给 AI 的清晰数据摘要 =====
    lines = []
    lines.append(f"【{city_name or '该城市'}天气预报 - {day_label}】")

    if best_temp:
        lines.append(f"温度：{best_temp}")
    if weather_condition:
        lines.append(f"天气状况：{weather_condition}")
    if wind_info:
        lines.append(f"风力：{wind_info}")

    # 附加原始搜索结果（只放天气官网的，且不超过 2 条）
    top_sites = [r for r in all_weather_results
                 if any(s in (r.get("url","") or "").lower()
                        for s in ["weather.com.cn", "tianqi.com", "tianqiy.com", "weather"])]
    if top_sites:
        lines.append("\n🔗 数据来源：")
        for r in top_sites[:2]:
            url = r.get("url", "")
            title = (r.get("title") or "").strip()[:50]
            if url:
                lines.append(f"- {title}: {url}")

    result = "\n".join(lines)
    logger.info(f"[天气] 最终摘要:\n{result}")
    return result


# ---------- 通用搜索 (weather-aware) ----------

async def web_search_impl(query: str) -> str:
    """联网搜索入口。天气类查询走智能化分支，其他走通用分支。"""
    if _is_weather_query(query):
        return await _smart_weather_search(query)

    # 通用搜索（非天气）
    try:
        results = await _search_searxng(query)
        if not results:
            return "未找到相关结果"
        return _format_search_results(results, max_items=8)
    except Exception as e:
        logger.error(f"[SearXNG] 搜索失败: {e}")
        return "搜索暂时不可用，请稍后再试"


async def chat_with_ai(user_id: int, messages: List[dict]) -> str:
    """调用 AI API 进行对话，支持工具调用"""
    if not settings.AI_ENABLED or not settings.AI_API_KEY:
        return ""

    client = get_ai_client()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y年%m月%d日 %H:%M:%S")
    system_content = (
        f"{settings.AI_SYSTEM_PROMPT}\n\n"
        f"当前时间：{now}（北京时间 UTC+8）\n"
        f"当需要实时信息（如天气、新闻、股票、百科等）时，必须使用 web_search 工具搜索互联网。\n"
        f"对于天气查询，搜索后直接提取结果中的温度、天气状况等信息回答用户。\n"
        f"重要规则：\n"
        f"1. web_search 返回的结果中必定包含天气数据（温度、天气状况等），你必须从中提取并回答。\n"
        f"2. 搜索结果中会有「📊 温度概况」部分，里面的数字就是温度数据，直接使用它们。\n"
        f"3. 绝对不允许说「数据格式解析失败」「无法获取」「技术接口错误」等空泛的失败信息——\n"
        f"   搜索结果里有你需要的一切数据，直接读出来告诉用户。\n"
        f"4. 如果搜索结果包含天气数据，直接从中提取并回答，不要说自己无法获取。\n"
        f"5. 如果你不确定某个信息，请使用 web_search 搜索而不是凭记忆回答。"
    )
    system_msg = {"role": "system", "content": system_content}
    full_messages = [system_msg] + messages

    try:
        logger.info(f"[AI] 发送请求到 {settings.AI_MODEL}，消息数={len(full_messages)}")
        response = await client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=full_messages,
            tools=TOOLS,
            temperature=0.7,
            max_tokens=2048,
        )
        logger.info(f"[AI] API 返回成功，choices={len(response.choices)}")
    except Exception as e:
        logger.error(f"[AI] API 调用失败: {e}")
        return "⚠️ AI 服务暂时不可用，请稍后再试。"

    msg = response.choices[0].message
    finish_reason = response.choices[0].finish_reason
    has_tools = bool(msg.tool_calls)
    logger.info(f"[AI] 首次响应 finish_reason={finish_reason}, tool_calls={has_tools}")

    # 判断 AI 是否调用了工具
    if msg.tool_calls:
        full_messages.append(msg)
        for tc in msg.tool_calls:
            logger.info(f"[AI] 工具调用: name={tc.function.name}, id={tc.id}")
            logger.info(f"[AI] 工具参数: {tc.function.arguments[:200]}")
            if tc.function.name == "web_search":
                args = json.loads(tc.function.arguments)
                query = args.get("query", "")
                logger.info(f"[AI工具] 调用 web_search: {query}")
                result = await web_search_impl(query)
                logger.info(f"[AI工具] web_search 返回 {len(result)} 字符")
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result[:1500]
                })

        # 把搜索结果发给 AI 生成最终回答
        try:
            logger.info(f"[AI] 发送二次请求, 消息数={len(full_messages)}")
            second_response = await client.chat.completions.create(
                model=settings.AI_MODEL,
                messages=full_messages,
                temperature=0.7,
                max_tokens=2048,
            )
            content = second_response.choices[0].message.content or ""
            logger.info(f"[AI] 二次响应: {content[:200]}")
            return content
        except Exception as e:
            logger.error(f"[AI] 二次调用失败: {e}")
            return "⚠️ AI 处理搜索结果时出错"

    logger.info(f"[AI] 纯文本响应: {msg.content[:150]}")
    return msg.content or ""


# --- 消息处理 ---

@router.message(F.chat.type == "private", F.text)
async def ai_private_chat(message: Message):
    if not settings.AI_ENABLED or not settings.AI_PRIVATE_AUTO:
        return
    if message.text.startswith("/"):
        return

    reply = await chat_with_ai(message.from_user.id, [{"role": "user", "content": message.text}])
    if reply:
        try:
            await message.reply(reply)
        except TelegramBadRequest:
            # Telegram 消息已删除或无法编辑，可忽略
            pass


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def ai_group_chat(message: Message, bot: Bot):
    if not settings.AI_ENABLED:
        return

    text = message.text
    bot_info = await bot.me()
    bot_username = bot_info.username

    triggered = False
    if settings.AI_GROUP_TRIGGER == "mention":
        if bot_username and f"@{bot_username}" in text:
            triggered = True
            text = text.replace(f"@{bot_username}", "").strip()
    elif settings.AI_GROUP_TRIGGER == "reply":
        if message.reply_to_message and message.reply_to_message.from_user.id == bot_info.id:
            triggered = True
    elif settings.AI_GROUP_TRIGGER == "all":
        triggered = True

    if not triggered:
        return

    reply = await chat_with_ai(message.from_user.id, [{"role": "user", "content": text}])
    if reply:
        try:
            await message.reply(reply)
        except TelegramBadRequest:
            pass


# --- 命令 ---

@router.message(Command("ai"))
async def cmd_ai(message: Message):
    if not settings.AI_ENABLED:
        return await message.reply("❌ AI 客服功能未启用。")

    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        return await message.reply("用法: /ai 你的问题")

    question = text[1]
    await message.reply("🤖 思考中...")

    reply = await chat_with_ai(message.from_user.id, [{"role": "user", "content": question}])
    if reply:
        try:
            await message.reply(f"🤖 {reply}")
        except TelegramBadRequest:
            pass


@router.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "👋 你好！我是 TGJQR 群管机器人。\n\n"
        "私聊中可用命令：\n"
        "  /start — 显示此帮助\n"
        "  /ai 你的问题 — 调用 AI 对话（支持联网搜索）\n"
        "  /ai_status — 查看 AI 配置状态\n"
        "  /listreply — 查看自动回复规则\n\n"
        "群组中可用命令：\n"
        "  /ban /mute /kick /warn — 群管操作\n"
        "  /setwelcome — 设置欢迎消息\n"
        "  /settings — 查看群组设置\n"
        "  /addreply 关键词 | 回复 — 添加自动回复\n"
        "  /addschedule — 添加定时消息\n\n"
        "💡 把我拉入群组并设为管理员，即可开始使用群管功能。"
    )
    await message.reply(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)


@router.message(Command("ai_status"))
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
        f"私聊自动: {'开' if settings.AI_PRIVATE_AUTO else '关'}\n"
        f"联网搜索: ✅ 开（AI 自动调用）"
    )
    await message.reply(text)
