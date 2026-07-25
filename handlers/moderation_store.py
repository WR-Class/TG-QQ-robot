"""
管理后台数据层（同步 SQLite）
- 违规记录
- 多群独立配置
- 定时解禁
- 自定义词库词条
可被 NapCat 与 Web 后台共用（共享 ./data/moderation.db）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("moderation_store")

_lock = threading.RLock()
_db_initialized = False
_db_path: Optional[str] = None

DEFAULT_GROUP_CFG = {
    "enabled": True,
    "ad_enabled": True,
    "ocr_enabled": True,
    "flood_enabled": True,
    "join_audit_enabled": True,
    "card_monitor_enabled": True,
    "flood_per_second": 5,
    "flood_per_minute": 20,
    "flood_per_hour": 240,
    "flood_mute_minutes": 10,
    "flood_repeat_window": 120,
    "flood_repeat_limit": 3,
    "ad_mute_score": 50,
    "ad_mute_minutes": 0,  # 0=不额外禁言，仅撤回
    "notify_owner": True,
}


def _resolve_db_path() -> str:
    global _db_path
    if _db_path:
        return _db_path
    env = os.environ.get("MODERATION_DB_PATH", "").strip()
    if env:
        _db_path = env
    else:
        base = Path(__file__).resolve().parent.parent
        data_dir = base / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        # 兼容容器路径
        for p in (Path("/app/data"), data_dir):
            try:
                p.mkdir(parents=True, exist_ok=True)
                _db_path = str(p / "moderation.db")
                break
            except Exception:
                continue
        if not _db_path:
            _db_path = str(data_dir / "moderation.db")
    return _db_path


def _conn() -> sqlite3.Connection:
    path = _resolve_db_path()
    c = sqlite3.connect(path, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock:
        c = _conn()
        try:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS violations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    group_id INTEGER DEFAULT 0,
                    user_id INTEGER DEFAULT 0,
                    user_name TEXT DEFAULT '',
                    vtype TEXT NOT NULL,
                    score INTEGER DEFAULT 0,
                    reason TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    action TEXT DEFAULT '',
                    extra TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_violations_created ON violations(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_violations_group ON violations(group_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_violations_type ON violations(vtype);

                CREATE TABLE IF NOT EXISTS group_configs (
                    group_id INTEGER PRIMARY KEY,
                    title TEXT DEFAULT '',
                    config_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_unmutes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    unmute_at REAL NOT NULL,
                    reason TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    status TEXT DEFAULT 'pending',
                    done_at REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_unmute_pending ON scheduled_unmutes(status, unmute_at);

                CREATE TABLE IF NOT EXISTS custom_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL UNIQUE,
                    category TEXT DEFAULT '广告',
                    score INTEGER DEFAULT 25,
                    enabled INTEGER DEFAULT 1,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_custom_words_word ON custom_words(word);

                -- 系统资源历史采样（健康监控每 5 分钟写入）
                CREATE TABLE IF NOT EXISTS sys_resource_samples (
                    sampled_at REAL NOT NULL,
                    cpu_percent REAL NOT NULL,
                    memory_percent REAL NOT NULL,
                    disk_percent REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sys_res_ts ON sys_resource_samples(sampled_at);

                CREATE TABLE IF NOT EXISTS group_openid_map (
                    group_openid TEXT PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    group_name TEXT DEFAULT '',
                    source TEXT DEFAULT 'auto',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_openid_map_gid ON group_openid_map(group_id);

                CREATE TABLE IF NOT EXISTS access_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    group_id INTEGER DEFAULT 0,
                    note TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(scope, target_type, target_id, group_id)
                );
                CREATE INDEX IF NOT EXISTS idx_access_scope ON access_list(scope, target_type);

                CREATE TABLE IF NOT EXISTS penalty_state (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    level INTEGER DEFAULT 0,
                    strike_count INTEGER DEFAULT 0,
                    last_action TEXT DEFAULT '',
                    last_reason TEXT DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS health_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    component TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT DEFAULT '',
                    notified INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_health_created ON health_events(created_at DESC);

                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'qq',
                    group_id INTEGER DEFAULT 0,
                    user_id INTEGER DEFAULT 0,
                    user_name TEXT DEFAULT '',
                    action_type TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    operator TEXT DEFAULT '',
                    extra TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_ops_created ON operations(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ops_action ON operations(action_type);
                CREATE INDEX IF NOT EXISTS idx_ops_platform ON operations(platform, created_at DESC);

                CREATE TABLE IF NOT EXISTS faq_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    question TEXT DEFAULT '',
                    answer TEXT NOT NULL,
                    group_id INTEGER DEFAULT 0,
                    enabled INTEGER DEFAULT 1,
                    match_type TEXT DEFAULT 'keyword',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_faq_keyword ON faq_entries(keyword);
                CREATE INDEX IF NOT EXISTS idx_faq_group ON faq_entries(group_id);
                CREATE INDEX IF NOT EXISTS idx_faq_enabled ON faq_entries(enabled);

                -- FAQ 反馈记录（用户对 FAQ 回复的有用/无用反馈）
                CREATE TABLE IF NOT EXISTS faq_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    faq_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL DEFAULT 0,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    user_name TEXT DEFAULT '',
                    feedback TEXT NOT NULL DEFAULT 'useful',
                    created_at REAL NOT NULL,
                    UNIQUE(faq_id, user_id, group_id)
                );
                CREATE INDEX IF NOT EXISTS idx_faq_fb_faq ON faq_feedback(faq_id);
                CREATE INDEX IF NOT EXISTS idx_faq_fb_created ON faq_feedback(created_at DESC);
                """
            )
            c.commit()
            global _db_initialized
            if not _db_initialized:
                logger.info(f"[管理数据] DB 就绪: {_resolve_db_path()}")
                _db_initialized = True
        finally:
            c.close()


