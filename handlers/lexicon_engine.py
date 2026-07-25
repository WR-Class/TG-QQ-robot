"""
Aho-Corasick 词库引擎
- 优先加载 data/lexicon.db（若存在）
- 同时合并内置种子词库
- 用于广告/违禁词初筛，替代线性关键词扫描
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("lexicon_engine")

# 类别别名归一化（英文 code -> 中文）
CATEGORY_ALIASES = {
    "ad": "广告",
    "ads": "广告",
    "illegal_url": "违规网址",
    "url": "违规网址",
    "political": "政治",
    "politics": "政治",
    "porn": "色情",
    "sexual": "色情",
    "swear": "辱骂",
    "abuse": "辱骂",
    "violence": "暴恐",
    "terror": "暴恐",
    "reactionary": "反动",
    "privacy": "隐私",
    "spam": "广告",
    "scam": "黑产",
    "fraud": "黑产",
}

# 类别 -> 基础分数
CATEGORY_SCORES = {
    "广告": 28,
    "广告推广": 28,
    "引流": 30,
    "兼职": 25,
    "黑产": 45,
    "色情": 50,
    "辱骂": 40,
    "政治": 55,
    "暴恐": 60,
    "反动": 60,
    "网址": 25,
    "违规网址": 30,
    "隐私": 35,
    "默认": 20,
}


def _norm_category(cat: str) -> str:
    c = (cat or "默认").strip()
    if not c:
        return "默认"
    low = c.lower()
    if low in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[low]
    if c in CATEGORY_SCORES:
        return c
    return c

# 内置种子词（无外部词库时也能工作）
SEED_WORDS: List[Tuple[str, str]] = [
    ("日结", "广告"),
    ("天入", "广告"),
    ("月入过万", "广告"),
    ("躺赚", "广告"),
    ("稳赚", "广告"),
    ("代刷", "广告"),
    ("刷单", "黑产"),
    ("跑分", "黑产"),
    ("租号", "黑产"),
    ("出号", "黑产"),
    ("私聊我", "引流"),
    ("加我微信", "引流"),
    ("加v", "引流"),
    ("免费领", "广告"),
    ("兼职日结", "兼职"),
    ("抖音代刷", "广告"),
    ("刷礼物", "广告"),
    ("一单一结", "广告"),
    ("天入3000", "广告"),
    ("招代理", "广告"),
    ("免费提链", "引流"),
]


class LexiconEngine:
    def __init__(self):
        self._automaton = None
        self._word_meta: Dict[str, str] = {}  # word -> category
        self._loaded = False
        self._word_count = 0

    @property
    def available(self) -> bool:
        return self._loaded and (
            self._automaton is not None or bool(getattr(self, "_fallback_words", None))
        )

    @property
    def word_count(self) -> int:
        return self._word_count

    def load(self, db_path: Optional[str] = None) -> bool:
        """加载词库并构建自动机。"""
        ahocorasick = None
        try:
            import ahocorasick as _ahc
            ahocorasick = _ahc
        except ImportError:
            logger.warning("[词库] 未安装 pyahocorasick，将使用回退扫描")

        words: Dict[str, str] = {}

        # 1) 内置种子
        for w, cat in SEED_WORDS:
            if w and len(w) >= 2:
                words[w.lower()] = cat

        # 2) 外部 SQLite 词库
        path = self._resolve_db_path(db_path)
        if path:
            loaded = self._load_sqlite(path)
            for w, cat in loaded.items():
                words[w] = cat
            logger.info(f"[词库] 从 {path} 加载 {len(loaded)} 条")

        # 3) Web 后台自定义词
        try:
            from handlers.moderation_store import list_custom_words
            custom = list_custom_words(enabled_only=True)
            for item in custom:
                w = str(item.get("word") or "").strip()
                cat = str(item.get("category") or "广告").strip() or "广告"
                if len(w) >= 2:
                    words[w.lower()] = cat
            if custom:
                logger.info(f"[词库] 合并自定义词 {len(custom)} 条")
        except Exception as e:
            logger.debug(f"[词库] 自定义词加载跳过: {e}")

        if not words:
            logger.warning("[词库] 词库为空")
            return False

        self._word_meta = words
        self._word_count = len(words)

        if ahocorasick is not None:
            try:
                A = ahocorasick.Automaton()
                for w, cat in words.items():
                    A.add_word(w, (w, cat))
                A.make_automaton()
                self._automaton = A
                self._loaded = True
                logger.info(f"[词库] Aho-Corasick 就绪，共 {self._word_count} 词")
                return True
            except Exception as e:
                logger.warning(f"[词库] 自动机构建失败: {e}")

        # 回退：纯 Python 扫描
        self._automaton = None
        self._fallback_words = sorted(words.items(), key=lambda x: len(x[0]), reverse=True)
        self._loaded = True
        logger.warning(f"[词库] 使用回退扫描，共 {self._word_count} 词")
        return True

    def _resolve_db_path(self, db_path: Optional[str]) -> Optional[str]:
        candidates = []
        if db_path:
            candidates.append(db_path)
        env_path = os.environ.get("LEXICON_DB_PATH", "")
        if env_path:
            candidates.append(env_path)
        base = Path(__file__).resolve().parent.parent
        candidates.extend([
            str(base / "data" / "lexicon.db"),
            str(base / "lexicon.db"),
            "/app/data/lexicon.db",
        ])
        for p in candidates:
            if p and os.path.isfile(p):
                return p
        return None

    def _load_sqlite(self, path: str) -> Dict[str, str]:
        """尽量兼容不同表结构的 lexicon.db。"""
        result: Dict[str, str] = {}
        try:
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                tables = [r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                logger.info(f"[词库] DB 表: {tables}")

                for table in tables:
                    if table.startswith("sqlite_"):
                        continue
                    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
                    cols_l = [c.lower() for c in cols]
                    # 猜测词列 / 类别列
                    word_col = None
                    cat_col = None
                    for c in cols:
                        cl = c.lower()
                        if cl in ("word", "keyword", "term", "pattern", "content", "text", "value"):
                            word_col = c
                        if cl in ("category", "type", "class", "tag", "label", "group"):
                            cat_col = c
                    if not word_col:
                        # 退化：取第一列文本
                        if cols:
                            word_col = cols[0]
                        else:
                            continue
                    try:
                        if cat_col:
                            rows = cur.execute(
                                f"SELECT {word_col}, {cat_col} FROM {table}"
                            ).fetchall()
                            for row in rows:
                                w = str(row[0] or "").strip()
                                cat = str(row[1] or "默认").strip() or "默认"
                                if len(w) >= 2:
                                    result[w.lower()] = cat
                        else:
                            rows = cur.execute(f"SELECT {word_col} FROM {table}").fetchall()
                            for row in rows:
                                w = str(row[0] or "").strip()
                                if len(w) >= 2:
                                    result[w.lower()] = "默认"
                    except Exception as e:
                        logger.warning(f"[词库] 读表 {table} 失败: {e}")
        except Exception as e:
            logger.warning(f"[词库] 打开 DB 失败: {e}")
        return result

    def scan(self, text: str) -> dict:
        """
        扫描文本，返回:
        {
          "score": int,
          "hits": [{"word":..., "category":..., "score":...}],
          "reason": str,
          "is_hit": bool
        }
        """
        if not self.available or not text:
            return {"score": 0, "hits": [], "reason": "", "is_hit": False}

        text_l = text.lower()
        hits = []
        seen = set()
        total = 0

        if self._automaton is not None:
            iterator = ((word, cat) for _, (word, cat) in self._automaton.iter(text_l))
        else:
            # 回退扫描
            iterator = (
                (word, cat)
                for word, cat in getattr(self, "_fallback_words", [])
                if word in text_l
            )

        for word, cat in iterator:
            key = (word, cat)
            if key in seen:
                continue
            seen.add(key)
            cat_n = _norm_category(cat)
            sc = CATEGORY_SCORES.get(cat_n, CATEGORY_SCORES["默认"])
            # 更长的词略加分
            if len(word) >= 4:
                sc += 5
            hits.append({"word": word, "category": cat_n, "score": sc})
            total += sc

        if not hits:
            return {"score": 0, "hits": [], "reason": "", "is_hit": False}

        # 多词命中加分，上限 100
        if len(hits) >= 3:
            total += 15
        total = min(100, total)

        # 严重类别抬高：短词（<3字）易误伤日常聊天，不自动抬到 80
        severe = {"黑产", "色情", "政治", "暴恐", "反动"}
        severe_hits = [h for h in hits if h["category"] in severe]
        strong_severe = [h for h in severe_hits if len(str(h.get("word") or "")) >= 3]
        if strong_severe:
            total = max(total, 80)
        elif severe_hits:
            # 仅短词命中：轻微加分，不直接按严重违规处理
            total = max(total, min(total + 15, 45))

        reasons = [f"{h['category']}:{h['word']}" for h in hits[:5]]
        if len(hits) > 5:
            reasons.append(f"等{len(hits)}词")

        return {
            "score": total,
            "hits": hits,
            "reason": "词库命中 " + "、".join(reasons),
            "is_hit": True,
        }


# 全局单例
_engine: Optional[LexiconEngine] = None


def get_lexicon_engine() -> LexiconEngine:
    global _engine
    if _engine is None:
        _engine = LexiconEngine()
        _engine.load()
    return _engine


def reload_lexicon(db_path: Optional[str] = None) -> bool:
    global _engine
    _engine = LexiconEngine()
    return _engine.load(db_path)
