# NapCat WebUI 认证流程完整分析

> 基于源码: https://github.com/NapNeko/NapCatQQ (main 分支, 2026-07-25)

---

## 一、NapCat WebUI Token 认证机制完整流程

### 1.1 核心概念

NapCat WebUI 使用一种基于 **HMAC-SHA256 签名的凭证(Credential)** 机制，而非传统的 JWT。整个认证流程涉及以下核心对象：

- **Token（密码）**: 配置在 `webui.json` 中的字符串，即 WebUI 的"密码"
- **Hash**: `SHA256(token + '.napcat')` 生成的十六进制摘要
- **Credential**: 一个 JSON 对象，包含时间戳、Hash 和 HMAC 签名
- **Credential（Base64）**: 将 Credential JSON 做 Base64 编码后的字符串，用于 HTTP 请求

### 1.2 /auth/login 接口详解

**源码位置**: `packages/napcat-webui-backend/src/api/Auth.ts` -> `LoginHandler`

**请求**:
```
POST /auth/login
Content-Type: application/json

{
  "hash": "<SHA256(token + '.napcat') 的十六进制字符串>",
  "totpCode": "<可选，2FA 验证码>"
}
```

**处理流程**:
1. 从请求体获取 `hash` 和可选的 `totpCode`
2. 检查 IP 登录频率限制（`loginRate` 配置项）
3. 使用启动时缓存的 `initialToken`（即 webui.json 中的 token）调用 `AuthHelper.comparePasswordHash(initialToken, hash)` 进行验证
4. 如果启用了 2FA 且未提供 `totpCode`，返回 `{ require2FA: true }`
5. 验证成功后，调用 `AuthHelper.signCredential(hash)` 生成凭证
6. 将凭证 JSON 做 Base64 编码作为 `Credential` 返回

**响应（成功）**:
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "Credential": "<Base64 编码的 JSON 凭证>"
  }
}
```

**响应（需要 2FA）**:
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "require2FA": true,
    "message": "Please enter your authenticator code"
  }
}
```

**响应（失败）**:
```json
{
  "code": 1,
  "message": "token is invalid"
}
```

### 1.3 PasswordHash 生成算法

**源码位置**: `packages/napcat-webui-backend/src/helper/SignToken.ts`

```typescript
public static generatePasswordHash(password: string): string {
    return crypto.createHash('sha256').update(password + '.napcat').digest().toString('hex');
}
```

即: `hash = SHA256(password + '.napcat')`，其中 `.napcat` 是固定后缀 salt。

### 1.4 Credential 签名算法

```typescript
signCredential(hash: string): WebUiCredentialJson {
    const innerJson = {
        CreatedTime: Math.floor(Date.now() / 1000),  // 秒级时间戳
        HashEncoded: hash,                            // 密码的 SHA256 hash
    };
    const jsonString = JSON.stringify(innerJson);
    const hmac = crypto.createHmac('sha256', secretKey).update(jsonString, 'utf8').digest('hex');
    return { Data: innerJson, Hmac: hmac };
}
```

**Credential JSON 结构**:
```typescript
interface WebUiCredentialJson {
    Data: {
        CreatedTime: number;   // 创建时间（秒级 Unix 时间戳）
        HashEncoded: string;   // SHA256(token + '.napcat')
    };
    Hmac: string;             // HMAC-SHA256(Data JSON字符串, secretKey)
}
```

### 1.5 Credential 验证机制（auth 中间件）

**源码位置**: `packages/napcat-webui-backend/src/middleware/auth.ts`

所有路由（除 `/auth/login` 和 passkey 相关路由外）都经过 `auth` 中间件：

1. 从 `Authorization: Bearer <credential_base64>` 头中获取凭证
2. 也支持 `?webui_token=<credential_base64>` 查询参数方式
3. Base64 解码得到 `WebUiCredentialJson`
4. 调用 `AuthHelper.validateCredentialWithinOneHour(initialToken, Credential)` 验证
5. 验证内容：
   - 检查 HMAC 签名是否正确（未被篡改）
   - 检查凭证是否在黑名单中（已注销）
   - 检查时间差是否在 3600 秒（1小时）内
   - 检查 HashEncoded 是否与当前 token 的 hash 匹配

### 1.6 SecretKey

```typescript
private static readonly secretKey = process.env['NAPCAT_WEBUI_JWT_SECRET_KEY'] 
    || Math.random().toString(36).slice(2);
```