# ---------------- 违规记录 ----------------

def add_violation(
    *,
    group_id: int = 0,
    user_id: int = 0,
    user_name: str = "",
    vtype: str = "ad",
    score: int = 0,
    reason: str = "",
    content: str = "",
    action: str = "",
    extra: Optional[dict] = None,
) -> int:
    """vtype: ad / flood / ocr / card / join / command"""
    init_db()
    now = time.time()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                """
                INSERT INTO violations
                (created_at, group_id, user_id, user_name, vtype, score, reason, content, action, extra)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    int(group_id or 0),
                    int(user_id or 0),
                    (user_name or "")[:80],
                    (vtype or "ad")[:32],
                    int(score or 0),
                    (reason or "")[:300],
                    (content or "")[:500],
                    (action or "")[:80],
                    json.dumps(extra or {}, ensure_ascii=False)[:1000],
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)
        finally:
            c.close()


def get_violation(violation_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 获取单条违规记录。"""
    init_db()
    with _lock:
        c = _conn()
        try:
            row = c.execute("SELECT * FROM violations WHERE id=?", (int(violation_id),)).fetchone()
            if row:
                d = dict(row)
                d["created_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"]))
                return d
            return None
        finally:
            c.close()


def list_violations(
    *,
    limit: int = 50,
    offset: int = 0,
    group_id: Optional[int] = None,
    vtype: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    init_db()
    limit = max(1, min(5000, int(limit or 50)))
    offset = max(0, int(offset or 0))
    where = []
    args: List[Any] = []
    if group_id is not None:
        where.append("group_id = ?")
        args.append(int(group_id))
    if vtype:
        where.append("vtype = ?")
        args.append(vtype)
    if user_id is not None:
        where.append("user_id = ?")
        args.append(int(user_id))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        c = _conn()
        try:
            total = c.execute(f"SELECT COUNT(*) FROM violations {wsql}", args).fetchone()[0]
            rows = c.execute(
                f"""
                SELECT * FROM violations {wsql}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                args + [limit, offset],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d["created_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"]))
                items.append(d)
            return {"total": total, "items": items, "limit": limit, "offset": offset}
        finally:
            c.close()


# ---------------- 群配置 ----------------

def get_group_config(group_id: int) -> Dict[str, Any]:
    init_db()
    gid = int(group_id)
    with _lock:
        c = _conn()
        try:
            row = c.execute("SELECT * FROM group_configs WHERE group_id = ?", (gid,)).fetchone()
            if not row:
                cfg = dict(DEFAULT_GROUP_CFG)
                return {"group_id": gid, "title": "", "config": cfg, "is_default": True}
            cfg = dict(DEFAULT_GROUP_CFG)
            try:
                cfg.update(json.loads(row["config_json"] or "{}"))
            except Exception:
                pass
            return {
                "group_id": gid,
                "title": row["title"] or "",
                "config": cfg,
                "updated_at": row["updated_at"],
                "is_default": False,
            }
        finally:
            c.close()


def list_group_configs() -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM group_configs ORDER BY group_id").fetchall()
            out = []
            for r in rows:
                cfg = dict(DEFAULT_GROUP_CFG)
                try:
                    cfg.update(json.loads(r["config_json"] or "{}"))
                except Exception:
                    pass
                out.append(
                    {
                        "group_id": r["group_id"],
                        "title": r["title"] or "",
                        "config": cfg,
                        "updated_at": r["updated_at"],
                    }
                )
            return out
        finally:
            c.close()


def ensure_group_config(group_id: int, group_name: str = "") -> Dict[str, Any]:
    """
    自动学习：首次发现某群时自动创建配置记录。
    如果配置已存在且群名为空，自动更新群名。
    返回当前配置。
    """
    init_db()
    gid = int(group_id)
    if gid <= 0:
        return get_group_config(0)
    current = get_group_config(gid)
    if current.get("is_default"):
        # 配置不存在，用默认值自动创建
        upsert_group_config(gid, {}, title=(group_name or "")[:120])
        logger.info(f"[自动学习] 新群配置已创建: group={gid} name={group_name!r}")
        return get_group_config(gid)
    # 配置存在但群名为空，自动更新
    if not current.get("title") and group_name:
        upsert_group_config(gid, {}, title=group_name[:120])
        logger.info(f"[自动学习] 群名已更新: group={gid} name={group_name!r}")
        return get_group_config(gid)
    return current


def upsert_group_config(group_id: int, config: dict, title: str = "") -> Dict[str, Any]:
    init_db()
    gid = int(group_id)
    current = get_group_config(gid)
    merged = dict(current["config"])
    if config:
        for k, v in config.items():
            if k in DEFAULT_GROUP_CFG:
                merged[k] = v
    title = title if title is not None and title != "" else current.get("title", "")
    now = time.time()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """
                INSERT INTO group_configs (group_id, title, config_json, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                  title=excluded.title,
                  config_json=excluded.config_json,
                  updated_at=excluded.updated_at
                """,
                (gid, title or "", json.dumps(merged, ensure_ascii=False), now),
            )
            c.commit()
        finally:
            c.close()
    return get_group_config(gid)


def delete_group_config(group_id: int) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute("DELETE FROM group_configs WHERE group_id = ?", (int(group_id),))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


# ---------------- 定时解禁 ----------------

def schedule_unmute(group_id: int, user_id: int, mute_seconds: int, reason: str = "") -> int:
    init_db()
    now = time.time()
    unmute_at = now + max(1, int(mute_seconds))
    with _lock:
        c = _conn()
        try:
            # 取消同用户未完成的旧计划
            c.execute(
                "UPDATE scheduled_unmutes SET status='cancelled' WHERE group_id=? AND user_id=? AND status='pending'",
                (int(group_id), int(user_id)),
            )
            cur = c.execute(
                """
                INSERT INTO scheduled_unmutes (group_id, user_id, unmute_at, reason, created_at, status)
                VALUES (?,?,?,?,?,'pending')
                """,
                (int(group_id), int(user_id), unmute_at, (reason or "")[:200], now),
            )
            c.commit()
            return int(cur.lastrowid or 0)
        finally:
            c.close()


def list_scheduled_unmutes(status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT * FROM scheduled_unmutes
                WHERE status = ?
                ORDER BY unmute_at ASC LIMIT ?
                """,
                (status, max(1, min(500, int(limit)))),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["unmute_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["unmute_at"]))
                d["created_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"]))
                out.append(d)
            return out
        finally:
            c.close()


def fetch_due_unmutes(now: Optional[float] = None) -> List[Dict[str, Any]]:
    init_db()
    now = now or time.time()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT * FROM scheduled_unmutes
                WHERE status='pending' AND unmute_at <= ?
                ORDER BY unmute_at ASC LIMIT 50
                """,
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def mark_unmute_done(row_id: int, status: str = "done") -> None:
    init_db()
    with _lock:
        c = _conn()
        try:
            c.execute(
                "UPDATE scheduled_unmutes SET status=?, done_at=? WHERE id=?",
                (status, time.time(), int(row_id)),
            )
            c.commit()
        finally:
            c.close()


def cancel_unmute(row_id: int) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "UPDATE scheduled_unmutes SET status='cancelled', done_at=? WHERE id=? AND status='pending'",
                (time.time(), int(row_id)),
            )
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


# ---------------- 自定义词库 ----------------

def list_custom_words(enabled_only: bool = False) -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            if enabled_only:
                rows = c.execute(
                    "SELECT * FROM custom_words WHERE enabled=1 ORDER BY id DESC"
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM custom_words ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def add_custom_word(word: str, category: str = "广告", score: int = 25) -> Dict[str, Any]:
    init_db()
    word = (word or "").strip()
    if len(word) < 2:
        raise ValueError("词条至少 2 个字符")
    now = time.time()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """
                INSERT INTO custom_words (word, category, score, enabled, created_at)
                VALUES (?,?,?,1,?)
                ON CONFLICT(word) DO UPDATE SET
                  category=excluded.category,
                  score=excluded.score,
                  enabled=1
                """,
                (word, (category or "广告")[:32], int(score or 25), now),
            )
            c.commit()
            row = c.execute("SELECT * FROM custom_words WHERE word=?", (word,)).fetchone()
            return dict(row) if row else {}
        finally:
            c.close()


def delete_custom_word(word_id: int) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute("DELETE FROM custom_words WHERE id=?", (int(word_id),))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def set_custom_word_enabled(word_id: int, enabled: bool) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "UPDATE custom_words SET enabled=? WHERE id=?",
                (1 if enabled else 0, int(word_id)),
            )
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


# ---------------- 群 openid 自动学习 ----------------

def upsert_group_openid_map(
    group_openid: str,
    group_id: int,
    group_name: str = "",
    source: str = "auto",
) -> bool:
    """学习/更新 openid→数字群号。返回是否为新映射或群号变更。"""
    init_db()
    openid = (group_openid or "").strip()
    if not openid:
        return False
    try:
        gid = int(group_id)
    except Exception:
        return False
    if gid <= 0:
        return False
    now = time.time()
    with _lock:
        c = _conn()
        try:
            old = c.execute(
                "SELECT group_id FROM group_openid_map WHERE group_openid=?",
                (openid,),
            ).fetchone()
            changed = True
            if old and int(old["group_id"]) == gid:
                changed = False
            c.execute(
                """
                INSERT INTO group_openid_map (group_openid, group_id, group_name, source, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(group_openid) DO UPDATE SET
                  group_id=excluded.group_id,
                  group_name=CASE
                    WHEN excluded.group_name != '' THEN excluded.group_name
                    ELSE group_openid_map.group_name
                  END,
                  source=excluded.source,
                  updated_at=excluded.updated_at
                """,
                (openid, gid, (group_name or "")[:120], (source or "auto")[:32], now),
            )
            c.commit()
            return changed or old is None
        finally:
            c.close()


def list_group_openid_map() -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT * FROM group_openid_map ORDER BY updated_at DESC"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["updated_at_str"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(d["updated_at"])
                )
                out.append(d)
            return out
        finally:
            c.close()


def get_group_id_by_openid(group_openid: str) -> int:
    init_db()
    openid = (group_openid or "").strip()
    if not openid:
        return 0
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT group_id FROM group_openid_map WHERE group_openid=?",
                (openid,),
            ).fetchone()
            return int(row["group_id"]) if row else 0
        finally:
            c.close()


def delete_group_openid_map(group_openid: str) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "DELETE FROM group_openid_map WHERE group_openid=?",
                ((group_openid or "").strip(),),
            )
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def merge_env_and_learned_map(env_raw: str = "") -> Dict[str, int]:
    """合并 .env 映射 + 自动学习映射，返回 openid→gid。"""
    result: Dict[str, int] = {}
    text = (env_raw or "").replace("\r", "\n").replace(";", "\n").replace(",", "\n")
    for line in text.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        oid, num = line.split(":", 1)
        oid = oid.strip()
        try:
            result[oid] = int(num.strip())
        except ValueError:
            continue
    for item in list_group_openid_map():
        try:
            result[str(item["group_openid"])] = int(item["group_id"])
        except Exception:
            continue
    return result


# ---------------- 黑白名单 ----------------

def list_access(scope: Optional[str] = None, group_id: Optional[int] = None) -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            sql = "SELECT * FROM access_list WHERE 1=1"
            params: List[Any] = []
            if scope:
                sql += " AND scope=?"
                params.append(scope)
            if group_id is not None:
                sql += " AND group_id=?"
                params.append(int(group_id))
            sql += " ORDER BY id DESC"
            rows = c.execute(sql, params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["created_at_str"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])
                )
                out.append(d)
            return out
        finally:
            c.close()


def add_access(
    scope: str,
    target_type: str,
    target_id: str,
    group_id: int = 0,
    note: str = "",
) -> Dict[str, Any]:
    """scope: whitelist/blacklist; target_type: user/word"""
    init_db()
    scope = (scope or "").strip().lower()
    target_type = (target_type or "").strip().lower()
    target_id = (target_id or "").strip()
    if scope not in ("whitelist", "blacklist"):
        raise ValueError("scope 必须是 whitelist 或 blacklist")
    if target_type not in ("user", "word"):
        raise ValueError("target_type 必须是 user 或 word")
    if not target_id:
        raise ValueError("target_id 不能为空")
    now = time.time()
    with _lock:
        c = _conn()
        try:
            c.execute(
                """
                INSERT INTO access_list (scope, target_type, target_id, group_id, note, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(scope, target_type, target_id, group_id) DO UPDATE SET
                  note=excluded.note
                """,
                (scope, target_type, target_id, int(group_id or 0), (note or "")[:200], now),
            )
            c.commit()
            row = c.execute(
                """
                SELECT * FROM access_list
                WHERE scope=? AND target_type=? AND target_id=? AND group_id=?
                """,
                (scope, target_type, target_id, int(group_id or 0)),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            c.close()


def delete_access(item_id: int) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute("DELETE FROM access_list WHERE id=?", (int(item_id),))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def is_user_whitelisted(user_id: int, group_id: int = 0) -> bool:
    init_db()
    uid = str(int(user_id))
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                """
                SELECT 1 FROM access_list
                WHERE scope='whitelist' AND target_type='user'
                  AND target_id=? AND group_id IN (0, ?)
                LIMIT 1
                """,
                (uid, int(group_id or 0)),
            ).fetchone()
            return row is not None
        finally:
            c.close()


def is_user_blacklisted(user_id: int, group_id: int = 0) -> bool:
    init_db()
    uid = str(int(user_id))
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                """
                SELECT 1 FROM access_list
                WHERE scope='blacklist' AND target_type='user'
                  AND target_id=? AND group_id IN (0, ?)
                LIMIT 1
                """,
                (uid, int(group_id or 0)),
            ).fetchone()
            return row is not None
        finally:
            c.close()


def match_blacklist_words(text: str, group_id: int = 0) -> List[str]:
    """返回命中的黑名单关键词。"""
    init_db()
    text = text or ""
    if not text:
        return []
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT target_id FROM access_list
                WHERE scope='blacklist' AND target_type='word'
                  AND group_id IN (0, ?)
                """,
                (int(group_id or 0),),
            ).fetchall()
            hits = []
            for r in rows:
                w = str(r["target_id"] or "")
                if w and w in text:
                    hits.append(w)
            return hits
        finally:
            c.close()


def match_whitelist_words(text: str, group_id: int = 0) -> List[str]:
    """返回命中的白名单关键词（命中则放行）。"""
    init_db()
    text = text or ""
    if not text:
        return []
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT target_id FROM access_list
                WHERE scope='whitelist' AND target_type='word'
                  AND group_id IN (0, ?)
                """,
                (int(group_id or 0),),
            ).fetchall()
            hits = []
            for r in rows:
                w = str(r["target_id"] or "")
                if w and w in text:
                    hits.append(w)
            return hits
        finally:
            c.close()


def get_recent_violation(
    group_id: int = 0,
    user_id: int = 0,
    within_sec: int = 3600,
) -> Optional[Dict[str, Any]]:
    """取最近一条违规记录（用于误判申诉）。"""
    init_db()
    since = time.time() - max(60, int(within_sec))
    with _lock:
        c = _conn()
        try:
            sql = "SELECT * FROM violations WHERE created_at>=?"
            params: List[Any] = [since]
            if group_id:
                sql += " AND group_id=?"
                params.append(int(group_id))
            if user_id:
                sql += " AND user_id=?"
                params.append(int(user_id))
            sql += " ORDER BY created_at DESC LIMIT 1"
            row = c.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            c.close()


def violation_stats(hours: int = 24) -> Dict[str, Any]:
    """近 N 小时违规统计：总数、按类型、按动作、高频原因。"""
    init_db()
    hours = max(1, min(168, int(hours)))
    since = time.time() - hours * 3600
    with _lock:
        c = _conn()
        try:
            total = c.execute(
                "SELECT COUNT(*) AS n FROM violations WHERE created_at>=?",
                (since,),
            ).fetchone()["n"]
            by_type = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT vtype, COUNT(*) AS cnt FROM violations
                    WHERE created_at>=? GROUP BY vtype ORDER BY cnt DESC
                    """,
                    (since,),
                ).fetchall()
            ]
            by_action = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT action, COUNT(*) AS cnt FROM violations
                    WHERE created_at>=? GROUP BY action ORDER BY cnt DESC LIMIT 15
                    """,
                    (since,),
                ).fetchall()
            ]
            top_reasons = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT reason, COUNT(*) AS cnt FROM violations
                    WHERE created_at>=? AND reason!=''
                    GROUP BY reason ORDER BY cnt DESC LIMIT 10
                    """,
                    (since,),
                ).fetchall()
            ]
            top_users = [
                dict(r)
                for r in c.execute(
                    """
                    SELECT user_id, user_name, COUNT(*) AS cnt FROM violations
                    WHERE created_at>=? AND user_id>0
                    GROUP BY user_id ORDER BY cnt DESC LIMIT 10
                    """,
                    (since,),
                ).fetchall()
            ]
            return {
                "hours": hours,
                "total": int(total or 0),
                "by_type": by_type,
                "by_action": by_action,
                "top_reasons": top_reasons,
                "top_users": top_users,
            }
        finally:
            c.close()


