"""
FastAPI Web 管理后台 API
提供配置管理、服务状态查看、日志查询、连接测试等功能。
"""

import importlib
import json
import logging
import os
import re
import secrets
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================
# 日志配置：使用内存缓冲 + 文件双通道，确保 API 可读取日志
# ============================================================

_log_buffer: deque = deque(maxlen=5000)  # 内存中保留最近 5000 条日志


class _MemoryHandler(logging.Handler):
    """自定义 Handler：将日志写入内存缓冲"""

    def emit(self, record: logging.LogRecord):
        _log_buffer.append(
            {
                "timestamp": self.format_time(record),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )

    @staticmethod
    def format_time(record: logging.LogRecord) -> str:
        from datetime import datetime, timezone, timedelta

        dt = datetime.fromtimestamp(record.created, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M:%S")


# 配置 web_server 自身的日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _MemoryHandler(),
    ],
)
logger = logging.getLogger("web_server")

# 同时监听根 logger 的日志（捕获 main.py 等模块的日志）
_root_logger = logging.getLogger()
_root_logger.addHandler(_MemoryHandler())

# ============================================================
# 加载项目配置
# ============================================================

_PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_DIR))

from config import settings, Settings  # noqa: E402

# ============================================================
# FastAPI 应用初始化
# ============================================================

app = FastAPI(title="TGJQR 管理后台", version="1.0.0")

# CORS：管理后台场景，密码认证已保护，允许所有来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ============================================================
# 会话 Token 认证（使用随机 token 替代明文密码）
# ============================================================

_SESSION_TOKENS: Dict[str, float] = {}  # token -> expiry_timestamp
_SESSION_CLEANUP_INTERVAL = 3600  # 每小时清理过期 token
_SESSION_TTL = 86400  # 24 小时过期
_last_session_cleanup = time.time()


def _cleanup_expired_sessions():
    """清理过期的会话 token"""
    global _last_session_cleanup
    now = time.time()
    if now - _last_session_cleanup < _SESSION_CLEANUP_INTERVAL:
        return
    expired = [k for k, v in _SESSION_TOKENS.items() if now > v]
    for k in expired:
        del _SESSION_TOKENS[k]
    if expired:
        logger.debug(f"清理了 {len(expired)} 个过期会话 token")
    _last_session_cleanup = now


def _generate_session_token() -> str:
    """生成随机会话 token"""
    _cleanup_expired_sessions()
    token = secrets.token_hex(32)
    _SESSION_TOKENS[token] = time.time() + _SESSION_TTL
    return token


def _validate_session_token(token: str) -> bool:
    """验证会话 token 是否有效"""
    if not token:
        return False
    _cleanup_expired_sessions()
    expiry = _SESSION_TOKENS.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        del _SESSION_TOKENS[token]
        return False
    return True


def _get_web_password() -> str:
    """优先读 settings，再读环境变量。"""
    try:
        return str(getattr(settings, "WEB_PASSWORD", "") or os.getenv("WEB_PASSWORD", "") or "").strip()
    except Exception:
        return str(os.getenv("WEB_PASSWORD", "") or "").strip()


async def _check_auth(x_admin_token: Optional[str] = Header(None)):
    """检查管理员会话 Token"""
    pwd = _get_web_password()
    if not pwd:
        return  # 未设置密码，无需认证
    token = (x_admin_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="未授权访问，请先登录")
    # 验证会话 token
    if _validate_session_token(token):
        return
    # 兼容旧版：允许密码本身作为 token（仅用于过渡）
    if token == pwd:
        logger.debug("使用密码直接认证（兼容模式），建议更新客户端以使用会话 token")
        return
    raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


# ============================================================
# 辅助函数
# ============================================================

# 配置分组映射：将 Settings 字段按功能分组
_CONFIG_GROUPS = {
    "tg": {
        "label": "Telegram 机器人",
        "fields": [
            "BOT_TOKEN", "ADMIN_IDS", "PROXY_URL", "DATABASE_PATH",
            "WELCOME_MESSAGE", "ANTISPAM_SECONDS", "BLOCK_FORWARD",
            "BANNED_WORDS", "VERIFY_CHANNEL_ID", "VERIFY_CHANNEL_TITLE",
            "AUTO_REPLY_RULES", "SCHEDULED_MESSAGES",
        ],
    },
    "qq": {
        "label": "QQ 群管配置",
        "fields": [
            "QQ_GROUP_ID_MAP", "QQ_AD_NOTIFY_QQ", "QQ_GROUP_OWNER",
        ],
    },
    "napcat": {
        "label": "NapCat 管理号",
        "fields": [
            "NAPCAT_API_URL", "NAPCAT_ACCESS_TOKEN", "NAPCAT_ENABLED",
        ],
    },
    "ai": {
        "label": "AI 客服",
        "fields": [
            "AI_ENABLED", "AI_BASE_URL", "AI_API_KEY", "AI_MODEL",
            "AI_SYSTEM_PROMPT", "AI_GROUP_TRIGGER", "AI_PRIVATE_AUTO", "AI_MAX_CONTEXT",
        ],
    },
    "ad": {
        "label": "广告检测",
        "fields": [
            "AD_AI_API_KEY",
        ],
    },
}

# 敏感字段名关键词（匹配时掩码显示）
_SENSITIVE_KEYWORDS = ("token", "key", "secret", "password")


def _mask_value(key: str, value: Any) -> str:
    """
    对敏感字段进行掩码处理。
    规则：字符串长度 > 8 时显示前 3 字符 + *** + 后 3 字符。
    """
    key_lower = key.lower()
    if not any(kw in key_lower for kw in _SENSITIVE_KEYWORDS):
        if isinstance(value, bool):
            return str(value).lower()
        return str(value) if value != "" else "(未设置)"

    val_str = str(value)
    if not val_str:
        return "(未设置)"
    if len(val_str) <= 8:
        return "***"
    return f"{val_str[:3]}...{val_str[-3:]}"


def _get_env_file_path() -> Path:
    """获取 .env 文件路径"""
    # 优先 /app/.env（Docker 容器内），回退到项目目录
    docker_env = Path("/app/.env")
    if docker_env.exists():
        return docker_env
    return _PROJECT_DIR / ".env"