- 如果设置了环境变量 `NAPCAT_WEBUI_JWT_SECRET_KEY`，则使用该值
- 否则，**每次启动时随机生成**一个 secretKey
- **重要**: 这意味着如果你不知道服务端的 secretKey，你无法在客户端自行构造有效的 Credential，必须通过 `/auth/login` 接口获取

---

## 二、如何在 Python 中使用已知 Token 获取 Credential

### 2.1 关键结论：必须调用 /auth/login 接口

由于 `secretKey` 是服务端随机生成的（除非显式设置了 `NAPCAT_WEBUI_JWT_SECRET_KEY`），**你无法在纯客户端构造有效的 Credential**。必须向服务端的 `/auth/login` 接口发送请求来获取。

### 2.2 Python 代码示例

```python
import hashlib
import json
import base64
import requests

NAPCAT_WEBUI_URL = "http://127.0.0.1:6099"  # 替换为实际地址
TOKEN = "tgjqr2024"  # WebUI 密码

def get_credential(webui_url: str, token: str) -> str:
    """
    通过 /auth/login 接口获取有效的 Credential
    
    Args:
        webui_url: NapCat WebUI 地址，如 http://127.0.0.1:6099
        token: WebUI 密码（即 webui.json 中的 token）
    
    Returns:
        Base64 编码的 Credential 字符串
    """
    # Step 1: 计算 hash = SHA256(token + '.napcat')
    hash_value = hashlib.sha256((token + '.napcat').encode('utf-8')).hexdigest()
    
    # Step 2: 调用 /auth/login
    resp = requests.post(
        f"{webui_url}/auth/login",
        json={"hash": hash_value},
        headers={"Content-Type": "application/json"}
    )
    result = resp.json()
    
    if result.get("code") != 0:
        raise Exception(f"Login failed: {result.get('message')}")
    
    data = result.get("data", {})
    
    # 检查是否需要 2FA
    if data.get("require2FA"):
        raise Exception("2FA is required. Please provide totpCode.")
    
    credential_base64 = data.get("Credential")
    if not credential_base64:
        raise Exception("No Credential in response")
    
    return credential_base64


def make_authenticated_request(webui_url: str, credential: str, method: str, path: str, 
                                 json_data: dict = None) -> dict:
    """
    使用 Credential 发起认证请求
    
    Args:
        webui_url: NapCat WebUI 基础地址
        credential: Base64 编码的 Credential
        method: HTTP 方法 (GET/POST)
        path: API 路径，如 /QQLogin/CheckLoginStatus
        json_data: POST 请求体
    
    Returns:
        响应 JSON
    """
    url = f"{webui_url}{path}"
    headers = {
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json"
    }
    
    if method.upper() == "GET":
        resp = requests.get(url, headers=headers)
    else:
        resp = requests.post(url, headers=headers, json=json_data)
    
    return resp.json()


# === 使用示例 ===
if __name__ == "__main__":
    # 登录获取 Credential
    credential = get_credential(NAPCAT_WEBUI_URL, TOKEN)
    print(f"获取到 Credential: {credential[:50]}...")
    
    # 使用 Credential 调用其他 API
    # 检查 QQ 登录状态
    status = make_authenticated_request(
        NAPCAT_WEBUI_URL, credential, "POST", "/QQLogin/CheckLoginStatus"
    )
    print(f"QQ 登录状态: {json.dumps(status, ensure_ascii=False, indent=2)}")
    
    # 获取 WebUI 配置
    config = make_authenticated_request(
        NAPCAT_WEBUI_URL, credential, "GET", "/WebUIConfig/GetConfig"
    )
    print(f"WebUI 配置: {json.dumps(config, ensure_ascii=False, indent=2)}")
```

### 2.3 Credential 格式详解

获取到的 `Credential` 是一个 Base64 编码的 JSON，解码后结构如下：

```json
{
    "Data": {
        "CreatedTime": 1721894400,
        "HashEncoded": "a1b2c3d4e5f6...（64位十六进制字符串）"
    },
    "Hmac": "f1e2d3c4b5a6...（64位十六进制字符串）"
}
```

- `Data.CreatedTime`: 凭证创建时间（秒级 Unix 时间戳）
- `Data.HashEncoded`: `SHA256("tgjqr2024" + ".napcat")` 的结果
- `Hmac`: `HMAC-SHA256(JSON.stringify(Data), secretKey)` 的结果

