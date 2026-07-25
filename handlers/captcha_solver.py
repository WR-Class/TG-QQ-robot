"""
YesCaptcha 验证码自动解决 + 密码登录自动恢复模块

功能:
1. YesCaptcha: 解决 NapCat QQ 登录时的 Tencent CAPTCHA（滑动验证码）
2. 密码登录: 通过 NapCat WebUI API 自动提交密码+验证码完成登录恢复

使用前提:
1. YesCaptcha: 在 https://yescaptcha.com 注册账号并充值，获取 clientKey
2. 密码登录: 在 .env 中设置 QQ_PASSWORD_MD5=密码的MD5值
"""

import asyncio
import hashlib
import logging
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("captcha_solver")

# YesCaptcha API 地址
_CREATE_TASK_URL = "https://api.yescaptcha.com/createTask"
_GET_TASK_RESULT_URL = "https://api.yescaptcha.com/getTaskResult"

# 任务类型
TYPE_TENCENT_CAPTCHA = "TencentCaptcha"


def get_client_key() -> str:
    """从配置获取 YesCaptcha clientKey。"""
    try:
        from config import settings
        return str(getattr(settings, "YESCAPTCHA_KEY", "") or "").strip()
    except Exception:
        return ""


def is_available() -> bool:
    """检查 YesCaptcha 是否配置可用。"""
    return bool(get_client_key())


async def solve_tencent_captcha(
    app_id: str,
    url: str = "https://ti.qq.com/",
) -> Optional[Dict[str, Any]]:
    """
    解决 Tencent CAPTCHA（QQ 登录滑动验证码）。

    Args:
        app_id: 腾讯验证码的 CaptchaAppId
        url: 触发验证码的页面 URL

    Returns:
        {"ticket": str, "randstr": str} 或 None（失败时）
    """
    client_key = get_client_key()
    if not client_key:
        logger.warning("[CaptchaSolver] YESCAPTCHA_KEY 未配置，跳过验证码自动解决")
        return None

    task_data: Dict[str, Any] = {
        "type": TYPE_TENCENT_CAPTCHA,
        "websiteURL": url,
        "appId": app_id,
    }

    task_payload = {
        "clientKey": client_key,
        "task": task_data,
    }

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            logger.info(f"[CaptchaSolver] 创建验证码解决任务: appId={app_id}")
            resp = await client.post(_CREATE_TASK_URL, json=task_payload)
            resp_data = resp.json()

            if resp_data.get("errorId") != 0:
                error_msg = resp_data.get("errorDescription", "未知错误")
                logger.warning(f"[CaptchaSolver] 创建任务失败: {error_msg}")
                return None

            task_id = resp_data.get("taskId")
            if not task_id:
                logger.warning("[CaptchaSolver] 未获取到 taskId")
                return None

            # 轮询等待结果
            for attempt in range(30):
                await asyncio.sleep(2)
                poll_resp = await client.post(
                    _GET_TASK_RESULT_URL,
                    json={"clientKey": client_key, "taskId": task_id},
                )
                poll_data = poll_resp.json()

                if poll_data.get("errorId") != 0:
                    error_msg = poll_data.get("errorDescription", "未知错误")
                    logger.warning(f"[CaptchaSolver] 轮询失败: {error_msg}")
                    return None

                status = poll_data.get("status", "")
                if status == "ready":
                    solution = poll_data.get("solution", {})
                    ticket = solution.get("ticket", "")
                    randstr = solution.get("randstr", "")
                    if ticket:
                        logger.info(
                            f"[CaptchaSolver] 验证码解决成功: "
                            f"ticket={ticket[:20]}... randstr={randstr[:20]}..."
                        )
                        return {"ticket": ticket, "randstr": randstr}
                    else:
                        logger.warning("[CaptchaSolver] solution 中缺少 ticket")
                        return None

                if status == "processing":
                    continue

                logger.warning(f"[CaptchaSolver] 未知状态: {status}")
                return None

            logger.warning("[CaptchaSolver] 验证码解决超时")
            return None

    except Exception as e:
        logger.warning(f"[CaptchaSolver] 验证码解决异常: {e}")
        return None