# ---------------- 处罚阶梯 ----------------

# level: 0=无, 1=警告, 2=短禁, 3=长禁, 4=踢出
DEFAULT_PENALTY_LADDER = [
    {"level": 1, "action": "warn", "mute_seconds": 0, "label": "警告"},
    {"level": 2, "action": "mute_short", "mute_seconds": 600, "label": "禁言10分钟"},
    {"level": 3, "action": "mute_long", "mute_seconds": 3600, "label": "禁言1小时"},
    {"level": 4, "action": "kick", "mute_seconds": 0, "label": "踢出"},
]


def get_penalty(group_id: int, user_id: int) -> Dict[str, Any]:
    init_db()
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT * FROM penalty_state WHERE group_id=? AND user_id=?",
                (int(group_id), int(user_id)),
            ).fetchone()
            if not row:
                return {
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                    "level": 0,
                    "strike_count": 0,
                    "last_action": "",
                    "last_reason": "",
                }
            return dict(row)
        finally:
            c.close()


def escalate_penalty(
    group_id: int,
    user_id: int,
    reason: str = "",
    ladder: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """提升处罚等级，返回本次应执行的动作。"""
    init_db()
    ladder = ladder or DEFAULT_PENALTY_LADDER
    now = time.time()
    with _lock:
        c = _conn()
        try:
            row = c.execute(
                "SELECT * FROM penalty_state WHERE group_id=? AND user_id=?",
                (int(group_id), int(user_id)),
            ).fetchone()
            cur_level = int(row["level"]) if row else 0
            strikes = int(row["strike_count"]) + 1 if row else 1
            next_level = min(cur_level + 1, len(ladder))
            step = ladder[next_level - 1] if next_level >= 1 else ladder[0]
            action = step.get("action", "warn")
            mute_seconds = int(step.get("mute_seconds") or 0)
            label = step.get("label") or action
            c.execute(
                """
                INSERT INTO penalty_state
                  (group_id, user_id, level, strike_count, last_action, last_reason, updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                  level=excluded.level,
                  strike_count=excluded.strike_count,
                  last_action=excluded.last_action,
                  last_reason=excluded.last_reason,
                  updated_at=excluded.updated_at
                """,
                (
                    int(group_id),
                    int(user_id),
                    next_level,
                    strikes,
                    action,
                    (reason or "")[:200],
                    now,
                ),
            )
            c.commit()
            return {
                "group_id": int(group_id),
                "user_id": int(user_id),
                "level": next_level,
                "strike_count": strikes,
                "action": action,
                "mute_seconds": mute_seconds,
                "label": label,
                "reason": reason or "",
            }
        finally:
            c.close()


def reset_penalty(group_id: int, user_id: int) -> bool:
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                "DELETE FROM penalty_state WHERE group_id=? AND user_id=?",
                (int(group_id), int(user_id)),
            )
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def list_penalties(limit: int = 100) -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT * FROM penalty_state ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["updated_at_str"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(d["updated_at"])
                )
                out.append(d)
            return out
        finally:
            c.close()