**有效期**: 1 小时（3600 秒），过期后需要重新调用 `/auth/login`

---

## 三、NapCat QQ 密码登录 API

### 3.1 PasswordLogin 接口（通过 WebUI API）

**源码位置**: `packages/napcat-webui-backend/src/api/QQLogin.ts` -> `QQPasswordLoginHandler`

**重要**: 这是一个 **QQ 账号密码登录**接口，不是 WebUI 认证接口。它用于让 QQ 号通过密码方式登录到腾讯服务器。

```
POST /QQLogin/PasswordLogin
Authorization: Bearer <credential>
Content-Type: application/json

{
    "uin": "123456789",           // QQ 号
    "passwordMd5": "abcdef..."      // QQ 密码的 MD5 值（32位十六进制）
}
```

**响应（成功）**:
```json
{ "code": 0, "message": "success", "data": null }
```

**响应（需要验证码）**:
```json
{
    "code": 0,
    "message": "success",
    "data": {
        "needCaptcha": true,
        "proofWaterUrl": "https://..."  // 验证码图片 URL
    }
}
```

**响应（需要新设备验证）**:
```json
{
    "code": 0,
    "message": "success",
    "data": {
        "needNewDevice": true,
        "jumpUrl": "https://accounts.qq.com/safe/verify?...",
        "newDevicePullQrCodeSig": "..."
    }
}
```

### 3.2 验证码登录（CaptchaLogin）

```
POST /QQLogin/CaptchaLogin
Authorization: Bearer <credential>

{
    "uin": "123456789",
    "passwordMd5": "abcdef...",
    "ticket": "验证码 ticket",
    "randstr": "验证码 randstr",
    "sid": "验证码 sid（可选）"
}
```

### 3.3 完整密码登录流程（三步可能）

1. **Step 1 - PasswordLogin**: 发送 QQ 号 + 密码 MD5
   - 成功 -> 登录完成
   - 返回 `needCaptcha` -> 进入 Step 2a
   - 返回 `needNewDevice` -> 进入 Step 2b

2. **Step 2a - CaptchaLogin**: 处理验证码
   - 用户从 `proofWaterUrl` 获取验证码图片，完成验证后获取 ticket/randstr
   - 成功 -> 登录完成
   - 返回 `needNewDevice` -> 进入 Step 2b

3. **Step 2b - NewDeviceLogin**: 处理新设备验证
   - 需要通过 OIDB 接口获取新设备验证二维码
   - 使用 `/QQLogin/GetNewDeviceQRCode` 和 `/QQLogin/PollNewDeviceQR` 完成扫码验证

### 3.4 其他 QQ 登录相关接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/QQLogin/GetQQLoginQrcode` | POST | 获取 QQ 登录二维码 |
| `/QQLogin/CheckLoginStatus` | POST | 检查 QQ 登录状态 |
| `/QQLogin/GetQuickLoginList` | ALL | 获取快速登录列表（历史登录的 QQ 号）
| `/QQLogin/GetQuickLoginListNew` | ALL | 获取快速登录列表（新格式，含昵称/头像） |
| `/QQLogin/SetQuickLogin` | POST | 快速登录（使用历史登录票据） |
| `/QQLogin/GetQQLoginInfo` | POST | 获取已登录 QQ 的信息 |
| `/QQLogin/GetQuickLoginQQ` | POST | 获取自动登录 QQ 账号配置 |
| `/QQLogin/SetQuickLoginQQ` | POST | 设置自动登录 QQ 账号 |
| `/QQLogin/RefreshQRcode` | POST | 刷新二维码 |
| `/QQLogin/PasswordLogin` | POST | 密码登录 |
| `/QQLogin/CaptchaLogin` | POST | 验证码登录 |
| `/QQLogin/NewDeviceLogin` | POST | 新设备验证登录 |
| `/QQLogin/GetNewDeviceQRCode` | POST | 获取新设备验证二维码 |
| `/QQLogin/PollNewDeviceQR` | POST | 轮询新设备验证二维码状态 |
| `/QQLogin/ResetDeviceID` | POST | 重置设备信息 |
| `/QQLogin/RestartNapCat` | POST | 重启 NapCat |

---

## 四、快速登录 / 自动登录配置机制

### 4.1 autoLoginAccount 配置

NapCat 的 "快速登录" 功能有两种含义：