async def detect_and_solve_captcha_from_logs(logs: str) -> Optional[Dict[str, Any]]:
    """
    从 NapCat 日志中检测是否需要解决验证码，如果是则自动处理。

    日志特征: "需要验证码" + "proofWaterUrl" 中包含 appId

    Returns:
        解决结果或 None
    """
    if not is_available():
        return None

    # 检查日志中是否有"需要验证码"字样
    if "需要验证码" not in logs and "proofWaterUrl" not in logs:
        return None

    # 尝试提取 CaptchaAppId
    import re

    app_id = None

    # 从 proofWaterUrl 中提取 appId
    for line in logs.splitlines():
        m = re.search(r"proofWaterUrl.*?aid[=\/](\d+)", line, re.IGNORECASE)
        if m:
            app_id = m.group(1)
            logger.info(f"[CaptchaSolver] 从日志提取到 CaptchaAppId: {app_id}")
            break

    # 从 sms-verify-login URL 提取 aid
    if not app_id:
        for line in logs.splitlines():
            m = re.search(r"aid[=\/](\d+)", line)
            if m:
                app_id = m.group(1)
                logger.info(f"[CaptchaSolver] 从日志提取到 aid: {app_id}")
                break

    if not app_id:
        logger.info("[CaptchaSolver] 检测到需要验证码但无法提取 appId")
        return None

    return await solve_tencent_captcha(app_id=app_id, url="https://ti.qq.com/safe/tools/captcha/sms-verify-login")


async def submit_captcha_via_webui(
    ticket: str,
    randstr: str,
    napcat_api_base: str = "http://napcat:6099",
) -> bool:
    """
    通过 NapCat WebUI API 提交验证码验证结果。

    使用正确的 Credential 认证流程（SHA256 → /auth/login → Bearer Credential）。
    """
    try:
        payload = {
            "ticket": ticket,
            "randstr": randstr,
        }

        # 使用统一的 WebUI POST 方法（自动处理 Credential 认证）
        result = await _napcat_webui_post(
            "/api/QQLogin/CaptchaLogin",
            payload,
            napcat_api_base=napcat_api_base,
        )
        if result and result.get("code") == 0:
            logger.info("[CaptchaSolver] 验证码已通过 API 提交")
            return True
        else:
            msg = (result or {}).get("message", "请求失败")
            logger.info(f"[CaptchaSolver] API 提交验证码失败: {msg}")
            return False

    except Exception as e:
        logger.debug(f"[CaptchaSolver] 提交验证码 API 异常: {e}")
        return False


# ==================== 密码登录自动恢复 ====================

def get_qq_password_md5() -> str:
    """从配置获取 QQ 密码 MD5（优先 QQ_PASSWORD_MD5，其次 NAPCAT_QUICK_PASSWORD_MD5）。"""
    try:
        from config import settings
        pwd = str(getattr(settings, "QQ_PASSWORD_MD5", "") or "").strip()
        if not pwd:
            pwd = str(getattr(settings, "NAPCAT_QUICK_PASSWORD_MD5", "") or "").strip()
        return pwd
    except Exception:
        return ""


def get_qq_uin() -> str:
    """获取需要密码登录的 QQ 号（优先 NAP_CAT_QQ，其次 NAPCAT_AUTO_LOGIN_ACCOUNT）。"""
    try:
        from config import settings
        uin = str(getattr(settings, "NAP_CAT_QQ", "") or "").strip()
        if not uin:
            uin = str(getattr(settings, "NAPCAT_AUTO_LOGIN_ACCOUNT", "") or "").strip()
        return uin
    except Exception:
        return ""


def password_login_available() -> bool:
    """检查密码登录是否配置可用（需要 QQ 号 + 密码 MD5）。"""
    return bool(get_qq_uin()) and bool(get_qq_password_md5())


async def _napcat_webui_post(
    path: str,
    body: Dict[str, Any],
    napcat_api_base: str = "http://napcat:6099",
) -> Optional[Dict[str, Any]]:
    """
    向 NapCat WebUI 发送 POST 请求。

    认证流程（参考 NapCat 源码 Auth.ts + auth.ts 中间件）:
    1. 计算 hash = SHA256(token + '.napcat')
    2. POST /auth/login {"hash": hash} → 获取 Credential（Base64 编码的 JSON）
    3. 后续请求 Authorization: Bearer <Credential>
    4. Credential 有效期 1 小时，到期需重新获取
    """
    try:
        import httpx

        # 从配置读取 WebUI token
        webui_token = ""
        try:
            from config import settings
            t = str(getattr(settings, "NAPCAT_WEBUI_TOKEN", "") or "").strip()
            if t:
                webui_token = t
        except Exception:
            logger.debug("[PasswordLogin] 从配置读取 NAPCAT_WEBUI_TOKEN 失败")
        if not webui_token:
            logger.warning("[PasswordLogin] NAPCAT_WEBUI_TOKEN 未配置，无法密码登录")
            return None

        # Step 1: 获取 Credential（有效期 1 小时）
        credential = await _get_webui_credential(webui_token, napcat_api_base)
        if not credential:
            logger.warning("[PasswordLogin] 无法获取 WebUI Credential")
            return None

        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{napcat_api_base}{path}",
                json=body,
                headers=headers,
            )
            result = resp.json()
            return result
    except Exception as e:
        logger.warning(f"[PasswordLogin] WebUI API 请求异常: {e}")
        return None