# ---------------- 健康事件 / 趋势 ----------------

def add_health_event(component: str, status: str, message: str = "", notified: bool = False) -> int:
    init_db()
    now = time.time()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                """
                INSERT INTO health_events (created_at, component, status, message, notified)
                VALUES (?,?,?,?,?)
                """,
                (now, (component or "")[:64], (status or "")[:32], (message or "")[:500], 1 if notified else 0),
            )
            c.commit()
            return int(cur.lastrowid or 0)
        finally:
            c.close()


def list_health_events(limit: int = 50, component: Optional[str] = None) -> List[Dict[str, Any]]:
    init_db()
    with _lock:
        c = _conn()
        try:
            if component:
                rows = c.execute(
                    "SELECT * FROM health_events WHERE component=? ORDER BY created_at DESC LIMIT ?",
                    (component, max(1, min(200, int(limit)))),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM health_events ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(200, int(limit))),),
                ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["created_at_str"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])
                )
                out.append(d)
            return out
        finally:
            c.close()


def latest_health_status(component: str) -> Optional[Dict[str, Any]]:
    items = list_health_events(limit=1, component=component)
    return items[0] if items else None


# ---------------- 操作日志（统一平台） ----------------

def add_operation(
    *,
    platform: str = "qq",
    group_id: int = 0,
    user_id: int = 0,
    user_name: str = "",
    action_type: str = "",
    detail: str = "",
    operator: str = "",
    extra: Optional[dict] = None,
) -> int:
    """
    记录一次管理操作。
    action_type: recall / mute / unmute / ban / unban / alert / kick / whitelist 等
    platform: qq / tg
    """
    init_db()
    now = time.time()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                """
                INSERT INTO operations
                (created_at, platform, group_id, user_id, user_name, action_type, detail, operator, extra)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    (platform or "qq")[:8],
                    int(group_id or 0),
                    int(user_id or 0),
                    (user_name or "")[:80],
                    (action_type or "")[:32],
                    (detail or "")[:300],
                    (operator or "")[:80],
                    json.dumps(extra or {}, ensure_ascii=False)[:500],
                ),
            )
            c.commit()
            return int(cur.lastrowid or 0)
        finally:
            c.close()


def list_operations(
    *,
    limit: int = 50,
    offset: int = 0,
    platform: Optional[str] = None,
    action_type: Optional[str] = None,
    group_id: Optional[int] = None,
) -> Dict[str, Any]:
    init_db()
    limit = max(1, min(5000, int(limit or 50)))
    offset = max(0, int(offset or 0))
    where = []
    args: List[Any] = []
    if platform:
        where.append("platform = ?")
        args.append(platform[:8])
    if action_type:
        where.append("action_type = ?")
        args.append(action_type[:32])
    if group_id is not None:
        where.append("group_id = ?")
        args.append(int(group_id))
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        c = _conn()
        try:
            total = c.execute(f"SELECT COUNT(*) FROM operations {wsql}", args).fetchone()[0]
            rows = c.execute(
                f"""
                SELECT * FROM operations {wsql}
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                args + [limit, offset],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d["created_at_str"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])
                )
                items.append(d)
            return {"total": total, "items": items, "limit": limit, "offset": offset}
        finally:
            c.close()