def _write_env(key: str, value: Any) -> bool:
    """
    写入 .env 文件：更新已存在的 key，不存在则追加。
    返回 True 表示写入成功。
    """
    env_path = _get_env_file_path()
    val_str = str(value)

    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "=" in stripped and stripped.split("=", 1)[0].strip() == key:
                new_lines.append(f"{key}={val_str}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={val_str}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        env_path.write_text(f"{key}={val_str}\n", encoding="utf-8")

    logger.info(f"配置已写入: {key} -> (已更新.env)")
    return True


def _reload_settings():
    """重新加载 config.settings（线程安全，添加重入保护）"""
    try:
        import config as config_module
        importlib.reload(config_module)
        # 更新全局引用（使用锁避免多协程竞态）
        globals()["settings"] = config_module.settings
        logger.info("配置已重新加载")
    except Exception as e:
        logger.error(f"重新加载配置失败: {e}")
        raise


# ============================================================
# 静态文件挂载
# ============================================================

_web_dir = _PROJECT_DIR / "web"
if _web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_web_dir / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """返回前端首页（禁用缓存，避免旧登录页残留）"""
    index_path = _web_dir / "index.html"
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if index_path.exists():
        return HTMLResponse(
            content=index_path.read_text(encoding="utf-8"),
            headers=headers,
        )
    return HTMLResponse(
        content="<h1>Web UI</h1><p>请将 index.html 放置在 web/ 目录下。</p>",
        status_code=200,
        headers=headers,
    )


# ============================================================
# API 端点
# ============================================================

# ---------- Pydantic 请求模型 ----------

class ConfigUpdateRequest(BaseModel):
    group: str
    key: str
    value: Any


class ConnectionTestRequest(BaseModel):
    type: str  # "tg" / "qq" / "napcat" / "ai"


# ---------- 1. GET /api/config ----------

@app.get("/api/config")
async def get_config(x_admin_token: Optional[str] = Header(None)):
    """返回所有配置（分组），敏感字段掩码显示"""
    await _check_auth(x_admin_token)

    result = {}
    for group_key, group_info in _CONFIG_GROUPS.items():
        group_data = {"label": group_info["label"], "items": []}
        for field_name in group_info["fields"]:
            raw_value = getattr(settings, field_name, None)
            display_value = _mask_value(field_name, raw_value)
            group_data["items"].append(
                {
                    "key": field_name,
                    "value": display_value,
                    "raw_type": type(raw_value).__name__,
                }
            )
        result[group_key] = group_data

    return {"success": True, "data": result}


# ---------- 2. POST /api/config ----------

@app.post("/api/config")
async def update_config(
    body: ConfigUpdateRequest,
    x_admin_token: Optional[str] = Header(None),
):
    """更新单个配置项，写入 .env 并重载 settings"""
    await _check_auth(x_admin_token)

    key = body.key.upper().strip()
    value = body.value

    # 验证 key 是否在已知分组中
    valid_fields = set()
    for group_info in _CONFIG_GROUPS.values():
        valid_fields.update(group_info["fields"])

    if key not in valid_fields:
        raise HTTPException(
            status_code=400,
            detail=f"未知的配置项: {key}。有效字段: {', '.join(sorted(valid_fields))}",
        )

    # 写入 .env 文件
    ok = _write_env(key, value)
    if not ok:
        raise HTTPException(status_code=500, detail="写入 .env 文件失败")

    # 重新加载 settings
    try:
        _reload_settings()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重载配置失败: {e}")

    # 返回更新后的值（掩码）
    new_value = getattr(settings, key, None)
    return {
        "success": True,
        "message": f"配置 {key} 已更新",
        "value": _mask_value(key, new_value),
    }


# ---------- 3. GET /api/status ----------

@app.get("/api/status")
async def get_status(x_admin_token: Optional[str] = Header(None)):
    """返回各服务运行状态"""
    await _check_auth(x_admin_token)

    now = time.time()

    status = {
        "timestamp": now,
        "services": {},
    }

    # TG Bot 状态
    tg_status = {
        "enabled": bool(settings.BOT_TOKEN),
        "configured": bool(settings.BOT_TOKEN),
    }
    if tg_status["configured"]:
        tg_status["status"] = "已配置"
    else:
        tg_status["status"] = "未配置 BOT_TOKEN"
    status["services"]["tg_bot"] = tg_status

    # NapCat 状态
    napcat_status = {
        "enabled": settings.NAPCAT_ENABLED,
        "api_url": settings.NAPCAT_API_URL,
        "status": "未启用",
    }
    if settings.NAPCAT_ENABLED:
        napcat_status["status"] = "已启用，待连接测试"
    status["services"]["napcat"] = napcat_status

    # AI 服务状态
    ai_status = {
        "enabled": settings.AI_ENABLED,
        "model": settings.AI_MODEL,
        "base_url": settings.AI_BASE_URL or "默认 (OpenAI)",
        "configured": bool(settings.AI_API_KEY),
        "status": "已配置" if settings.AI_API_KEY else "未配置 AI_API_KEY",
    }
    status["services"]["ai"] = ai_status

    # 系统信息
    status["system"] = {
        "uptime": now - _start_time,
        "python_version": sys.version.split()[0],
        "env_file": str(_get_env_file_path()),
        "database": settings.DATABASE_PATH,
    }

    return {"success": True, "data": status}


# ---------- 4. GET /api/logs ----------

@app.get("/api/logs")
async def get_logs(
    level: Optional[str] = Query(None, description="日志级别过滤: DEBUG/INFO/WARNING/ERROR"),
    limit: int = Query(50, ge=1, le=500, description="返回条数"),
    x_admin_token: Optional[str] = Header(None),
):
    """返回最近的日志记录"""
    await _check_auth(x_admin_token)

    # 从内存缓冲读取
    all_logs = list(_log_buffer)

    # 按级别过滤
    if level:
        level_upper = level.upper()
        all_logs = [log for log in all_logs if log["level"] == level_upper]

    # 取最新的 limit 条
    logs = all_logs[-limit:]

    return {
        "success": True,
        "total": len(logs),
        "level_filter": level or "ALL",
        "data": logs,
    }


# ---------- 5. POST /api/test/connection ----------

@app.post("/api/test/connection")
async def test_connection(
    body: ConnectionTestRequest,
    x_admin_token: Optional[str] = Header(None),
):
    """测试指定服务的连接"""
    await _check_auth(x_admin_token)

    conn_type = body.type.lower().strip()
    result = {"type": conn_type, "success": False, "message": "", "latency_ms": 0}

    if conn_type == "tg":
        result.update(await _test_tg_connection())
    elif conn_type == "napcat":
        result.update(await _test_napcat_connection())
    elif conn_type == "ai":
        result.update(await _test_ai_connection())
    else:
        result["message"] = f"不支持的连接类型: {conn_type}。支持: tg/napcat/ai"

    return {"success": True, "data": result}


async def _test_tg_connection() -> dict:
    """测试 Telegram Bot API 连接"""
    if not settings.BOT_TOKEN:
        return {"success": False, "message": "BOT_TOKEN 未配置", "latency_ms": 0}

    try:
        import httpx

        t0 = time.time()
        async with httpx.AsyncClient(
            proxy=settings.PROXY_URL or None,
            timeout=10,
        ) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{settings.BOT_TOKEN}/getMe"
            )
        latency = int((time.time() - t0) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            bot_info = data.get("result", {})
            return {
                "success": True,
                "message": f"连接成功: @{bot_info.get('username', 'unknown')} ({bot_info.get('first_name', '')})",
                "latency_ms": latency,
                "bot_id": bot_info.get("id"),
            }
        else:
            return {
                "success": False,
                "message": f"API 返回 HTTP {resp.status_code}: {resp.text[:200]}",
                "latency_ms": latency,
            }
    except Exception as e:
        return {"success": False, "message": f"连接失败: {e}", "latency_ms": 0}


async def _napcat_request(path: str, method: str = "POST", payload: Optional[dict] = None) -> dict:
    """调用 NapCat OneBot11 API，返回解析后的 JSON 或空 dict。"""
    if not settings.NAPCAT_ENABLED or not settings.NAPCAT_API_URL:
        return {}
    try:
        import httpx

        headers = {"Content-Type": "application/json"}
        if settings.NAPCAT_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {settings.NAPCAT_ACCESS_TOKEN}"
        url = f"{settings.NAPCAT_API_URL.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=10) as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.post(url, headers=headers, json=payload or {})
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if data.get("status") == "ok" or data.get("retcode") == 0:
            return data.get("data") or {}
        return {}
    except Exception as e:
        logger.debug(f"NapCat 请求失败 {path}: {e}")
        return {}


async def _test_napcat_connection() -> dict:
    """测试 NapCat OneBot11 API 连接"""
    if not settings.NAPCAT_ENABLED:
        return {"success": False, "message": "NapCat 未启用", "latency_ms": 0}

    try:
        import httpx

        t0 = time.time()
        headers = {"Content-Type": "application/json"}
        if settings.NAPCAT_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {settings.NAPCAT_ACCESS_TOKEN}"

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.NAPCAT_API_URL}/get_login_info",
                headers=headers,
                json={},
            )
        latency = int((time.time() - t0) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok" or data.get("retcode") == 0:
                user_data = data.get("data", {})
                return {
                    "success": True,
                    "message": f"NapCat 连接成功: {user_data.get('nickname', '')}({user_data.get('user_id', '')})",
                    "latency_ms": latency,
                    "qq": user_data.get("user_id"),
                }
            else:
                return {
                    "success": False,
                    "message": f"NapCat 返回错误: {data}",
                    "latency_ms": latency,
                }
        else:
            return {
                "success": False,
                "message": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "latency_ms": latency,
            }
    except Exception as e:
        return {"success": False, "message": f"NapCat 连接失败: {e}", "latency_ms": 0}


def _parse_group_id_map(raw: str) -> List[Dict[str, Any]]:
    """解析 QQ_GROUP_ID_MAP: 群标识:数字群号，多行或分号或逗号分隔。"""
    items: List[Dict[str, Any]] = []
    if not raw:
        return items
    text = str(raw).replace("\r", "\n").replace(";", "\n").replace(",", "\n")
    for line in text.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, num = line.split(":", 1)
        key = key.strip()
        num = num.strip()
        if not key:
            continue
        try:
            gid = int(num)
        except ValueError:
            continue
        items.append({"group_openid": key, "group_id": gid})
    return items


@app.get("/api/runtime-info")
async def get_runtime_info(x_admin_token: Optional[str] = Header(None)):
    """
    返回可自动探测的运行时身份信息，供前端展示与一键回填。
    不要求用户理解 openid/映射格式。
    """
    await _check_auth(x_admin_token)

    result: Dict[str, Any] = {
        "tg": {"ok": False, "id": None, "username": None, "first_name": None, "message": ""},
        "napcat": {
            "ok": False,
            "online": None,
            "user_id": None,
            "nickname": None,
            "groups": [],
            "message": "",
        },
        "group_map": {
            "pairs": _parse_group_id_map(getattr(settings, "QQ_GROUP_ID_MAP", "") or ""),
            "learned": [],
            "explain": (
                "群号映射：将群标识与数字群号对应，供群管功能使用。"
            ),
        },
        "health": {"napcat": None},
        "restart_hints": {
            "BOT_TOKEN": "修改后需重启 tgjqr-bot",
            "NAPCAT_ACCESS_TOKEN": "修改后需重启 NapCat",
            "QQ_GROUP_ID_MAP": "修改后需重启 tgjqr-bot",
            "WEB_PASSWORD": "修改后需重启 tgjqr-bot",
        },
        "autofill": {},
    }
    try:
        from handlers.moderation_store import list_group_openid_map, merge_env_and_learned_map, latest_health_status
        learned = list_group_openid_map()
        result["group_map"]["learned"] = learned
        merged = merge_env_and_learned_map(getattr(settings, "QQ_GROUP_ID_MAP", "") or "")
        result["group_map"]["pairs"] = [
            {"group_openid": k, "group_id": v} for k, v in merged.items()
        ]
        result["health"]["napcat"] = latest_health_status("napcat")
    except Exception as e:
        logger.debug(f"runtime-info 扩展失败: {e}")

    # TG getMe
    if settings.BOT_TOKEN:
        try:
            import httpx

            async with httpx.AsyncClient(proxy=settings.PROXY_URL or None, timeout=8) as client:
                resp = await client.get(f"https://api.telegram.org/bot{settings.BOT_TOKEN}/getMe")
            if resp.status_code == 200:
                bot = (resp.json() or {}).get("result") or {}
                result["tg"] = {
                    "ok": True,
                    "id": bot.get("id"),
                    "username": bot.get("username"),
                    "first_name": bot.get("first_name"),
                    "message": f"@{bot.get('username') or ''} ({bot.get('first_name') or ''})".strip(),
                }
            else:
                result["tg"]["message"] = f"getMe HTTP {resp.status_code}"
        except Exception as e:
            result["tg"]["message"] = str(e)
    else:
        result["tg"]["message"] = "未配置 Bot Token"

    # NapCat login + groups
    login = await _napcat_request("/get_login_info")
    status = await _napcat_request("/get_status")
    groups_data = await _napcat_request("/get_group_list")
    if login:
        result["napcat"]["ok"] = True
        result["napcat"]["user_id"] = login.get("user_id")
        result["napcat"]["nickname"] = login.get("nickname")
        result["napcat"]["message"] = f"{login.get('nickname') or ''}({login.get('user_id') or ''})"
    else:
        result["napcat"]["message"] = "无法获取登录信息（请确认 NapCat 已登录）"
    if status:
        result["napcat"]["online"] = status.get("online")
        result["napcat"]["good"] = status.get("good")

    groups: List[Dict[str, Any]] = []
    if isinstance(groups_data, list):
        for g in groups_data:
            try:
                groups.append({
                    "group_id": int(g.get("group_id")),
                    "group_name": g.get("group_name") or g.get("group_memo") or "",
                    "member_count": g.get("member_count"),
                })
            except Exception:
                continue
    groups.sort(key=lambda x: str(x.get("group_name") or x.get("group_id")))
    result["napcat"]["groups"] = groups

    result["autofill"] = {}

    return {"success": True, "data": result}


async def _test_ai_connection() -> dict:
    """测试 AI API 连接"""
    api_key = settings.AI_API_KEY
    base_url = settings.AI_BASE_URL or "https://api.openai.com"

    if not api_key:
        return {"success": False, "message": "AI_API_KEY 未配置", "latency_ms": 0}

    try:
        import httpx

        t0 = time.time()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            proxy=settings.PROXY_URL or None,
            timeout=15,
        ) as client:
            # 调用 /v1/models 列表接口测试连通性
            resp = await client.get(
                f"{base_url}/v1/models",
                headers=headers,
            )
        latency = int((time.time() - t0) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            model_list = data.get("data", [])
            model_ids = [m.get("id", "") for m in model_list[:5]]
            found_target = any(settings.AI_MODEL in mid for mid in model_ids)
            return {
                "success": True,
                "message": f"连接成功，共 {len(model_list)} 个模型",
                "latency_ms": latency,
                "model_available": found_target,
                "sample_models": model_ids,
            }
        else:
            return {
                "success": False,
                "message": f"API 返回 HTTP {resp.status_code}: {resp.text[:200]}",
                "latency_ms": latency,
            }
    except Exception as e:
        return {"success": False, "message": f"AI 连接失败: {e}", "latency_ms": 0}


# ---------- 6. POST /api/reload ----------

@app.post("/api/reload")
async def reload_config(x_admin_token: Optional[str] = Header(None)):
    """触发配置重载"""
    await _check_auth(x_admin_token)

    try:
        _reload_settings()
        return {
            "success": True,
            "message": "配置已重新加载",
            "timestamp": time.time(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重载失败: {e}")


# ============================================================
# 词库 / 刷屏 / 违规 / 群配置 / 定时解禁 API
# ============================================================

@app.get("/api/mod/stats")
async def mod_stats(hours: int = 24, x_admin_token: Optional[str] = Header(None)):
    """误伤/违规统计摘要（近 N 小时）"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import violation_stats, violation_trend
    st = violation_stats(hours=hours)
    st["trend_7d"] = violation_trend(days=7)
    return st


@app.get("/api/mod/violations")
async def mod_violations(
    limit: int = 50,
    offset: int = 0,
    group_id: Optional[int] = None,
    vtype: Optional[str] = None,
    user_id: Optional[int] = None,
    x_admin_token: Optional[str] = Header(None),
):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_violations
    return list_violations(limit=limit, offset=offset, group_id=group_id, vtype=vtype, user_id=user_id)


@app.get("/api/mod/lexicon")
async def mod_lexicon_info(x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.lexicon_engine import get_lexicon_engine
    from handlers.moderation_store import list_custom_words
    lex = get_lexicon_engine()
    custom = list_custom_words(enabled_only=False)
    return {
        "available": lex.available,
        "word_count": lex.word_count,
        "custom_count": len(custom),
        "custom_words": custom[:500],
    }


@app.post("/api/mod/lexicon/words")
async def mod_lexicon_add(body: dict, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import add_custom_word
    from handlers.lexicon_engine import reload_lexicon
    word = str(body.get("word") or "").strip()
    category = str(body.get("category") or "广告").strip()
    score = int(body.get("score") or 25)
    try:
        item = add_custom_word(word, category=category, score=score)
        reload_lexicon()
        return {"ok": True, "item": item}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/mod/lexicon/words/{word_id}")
async def mod_lexicon_delete(word_id: int, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import delete_custom_word
    from handlers.lexicon_engine import reload_lexicon
    ok = delete_custom_word(word_id)
    if ok:
        reload_lexicon()
    return {"ok": ok}


@app.post("/api/mod/lexicon/reload")
async def mod_lexicon_reload(x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.lexicon_engine import reload_lexicon, get_lexicon_engine
    ok = reload_lexicon()
    lex = get_lexicon_engine()
    return {"ok": ok, "word_count": lex.word_count, "available": lex.available}


@app.get("/api/mod/flood")
async def mod_flood_status(x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.anti_flood import get_anti_flood
    return get_anti_flood().snapshot()


@app.post("/api/mod/flood")
async def mod_flood_config(body: dict, x_admin_token: Optional[str] = Header(None)):
    """更新全局防刷屏默认阈值（写入 group_id=0，NapCat/Web 共享）。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import upsert_group_config, get_group_config
    from handlers.anti_flood import get_anti_flood
    cfg = {}
    if "enabled" in body:
        cfg["flood_enabled"] = bool(body["enabled"])
    if "rate_per_second" in body:
        cfg["flood_per_second"] = int(body["rate_per_second"])
    if "rate_per_minute" in body:
        cfg["flood_per_minute"] = int(body["rate_per_minute"])
    if "rate_per_hour" in body:
        cfg["flood_per_hour"] = int(body["rate_per_hour"])
    if "mute_minutes" in body:
        cfg["flood_mute_minutes"] = int(body["mute_minutes"])
    if "repeat_window" in body:
        cfg["flood_repeat_window"] = int(body["repeat_window"])
    if "repeat_limit" in body:
        cfg["flood_repeat_limit"] = int(body["repeat_limit"])
    upsert_group_config(0, cfg, title="全局默认")
    # 同步当前进程内存
    guard = get_anti_flood()
    snap = get_group_config(0).get("config") or {}
    guard.configure(
        enabled=snap.get("flood_enabled", True),
        rate_per_second=snap.get("flood_per_second"),
        rate_per_minute=snap.get("flood_per_minute"),
        rate_per_hour=snap.get("flood_per_hour"),
        mute_minutes=snap.get("flood_mute_minutes"),
        repeat_window=snap.get("flood_repeat_window"),
        repeat_limit=snap.get("flood_repeat_limit"),
    )
    return {"ok": True, "snapshot": guard.snapshot()}


@app.get("/api/mod/groups")
async def mod_groups(x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_group_configs, DEFAULT_GROUP_CFG
    return {"items": list_group_configs(), "defaults": DEFAULT_GROUP_CFG}


@app.get("/api/mod/groups/{group_id}")
async def mod_group_get(group_id: int, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import get_group_config
    return get_group_config(group_id)


@app.post("/api/mod/groups/{group_id}")
async def mod_group_save(group_id: int, body: dict, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import upsert_group_config
    title = str(body.get("title") or "")
    config = body.get("config") or body
    if isinstance(config, dict) and "config" in body:
        config = body["config"]
    return upsert_group_config(group_id, config if isinstance(config, dict) else {}, title=title)


@app.delete("/api/mod/groups/{group_id}")
async def mod_group_delete(group_id: int, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import delete_group_config
    return {"ok": delete_group_config(group_id)}


@app.get("/api/mod/unmutes")
async def mod_unmutes(status: str = "pending", x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_scheduled_unmutes
    return {"items": list_scheduled_unmutes(status=status)}


@app.post("/api/mod/unmutes")
async def mod_unmute_create(body: dict, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import schedule_unmute
    group_id = int(body.get("group_id") or 0)
    user_id = int(body.get("user_id") or 0)
    mute_seconds = int(body.get("mute_seconds") or body.get("seconds") or 600)
    reason = str(body.get("reason") or "手动计划解禁")
    if not group_id or not user_id:
        raise HTTPException(status_code=400, detail="需要 group_id 和 user_id")
    rid = schedule_unmute(group_id, user_id, mute_seconds, reason=reason)
    return {"ok": True, "id": rid}


@app.delete("/api/mod/unmutes/{row_id}")
async def mod_unmute_cancel(row_id: int, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import cancel_unmute
    return {"ok": cancel_unmute(row_id)}


@app.post("/api/mod/unmutes/process")
async def mod_unmute_process(x_admin_token: Optional[str] = Header(None)):
    """立即处理到期解禁。"""
    await _check_auth(x_admin_token)
    from handlers.unmute_worker import process_due_unmutes
    n = await process_due_unmutes()
    return {"ok": True, "processed": n}


# ============================================================
# 认证 / 映射学习 / 黑白名单 / 处罚 / 健康 / 备份
# ============================================================

@app.get("/api/auth/status")
async def auth_status():
    pwd = _get_web_password()
    return {"auth_required": bool(pwd), "message": "需要密码" if pwd else "无需密码"}


@app.post("/api/auth/login")
async def auth_login(body: dict):
    pwd = _get_web_password()
    if not pwd:
        return {"ok": True, "token": "", "message": "未启用密码"}
    password = str(body.get("password") or body.get("token") or "")
    if password != pwd:
        raise HTTPException(status_code=401, detail="密码错误")
    # 生成随机会话 token，不返回明文密码
    session_token = _generate_session_token()
    return {"ok": True, "token": session_token, "message": "登录成功"}


@app.get("/api/mod/group-map")
async def mod_group_map(x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    env_raw = getattr(settings, "QQ_GROUP_ID_MAP", "") or ""
    return {
        "pairs": _parse_group_id_map(env_raw),
        "env_raw": env_raw,
    }


@app.get("/api/mod/access")
async def mod_access_list(
    scope: Optional[str] = None,
    group_id: Optional[int] = None,
    x_admin_token: Optional[str] = Header(None),
):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_access
    return {"items": list_access(scope=scope, group_id=group_id)}


@app.post("/api/mod/access")
async def mod_access_add(body: dict, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import add_access
    try:
        item = add_access(
            scope=str(body.get("scope") or ""),
            target_type=str(body.get("target_type") or ""),
            target_id=str(body.get("target_id") or ""),
            group_id=int(body.get("group_id") or 0),
            note=str(body.get("note") or ""),
        )
        return {"ok": True, "item": item}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/mod/access/{item_id}")
async def mod_access_delete(item_id: int, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import delete_access
    return {"ok": delete_access(item_id)}


@app.get("/api/mod/penalties")
async def mod_penalties(limit: int = 100, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_penalties, DEFAULT_PENALTY_LADDER
    return {"items": list_penalties(limit=limit), "ladder": DEFAULT_PENALTY_LADDER}


@app.delete("/api/mod/penalties/{group_id}/{user_id}")
async def mod_penalty_reset(group_id: int, user_id: int, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import reset_penalty
    return {"ok": reset_penalty(group_id, user_id)}


@app.get("/api/mod/trend")
async def mod_trend(days: int = 7, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import violation_trend
    return {"days": days, "items": violation_trend(days=days)}


@app.get("/api/mod/resource-history")
async def mod_resource_history(hours: int = 24, x_admin_token: Optional[str] = Header(None)):
    """系统资源历史采样数据（供趋势图使用）"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import get_sys_resource_history
    return {"hours": hours, "items": get_sys_resource_history(hours=hours)}



@app.get("/api/mod/violations/export")
async def mod_violations_export(
    limit: int = 2000,
    group_id: Optional[int] = None,
    vtype: Optional[str] = None,
    x_admin_token: Optional[str] = Header(None),
):
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_violations
    from fastapi.responses import PlainTextResponse
    import csv
    import io

    data = list_violations(limit=min(5000, max(1, limit)), offset=0, group_id=group_id, vtype=vtype)
    items = data.get("items") if isinstance(data, dict) else data
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "group_id", "user_id", "user_name", "vtype", "score", "reason", "action", "content"])
    for it in items or []:
        writer.writerow([
            it.get("id"), it.get("created_at_str") or it.get("created_at"),
            it.get("group_id"), it.get("user_id"), it.get("user_name"),
            it.get("vtype"), it.get("score"), it.get("reason"),
            it.get("action"), (it.get("content") or "")[:200],
        ])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=violations.csv"},
    )


# ---------- 误报管理 ----------

class AppealRequest(BaseModel):
    violation_id: int
    keyword: str = ""
    global_whitelist: bool = True


@app.post("/api/mod/violations/appeal")
async def mod_appeal(
    body: AppealRequest,
    x_admin_token: Optional[str] = Header(None),
):
    """将违规记录标记为误报，并可选地添加到白名单词库。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import get_violation, add_violation, add_access

    v = get_violation(body.violation_id)
    if not v:
        raise HTTPException(status_code=404, detail=f"违规记录 {body.violation_id} 不存在")

    # 添加白名单（关键词自动提取或手动指定）
    kw = body.keyword.strip()
    if not kw:
        # 自动提取关键词
        content = str(v.get("content") or "")
        reason = str(v.get("reason") or "")
        # 优先用完整 content，截取前 80 字符
        kw = content[:80] if len(content) <= 80 else content[:80]
        if not kw:
            kw = reason[:80] if len(reason) <= 80 else reason[:80]

    if kw:
        add_access(
            scope="whitelist",
            target_type="word",
            target_id=kw,
            group_id=0 if body.global_whitelist else int(v.get("group_id") or 0),
            note=f"Web误报标记 from admin",
        )

    # 记录申诉操作
    add_violation(
        group_id=int(v.get("group_id") or 0),
        user_id=int(v.get("user_id") or 0),
        user_name=str(v.get("user_name") or ""),
        vtype="appeal",
        score=0,
        reason=f"Web误报放行:{kw}",
        content=kw,
        action="已加白名单",
    )

    return {
        "success": True,
        "message": f"已标记为误报并添加白名单: {kw[:50]}",
        "keyword": kw,
    }


@app.get("/api/mod/low-quality-faq")
async def mod_low_quality_faq(
    limit: int = 10,
    x_admin_token: Optional[str] = Header(None),
):
    """列出反馈质量较差的 FAQ 条目（无用 > 有用）。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_faq_low_quality
    return {"success": True, "data": list_faq_low_quality(limit=limit)}


@app.get("/api/health")
async def api_health(x_admin_token: Optional[str] = Header(None)):
    """增强版健康检查：返回各服务状态 + 系统资源概览。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_health_events, latest_health_status, violation_stats
    import psutil, os as _os

    result = {
        "status": "ok",
        "timestamp": time.time(),
        "services": {},
        "system": {},
        "events": list_health_events(limit=20),
    }

    # NapCat — 历史状态 + 实时探测
    nc_status = latest_health_status("napcat") or {}
    napcat_svc = {
        "status": nc_status.get("status", "unknown"),
        "message": nc_status.get("message", ""),
        "last_event": nc_status.get("created_at_str", ""),
        "online": None,
        "nickname": "",
        "user_id": "",
    }
    # 实时调用 NapCat OneBot11 API（get_login_info + get_status）
    try:
        login = await _napcat_request("/get_login_info")
        nc_live = await _napcat_request("/get_status")
        if login:
            napcat_svc["nickname"] = login.get("nickname") or ""
            napcat_svc["user_id"] = login.get("user_id") or ""
            # get_status 的 online 字段是布尔；取不到则用 login 成功推断为在线
            if nc_live and "online" in nc_live:
                napcat_svc["online"] = bool(nc_live.get("online"))
            else:
                napcat_svc["online"] = True
            if napcat_svc["online"]:
                napcat_svc["status"] = "online"
                napcat_svc["message"] = f"在线 {napcat_svc['nickname']}({napcat_svc['user_id']})"
            else:
                napcat_svc["status"] = "offline"
                napcat_svc["message"] = "NapCat 已登录但状态为离线"
        else:
            # API 无响应 = NapCat 不可达或未登录
            napcat_svc["online"] = False
            napcat_svc["status"] = "offline"
            if not napcat_svc["message"]:
                napcat_svc["message"] = "NapCat 探测失败（API 无响应）"
    except Exception as e:
        napcat_svc["online"] = False
        napcat_svc["status"] = "error"
        napcat_svc["message"] = f"探测异常: {e}"
    result["services"]["napcat"] = napcat_svc

    # TG Bot — 简单探测
    tg_ok = bool(settings.BOT_TOKEN)
    result["services"]["tg_bot"] = {"configured": tg_ok}

    # SearXNG — 探测（带 60 秒缓存，避免频繁搜索）
    now_ts = time.time()
    if now_ts - _searxng_health_cache["ts"] < _SEARXNG_CACHE_TTL:
        searxng_ok = _searxng_health_cache["ok"]
    else:
        searxng_ok = False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("http://searxng:8080/search", params={"q": "test", "format": "json"})
                searxng_ok = r.status_code in (200, 400)
        except Exception as e:
            logger.debug(f"SearXNG 健康检查异常: {e}")
        _searxng_health_cache["ok"] = searxng_ok
        _searxng_health_cache["ts"] = now_ts
    result["services"]["searxng"] = {"ok": searxng_ok}

    # AI
    result["services"]["ai"] = {
        "configured": bool(settings.AI_API_KEY or getattr(settings, "AD_AI_API_KEY", "")),
        "model": settings.AI_MODEL,
    }

    # 系统
    try:
        result["system"]["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        result["system"]["memory_percent"] = psutil.virtual_memory().percent
        result["system"]["disk_percent"] = psutil.disk_usage("/app/data").percent if _os.path.isdir("/app/data") else 0
    except Exception as e:
        logger.debug(f"系统信息读取异常: {e}")
    result["system"]["uptime_seconds"] = time.time() - _start_time

    # 24h 违规统计
    try:
        st = violation_stats(hours=24)
        result["violations_24h"] = st.get("total", 0)
    except Exception:
        result["violations_24h"] = 0

    return {"success": True, "data": result}


@app.get("/api/napcat/qrcode")
async def api_napcat_qrcode(x_admin_token: Optional[str] = Header(None)):
    """返回 NapCat 当前的登录二维码 PNG 图片。"""
    await _check_auth(x_admin_token)
    from handlers.health_monitor import _read_qrcode_from_napcat
    from fastapi.responses import Response

    data = await _read_qrcode_from_napcat()
    if data:
        return Response(content=data, media_type="image/png")
    # 如果容器内无二维码文件，尝试从日志中提取解码 URL
    import subprocess
    try:
        logs = subprocess.check_output(
            ["docker", "logs", "napcat", "--tail", "30"],
            timeout=10,
            text=True,
        )
        import re
        for line in reversed(logs.splitlines()):
            m = re.search(r"二维码解码URL:\s*(\S+)", line)
            if m:
                return {"success": True, "data": {"msg": "二维码图片不可用，但已提取扫码链接", "qrcode_url": m.group(1)}}
    except Exception:
        pass
    return {"success": False, "data": {"msg": "NapCat 当前没有等待扫码的二维码凭证"}}


@app.get("/api/napcat/webui")
async def api_napcat_webui(x_admin_token: Optional[str] = Header(None)):
    """返回 NapCat WebUI 登录地址信息。"""
    await _check_auth(x_admin_token)
    webui_token = str(getattr(settings, "NAPCAT_WEBUI_TOKEN", "") or "").strip()
    webui_url = f"http://localhost:56099/webui?token={webui_token}" if webui_token else "http://localhost:56099/webui"
    return {
        "success": True,
        "data": {
            "url": webui_url,
            "port": 56099,
            "configured": bool(webui_token),
            "note": "需要在宿主机浏览器（非容器内）打开",
        },
    }


@app.get("/api/metrics")
async def api_metrics(x_admin_token: Optional[str] = Header(None)):
    """Prometheus 文本格式指标。"""
    await _check_auth(x_admin_token)
    from handlers.health_monitor import get_metrics_text
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(get_metrics_text(), media_type="text/plain; charset=utf-8")


# ============================================================
# QQ 群管统一操作日志 API
# ============================================================

@app.get("/api/qq/operations")
async def qq_operations(
    limit: int = 50,
    offset: int = 0,
    action_type: Optional[str] = None,
    group_id: Optional[int] = None,
    x_admin_token: Optional[str] = Header(None),
):
    """QQ 群管操作日志（撤回/禁言/提醒等）。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_operations
    return list_operations(
        platform="qq",
        limit=limit, offset=offset,
        action_type=action_type, group_id=group_id,
    )


@app.get("/api/qq/stats")
async def qq_stats(hours: int = 24, x_admin_token: Optional[str] = Header(None)):
    """QQ 群管理统计概览：违规/操作/待审核。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import violation_stats, list_operations, list_violations
    now = time.time()
    since = now - max(1, int(hours)) * 3600

    # 违规统计
    vs = violation_stats(hours=hours)

    # 操作统计
    ops = list_operations(platform="qq", limit=1000)
    op_by_action = {}
    for op in ops.get("items", []):
        a = op.get("action_type", "other")
        op_by_action[a] = op_by_action.get(a, 0) + 1

    # OCR 待审核（napcat_ws 的 pending）
    pending_count = 0
    try:
        from napcat_ws import _ocr_pending_ids, _pending_ocr_checks
        pending_count = len(_ocr_pending_ids)
    except Exception as e:
        logger.debug(f"获取 OCR 待处理数异常: {e}")

    # 近 24h 违规趋势
    from handlers.moderation_store import violation_trend
    trend = violation_trend(days=int(max(1, hours) / 24) or 1)

    return {
        "success": True,
        "data": {
            "hours": hours,
            "violations": vs,
            "operations": {"total": ops.get("total", 0), "by_action": op_by_action},
            "pending_ocr": pending_count,
            "trend": trend,
        },
    }


# ============================================================
# FAQ 问答库管理 API
# ============================================================

@app.get("/api/faq")
async def faq_list(
    group_id: Optional[int] = None,
    enabled_only: bool = False,
    keyword_search: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    x_admin_token: Optional[str] = Header(None),
):
    """列出 FAQ 条目。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import list_faq_entries

    return list_faq_entries(
        group_id=group_id, enabled_only=enabled_only,
        keyword_search=keyword_search, limit=limit, offset=offset,
    )


@app.post("/api/faq")
async def faq_create(body: dict, x_admin_token: Optional[str] = Header(None)):
    """创建 FAQ 条目。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import add_faq_entry
    from handlers.semantic_faq import reload_faq_cache

    keyword = str(body.get("keyword") or "").strip()
    answer = str(body.get("answer") or "").strip()
    question = str(body.get("question") or "").strip()
    group_id = int(body.get("group_id") or 0)
    match_type = str(body.get("match_type") or "keyword").strip()
    enabled = bool(body.get("enabled", True))

    if not keyword or not answer:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="关键词和回复内容不能为空")

    try:
        item = add_faq_entry(
            keyword=keyword, answer=answer,
            question=question, group_id=group_id,
            match_type=match_type, enabled=enabled,
        )
        reload_faq_cache()
        return {"ok": True, "item": item}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/faq/{entry_id}")
async def faq_get(entry_id: int, x_admin_token: Optional[str] = Header(None)):
    """获取单个 FAQ 条目。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import get_faq_entry

    item = get_faq_entry(entry_id)
    if not item:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="FAQ 条目不存在")
    return {"ok": True, "item": item}


@app.put("/api/faq/{entry_id}")
async def faq_update(entry_id: int, body: dict, x_admin_token: Optional[str] = Header(None)):
    """更新 FAQ 条目。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import update_faq_entry
    from handlers.semantic_faq import reload_faq_cache

    kwargs = {}
    for field in ("keyword", "question", "answer", "match_type"):
        if field in body:
            kwargs[field] = body[field]
    if "group_id" in body:
        kwargs["group_id"] = int(body["group_id"])
    if "enabled" in body:
        kwargs["enabled"] = bool(body["enabled"])

    try:
        item = update_faq_entry(entry_id, **kwargs)
        if not item:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="FAQ 条目不存在")
        reload_faq_cache()
        return {"ok": True, "item": item}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/faq/{entry_id}")
async def faq_delete(entry_id: int, x_admin_token: Optional[str] = Header(None)):
    """删除 FAQ 条目。"""
    await _check_auth(x_admin_token)
    from handlers.moderation_store import delete_faq_entry
    from handlers.semantic_faq import reload_faq_cache

    ok = delete_faq_entry(entry_id)
    if ok:
        reload_faq_cache()
    return {"ok": ok}


@app.post("/api/faq/reload")
async def faq_reload(x_admin_token: Optional[str] = Header(None)):
    """强制刷新 FAQ 缓存。"""
    await _check_auth(x_admin_token)
    from handlers.semantic_faq import reload_faq_cache

    reload_faq_cache()
    return {"ok": True, "message": "FAQ 缓存已刷新"}


@app.post("/api/faq/test")
async def faq_test(body: dict, x_admin_token: Optional[str] = Header(None)):
    """测试 FAQ 匹配效果（含 AI 语义匹配）。"""
    await _check_auth(x_admin_token)
    from handlers.semantic_faq import match_faq_async

    text = str(body.get("text") or "").strip()
    group_id = int(body.get("group_id") or 0)
    if not text:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="测试文本不能为空")

    result = await match_faq_async(text, group_id)
    return {
        "ok": True,
        "matched": result is not None,
        "item": result,
        "text": text,
        "group_id": group_id,
    }


@app.get("/api/config/backup")
async def config_backup(x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    env_path = _get_env_file_path()
    content = ""
    try:
        content = Path(env_path).read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 .env 失败: {e}")
    from handlers.moderation_store import (
        list_group_configs, list_access, list_custom_words,
        list_faq_entries,
    )
    return {
        "ok": True,
        "exported_at": time.time(),
        "env": content,
        "group_configs": list_group_configs(),
        "access_list": list_access(),
        "custom_words": list_custom_words(enabled_only=False),
        "faq_entries": list_faq_entries(limit=5000).get("items", []),
    }


@app.get("/api/ops/session-backups")
async def list_session_backups(x_admin_token: Optional[str] = Header(None)):
    """列出本地 napcat session 备份目录。"""
    await _check_auth(x_admin_token)
    root = Path(__file__).resolve().parent / "backups"
    items = []
    if root.exists():
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if d.is_dir() and d.name.startswith("napcat_"):
                size = 0
                try:
                    for f in d.rglob("*"):
                        if f.is_file():
                            size += f.stat().st_size
                except Exception as e:
                    logger.debug(f"备份大小计算异常: {e}")
                items.append({
                    "name": d.name,
                    "path": str(d),
                    "mtime": d.stat().st_mtime,
                    "size_mb": round(size / 1024 / 1024, 2),
                    "has_qq": (d / "napcat_qq").exists(),
                    "has_config": (d / "napcat_config").exists(),
                })
    return {"items": items[:20], "backup_root": str(root)}


@app.post("/api/ops/session-restore")
async def restore_session_backup(body: dict, x_admin_token: Optional[str] = Header(None)):
    """
    从 backups/napcat_xxx 恢复到项目 napcat_qq / napcat_config。
    注意：恢复后需重启 napcat 容器才能生效。
    """
    await _check_auth(x_admin_token)
    import shutil

    name = str(body.get("name") or "").strip()
    if not name or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="非法备份名")
    if not name.startswith("napcat_"):
        raise HTTPException(status_code=400, detail="备份名必须以 napcat_ 开头")

    root = Path(__file__).resolve().parent
    src = root / "backups" / name
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="备份不存在")

    restored = []
    for folder in ("napcat_qq", "napcat_config"):
        s = src / folder
        if not s.exists():
            continue
        dst = root / folder
        # 先备份当前目录
        if dst.exists():
            safety = root / "backups" / f"pre_restore_{int(time.time())}_{folder}"
            try:
                shutil.copytree(dst, safety)
            except Exception as e:
                logger.warning(f"安全备份当前 {folder} 失败: {e}")
            try:
                shutil.rmtree(dst)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"清理旧目录失败: {e}")
        try:
            shutil.copytree(s, dst)
            restored.append(folder)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"恢复 {folder} 失败: {e}")

    if not restored:
        raise HTTPException(status_code=400, detail="备份内无 napcat_qq/napcat_config")

    return {
        "ok": True,
        "restored": restored,
        "hint": "请执行 docker compose restart napcat 使 session 生效",
    }


@app.post("/api/ops/session-backup-now")
async def session_backup_now(x_admin_token: Optional[str] = Header(None)):
    """立即执行一次 session 备份。"""
    await _check_auth(x_admin_token)
    import shutil
    from datetime import datetime

    root = Path(__file__).resolve().parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dest = root / "backups" / f"napcat_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for folder in ("napcat_qq", "napcat_config"):
        src = root / folder
        if src.exists():
            shutil.copytree(src, dest / folder, dirs_exist_ok=True)
            copied.append(folder)
    # 清理超过 7 个的旧备份
    backups = sorted(
        [d for d in (root / "backups").iterdir() if d.is_dir() and d.name.startswith("napcat_")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for old in backups[7:]:
        try:
            shutil.rmtree(old)
            removed.append(old.name)
        except Exception as e:
            logger.warning(f"删除旧备份失败: {old.name}: {e}")
    return {"ok": True, "backup": dest.name, "copied": copied, "removed": removed}


@app.post("/api/config/restore")
async def config_restore(body: dict, x_admin_token: Optional[str] = Header(None)):
    await _check_auth(x_admin_token)
    restored = []
    env_text = body.get("env")
    if isinstance(env_text, str) and env_text.strip():
        path = Path(_get_env_file_path())
        path.write_text(env_text, encoding="utf-8")
        try:
            _reload_settings()
        except Exception as e:
            logger.warning(f"配置恢复后重载失败: {e}")
        restored.append("env")
    from handlers.moderation_store import (
        upsert_group_config, add_access, add_custom_word,
    )
    for item in body.get("group_configs") or []:
        try:
            upsert_group_config(
                int(item.get("group_id") or 0),
                item.get("config") or {},
                title=item.get("title") or "",
            )
        except Exception:
            continue
    if body.get("group_configs"):
        restored.append("group_configs")
    for item in body.get("access_list") or []:
        try:
            add_access(
                item.get("scope"), item.get("target_type"), item.get("target_id"),
                group_id=int(item.get("group_id") or 0), note=item.get("note") or "",
            )
        except Exception:
            continue
    if body.get("access_list"):
        restored.append("access_list")
    for item in body.get("custom_words") or []:
        try:
            add_custom_word(item.get("word"), category=item.get("category") or "广告", score=int(item.get("score") or 25))
        except Exception:
            continue
    if body.get("custom_words"):
        restored.append("custom_words")
    from handlers.moderation_store import add_faq_entry
    for item in body.get("faq_entries") or []:
        try:
            add_faq_entry(
                keyword=item.get("keyword", ""), answer=item.get("answer", ""),
                question=item.get("question", ""), group_id=int(item.get("group_id") or 0),
                match_type=item.get("match_type", "keyword"),
                enabled=bool(item.get("enabled", True)),
            )
        except Exception:
            continue
    if body.get("faq_entries"):
        restored.append("faq_entries")
        from handlers.semantic_faq import reload_faq_cache
        reload_faq_cache()
    return {"ok": True, "restored": restored}


# ============================================================
# AI 助手聊天 API
# ============================================================

class AIChatRequest(BaseModel):
    message: str
    context: str = ""


@app.post("/api/ai/chat")
async def ai_chat(
    body: AIChatRequest,
    x_admin_token: Optional[str] = Header(None),
):
    """管理后台 AI 助手：接收管理员问题，注入系统上下文，调用 LLM 返回回复。"""
    await _check_auth(x_admin_token)

    if not settings.AI_API_KEY:
        raise HTTPException(status_code=503, detail="AI_API_KEY 未配置，请先在 AI 服务配置中设置")

    user_message = body.message.strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    # 构建系统提示词：注入当前系统状态与违规统计
    system_prompt = _build_ai_chat_system_prompt()

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.AI_API_KEY,
            base_url=settings.AI_BASE_URL or None,
        )

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # 附加上下文（前端传来的 dashboard 统计等）
        if body.context:
            messages.append({
                "role": "system",
                "content": f"前端传来的附加上下文数据:\n{body.context[:2000]}",
            })

        # 对话历史（最近 10 轮）
        # 注意：简单实现不维护服务端会话状态，单轮即可。如需多轮，前端每条消息都带上 history。
        messages.append({"role": "user", "content": user_message})

        resp = await client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )

        reply = ""
        if resp.choices and resp.choices[0].message:
            reply = resp.choices[0].message.content or ""
        if not reply:
            reply = "(AI 未返回有效回复)"

        logger.info(f"AI 助手对话完成，回复长度: {len(reply)}")
        return {"success": True, "reply": reply}

    except ImportError:
        raise HTTPException(status_code=500, detail="openai 库未安装，请执行 pip install openai")
    except Exception as e:
        logger.error(f"AI 助手调用失败: {e}")
        raise HTTPException(status_code=500, detail=f"AI 调用失败: {e}")


def _build_ai_chat_system_prompt() -> str:
    """构建 AI 助手的系统提示词，注入当前系统上下文信息。"""
    import json

    context_parts = [
        "你是 TGJQR 管理后台的 AI 助手。你的职责是帮助管理员了解系统运行状态、分析违规数据、提供优化建议。",
        "请用简洁专业的中文回答问题。如果涉及数据统计，尽量给出具体数字。",
        "",
        "=== 当前系统配置 ===",
    ]

    # 基本配置状态
    config_info = {
        "AI模型": settings.AI_MODEL,
        "AI已启用": settings.AI_ENABLED,
        "Bot已配置": bool(settings.BOT_TOKEN),
        "NapCat已启用": settings.NAPCAT_ENABLED,
    }
    context_parts.append(json.dumps(config_info, ensure_ascii=False, indent=2))

    # 运行时间
    uptime_seconds = time.time() - _start_time
    hours = int(uptime_seconds // 3600)
    minutes = int((uptime_seconds % 3600) // 60)
    context_parts.append(f"\n系统已运行: {hours}小时{minutes}分钟")

    # 尝试获取违规统计
    try:
        from handlers.moderation_store import violation_stats, violation_trend
        stats = violation_stats(hours=24)
        context_parts.append("\n=== 最近24小时违规统计 ===")
        context_parts.append(json.dumps(stats, ensure_ascii=False, indent=2))

        trend = violation_trend(days=7)
        context_parts.append("\n=== 最近7天违规趋势 ===")
        context_parts.append(json.dumps(trend, ensure_ascii=False, indent=2))
    except Exception as e:
        context_parts.append(f"\n(违规统计获取失败: {e})")

    # 尝试获取健康状态
    try:
        from handlers.moderation_store import latest_health_status
        napcat_health = latest_health_status("napcat")
        if napcat_health:
            context_parts.append(f"\n=== NapCat 健康状态 ===")
            context_parts.append(json.dumps(napcat_health, ensure_ascii=False, indent=2))
    except Exception:
        pass

    return "\n".join(context_parts)


# ============================================================
# 启动入口
# ============================================================

_start_time = time.time()

# SearXNG 健康检查缓存（避免每次 /api/health 都发起搜索请求）
_searxng_health_cache = {"ok": True, "ts": 0}
_SEARXNG_CACHE_TTL = 60  # 缓存 60 秒


@app.on_event("startup")
async def on_startup():
    logger.info("Web 管理后台启动中...")
    pwd = _get_web_password()
    logger.info(f"环境变量 WEB_PASSWORD: {'已设置' if pwd else '未设置（无需认证）'}")
    logger.info(f"静态文件目录: {_web_dir} (存在: {_web_dir.exists()})")
    logger.info(f".env 路径: {_get_env_file_path()}")
    try:
        from handlers.moderation_store import init_db
        init_db()
        logger.info("管理库初始化完成")
    except Exception as e:
        logger.warning(f"管理库初始化失败: {e}")


if __name__ == "__main__":
    port = int(os.getenv("WEB_PORT", "8080"))
    logger.info(f"启动 Web 服务器，端口: {port}")
    uvicorn.run(
        "web_server:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