_webui_credential_cache: Dict[str, Any] = {"credential": "", "expires_at": 0}


async def _get_webui_credential(
    token: str,
    napcat_api_base: str = "http://napcat:6099",
) -> Optional[str]:
    """
    通过 NapCat /auth/login 接口获取有效的 Credential。

    流程:
    1. 计算 hash = SHA256(token + '.napcat')
    2. POST /auth/login {"hash": hash}
    3. 从返回的 data.Credential 字段获取凭证
    """
    global _webui_credential_cache

    # 缓存有效期内复用（提前 60 秒过期）
    if _webui_credential_cache["credential"] and time.time() < _webui_credential_cache["expires_at"] - 60:
        return _webui_credential_cache["credential"]

    try:
        import httpx

        # Step 1: 计算哈希
        hash_value = hashlib.sha256((token + ".napcat").encode()).hexdigest()
        logger.info(f"[PasswordLogin] 请求 WebUI Credential (hash={hash_value[:16]}...)")

        # Step 2: 调用 /auth/login
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{napcat_api_base}/api/auth/login",
                json={"hash": hash_value},
            )
            if resp.status_code != 200:
                logger.warning(f"[PasswordLogin] /auth/login 失败: status={resp.status_code}")
                return None

            result = resp.json()
            if result.get("code") != 0:
                logger.warning(f"[PasswordLogin] /auth/login 返回错误: {result.get('message', '')}")
                return None

            credential = result.get("data", {}).get("Credential", "")
            if not credential:
                logger.warning("[PasswordLogin] /auth/login 未返回 Credential")
                return None

            # 缓存 1 小时（NapCat Credential 有效期 1 小时）
            _webui_credential_cache["credential"] = credential
            _webui_credential_cache["expires_at"] = time.time() + 3600
            logger.info("[PasswordLogin] WebUI Credential 获取成功")
            return credential

    except Exception as e:
        logger.warning(f"[PasswordLogin] 获取 Credential 异常: {e}")
        return None