def violation_trend(days: int = 7) -> List[Dict[str, Any]]:
    """按天聚合违规趋势。"""
    init_db()
    days = max(1, min(90, int(days)))
    since = time.time() - days * 86400
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT date(created_at, 'unixepoch', 'localtime') AS day,
                       vtype, COUNT(*) AS cnt
                FROM violations
                WHERE created_at >= ?
                GROUP BY day, vtype
                ORDER BY day ASC
                """,
                (since,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


def sample_sys_resource(cpu: float, memory: float, disk: float) -> None:
    """记录一次系统资源采样（由健康监控定期调用）。"""
    init_db()
    now = time.time()
    with _lock:
        c = _conn()
        try:
            c.execute(
                "INSERT INTO sys_resource_samples (sampled_at, cpu_percent, memory_percent, disk_percent) VALUES (?,?,?,?)",
                (now, round(cpu, 1), round(memory, 1), round(disk, 1)),
            )
            c.commit()
            # 只保留最近 24 小时数据（约 288 条）
            cutoff = now - 86400
            c.execute("DELETE FROM sys_resource_samples WHERE sampled_at < ?", (cutoff,))
            c.commit()
        finally:
            c.close()


def get_sys_resource_history(hours: int = 24) -> List[Dict[str, Any]]:
    """获取系统资源历史采样数据。"""
    init_db()
    hours = max(1, min(72, int(hours)))
    since = time.time() - hours * 3600
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT * FROM sys_resource_samples WHERE sampled_at >= ? ORDER BY sampled_at ASC",
                (since,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["time_str"] = time.strftime("%H:%M", time.localtime(d["sampled_at"]))
                out.append(d)
            return out
        finally:
            c.close()


# ---------------- FAQ 问答库 ----------------

def add_faq_entry(
    keyword: str,
    answer: str,
    *,
    question: str = "",
    group_id: int = 0,
    match_type: str = "keyword",
    enabled: bool = True,
) -> Dict[str, Any]:
    """添加 FAQ 条目。keyword=触发词, answer=回复内容, question=标准问法(语义匹配用)。"""
    init_db()
    keyword = (keyword or "").strip()
    answer = (answer or "").strip()
    if not keyword or not answer:
        raise ValueError("关键词和回复内容不能为空")
    if match_type not in ("keyword", "semantic"):
        raise ValueError("match_type 必须是 keyword 或 semantic")
    now = time.time()
    with _lock:
        c = _conn()
        try:
            cur = c.execute(
                """
                INSERT INTO faq_entries (keyword, question, answer, group_id, enabled, match_type, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (keyword, (question or "")[:300], answer, int(group_id or 0),
                 1 if enabled else 0, match_type[:16], now, now),
            )
            c.commit()
            row = c.execute("SELECT * FROM faq_entries WHERE id=?", (cur.lastrowid,)).fetchone()
            return _faq_row_to_dict(row) if row else {}
        finally:
            c.close()


