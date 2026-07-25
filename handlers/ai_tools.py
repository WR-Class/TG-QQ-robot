"""
AI 工具调用协议（Function Calling）共享模块
- 定义所有 AI 可调用的工具 schema
- 提供工具执行函数
- 提供 AI 调用 + 工具分派 + 结果回填的统一处理流程
- napcat_ws.py（QQ 群聊/私聊）和 ai_chat.py（TG）共用此模块
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_tools")

AI_TIMEOUT = 25  # 工具调用超时


# ============================================================
# 工具 Schema 定义（OpenAI Function Calling 格式）
# ============================================================

AI_TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询中国城市的天气预报。支持实时天气和未来1-3天预报。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，如：北京、上海、广州",
                    },
                    "date": {
                        "type": "string",
                        "description": "日期，可选。支持中文：今天、明天、后天、3天后；也支持具体日期格式如 2026-07-26、2026/07/26。不传则返回实时天气。",
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息，如新闻、百科、知识问答等",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，建议用中文",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "获取指定URL的网页内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "网页URL链接",
                    },
                },
                "required": ["url"],
            },
        },
    },
]


# ============================================================
# 工具执行函数
# ============================================================

def _parse_weather_date(date_str: str):
    """解析天气查询日期，返回 (days_offset, label, error_msg)。

    支持：
    - 中文枚举：今天、明天、后天、3天后
    - 具体日期：2026-07-26、2026/07/26 等
    """
    from datetime import datetime, timedelta

    date_str = (date_str or "").strip()
    if not date_str or date_str == "今天":
        return 0, "今天", None

    date_map = {"今天": 0, "明天": 1, "后天": 2, "3天后": 3}
    if date_str in date_map:
        return date_map[date_str], date_str, None

    # 尝试解析具体日期格式
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            target = datetime.strptime(date_str, fmt)
            target = target.replace(hour=0, minute=0, second=0, microsecond=0)
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            delta = (target - today).days
            if delta < 0:
                return None, date_str, f"❌ {date_str} 是过去的日期，无法查询历史天气"
            if delta > 3:
                return None, date_str, f"❌ {date_str} 超出3天预报范围，wttr.in 仅支持未来3天"
            label_map = {0: "今天", 1: "明天", 2: "后天", 3: "3天后"}
            return delta, label_map.get(delta, date_str), None
        except ValueError:
            continue

    # 兜底：如果字符串中包含中文关键词
    for k, v in date_map.items():
        if k in date_str:
            return v, k, None

    return None, date_str, f"❌ 不支持的日期格式 '{date_str}'，请使用：今天、明天、后天、3天后，或具体日期如 2026-07-26"


async def execute_weather(city: str, date: str = "今天") -> str:
    """调用 wttr.in 查询天气。"""
    import httpx

    days_offset, date_label, err = _parse_weather_date(date)
    if err:
        return err

    def _sync_call() -> str:
        try:
            proxies = None
            from config import settings
            if settings.PROXY_URL:
                proxies = settings.PROXY_URL
            with httpx.Client(proxy=proxies, timeout=10) as client:
                url = f"https://wttr.in/{city}?format=j1&lang=zh"
                resp = client.get(url)
                data = resp.json()
        except Exception as e:
            return f"天气查询失败: {e}"

        try:
            if days_offset == 0:
                cc = data.get("current_condition", [{}])[0]
                temp = cc.get("temp_C", "?")
                humidity = cc.get("humidity", "?")
                desc = cc.get("lang_zh", [{}])[0].get("value", cc.get("weatherDesc", [{}])[0].get("value", "未知"))
                wind = cc.get("windspeedKmph", "?")
                feels = cc.get("FeelsLikeC", "?")
                return (
                    f"📍 {city}实时天气\n"
                    f"🌡 温度: {temp}°C（体感 {feels}°C）\n"
                    f"☁ 天气: {desc}\n"
                    f"💧 湿度: {humidity}%\n"
                    f"🌬 风速: {wind}km/h"
                )
            else:
                forecast = data.get("weather", [])
                if days_offset >= len(forecast):
                    return f"暂无{date_label}的预报数据"
                day = forecast[days_offset]
                date_str = day.get("date", "")
                max_t = day.get("maxtempC", "?")
                min_t = day.get("mintempC", "?")
                hourly = day.get("hourly", [])
                desc_list = []
                for h in hourly[:4]:
                    h_desc = h.get("lang_zh", [{}])[0].get("value", h.get("weatherDesc", [{}])[0].get("value", ""))
                    h_time = f"{int(h.get('time', '0')):04d}"[:2]
                    h_temp = h.get("tempC", "?")
                    h_rain = h.get("chanceofrain", "0")
                    desc_list.append(f"  {h_time}时 {h_desc} {h_temp}°C 降雨概率{h_rain}%")
                return (
                    f"📍 {city} {date_label}({date_str})天气\n"
                    f"🌡 温度: {min_t}°C ~ {max_t}°C\n"
                    f"📅 逐时预报:\n" + "\n".join(desc_list)
                )
        except Exception as e:
            return f"天气数据解析失败: {e}"

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=AI_TIMEOUT)
    except asyncio.TimeoutError:
        return "天气查询超时，请稍后重试"


async def execute_web_search(query: str) -> str:
    """Bing 爬虫搜索 + DuckDuckGo 兜底。"""
    def _sync_call() -> str:
        try:
            import httpx
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            proxy = None
            from config import settings
            if settings.PROXY_URL:
                proxy = settings.PROXY_URL
            with httpx.Client(proxy=proxy, timeout=10, follow_redirects=True) as client:

                # Bing 搜索
                resp = client.get(
                    "https://www.bing.com/search",
                    params={"q": query},
                    headers=headers,
                )
                if resp.status_code == 200:
                    results = []
                    # 提取每个 b_algo 块
                    for m in re.finditer(
                        r'<li class="b_algo"[^>]*>(.*?)</li>',
                        resp.text, re.S,
                    ):
                        block = m.group(1)
                        # 去掉 block 内的 link/css 标签
                        block_clean = re.sub(r'<link[^>]*/>', '', block)
                        # 提取标题
                        title_m = re.search(r'<h2[^>]*><a[^>]*>(.*?)</a>', block_clean, re.S)
                        if title_m:
                            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
                            if title:
                                results.append(f"📰 {title}")
                        # 提取摘要
                        caption_m = re.search(r'<div class="b_caption"[^>]*>(.*?)</div>', block_clean, re.S)
                        if caption_m:
                            snippet = re.sub(r'<[^>]+>', '', caption_m.group(1)).strip()
                            if len(snippet) > 15:
                                results.append(f"📝 {snippet}")
                    if results:
                        return "搜索结果：\n" + "\n".join(f"· {r[:250]}" for r in results[:8])

                # DuckDuckGo 兜底（接受 202 状态码）
                resp2 = client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                    headers=headers,
                )
                if resp2.status_code in (200, 202):
                    results = []
                    for m in re.finditer(
                        r'class="result__snippet"[^>]*>(.*?)</a>',
                        resp2.text, re.S,
                    ):
                        snippet = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                        if snippet:
                            results.append(f"📝 {snippet[:200]}")
                    # 也提取 DDG 的标题
                    for m in re.finditer(r'class="result__title"[^>]*>(.*?)</h2>', resp2.text, re.S):
                        title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                        if title:
                            results.append(f"📰 {title[:200]}")
                    if results:
                        return "搜索结果：\n" + "\n".join(f"· {r[:250]}" for r in results[:8])

                return "未找到相关搜索结果"
        except Exception as e:
            return f"搜索失败: {e}"

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=AI_TIMEOUT)
    except asyncio.TimeoutError:
        return "搜索超时，请稍后重试"


async def execute_fetch_webpage(url: str) -> str:
    """获取网页正文内容（截取前 3000 字符）。"""
    def _sync_call() -> str:
        try:
            import httpx
            from readability import Document
            from config import settings
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            proxy = settings.PROXY_URL or None
            with httpx.Client(proxy=proxy, timeout=10, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code != 200:
                    return f"获取网页失败: HTTP {resp.status_code}"
                doc = Document(resp.text)
                title = doc.title()
                summary = doc.summary()
                text = re.sub(r'<[^>]+>', '', summary).strip()
                text = re.sub(r'\s+', ' ', text)
                return f"标题: {title}\n内容: {text[:3000]}"
        except ImportError:
            return "缺少 readability 依赖库"
        except Exception as e:
            return f"获取网页失败: {e}"

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=AI_TIMEOUT)
    except asyncio.TimeoutError:
        return "获取网页超时，请稍后重试"


# ============================================================
# 工具调用分派表
# ============================================================

_TOOL_EXECUTORS = {
    "get_weather": execute_weather,
    "web_search": execute_web_search,
    "fetch_webpage": execute_fetch_webpage,
}


async def dispatch_tool_call(name: str, arguments: Dict[str, Any]) -> str:
    """分派并执行单个工具调用，返回结果字符串。"""
    executor = _TOOL_EXECUTORS.get(name)
    if not executor:
        return f"未知工具: {name}"
    try:
        return await executor(**arguments)
    except Exception as e:
        logger.warning(f"[AI工具] {name} 执行异常: {e}")
        return f"工具执行失败({name}): {e}"


# ============================================================
# AI 调用 + 工具调用统一处理流程
# ============================================================

async def ai_chat_with_tools(
    messages: List[Dict[str, Any]],
    *,
    system_prompt: str = "",
    max_tool_rounds: int = 2,
) -> Dict[str, Any]:
    """
    AI 调用统一入口：发送消息给 AI，自动处理工具调用，返回最终回复。

    Args:
        messages: 对话消息列表 [{"role": "user"/"assistant"/"tool", "content": ..., "tool_call_id": ...}]
        system_prompt: 系统提示词（会注入到消息列表头部）
        max_tool_rounds: 最大工具调用轮次（防止无限循环）

    Returns:
        {"content": "AI 最终回复文本", "tool_calls": [...], "tool_results": [...]}
    """
    from config import settings
    from openai import AsyncOpenAI

    api_key = str(getattr(settings, "AI_API_KEY", "") or "")
    base_url = str(getattr(settings, "AI_BASE_URL", "") or "")
    model = str(getattr(settings, "AI_MODEL", "agnes-2.0-flash") or "agnes-2.0-flash")

    if not api_key:
        return {"content": "AI 未配置 API Key", "tool_calls": [], "tool_results": []}

    # 注入系统提示词
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)

    all_tool_calls = []
    all_tool_results = []

    for round_idx in range(max_tool_rounds + 1):
        try:
            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": full_messages,
                "temperature": 0.7,
                "max_tokens": 1024,
            }
            if round_idx == 0:
                # 首轮传入工具定义
                kwargs["tools"] = AI_TOOLS_SCHEMA
                kwargs["tool_choice"] = "auto"
            # 后续轮次不传 tools/tool_choice，让 AI 用工具结果生成最终回复

            response = await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=AI_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return {"content": "AI 响应超时，请稍后重试", "tool_calls": all_tool_calls, "tool_results": all_tool_results}
        except Exception as e:
            logger.warning(f"[AI工具] AI 调用异常: {e}")
            return {"content": f"AI 调用失败: {e}", "tool_calls": all_tool_calls, "tool_results": all_tool_results}

        choice = response.choices[0] if response.choices else None
        if not choice or not choice.message:
            return {"content": "AI 无响应", "tool_calls": all_tool_calls, "tool_results": all_tool_results}

        msg = choice.message
        assistant_msg = {"role": "assistant", "content": msg.content or ""}

        # 诊断日志：检查 AI 是否使用了结构化 tool_calls
        raw_content = (msg.content or "").strip()
        has_tool_calls = bool(msg.tool_calls)
        logger.info(f"[AI工具] 第{round_idx}轮: tool_calls={has_tool_calls}, content长度={len(raw_content)}, content前200={raw_content[:200]}")

        # AI 请求调用工具
        if msg.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
            full_messages.append(assistant_msg)
            all_tool_calls.extend([tc.function.name for tc in msg.tool_calls])

            # 执行所有工具调用
            for tc in msg.tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                logger.info(f"[AI工具] 调用: {func_name}({args})")
                result = await dispatch_tool_call(func_name, args)
                all_tool_results.append({"name": func_name, "result": result[:500]})
                # 追加工具结果到消息列表
                full_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result[:2000],  # 限制长度避免超出上下文
                })

            # 继续下一轮（AI 用工具结果生成回复）
            # 追加一条 system 消息强制 AI 直接用工具结果回答
            tool_names = [tc.function.name for tc in msg.tool_calls]
            tool_summary = ", ".join(tool_names)
            full_messages.append({
                "role": "system",
                "content": (
                    f"工具 {tool_summary} 已执行完毕，结果已注入上方。"
                    f"请直接基于工具结果回答用户问题。"
                    f"禁止再次调用任何工具，禁止输出 <function> 或 <tool_call_ 等标记。"
                ),
            })
            continue

        # AI 没有调用工具，返回最终文本回复
        full_messages.append(assistant_msg)
        # 清理 content 中残留的函数调用文本（AI 幻觉产生的 <function=...> 等）
        content = (msg.content or "")
        content = re.sub(r'<function[\s\S]*?</function>', '', content, flags=re.DOTALL)
        content = re.sub(r'<tool_call_[\s\S]*?</tool_call_\w+>', '', content, flags=re.DOTALL)
        content = re.sub(r' Patreon[\s\S]*?$', '', content, flags=re.DOTALL)  # 切除 AI 模型残留的尾部广告
        content = content.strip()
        return {
            "content": content,
            "tool_calls": all_tool_calls,
            "tool_results": all_tool_results,
        }

    # 超出最大工具调用轮次
    return {
        "content": "处理完成，但可能未完全执行所有工具调用",
        "tool_calls": all_tool_calls,
        "tool_results": all_tool_results,
    }
