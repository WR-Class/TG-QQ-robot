"""
NapCat / 服务健康监控
- 周期性探测 get_status / get_login_info
- 主动监控 NapCat 日志中 FetchRkey 失败（session 提前死亡前兆）
- 检测到前兆时自动重启 napcat 容器，利用持久化的 session 快速恢复
- 状态变化时写 health_events，可私聊通知群主
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("health_monitor")

_last_online: Optional[bool] = None
_last_notify_ts: float = 0
_last_restart_ts: float = 0
_RESTART_COOLDOWN = 600  # 同一轮重启至少间隔 10 分钟
_FETCH_RKEY_CACHE: dict = {}  # {timestamp}
_FETCH_RKEY_WINDOW = 300  # 5 分钟内出现 3+ 次 FetchRkey 失败则触发重启
_NOTIFY_COOLDOWN = 300  # 同类告警 5 分钟内不重复
_OFFLINE_RESTART_INTERVAL = 600  # 持续离线每 10 分钟尝试重启一次
_START_TIME: float = time.time()


_last_qrcode_notify_ts: float = 0
_QRCODE_NOTIFY_COOLDOWN = 120  # 二维码推送冷却 2 分钟


async def _read_qrcode_from_napcat() -> Optional[bytes]:
    """通过 Docker API 从 NapCat 容器读取二维码图片（PNG 字节）。"""
    try:
        import aiohttp
        import io, tarfile

        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path="/var/run/docker.sock"),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            async with session.get(
                "http://localhost/containers/napcat/archive?path=/app/napcat/cache/qrcode.png"
            ) as resp:
                if resp.status != 200:
                    return None
                tar_data = await resp.read()
                with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r") as tar:
                    for member in tar.getmembers():
                        if member.name.endswith("qrcode.png"):
                            return tar.extractfile(member).read()
    except Exception as e:
        logger.debug(f"[健康监控] 读取二维码失败: {e}")
    return None


async def _send_tg_photo(
    photo_data: bytes, caption: str = "", filename: str = "qrcode.png"
) -> bool:
    """通过 TG Bot 发送图片给所有管理员。"""
    try:
        from config import settings

        token = str(getattr(settings, "BOT_TOKEN", "") or "")
        admin_ids = getattr(settings, "ADMIN_IDS", None) or []
        if not token or not admin_ids:
            return False

        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            for admin_id in admin_ids:
                files = {"photo": (filename, photo_data, "image/png")}
                url = f"https://api.telegram.org/bot{token}/sendPhoto"
                payload: Dict[str, Any] = {"chat_id": int(admin_id)}
                if caption:
                    payload["caption"] = caption
                resp = await client.post(url, data=payload, files=files)
                if resp.status_code != 200:
                    logger.warning(
                        f"[健康监控] TG 图片发送失败(admin={admin_id}): "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
        return True
    except Exception as e:
        logger.warning(f"[健康监控] TG 图片发送异常: {e}")
        return False


def _detect_qrcode_waiting(logs: str, within_sec: int = 300) -> bool:
    """检测日志中 NapCat 是否在等待扫码（掉线后进入二维码登录状态）。"""
    import re

    now = time.time()
    for line in logs.splitlines():
        if "二维码" not in line and "请扫描" not in line:
            continue
        m = re.match(r"(\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            from datetime import datetime
            import calendar

            ts = datetime.strptime(m.group(1), "%m-%d %H:%M:%S")
            from datetime import datetime as dt2

            now_dt = dt2.fromtimestamp(now)
            ts = ts.replace(year=now_dt.year)
            if time.mktime(ts.timetuple()) > now - within_sec:
                return True
        except Exception:
            continue
    return False


def _extract_qrcode_url(logs: str) -> Optional[str]:
    """从日志中提取最新的二维码解码 URL。"""
    import re

    for line in reversed(logs.splitlines()):
        # 日志格式: "二维码解码URL: https://txz.qq.com/p?k=...&f=..."
        # 提取冒号后的完整 URL（可能含 & 参数）
        m = re.search(r"二维码解码URL:\s*(https?://\S+)", line)
        if m:
            return m.group(1)
    return None


async def _read_napcat_logs(tail: int = 150) -> str:
    """通过 docker socket 读取 napcat 最近日志。"""
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path="/var/run/docker.sock"),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            async with session.get(
                "http://localhost/containers/napcat/logs?stdout=1&stderr=1&tail=%d" % tail,
            ) as resp:
                raw = await resp.read()
                # docker logs stream frame: 1字节流类型 + 3字节pad + 4字节size = 8字节头
                lines = []
                i = 0
                while i < len(raw):
                    if len(raw) - i < 8:
                        break
                    size = int.from_bytes(raw[i + 4 : i + 8], "big")
                    i += 8
                    chunk = raw[i : i + size]
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                    except Exception:
                        text = chunk.decode("gbk", errors="replace")
                    lines.append(text.rstrip("\n"))
                    i += size
                return "\n".join(lines[-tail:])
    except Exception as e:
        logger.debug(f"[健康监控] 读 NapCat 日志失败: {e}")
        return ""


async def _restart_napcat_container() -> bool:
    """通过 docker socket 重启 napcat 容器。"""
    global _last_restart_ts
    now = time.time()
    if now - _last_restart_ts < _RESTART_COOLDOWN:
        logger.info(f"[健康监控] 距上次重启 {now - _last_restart_ts:.0f}s，跳过")
        return False
    try:
        import aiohttp

        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path="/var/run/docker.sock"),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            # 先检查容器是否存在
            async with session.get("http://localhost/containers/napcat/json") as resp:
                if resp.status != 200:
                    logger.warning(f"[健康监控] napcat 容器不存在, status={resp.status}")
                    return False
            # 发送重启
            async with session.post(
                "http://localhost/containers/napcat/restart",
                params={"t": 5},
            ) as resp:
                if resp.status in (200, 204):
                    _last_restart_ts = now
                    logger.info("[健康监控] NapCat 容器已自动重启（会话续命）")
                    return True
                else:
                    logger.warning(f"[健康监控] 重启失败: status={resp.status}")
                    return False
    except ImportError:
        logger.warning("[健康监控] aiohttp 无 Unix 连接器，跳过自动重启")
        return False
    except Exception as e:
        logger.warning(f"[健康监控] 重启异常: {e}")
        return False


async def _auto_backup_session() -> None:
    """在 NapCat 恢复在线后自动备份 session 文件。"""
    try:
        import aiohttp
        import io, tarfile

        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path="/var/run/docker.sock"),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            # 从容器中拷贝 QQ 配置目录（session 文件）
            async with session.get(
                "http://localhost/containers/napcat/archive?path=/app/.config/QQ"
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"[健康监控] 自动备份 session: 读取容器目录失败 status={resp.status}")
                    return
                tar_data = await resp.read()

        # 解压到 backups 目录
        import shutil
        from datetime import datetime
        from pathlib import Path

        project_dir = Path(__file__).resolve().parent.parent
        # Docker 内路径为 /app，宿主机映射为项目根目录
        # napcat_qq 映射到 ./napcat_qq
        # 使用独立的备份目录，不污染当前使用的 napcat_qq
        backup_root = project_dir / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        dest = backup_root / f"napcat_{stamp}"
        if dest.exists():
            logger.debug(f"[健康监控] 自动备份 session: {dest} 已存在，跳过")
            return

        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r") as tar:
            # 只提取 session 相关文件（排除缓存和崩溃报告）
            qq_session_members = [
                m for m in tar.getmembers()
                if "nt_qq" in m.name or "NapCat" in m.name
            ]
            if not qq_session_members:
                logger.debug("[健康监控] 自动备份 session: 未找到 session 文件")
                return
            dest.mkdir(parents=True, exist_ok=True)
            for member in qq_session_members:
                # 安全检查：不跳转到上级目录
                member_path = Path(member.name)
                if ".." in member_path.parts:
                    continue
                tar.extract(member, dest)

        # 清理超过 7 个的旧备份
        existing = sorted(
            [d for d in backup_root.iterdir() if d.is_dir() and d.name.startswith("napcat_")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in existing[7:]:
            try:
                shutil.rmtree(old)
            except Exception as e:
                logger.debug(f"[健康监控] 清理旧备份失败: {e}")

        logger.info(f"[健康监控] Session 自动备份完成: {dest.name}")

    except Exception as e:
        logger.debug(f"[健康监控] 自动备份 session 异常: {e}")


def _count_fetch_rkey_errors(logs: str, within_sec: int = 300) -> int:
    """统计日志中最近 within_sec 秒内的 FetchRkey 失败次数。"""
    import re

    now = time.time()
    count = 0
    for line in logs.splitlines():
        if "FetchRkey 失败" not in line:
            continue
        # 提取时间: 07-23 15:00:55
        m = re.match(r"(\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            count += 1
            continue
        try:
            from datetime import datetime
            ts = datetime.strptime(m.group(1), "%m-%d %H:%M:%S")
            # 假设年份与当前相同
            import calendar
            from datetime import datetime as dt2
            now_dt = dt2.fromtimestamp(now)
            ts = ts.replace(year=now_dt.year)
            if time.mktime(ts.timetuple()) > now - within_sec:
                count += 1
        except Exception:
            count += 1
    return count


def _detect_offline_in_logs(logs: str, within_sec: int = 600) -> bool:
    """检测日志中是否有近期掉线记录。匹配多种掉线日志模式。within_sec 默认 10 分钟。"""
    import re

    now = time.time()
    patterns = [
        "账号状态变更为离线",
        "连接断开",
        "网络连接已断开",
        "网络连接异常",
        "session 已过期",
        "被踢下线",
        "登录失效",
        "1006514",  # NTQQ 网络连接异常错误码
    ]
    # 注意："离线" 和 "sendMsg" 太常见（正常消息日志也包含），
    # 不单独匹配，避免误触发重启。只匹配上面的明确掉线关键字。
    for line in logs.splitlines():
        matched = False
        for p in patterns:
            if p in line:
                matched = True
                break
        if not matched:
            continue
        # 提取时间
        m = re.match(r"(\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not m:
            continue
        try:
            from datetime import datetime
            ts = datetime.strptime(m.group(1), "%m-%d %H:%M:%S")
            import calendar
            from datetime import datetime as dt2
            now_dt = dt2.fromtimestamp(now)
            ts = ts.replace(year=now_dt.year)
            if time.mktime(ts.timetuple()) > now - within_sec:
                logger.info(f"[健康监控] 检测到近期掉线日志: {line.strip()[:80]}")
                return True
        except Exception:
            continue
    return False


async def _napcat_probe() -> Dict[str, Any]:
    """探测 NapCat 当前状态。"""
    from config import settings

    result = {"ok": False, "online": None, "user_id": None, "nickname": "", "message": ""}
    if not getattr(settings, "NAPCAT_ENABLED", False):
        result["message"] = "NapCat 未启用"
        return result
    try:
        import aiohttp

        headers = {"Content-Type": "application/json"}
        token = getattr(settings, "NAPCAT_ACCESS_TOKEN", "") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        base = (getattr(settings, "NAPCAT_API_URL", "") or "").rstrip("/")
        if not base:
            result["message"] = "NAPCAT_API_URL 未配置"
            return result

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/get_login_info",
                headers=headers,
                json={},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
            login = data.get("data") or {}
            if data.get("status") == "ok" or data.get("retcode") == 0:
                result["ok"] = True
                result["user_id"] = login.get("user_id")
                result["nickname"] = login.get("nickname") or ""
            async with session.post(
                f"{base}/get_status",
                headers=headers,
                json={},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp2:
                st = await resp2.json()
            sdata = st.get("data") or {}
            result["online"] = sdata.get("online")
            if result["online"] is True:
                result["message"] = f"在线 {result.get('nickname')}({result.get('user_id')})"
            elif result["online"] is False:
                result["message"] = f"已登录但离线 {result.get('user_id')}"
            else:
                result["message"] = "状态未知"
        return result
    except Exception as e:
        result["message"] = f"探测失败: {e}"
        return result


# ===== Prometheus 指标收集 =====
_metrics_violations: Dict[str, int] = {}  # type -> count
_metrics_actions: Dict[str, int] = {}     # action -> count
_metrics_checks_total: int = 0
_metrics_checks_ok: int = 0


def _inc_violation(vtype: str):
    _metrics_violations[vtype] = _metrics_violations.get(vtype, 0) + 1


def _inc_action(action: str):
    _metrics_actions[action] = _metrics_actions.get(action, 0) + 1


def get_metrics_text() -> str:
    """返回 Prometheus 文本格式指标。"""
    import time
    lines = []
    lines.append("# HELP tgjqr_uptime_seconds Bot uptime in seconds")
    lines.append("# TYPE tgjqr_uptime_seconds gauge")
    lines.append(f"tgjqr_uptime_seconds {time.time() - _START_TIME}")
    lines.append("")
    lines.append("# HELP tgjqr_health_checks_total Total health checks performed")
    lines.append("# TYPE tgjqr_health_checks_total counter")
    lines.append(f"tgjqr_health_checks_total {_metrics_checks_total}")
    lines.append("")
    lines.append("# HELP tgjqr_health_checks_ok Successful health checks")
    lines.append("# TYPE tgjqr_health_checks_ok counter")
    lines.append(f"tgjqr_health_checks_ok {_metrics_checks_ok}")
    lines.append("")
    lines.append("# HELP tgjqr_violations_total Violations by type")
    lines.append("# TYPE tgjqr_violations_total counter")
    for vtype, cnt in sorted(_metrics_violations.items()):
        lines.append(f'tgjqr_violations_total{{type="{vtype}"}} {cnt}')
    lines.append("")
    lines.append("# HELP tgjqr_actions_total Moderation actions by type")
    lines.append("# TYPE tgjqr_actions_total counter")
    for action, cnt in sorted(_metrics_actions.items()):
        lines.append(f'tgjqr_actions_total{{action="{action}"}} {cnt}')
    return "\n".join(lines)


async def _notify_via_tg_async(text: str) -> bool:
    """用 TG Bot 发送通知给管理员（async 版本）。"""
    try:
        from config import settings
        token = str(getattr(settings, "BOT_TOKEN", "") or "")
        admin_ids = getattr(settings, "ADMIN_IDS", None) or []
        if not token or not admin_ids:
            logger.warning("[健康监控] TG Bot 未配置，无法发送通知")
            return False

        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            for admin_id in admin_ids:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {
                    "chat_id": int(admin_id),
                    "text": text,
                    "parse_mode": "HTML",
                }
                try:
                    resp = await client.post(url, json=payload)
                    if resp.status_code != 200:
                        logger.warning(f"[健康监控] TG 通知失败: {resp.status_code} {resp.text[:200]}")
                except Exception as e:
                    logger.warning(f"[健康监控] TG 通知异常: {e}")
        logger.info("[健康监控] 已通过 TG Bot 发送通知")
        return True
    except Exception as e:
        logger.warning(f"[健康监控] 通知发送失败: {e}")
        return False


def _notify_via_tg_sync(text: str) -> bool:
    """用 TG Bot 发送通知给管理员（sync 版本，供非 async 上下文使用）。"""
    try:
        from config import settings
        token = str(getattr(settings, "BOT_TOKEN", "") or "")
        admin_ids = getattr(settings, "ADMIN_IDS", None) or []
        if not token or not admin_ids:
            logger.warning("[健康监控] TG Bot 未配置，无法发送通知")
            return False

        import httpx
        try:
            with httpx.Client(timeout=15) as client:
                for admin_id in admin_ids:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    payload = {
                        "chat_id": int(admin_id),
                        "text": text,
                        "parse_mode": "HTML",
                    }
                    try:
                        resp = client.post(url, json=payload)
                        if resp.status_code != 200:
                            logger.warning(f"[健康监控] TG 通知失败: {resp.status_code} {resp.text[:200]}")
                    except Exception as e:
                        logger.warning(f"[健康监控] TG 通知异常: {e}")
            logger.info("[健康监控] 已通过 TG Bot 发送通知")
            return True
        except Exception as e:
            logger.warning(f"[健康监控] 通知发送失败: {e}")
            return False
    except Exception as e:
        logger.warning(f"[健康监控] 通知发送失败: {e}")
        return False


async def _try_push_qrcode(logs: str) -> bool:
    """检测日志中是否在等待扫码，如果是则先尝试密码登录，失败后再推送二维码。"""
    global _last_qrcode_notify_ts

    # ===== 新增：在推送二维码之前，先尝试密码登录自动恢复 =====
    try:
        from handlers.captcha_solver import (
            password_login_available,
            try_password_login,
        )
        if password_login_available():
            logger.warning("[健康监控] 检测到需要扫码，先尝试密码登录自动恢复...")
            await _notify_via_tg_async(
                "🔄 [健康监控] QQ 掉线，正在尝试密码登录自动恢复..."
            )
            pwd_ok = await try_password_login()
            if pwd_ok:
                add_health_event(
                    "napcat", "password_login_ok",
                    "密码登录自动恢复成功",
                    notified=True,
                )
                await _notify_via_tg_async(
                    "✅ [健康监控] QQ 密码登录自动恢复成功！无需手动操作。"
                )
                # 登录成功后备份 session
                await _auto_backup_session()
                return True
            else:
                logger.warning("[健康监控] 密码登录自动恢复失败，回退到二维码推送")
                await _notify_via_tg_async(
                    "⚠️ [健康监控] 密码登录自动恢复失败，请手动扫码登录。"
                )
    except Exception as e:
        logger.warning(f"[健康监控] 密码登录尝试异常: {e}")

    # ===== 原有逻辑：推送二维码 =====
    now = time.time()
    if now - _last_qrcode_notify_ts < _QRCODE_NOTIFY_COOLDOWN:
        return False
    if not logs or not _detect_qrcode_waiting(logs, within_sec=600):
        return False

    _last_qrcode_notify_ts = now
    qrcode_url = _extract_qrcode_url(logs)
    qrcode_data = await _read_qrcode_from_napcat()

    text = (
        "📱 <b>QQ 需要重新扫码登录</b>\n\n"
        "NapCat 已掉线且自动重启无效，需手动扫码授权。\n"
        "用手机 QQ 扫描下方二维码即可恢复。\n\n"
        "📌 <b>扫码方法</b>：\n"
        "1. 打开手机 QQ → 右上角 + 号 → 扫一扫\n"
        "2. 扫描下方图片二维码\n"
        "3. 在手机上点击「允许登录」\n"
    )
    if qrcode_url:
        # 将原始 URL 转成在线二维码生成器链接，外网也能扫码
        # 使用 qrserver.cn 生成二维码图片（国内可访问）
        qr_gen_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={qrcode_url}"
        text += (
            f"\n🔗 <b>在线二维码链接</b>（图片失败时备用）:\n"
            f"复制下方链接到浏览器打开，用手机QQ扫描显示的二维码:\n"
            f"<code>{qr_gen_url}</code>\n"
            f"\n或使用原始链接（需自行生成二维码）:\n"
            f"<code>{qrcode_url}</code>\n"
        )
    pushed = False
    if qrcode_data:
        pushed = await _send_tg_photo(qrcode_data, caption=text[:1000])
    if not pushed:
        # 图片发送失败则仅发文字
        pushed = await _notify_via_tg_async(text)

    if pushed:
        logger.warning("[健康监控] 已向 TG 推送二维码，等待用户扫码")
    return pushed


async def _try_auto_solve_captcha(logs: str) -> bool:
    """
    检测日志中是否有验证码需要自动解决，用 YesCaptcha 处理。
    
    流程：
    1. 从日志检测"需要验证码"并提取 CaptchaAppId
    2. 调用 YesCaptcha 解决滑动验证码，获取 ticket/randstr
    3. 通过 NapCat WebUI API 提交验证码
    4. 等待 NapCat 自动重试登录（loginRate=10s）
    5. 探测 API 确认是否恢复在线
    """
    if not logs or "需要验证码" not in logs:
        return False

    from handlers.captcha_solver import (
        is_available as captcha_available,
        detect_and_solve_captcha_from_logs,
        submit_captcha_via_webui,
    )

    if not captcha_available():
        logger.info("[健康监控] YesCaptcha 未配置，跳过验证码自动解决")
        return False

    logger.warning("[健康监控] 检测到登录需要验证码，尝试用 YesCaptcha 自动解决...")
    await _notify_via_tg_async("🔐 [健康监控] QQ 登录需要验证码，正在尝试用 YesCaptcha 自动解决...")

    result = await detect_and_solve_captcha_from_logs(logs)
    if not result:
        logger.warning("[健康监控] YesCaptcha 验证码解决失败")
        await _notify_via_tg_async("❌ [健康监控] YesCaptcha 验证码自动解决失败，请手动处理。")
        return False

    ticket = result.get("ticket", "")
    logger.warning(
        f"[健康监控] YesCaptcha 验证码解决成功! "
        f"ticket={ticket[:30]}..."
    )

    # 提交验证码给 NapCat
    api_ok = await submit_captcha_via_webui(
        ticket=ticket,
        randstr=result.get("randstr", ""),
    )
    if api_ok:
        logger.warning("[健康监控] 验证码已通过 API 提交给 NapCat")
    else:
        logger.info(
            "[健康监控] API 提交受限，等待 NapCat 下一轮自动登录尝试..."
        )

    # 等待 NapCat 自动重试登录（loginRate=10s，预留 2 轮 = 25 秒）
    await _notify_via_tg_async(
        "✅ [健康监控] 验证码已提交，等待 NapCat 自动重试登录..."
    )
    logger.info("[健康监控] 等待 NapCat 自动重试登录（25 秒）...")
    await asyncio.sleep(25)

    # 探测是否恢复在线
    probe = await _napcat_probe()
    online = probe.get("online")
    if online is True:
        logger.warning("[健康监控] 验证码提交后 NapCat 已恢复在线!")
        await _notify_via_tg_async(
            f"✅ [健康监控] 验证码登录成功! NapCat 已恢复在线: {probe.get('message', '')}"
        )
        return True

    # 第一次没成功，再等一轮（NapCat 可能还在处理）
    logger.info("[健康监控] 首次探测未在线，再等待 20 秒...")
    await asyncio.sleep(20)
    probe = await _napcat_probe()
    online = probe.get("online")
    if online is True:
        logger.warning("[健康监控] 验证码提交后 NapCat 已恢复在线（第二轮）!")
        await _notify_via_tg_async(
            f"✅ [健康监控] 验证码登录成功! NapCat 已恢复在线: {probe.get('message', '')}"
        )
        return True

    logger.warning("[健康监控] 验证码已提交但 NapCat 仍未上线，将在下一轮健康检查中重试")
    await _notify_via_tg_async(
        "⚠️ [健康监控] 验证码已提交但登录未成功，将在下次检查时继续尝试。"
    )
    return True  # 返回 True 表示验证码处理流程已执行（避免重复触发）


async def check_once() -> Dict[str, Any]:
    """执行一轮探测 + 日志检查 + 主动续命。"""
    global _last_online, _last_restart_ts
    global _metrics_checks_total, _metrics_checks_ok
    from handlers.moderation_store import add_health_event, latest_health_status

    _metrics_checks_total += 1

    # === 第一步：NapCat 主动日志检查（FetchRkey 失败 → 提前重启续命）===
    logs = await _read_napcat_logs(tail=200)
    if logs:
        should_restart = False
        restart_reason = ""

        # 1a. 检测 FetchRkey 失败（session 即将死亡前兆）
        rkey_count = _count_fetch_rkey_errors(logs, within_sec=_FETCH_RKEY_WINDOW)
        if rkey_count >= 3:
            should_restart = True
            restart_reason = f"检测到 {rkey_count} 次 FetchRkey 失败（session 即将死亡）"

        # 1b. 检测掉线日志（窗口 600 秒，覆盖重启后启动延迟）
        if not should_restart and _detect_offline_in_logs(logs, within_sec=600):
            should_restart = True
            restart_reason = "检测到掉线日志"

        if should_restart:
            now = time.time()
            if now - _last_restart_ts >= _RESTART_COOLDOWN:
                logger.warning(f"[健康监控] {restart_reason}，主动重启 NapCat 续命")
                notified = await _notify_via_tg_async(
                    f"⚠️ [健康监控] {restart_reason}，自动重启续命中..."
                )
                restart_ok = await _restart_napcat_container()
                if restart_ok:
                    add_health_event(
                        "napcat", "auto_restart",
                        restart_reason,
                        notified=notified,
                    )
                else:
                    add_health_event(
                        "napcat", "restart_failed",
                        "自动重启失败，需手动处理",
                        notified=False,
                    )
                    await _notify_via_tg_async(
                        f"❌ [健康监控] NapCat 自动重启失败，请手动重启。"
                    )
                # 重启后等待一会儿再探测
                await asyncio.sleep(8)

                # 1c. 重启后检查是否进入验证码登录流程
                # NapCat 重启后可能：快速登录成功 → 密码登录回退 → 需要验证码
                # 此时应在二维码循环超时前主动解决验证码
                post_logs = await _read_napcat_logs(tail=30)
                if post_logs and "需要验证码" in post_logs:
                    captcha_solved = await _try_auto_solve_captcha(post_logs)
                    if captcha_solved:
                        add_health_event(
                            "napcat", "captcha_solved",
                            "重启后检测到验证码，已自动解决",
                            notified=True,
                        )

    # === 第二步：API 探测 ==
    probe = await _napcat_probe()
    online = probe.get("online")
    status = (
        "online"
        if online is True
        else ("offline" if online is False else ("error" if not probe.get("ok") else "unknown"))
    )
    msg = probe.get("message") or ""

    prev = latest_health_status("napcat")
    prev_status = (prev or {}).get("status")
    changed = prev_status != status

    notified = False
    if changed and status in ("offline", "error"):
        # NapCat 已掉线，先尝试自动重启
        restart_ok = await _restart_napcat_container()
        if restart_ok:
            notified = await _notify_via_tg_async(
                f"⚠️ [健康监控] NapCat 掉线，已自动重启\n状态: {status}\n详情: {msg}"
            )
            add_health_event("napcat", "auto_restart", f"API探测掉线触发重启: {msg}", notified=notified)
            logger.warning(f"[健康监控] NapCat {status}，已自动重启: {msg}")
        else:
            # 重启失败 → 检查是否有验证码需要自动解决
            if logs:
                captcha_solved = await _try_auto_solve_captcha(logs)
            else:
                captcha_solved = False
            if captcha_solved:
                notified = True
                add_health_event("napcat", "captcha_solved",
                                 "验证码已通过 YesCaptcha 自动解决", notified=True)
            else:
                # 没有验证码或解决失败 → 尝试推送二维码
                qr_pushed = await _try_push_qrcode(logs)
                extra = ""
                if qr_pushed:
                    extra = "\n已推送二维码至 TG，扫码即可恢复"
                notified = await _notify_via_tg_async(
                    f"⚠️ [健康监控] NapCat 异常（自动重启失败）\n"
                    f"状态: {status}\n详情: {msg}{extra}\n"
                    f"请打开 http://localhost:56099 扫码重新登录管理号。"
                )
            add_health_event("napcat", status, f"自动重启失败: {msg}", notified=notified)
            logger.warning(f"[健康监控] NapCat {status}（重启失败）: {msg}")
    elif changed and status == "online":
        notified = await _notify_via_tg_async(f"✅ [健康监控] NapCat 已恢复在线\n{msg}")
        add_health_event("napcat", status, msg, notified=notified)
        logger.info(f"[健康监控] NapCat 恢复在线: {msg}")
        # 恢复在线后自动备份 session
        await _auto_backup_session()
    else:
        if status == "online":
            last_ts = float((prev or {}).get("created_at") or 0)
            if time.time() - last_ts > 3600:
                add_health_event("napcat", status, msg, notified=False)
        elif status in ("offline", "error"):
            last_ts = float((prev or {}).get("created_at") or 0)
            now = time.time()
            # 持续离线超过 10 分钟且有日志证据 → 再尝试重启
            restart_ok = False  # 初始化，避免 UnboundLocalError
            if now - _last_restart_ts >= _OFFLINE_RESTART_INTERVAL:
                logs = await _read_napcat_logs(tail=100)
                if logs and _detect_offline_in_logs(logs, within_sec=1200):
                    logger.warning(f"[健康监控] NapCat 持续离线，再次尝试自动重启")
                    restart_ok = await _restart_napcat_container()
                    if restart_ok:
                        notified = await _notify_via_tg_async(
                            f"⚠️ [健康监控] NapCat 持续离线，已第2次自动重启"
                        )
                        add_health_event("napcat", "auto_restart", "持续离线触发重启", notified=notified)
                    else:
                        notified = await _notify_via_tg_async(
                            f"⚠️ [健康监控] NapCat 持续离线，自动重启失败，请手动处理"
                        )
                        add_health_event("napcat", "restart_failed", "持续离线重启失败", notified=notified)
                # 持续离线状态下也检查是否需要推送二维码
                if not restart_ok:
                    await _try_push_qrcode(logs)
            else:
                # 冷却期内，仍尝试推送二维码（不重启）
                logs = await _read_napcat_logs(tail=100)
                if logs:
                    await _try_push_qrcode(logs)
            # 超时仍发通知
            if now - last_ts > 1800 and now - _last_notify_ts > _NOTIFY_COOLDOWN:
                notified = await _notify_via_tg_async(
                    f"⚠️ [健康监控] NapCat 仍异常\n状态: {status}\n详情: {msg}"
                )
                add_health_event("napcat", status, msg, notified=notified)

    _last_online = online if isinstance(online, bool) else _last_online
    if online is True:
        _metrics_checks_ok += 1
    probe["status"] = status
    probe["changed"] = changed
    probe["notified"] = notified
    return probe


async def health_loop(interval: int = 60):
    """主循环：每 interval 秒执行一次。"""
    global _START_TIME
    _START_TIME = time.time()
    logger.info(f"[健康监控] 启动，间隔 {interval}s（含日志监控+自动续命）")
    await asyncio.sleep(20)  # 启动后等 NapCat 就绪
    while True:
        try:
            result = await check_once()
            # 采样系统资源到数据库（供前端历史趋势图使用）
            try:
                sys_info = result.get("system") or {}
                if sys_info.get("cpu_percent") is not None:
                    from handlers.moderation_store import sample_sys_resource
                    sample_sys_resource(
                        sys_info["cpu_percent"],
                        sys_info.get("memory_percent", 0),
                        sys_info.get("disk_percent", 0),
                    )
            except Exception as e:
                logger.debug(f"[健康监控] 采样系统资源失败: {e}")
        except Exception as e:
            logger.warning(f"[健康监控] 检测异常: {e}", exc_info=True)
        await asyncio.sleep(max(15, int(interval)))


def start_health_monitor(loop: Optional[asyncio.AbstractEventLoop] = None, interval: int = 60):
    try:
        loop = loop or asyncio.get_event_loop()
        loop.create_task(health_loop(interval=interval))
        logger.info("[健康监控] 任务已调度（日志监控+自动续命）")
    except Exception as e:
        logger.warning(f"[健康监控] 启动失败: {e}")