def update_faq_entry(
    entry_id: int,
    *,
    keyword: Optional[str] = None,
    question: Optional[str] = None,
    answer: Optional[str] = None,
    group_id: Optional[int] = None,
    match_type: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """更新 FAQ 条目。只更新传入的非 None 字段。"""
    init_db()
    now = time.time()
    with _lock:
        c = _conn()
        try:
            row = c.execute("SELECT * FROM faq_entries WHERE id=?", (int(entry_id),)).fetchone()
            if not row:
                return None
            d = dict(row)
            if keyword is not None:
                d["keyword"] = (keyword or "").strip()
            if question is not None:
                d["question"] = (question or "")[:300]
            if answer is not None:
                d["answer"] = (answer or "").strip()
            if group_id is not None:
                d["group_id"] = int(group_id)
            if match_type is not None:
                mt = match_type[:16]
                if mt not in ("keyword", "semantic"):
                    raise ValueError("match_type 必须是 keyword 或 semantic")
                d["match_type"] = mt
            if enabled is not None:
                d["enabled"] = 1 if enabled else 0
            if not d.get("keyword") or not d.get("answer"):
                raise ValueError("关键词和回复内容不能为空")
            c.execute(
                """
                UPDATE faq_entries SET keyword=?, question=?, answer=?, group_id=?, enabled=?, match_type=?, updated_at=?
                WHERE id=?
                """,
                (d["keyword"], d["question"], d["answer"], d["group_id"], d["enabled"], d["match_type"], now, int(entry_id)),
            )
            c.commit()
            row = c.execute("SELECT * FROM faq_entries WHERE id=?", (int(entry_id),)).fetchone()
            return _faq_row_to_dict(row) if row else None
        finally:
            c.close()


def delete_faq_entry(entry_id: int) -> bool:
    """删除 FAQ 条目。"""
    init_db()
    with _lock:
        c = _conn()
        try:
            cur = c.execute("DELETE FROM faq_entries WHERE id=?", (int(entry_id),))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()


def get_faq_entry(entry_id: int) -> Optional[Dict[str, Any]]:
    """获取单个 FAQ 条目。"""
    init_db()
    with _lock:
        c = _conn()
        try:
            row = c.execute("SELECT * FROM faq_entries WHERE id=?", (int(entry_id),)).fetchone()
            return _faq_row_to_dict(row) if row else None
        finally:
            c.close()


def list_faq_entries(
    *,
    group_id: Optional[int] = None,
    enabled_only: bool = False,
    keyword_search: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    """列出 FAQ 条目，支持按群、按状态、按关键词搜索。"""
    init_db()
    limit = max(1, min(1000, int(limit)))
    offset = max(0, int(offset))
    where = []
    args: List[Any] = []
    if group_id is not None:
        where.append("group_id = ?")
        args.append(int(group_id))
    if enabled_only:
        where.append("enabled = 1")
    if keyword_search:
        kw = (keyword_search or "").strip()
        if kw:
            where.append("(keyword LIKE ? OR question LIKE ? OR answer LIKE ?)")
            pattern = f"%{kw}%"
            args.extend([pattern, pattern, pattern])
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        c = _conn()
        try:
            total = c.execute(f"SELECT COUNT(*) FROM faq_entries {wsql}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT * FROM faq_entries {wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
            items = [_faq_row_to_dict(r) for r in rows]
            return {"total": total, "items": items, "limit": limit, "offset": offset}
        finally:
            c.close()


def match_faq_keyword(text: str, group_id: int = 0) -> Optional[Dict[str, Any]]:
    """
    关键词匹配 FAQ，返回置信度最高的条目。
    text 为待匹配的消息文本。
    
    增强规则：
    - 支持多关键词 AND（用 | 分隔，需全部匹配）
    - 长关键词优先匹配（减少短词误匹配）
    - 返回匹配长度最长的条目（置信度最高）
    """
    init_db()
    text_lower = (text or "").lower().strip()
    if not text_lower:
        return None
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT * FROM faq_entries
                WHERE enabled=1 AND match_type='keyword'
                  AND (group_id=0 OR group_id=?)
                ORDER BY group_id DESC, id ASC
                """,
                (int(group_id or 0),),
            ).fetchall()
            best_match = None
            best_score = 0  # 匹配分数（关键词总长度）
            for r in rows:
                keyword = str(r["keyword"] or "").strip().lower()
                if not keyword:
                    continue
                # 支持 | 分隔的多关键词 AND 模式
                parts = [p.strip() for p in keyword.split("|") if p.strip()]
                if not parts:
                    continue
                # 检查所有部分是否都出现在文本中
                if all(part in text_lower for part in parts):
                    # 分数 = 关键词总长度（越长越精确）
                    score = sum(len(p) for p in parts)
                    # 群级条目加分（更精确）
                    if int(r["group_id"] or 0) > 0:
                        score += 10
                    if score > best_score:
                        best_score = score
                        best_match = _faq_row_to_dict(r)
            return best_match
        finally:
            c.close()


def list_semantic_faq_entries(group_id: int = 0) -> List[Dict[str, Any]]:
    """列出启用且匹配类型为 semantic 的 FAQ 条目，供语义引擎加载。"""
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT * FROM faq_entries
                WHERE enabled=1 AND match_type='semantic'
                  AND (group_id=0 OR group_id=?)
                ORDER BY id ASC
                """,
                (int(group_id or 0),),
            ).fetchall()
            return [_faq_row_to_dict(r) for r in rows]
        finally:
            c.close()


def _faq_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["created_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"]))
    d["updated_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["updated_at"]))
    d["enabled"] = bool(d["enabled"])
    return d


# ---------------- FAQ 反馈 ----------------

def add_faq_feedback(
    faq_id: int,
    feedback: str,
    user_id: int = 0,
    user_name: str = "",
    group_id: int = 0,
) -> bool:
    """记录 FAQ 反馈。feedback: 'useful' 或 'useless'。同一用户对同一 FAQ 只记录最新反馈。"""
    init_db()
    if feedback not in ("useful", "useless"):
        return False
    with _lock:
        c = _conn()
        try:
            c.execute(
                """
                INSERT INTO faq_feedback (faq_id, group_id, user_id, user_name, feedback, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(faq_id, user_id, group_id) DO UPDATE SET feedback=excluded.feedback, created_at=excluded.created_at
                """,
                (int(faq_id), int(group_id or 0), int(user_id or 0), str(user_name), feedback, time.time()),
            )
            c.commit()
            return True
        finally:
            c.close()


def get_faq_feedback_stats(faq_id: int) -> Dict[str, Any]:
    """获取某 FAQ 的反馈统计：{useful: N, useless: N, total: N}"""
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT feedback, COUNT(*) as cnt FROM faq_feedback WHERE faq_id=? GROUP BY feedback",
                (int(faq_id),),
            ).fetchall()
            stats = {"useful": 0, "useless": 0, "total": 0}
            for r in rows:
                fb = str(r["feedback"])
                if fb in stats:
                    stats[fb] = int(r["cnt"])
            stats["total"] = stats["useful"] + stats["useless"]
            return stats
        finally:
            c.close()