1. **QuickLogin（票据登录）**: 使用 NTQQ 本地保存的登录票据，无需密码即可登录。通过 `/QQLogin/SetQuickLogin` 触发，或通过 WebUI 界面选择历史登录账号。

2. **AutoLoginAccount（自动登录账号）**: 在 WebUI 配置中设置一个 QQ 号作为自动登录目标。通过 `/QQLogin/SetQuickLoginQQ` 设置，通过 `/QQLogin/GetQuickLoginQQ` 读取。

### 4.2 密码自动登录（环境变量方式）

**源码位置**: `packages/napcat-shell/base.ts` -> `handleLoginInner`

NapCat 支持通过环境变量实现 QQ 密码自动登录，这是 Shell 模式下启动时的逻辑：

**登录优先级**:
1. 命令行 `-q <QQ号>` 指定快速登录 -> 使用票据快速登录
2. 快速登录失败 -> 尝试密码回退登录（使用环境变量）
3. 检查 WebUI 配置的自动登录账号 -> `runWebUiConfigQuickFunction()`
4. 以上均失败 -> 显示二维码

**相关环境变量**:

| 环境变量 | 说明 |
|----------|------|
| `NAPCAT_QUICK_ACCOUNT` | 指定快速登录的 QQ 号 | 
| `NAPCAT_QUICK_PASSWORD` | QQ 密码明文（会在内存中计算 MD5） |
| `NAPCAT_QUICK_PASSWORD_MD5` | QQ 密码的 MD5 值（32位十六进制，优先于明文密码） |

**密码回退逻辑**:

当快速登录失败时，NapCat 会检查是否配置了 `NAPCAT_QUICK_PASSWORD` 或 `NAPCAT_QUICK_PASSWORD_MD5`：

- 优先使用 `NAPCAT_QUICK_PASSWORD_MD5`（必须是 32 位十六进制）
- 其次使用 `NAPCAT_QUICK_PASSWORD`（会在内存中 MD5 后使用）
- 如果都未配置，跳过密码回退

```python
# 密码回退登录的 Python 等效逻辑
import hashlib

password_md5 = os.environ.get('NAPCAT_QUICK_PASSWORD_MD5', '').strip()
if password_md5 and not re.match(r'^[a-fA-F0-9]{32}$', password_md5):
    password_md5 = None  # 格式无效

if not password_md5:
    password = os.environ.get('NAPCAT_QUICK_PASSWORD', '')
    if password:
        password_md5 = hashlib.md5(password.encode('utf-8')).hexdigest()
```

### 4.3 WebUI 配置的自动登录（SetQuickLoginQQ）

通过 WebUI API 设置自动登录账号：

```python
# 设置自动登录 QQ 号
make_authenticated_request(
    webui_url, credential, "POST", "/QQLogin/SetQuickLoginQQ",
    json_data={"uin": "123456789"}
)

# 获取当前自动登录 QQ 号
result = make_authenticated_request(
    webui_url, credential, "POST", "/QQLogin/GetQuickLoginQQ"
)
```

**注意**: `SetQuickLoginQQ` 只保存了 QQ 号（uin），**不保存密码**。它的实际效果是在下次启动时，NapCat 会尝试使用该 QQ 号的历史登录票据进行快速登录。如果票据失效，则需要重新扫码或通过环境变量配置密码回退。

### 4.4 是否支持纯密码自动登录？

**不支持通过 WebUI API 设置密码实现自动登录**。密码自动登录只能通过以下方式实现：

1. **环境变量**（推荐用于 Docker/自动化部署）:
   ```bash
   NAPCAT_QUICK_ACCOUNT=123456789
   NAPCAT_QUICK_PASSWORD=your_password
   # 或
   NAPCAT_QUICK_PASSWORD_MD5=md5_of_your_password
   ```

2. **命令行 + 环境变量**:
   ```bash
   napcat.sh -q 123456789  # 配合 NAPCAT_QUICK_PASSWORD 环境变量
   ```

3. **WebUI API 密码登录**（需要手动触发，不能自动）：
   - 调用 `/QQLogin/PasswordLogin` 传入 QQ 号和密码 MD5
   - 但需要先通过 WebUI 认证获取 Credential
   - 可能触发验证码或新设备验证，需要人工介入

---

## 五、使用 token="tgjqr2024" 获取 Credential 的完整 Python 流程

