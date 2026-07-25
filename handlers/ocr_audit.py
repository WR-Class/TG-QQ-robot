"""
OCR / 图片广告审核
从图片 URL 调用视觉模型提取文字，再走词库 + 广告规则评分。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("ocr_audit")

SCORE_MUTE = 50
AI_TIMEOUT = 25


class OcrAuditor:
    def __init__(self):
        try:
            from config import settings
            self.enabled = bool(getattr(settings, "OCR_ENABLED", True))
            self.min_text_len = int(getattr(settings, "OCR_MIN_TEXT_LEN", 4) or 4)
        except Exception:
            self.enabled = self._env_bool("OCR_ENABLED", True)
            self.min_text_len = int(os.environ.get("OCR_MIN_TEXT_LEN", "4") or 4)

    @staticmethod
    def _env_bool(key: str, default: bool) -> bool:
        v = os.environ.get(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def reload(self):
        self.__init__()

    async def extract_text_from_image(self, image_url: str) -> str:
        """调用视觉模型识别图片文字。失败返回空串。"""
        if not image_url:
            return ""

        from config import settings
        if not settings.AI_ENABLED:
            return ""

        def _sync_call() -> str:
            import httpx
            from openai import OpenAI

            api_key = settings.AD_AI_API_KEY or settings.AI_API_KEY
            if not api_key:
                return ""
            proxy_url = settings.PROXY_URL or None
            if proxy_url:
                with httpx.Client(proxy=proxy_url, timeout=AI_TIMEOUT) as http_client:
                    client = OpenAI(
                        api_key=api_key,
                        base_url=settings.AI_BASE_URL if settings.AI_BASE_URL else None,
                        http_client=http_client,
                    )
                    response = client.chat.completions.create(
                        model=settings.AI_MODEL,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "你是图片文字提取助手。只输出图片中可见的中文/英文文字，"
                                    "不要解释，不要加前缀。若几乎没有文字，输出 EMPTY。"
                                ),
                            },
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "请提取这张图片里的全部文字："},
                                    {"type": "image_url", "image_url": {"url": image_url}},
                                ],
                            },
                        ],
                        temperature=0.0,
                        max_tokens=500,
                    )
                    return (response.choices[0].message.content or "").strip()
            else:
                client = OpenAI(
                    api_key=api_key,
                    base_url=settings.AI_BASE_URL if settings.AI_BASE_URL else None,
                )
                response = client.chat.completions.create(
                    model=settings.AI_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是图片文字提取助手。只输出图片中可见的中文/英文文字，"
                                "不要解释，不要加前缀。若几乎没有文字，输出 EMPTY。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "请提取这张图片里的全部文字："},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        },
                    ],
                    temperature=0.0,
                    max_tokens=500,
                )
                return (response.choices[0].message.content or "").strip()

        try:
            content = await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=AI_TIMEOUT)
            if not content or content.upper() == "EMPTY":
                return ""
            # 去掉 markdown 代码块包装
            content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
            return content.strip()
        except Exception as e:
            logger.warning(f"[OCR] 识图失败: {e}")
            return ""

    async def audit_image(
        self,
        image_url: str,
        username: str = "",
        extra_text: str = "",
        group_id: int = 0,
    ) -> dict:
        """
        审核图片。
        流程：OCR 提取文字 → 词库+规则+AI文本评分 → 低分时 AI 多模态语义分析
        返回: {score, reason, is_ad, ocr_text, source}
        """
        if not self.enabled:
            return {"score": 0, "reason": "OCR未启用", "is_ad": False, "ocr_text": "", "source": "disabled"}

        ocr_text = await self.extract_text_from_image(image_url)
        # 清除 OCR 文字中的 URL（含裸域名），图片里的链接不算广告（官方群管家会处理）
        ocr_text_clean = re.sub(
            r'https?://[^\s]+|(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s]*)?',
            '',
            ocr_text,
        ).strip()
        combined = " ".join(x for x in [extra_text, ocr_text_clean] if x).strip()

        if not combined or len(combined) < self.min_text_len:
            return {
                "score": 0,
                "reason": "图片未识别到足够文字",
                "is_ad": False,
                "ocr_text": ocr_text,
                "source": "ocr_empty",
            }

        # 福利分享识别：API key、带key的URL截图等
        if _is_tech_share_image(combined):
            logger.info(f"[OCR] 技术资源/福利分享截图放行: {combined[:60]}")
            return {
                "score": 0,
                "reason": "技术资源/福利分享",
                "is_ad": False,
                "ocr_text": ocr_text,
                "source": "ocr_tech_share",
            }

        # 复用广告检测（词库 + 规则 + AI 文本语义）
        try:
            from handlers.ad_detector import ai_detect_ad, SCORE_MUTE as AD_SCORE_MUTE
            result = await ai_detect_ad(combined, username or "图片用户", "", group_id=group_id)
            result = dict(result or {})
            text_score = result.get("score", 0)

            # 文本评分高分：直接采纳（广告特征明显）
            if text_score >= AD_SCORE_MUTE:
                result["ocr_text"] = ocr_text
                result["detail_reason"] = result.get("reason", "")
                result["source"] = "ocr+ad_detector"
                result["is_ad"] = True
                from handlers.ad_detector import summarize_ad_reason
                result["reason"] = summarize_ad_reason(
                    result.get("detail_reason", ""),
                    ocr_text=ocr_text,
                    is_image=True,
                )
                return result

            # 文本评分中等（50-69）：警告级别，不做额外分析
            if text_score >= 50:
                result["ocr_text"] = ocr_text
                result["detail_reason"] = result.get("reason", "")
                result["source"] = "ocr+ad_detector"
                result["is_ad"] = False
                from handlers.ad_detector import summarize_ad_reason
                result["reason"] = summarize_ad_reason(
                    result.get("detail_reason", ""),
                    ocr_text=ocr_text,
                    is_image=True,
                )
                return result

            # 文本评分低分（0-49）：调用 AI 多模态直接分析图片语义
            # 解决"图片内容是广告但文字提取后不含广告关键词"的漏检问题
            logger.info(
                f"[OCR] 文本评分低({text_score})，启动AI图片语义分析: "
                f"ocr_text={combined[:60]!r}"
            )
            vision_result = await self._ai_vision_analyze(image_url, ocr_text)
            if vision_result is not None:
                vision_score = vision_result.get("score", 0)
                vision_reason = vision_result.get("reason", "")
                logger.info(
                    f"[OCR] AI图片语义分析完成: vision={vision_score} text={text_score}"
                )
                # 取两者中的高分
                final_score = max(text_score, vision_score)
                sources = ["ocr+text", "ocr+vision"]
                result["ocr_text"] = ocr_text
                result["score"] = final_score
                result["is_ad"] = final_score >= SCORE_MUTE
                result["source"] = "+".join(sources)
                result["detail_reason"] = vision_reason or result.get("reason", "")
                from handlers.ad_detector import summarize_ad_reason
                result["reason"] = summarize_ad_reason(
                    result.get("detail_reason", ""),
                    ocr_text=ocr_text,
                    is_image=True,
                )
                return result

            # AI 视觉分析失败，使用文本评分结果
            result["ocr_text"] = ocr_text
            result["detail_reason"] = result.get("reason", "")
            result["source"] = "ocr+ad_detector"
            result["is_ad"] = result.get("score", 0) >= SCORE_MUTE
            from handlers.ad_detector import summarize_ad_reason
            result["reason"] = summarize_ad_reason(
                result.get("detail_reason", ""),
                ocr_text=ocr_text,
                is_image=True,
            )
            return result

        except Exception as e:
            logger.warning(f"[OCR] 广告评分失败: {e}")
            # 退化为仅词库
            try:
                from handlers.lexicon_engine import get_lexicon_engine
                lex = get_lexicon_engine().scan(combined)
                score = lex.get("score", 0)
                return {
                    "score": score,
                    "reason": lex.get("reason") or "词库命中",
                    "is_ad": score >= SCORE_MUTE,
                    "ocr_text": ocr_text,
                    "source": "ocr+lexicon",
                }
            except Exception:
                return {
                    "score": 0,
                    "reason": f"OCR后续评分失败: {e}",
                    "is_ad": False,
                    "ocr_text": ocr_text,
                    "source": "error",
                }

    async def _ai_vision_analyze(self, image_url: str, ocr_text: str = "") -> dict | None:
        """
        用 AI 多模态模型直接分析图片内容，判断是否为广告。
        解决纯文字检测的盲区：图片通过排版/设计暗示广告，但提取的文字无明显广告词。
        """
        if not image_url:
            return None

        from config import settings
        if not settings.AI_ENABLED:
            return None

        prompt = (
            "你是一个图片广告检测专家。分析这张图片，判断是否为广告或垃圾营销图片。\n\n"
        )
        if ocr_text:
            prompt += f"图片中提取到的文字：{ocr_text[:200]}\n\n"
        prompt += (
            "请从以下角度分析：\n"
            "1. 图片是否包含明显的广告设计元素（大字促销、收益承诺、二维码引流、联系方式）\n"
            "2. 图片是否为营销海报、推广截图、兼职广告等\n"
            "3. 图片排版是否具有明显的营销特征（颜色醒目、文字堆叠、诱导性语言）\n\n"
            "注意：技术分享截图（API key、代码、终端输出、配置页面）不是广告。\n\n"
            '请严格按以下JSON格式回复，不要输出其他内容：\n'
            '{"score": 0-100, "reason": "一句话类型总结，不要复述原文"}\n'
            "评分标准：\n"
            "- 0-30：正常图片（日常分享、技术截图、表情包等）\n"
            "- 31-49：疑似广告图片，但不确定\n"
            "- 50-69：较大概率是广告图片\n"
            "- 70-89：明确是广告图片\n"
            "- 90-100：恶劣广告/诈骗图片\n"
        )

        def _sync_call() -> str:
            import httpx
            from openai import OpenAI

            api_key = settings.AD_AI_API_KEY or settings.AI_API_KEY
            if not api_key:
                return ""
            proxy_url = settings.PROXY_URL or None
            if proxy_url:
                with httpx.Client(proxy=proxy_url, timeout=AI_TIMEOUT) as http_client:
                    client = OpenAI(
                        api_key=api_key,
                        base_url=settings.AI_BASE_URL if settings.AI_BASE_URL else None,
                        http_client=http_client,
                    )
                    response = client.chat.completions.create(
                        model=settings.AI_MODEL,
                        messages=[
                            {
                                "role": "system",
                                "content": "你是图片广告检测AI。只返回JSON格式{\"score\":数字,\"reason\":\"理由\"}，不要其他文字。",
                            },
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": image_url}},
                                ],
                            },
                        ],
                        temperature=0.0,
                        max_tokens=200,
                    )
                    return (response.choices[0].message.content or "").strip()
            else:
                client = OpenAI(
                    api_key=api_key,
                    base_url=settings.AI_BASE_URL if settings.AI_BASE_URL else None,
                )
                response = client.chat.completions.create(
                    model=settings.AI_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "你是图片广告检测AI。只返回JSON格式{\"score\":数字,\"reason\":\"理由\"}，不要其他文字。",
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        },
                    ],
                    temperature=0.0,
                    max_tokens=200,
                )
                return (response.choices[0].message.content or "").strip()

        for attempt in range(2):  # 最多重试 2 次
            try:
                content = await asyncio.wait_for(
                    asyncio.to_thread(_sync_call),
                    timeout=AI_TIMEOUT,
                )
                if not content:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                json_match = re.search(r'\{[^}]+\}', content)
                if json_match:
                    result = json.loads(json_match.group())
                    score = max(0, min(100, int(result.get("score", 0))))
                    reason = result.get("reason", "")
                    return {"score": score, "reason": reason}

                # 纯文本格式兜底
                lines = content.split("\n", 1)
                score_str = re.sub(r'[^\d]', '', lines[0].strip())
                if score_str:
                    score = max(0, min(100, int(score_str)))
                    reason = lines[1].strip() if len(lines) > 1 else ""
                    return {"score": score, "reason": reason}
            except Exception as e:
                logger.warning(f"[OCR] AI图片语义分析失败(尝试{attempt+1}): {e}")
                await asyncio.sleep(0.5 * (attempt + 1))

        return None


def _is_tech_share_image(text: str) -> bool:
    """
    识别技术资源/福利分享截图。
    常见模式：
    - API key: sk-xxx, ak-xxx, api_key=xxx 等
    - 带 key/invite/code/token 参数的URL截图
    - GitHub / Gitee 等开源项目链接
    - 激活码、兑换码分享
    """
    if not text:
        return False
    text_lower = text.lower()

    # 0. 开源/技术托管平台（优先放行）
    if re.search(
        r"(github\.com|gitee\.com|gitcode\.com|raw\.githubusercontent\.com|"
        r"gitlab\.com|npmjs\.com|pypi\.org|huggingface\.co|docker\.io|hub\.docker)",
        text_lower,
    ):
        return True
    # 1. API key 格式（OpenAI、Claude 等）
    if re.search(r"\b(sk-[a-zA-Z0-9]{20,})\b", text):
        return True
    # 2. API key 显式标注
    if re.search(r"\b(api[_-]?key|apikey|secret[_-]?key|access[_-]?token)\b", text_lower):
        return True
    # 3. 带分享参数的URL
    if re.search(r"https?://[^\s]+\?(?:[^&]*(?:key|invite|code|token|ref)=\w+)", text_lower):
        return True
    # 4. 激活码/兑换码格式（纯大写字母数字组合，长度适中）
    if re.search(r"\b[A-Z0-9]{10,20}\b", text) and "激活" in text:
        return True

    return False


_ocr: Optional[OcrAuditor] = None


def get_ocr_auditor() -> OcrAuditor:
    global _ocr
    if _ocr is None:
        _ocr = OcrAuditor()
    return _ocr


def extract_image_urls_from_message(data: dict) -> list:
    """从 OneBot 消息事件中提取图片 URL。"""
    urls = []
    segs = data.get("message") or []
    if isinstance(segs, str):
        # 兼容 CQ 码
        for m in re.finditer(r"\[CQ:image,[^\]]*url=([^,\]]+)", segs):
            urls.append(m.group(1))
        return urls

    for seg in segs:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") != "image":
            continue
        d = seg.get("data") or {}
        url = d.get("url") or d.get("file") or d.get("path") or ""
        if url and str(url).startswith("http"):
            urls.append(str(url))
        elif url and not str(url).startswith("base64"):
            # 某些协议给 file 字段
            urls.append(str(url))
    return urls