def list_faq_low_quality(limit: int = 10) -> List[Dict[str, Any]]:
    """列出反馈无用次数最多的 FAQ 条目（用于 Web 管理面板展示）。"""
    init_db()
    with _lock:
        c = _conn()
        try:
            rows = c.execute(
                """
                SELECT f.id, f.keyword, f.answer, f.group_id,
                       COALESCE(s.useless, 0) as useless_cnt,
                       COALESCE(s.useful, 0) as useful_cnt,
                       s.total as total_fb
                FROM faq_entries f
                LEFT JOIN (
                    SELECT faq_id,
                           SUM(CASE WHEN feedback='useless' THEN 1 ELSE 0 END) as useless,
                           SUM(CASE WHEN feedback='useful' THEN 1 ELSE 0 END) as useful,
                           COUNT(*) as total
                    FROM faq_feedback GROUP BY faq_id
                ) s ON s.faq_id = f.id
                WHERE f.enabled = 1
                  AND (COALESCE(s.useless, 0) - COALESCE(s.useful, 0)) >= 1
                  AND s.total >= 2
                ORDER BY (COALESCE(s.useless, 0) - COALESCE(s.useful, 0)) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()


# 模块加载时初始化
try:
    init_db()
except Exception as e:
    logger.warning(f"[管理数据] 初始化失败: {e}")