async def try_password_login() -> bool:
    """
    通过 NapCat WebUI API 自动密码登录。

    流程:
    1. 检查当前登录状态（已登录则跳过）
    2. POST /api/QQLogin/PasswordLogin 提交 uin + passwordMd5
    3. 如果返回 needCaptcha → 调用 YesCaptcha 解决 → 提交 CaptchaLogin
    4. 如果返回 needNewDevice → 无法自动处理，返回失败
    5. 等待 NapCat 登录完成，探测是否在线

    Returns:
        True: 登录成功; False: 登录失败（无法处理或需要人工干预）
    """
    if not password_login_available():
        logger.info("[PasswordLogin] 未配置 QQ_PASSWORD_MD5 或 QQ 号，跳过密码登录")
        return False

    uin = get_qq_uin()
    pwd_md5 = get_qq_password_md5()

    # 0. 检查是否已经登录
    status_result = await _napcat_webui_post("/api/QQLogin/CheckLoginStatus", {})
    if status_result and status_result.get("code") == 0:
        data = status_result.get("data", {})
        if data.get("isLogined"):
            logger.info("[PasswordLogin] QQ 已登录，跳过密码登录")
            return True

    logger.warning(f"[PasswordLogin] 尝试密码登录 QQ: {uin}")

    # 1. 提交密码登录
    login_result = await _napcat_webui_post(
        "/api/QQLogin/PasswordLogin",
        {"uin": uin, "passwordMd5": pwd_md5},
    )

    if not login_result:
        logger.warning("[PasswordLogin] 密码登录 API 请求失败")
        return False

    if login_result.get("code") != 0:
        # code != 0 表示失败（如 "QQ Is Logined"）
        msg = login_result.get("message", "")
        if "Logined" in msg or "logined" in msg:
            logger.info("[PasswordLogin] QQ 已登录")
            return True
        logger.warning(f"[PasswordLogin] 密码登录失败: {msg}")
        return False

    # code == 0 表示请求成功，检查 data
    data = login_result.get("data")
    if data is None:
        # 空 data 表示直接登录成功
        logger.warning("[PasswordLogin] 密码登录提交成功，等待登录完成...")
        return await _wait_login_success()

    # 2. 需要验证码
    if data.get("needCaptcha"):
        proof_url = data.get("proofWaterUrl") or ""
        logger.warning(f"[PasswordLogin] 密码登录需要验证码，proofWaterUrl: {proof_url[:80]}...")

        if not is_available():
            logger.warning("[PasswordLogin] YesCaptcha 未配置，无法自动解决验证码")
            return False

        # 从 proofWaterUrl 提取 appId
        app_id = None
        m = re.search(r"aid[=\/](\d+)", proof_url)
        if m:
            app_id = m.group(1)

        if not app_id:
            logger.warning("[PasswordLogin] 无法从 proofWaterUrl 提取 appId")
            return False

        # 用 YesCaptcha 解决验证码
        captcha_result = await solve_tencent_captcha(
            app_id=app_id,
            url="https://ti.qq.com/safe/tools/captcha/sms-verify-login",
        )
        if not captcha_result:
            logger.warning("[PasswordLogin] YesCaptcha 验证码解决失败")
            return False

        ticket = captcha_result.get("ticket", "")
        randstr = captcha_result.get("randstr", "")

        # 提交验证码登录
        captcha_login_result = await _napcat_webui_post(
            "/api/QQLogin/CaptchaLogin",
            {
                "uin": uin,
                "passwordMd5": pwd_md5,
                "ticket": ticket,
                "randstr": randstr,
            },
        )

        if not captcha_login_result:
            logger.warning("[PasswordLogin] CaptchaLogin API 请求失败")
            return False

        if captcha_login_result.get("code") == 0:
            captcha_data = captcha_login_result.get("data")
            if captcha_data is None:
                logger.warning("[PasswordLogin] 验证码登录成功，等待登录完成...")
                return await _wait_login_success()
            # 可能还需要新设备验证
            if captcha_data.get("needNewDevice"):
                logger.warning("[PasswordLogin] 验证码通过但需要新设备验证，无法自动处理")
                return False
        else:
            logger.warning(
                f"[PasswordLogin] CaptchaLogin 失败: {captcha_login_result.get('message', '')}"
            )
            return False

    # 3. 需要新设备验证
    if data.get("needNewDevice"):
        logger.warning("[PasswordLogin] 密码登录需要新设备验证，无法自动处理")
        return False

    # 无额外 data 字段，可能直接成功
    logger.warning("[PasswordLogin] 密码登录返回成功，等待登录完成...")
    return await _wait_login_success()


async def _wait_login_success(wait_seconds: int = 30) -> bool:
    """等待 NapCat 登录成功（轮询 OneBot11 get_login_info + WebUI CheckLoginStatus）。"""
    for i in range(wait_seconds // 5):
        await asyncio.sleep(5)

        # 方式1: 探测 OneBot11（最可靠，不需要 WebUI 认证）
        try:
            import httpx
            from config import settings

            token = str(getattr(settings, "NAPCAT_ACCESS_TOKEN", "") or "NNgQQHG6rqv4nrKq")
            base = (getattr(settings, "NAPCAT_API_URL", "") or "http://napcat:30101").rstrip("/")

            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    f"{base}/get_login_info",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("data", {}).get("nickname"):
                        logger.warning(f"[PasswordLogin] 登录成功！（OneBot11 确认: {data['data']['nickname']})")
                        return True
        except Exception:
            logger.debug("[PasswordLogin] WebUI 登录确认失败，尝试下一种方式")
        # 方式2: 检查 WebUI 登录状态（需要 Credential 认证）
        try:
            status_result = await _napcat_webui_post("/api/QQLogin/CheckLoginStatus", {})
            if status_result and status_result.get("code") == 0:
                data = status_result.get("data", {})
                if data.get("isLogined"):
                    logger.warning("[PasswordLogin] 登录成功！（WebUI 确认）")
                    return True
        except Exception:
            pass

    logger.warning("[PasswordLogin] 等待登录超时")
    return False