```python
import hashlib
import json
import requests

WEBUI_BASE = "http://127.0.0.1:6099"
TOKEN = "tgjqr2024"

# Step 1: 计算 hash
password_hash = hashlib.sha256((TOKEN + '.napcat').encode()).hexdigest()
# 结果例如: "5e8e9c8b2a1f..." (64 位十六进制)

# Step 2: 发送登录请求
resp = requests.post(
    f"{WEBUI_BASE}/auth/login",
    json={"hash": password_hash},
    headers={"Content-Type": "application/json"}
)
result = resp.json()

# Step 3: 提取 Credential
if result["code"] == 0 and "Credential" in result.get("data", {}):
    credential = result["data"]["Credential"]
    # credential 是一个 Base64 字符串
    # 解码后格式: {"Data":{"CreatedTime":...,"HashEncoded":"..."},"Hmac":"..."}
    
    # Step 4: 使用 Credential 调用其他 API
    headers = {"Authorization": f"Bearer {credential}"}
    
    # 检查 QQ 登录状态
    status = requests.post(f"{WEBUI_BASE}/QQLogin/CheckLoginStatus", headers=headers).json()
    print(json.dumps(status, indent=2, ensure_ascii=False))
    
    # 密码登录 QQ
    login_resp = requests.post(
        f"{WEBUI_BASE}/QQLogin/PasswordLogin",
        headers=headers,
        json={"uin": "123456789", "passwordMd5": hashlib.md5(b"qq_password").hexdigest()}
    ).json()
    print(json.dumps(login_resp, indent=2, ensure_ascii=False))
```

---

## 六、API 路由总览

所有 API 路由前缀为 WebUI 监听地址（默认 `http://0.0.0.0:6099`），路由挂载如下：

| 路径前缀 | 需要认证 | 说明 |
|----------|----------|------|
| `/auth/login` | 否 | WebUI 登录 |
| `/auth/check` | 否* | 检查登录状态 |
| `/auth/logout` | 是 | 注销 |
| `/auth/update_token` | 是 | 修改密码 |
| `/auth/passkey/*` | 否 | Passkey 认证 |
| `/auth/2fa/*` | 混合 | 2FA 管理 |
| `/QQLogin/*` | 是 | QQ 登录管理 |
| `/OB11Config/*` | 是 | OneBot 配置 |
| `/WebUIConfig/*` | 是 | WebUI 配置 |
| `/base/*` | 是 | 基础信息 |
| `/Log/*` | 是 | 日志 |
| `/File/*` | 是 | 文件管理 |
| `/Process/*` | 是 | 进程管理 |
| `/Plugin/*` | 是 | 插件管理 |
| `/Debug/*` | 是 | 调试 |
| `/NapCatConfig/*` | 是 | NapCat 配置 |
| `/UpdateNapCat/*` | 是 | 更新 |
| `/Mirror/*` | 是 | 镜像管理 |

*注: `/auth/check` 需要在请求头中携带 Credential，但不经过 auth 中间件的跳过逻辑（因为它的 URL 不在白名单中），所以实际上仍需要 Credential。只有 `/auth/login` 和 passkey 相关路由完全跳过认证。*

---

## 七、关键源码文件索引

| 文件路径 | 作用 |
|----------|------|
| `packages/napcat-webui-backend/src/router/auth.ts` | 认证路由定义 |
| `packages/napcat-webui-backend/src/api/Auth.ts` | 登录/注销/2FA 处理逻辑 |
| `packages/napcat-webui-backend/src/helper/SignToken.ts` | 凭证签名/验证核心算法 |
| `packages/napcat-webui-backend/src/middleware/auth.ts` | 鉴权中间件 |
| `packages/napcat-webui-backend/src/router/QQLogin.ts` | QQ 登录路由定义 |
| `packages/napcat-webui-backend/src/api/QQLogin.ts` | QQ 登录 API 实现 |
| `packages/napcat-webui-backend/src/helper/Data.ts` | WebUI 运行时数据管理 |
| `packages/napcat-webui-backend/src/types/index.ts` | 类型定义 |
| `packages/napcat-webui-backend/src/router/index.ts` | 路由总入口 |
| `packages/napcat-webui-backend/src/utils/response.ts` | 响应格式化工具 |
| `packages/napcat-shell/base.ts` | Shell 模式入口，含 handleLogin 逻辑 |
</arg_value>