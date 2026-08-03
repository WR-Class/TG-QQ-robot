"""
AI 语义匹配 FAQ 引擎
工作流程：
1. 优先关键词匹配（快速、零成本）
2. 关键词未命中时，使用 AI 进行语义相似度匹配（需配置 AI_API_KEY）
3. 支持按群组隔离 FAQ
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("semantic_faq")

# 缓存已加载的 FAQ 条目，减少 DB 查询
_faq_cache: List[Dict[str, Any]] = []
_faq_cache_time: float = 0
_FAQ_CACHE_TTL = 60  # 缓存刷新间隔（秒）


def _load_faq_entries() -> List[Dict[str, Any]]:
    """从数据库加载启用的 FAQ 条目，带本地缓存。"""
    global _faq_cache, _faq_cache_time
    now = time.time()
    if _faq_cache and now - _faq_cache_time < _FAQ_CACHE_TTL:
        return _faq_cache
    try:
        from handlers.moderation_store import list_faq_entries

        result = list_faq_entries(enabled_only=True, limit=5000)
        _faq_cache = result.get("items", [])
        _faq_cache_time = now
        logger.debug(f"[FAQ] 已加载 {len(_faq_cache)} 条 FAQ 条目")
    except Exception as e:
        logger.warning(f"[FAQ] 加载 FAQ 条目失败: {e}")
        if not _faq_cache:
            _faq_cache = []
    return _faq_cache


def match_faq(text: str, group_id: int = 0) -> Optional[Dict[str, Any]]:
    """
    同步匹配 FAQ：仅做关键词匹配（快速、不阻塞）。
    返回匹配到的条目 dict 或 None。
    """
    text = (text or "").strip()
    if not text:
        return None

    try:
        from handlers.moderation_store import match_faq_keyword

        result = match_faq_keyword(text, group_id)
        if result:
            logger.info(f"[FAQ] 关键词命中: {result['keyword']} -> {result['answer'][:40]}")
            return result
    except Exception as e:
        logger.warning(f"[FAQ] 关键词匹配异常: {e}")

    return None


async def match_faq_async(text: str, group_id: int = 0) -> Optional[Dict[str, Any]]:
    """
    异步匹配 FAQ（关键词 + AI 语义，适合在异步上下文中调用）。
    """
    text = (text or "").strip()
    if not text:
        return None

    # 1. 关键词匹配
    try:
        from handlers.moderation_store import match_faq_keyword

        result = match_faq_keyword(text, group_id)
        if result:
            logger.info(f"[FAQ] 关键词命中: {result['keyword']} -> {result['answer'][:40]}")
            return result
    except Exception as e:
        logger.warning(f"[FAQ] 关键词匹配异常: {e}")

    # 2. AI 语义匹配
    try:
        result = await _semantic_match_async(text, group_id)
        if result:
            logger.info(f"[FAQ] 语义命中: {result['keyword']} -> {result['answer'][:40]}")
            return result
    except Exception as e:
        logger.warning(f"[FAQ] 语义匹配异常: {e}")

    return None


def _get_candidates(group_id: int = 0) -> List[Dict[str, Any]]:
    """获取 AI 语义匹配候选条目（仅 semantic 类型）。

    keyword 类型条目不进入 AI 语义匹配候选，避免 AI 误判导致精确关键词
    条目被误触发（例如用户发 URL，AI 误关联到"群文档"）。
    keyword 条目由 match_faq_keyword() 做精确子串匹配，只有消息中真正
    包含关键词才会命中。
    """
    try:
        from handlers.moderation_store import list_semantic_faq_entries

        semantic_entries = list_semantic_faq_entries(group_id)
    except Exception as e:
        logger.warning(f"[FAQ] 获取语义条目失败: {e}")
        semantic_entries = []

    # 仅返回 semantic 类型条目（有 question/keyword 的）
    candidates = [
        e for e in semantic_entries
        if e.get("question", "").strip() or e.get("keyword", "").strip()
    ]
    return candidates


async def _semantic_match_async(text: str, group_id: int = 0) -> Optional[Dict[str, Any]]:
    """异步 AI 语义匹配。"""
    from config import settings

    if not settings.AI_API_KEY:
        return None

    candidates = _get_candidates(group_id)
    if not candidates:
        return None

    return await _call_ai_semantic_async(text, candidates)


async def _call_ai_semantic_async(user_text: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """异步调用 AI API 判断用户消息命中哪个 FAQ。"""
    from config import settings
    import httpx

    # 构建 FAQ 知识列表
    faq_items = []
    for i, entry in enumerate(candidates):
        question = entry.get("question", "").strip() or entry.get("keyword", "").strip()
        answer = entry.get("answer", "").strip()[:60]
        faq_items.append(f'{i+1}. 关键词: {question} -> 回答: {answer}')

    faq_text = "\n".join(faq_items)
    prompt = (
        "你是一个FAQ匹配助手。下面是已知的关键词-回答库。\n"
        "判断用户的消息是否在**明确询问或请求**某个关键词对应的内容。\n\n"
        "匹配规则（严格遵守）：\n"
        "1. 用户必须是在**主动询问/索要**该关键词对应的东西，而不是在聊天中顺带提及\n"
        "2. 例如关键词「群文档」：只有用户说「群文档在哪」「发一下群文档」「群文档地址」才算命中；"
        "用户说「这个任务太长了」「文档里写了什么」不算命中\n"
        "3. 例如关键词「中转地址」：只有用户说「中转地址是什么」「发一下中转地址」才算命中\n"
        "4. 普通聊天、讨论、闲聊中的偶发性词汇不算命中\n"
        "5. 如果不确定，输出0（不命中）\n\n"
        "如果命中，请只输出该条目的编号（数字）；如果都不匹配，输出0。\n"
        "只输出数字，不要输出任何其他文字。\n\n"
        f"关键词-回答库:\n{faq_text}\n\n"
        f"用户消息: {user_text}\n"
        "命中的条目编号:"
    )

    headers = {
        "Authorization": f"Bearer {settings.AI_API_KEY}",
        "Content-Type": "application/json",
    }
    base_url = (settings.AI_BASE_URL or "https://api.openai.com").rstrip("/")
    payload = {
        "model": settings.AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 10,
        "temperature": 0.1,
    }

    proxy = settings.PROXY_URL or None
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=15) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        if resp.status_code != 200:
            logger.warning(f"[FAQ] AI API 返回 HTTP {resp.status_code}: {resp.text[:100]}")
            return None
        data = resp.json()
        content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "").strip()
        try:
            idx = int(content)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
        except (ValueError, TypeError):
            # 非数字响应，尝试模糊匹配
            content_lower = content.lower()
            for i, entry in enumerate(candidates):
                kw = entry.get("keyword", "").lower()
                q = entry.get("question", "").lower()
                if kw in content_lower or q in content_lower:
                    return entry
    except Exception as e:
        logger.warning(f"[FAQ] AI API 调用异常: {e}")
    return None


def reload_faq_cache():
    """强制刷新 FAQ 缓存（外部调用，如 Web 后台新增后）。"""
    global _faq_cache_time
    _faq_cache_time = 0
    _load_faq_entries()
    logger.info("[FAQ] 缓存已刷新")
