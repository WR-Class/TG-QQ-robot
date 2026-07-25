"""
NapCat OneBot11 桥接模块
通过 OneBot 11 HTTP API 调用 NapCat 实现撤回消息、禁言等管理操作
这些操作是 QQ 官方 Bot API 不支持的
"""

import logging
import aiohttp
import asyncio

logger = logging.getLogger("napcat_bridge")


class NapCatBridge:
    """NapCat OneBot11 桥接客户端"""

    def __init__(self, base_url: str, access_token: str = ""):
        """
        Args:
            base_url: NapCat OneBot11 HTTP API 地址，如 http://napcat:3001
            access_token: NapCat 配置的 access_token（可选）
        """
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self._available = False

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    async def check_available(self) -> bool:
        """检查 NapCat 是否可用"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/get_login_info",
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            user_id = data.get("data", {}).get("user_id", "")
                            nickname = data.get("data", {}).get("nickname", "")
                            logger.info(f"[NapCat] 已连接 QQ 小号: {nickname}({user_id})")
                            self._available = True
                            return True
                    self._available = False
                    return False
        except Exception as e:
            logger.warning(f"[NapCat] 连接失败: {e}")
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available

    async def delete_group_msg(self, group_id: int, message_id: int, retries: int = 2) -> bool:
        """
        撤回群消息（失败自动重试）

        Args:
            group_id: 群号（数字格式）
            message_id: 消息 ID
            retries: 额外重试次数，默认 2（共最多 3 次）
        """
        import asyncio

        last_err = ""
        for attempt in range(max(1, int(retries) + 1)):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/delete_msg",
                        headers=self._headers(),
                        json={
                            "message_id": message_id,
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        data = await resp.json()
                        if data.get("status") == "ok" or data.get("retcode") == 0:
                            if attempt > 0:
                                logger.info(
                                    f"[NapCat] 撤回消息成功(重试{attempt}): "
                                    f"group={group_id} msg={message_id}"
                                )
                            else:
                                logger.info(
                                    f"[NapCat] 撤回消息成功: group={group_id} msg={message_id}"
                                )
                            return True
                        last_err = str(data)
                        logger.warning(
                            f"[NapCat] 撤回消息失败 attempt={attempt + 1}: {data}"
                        )
            except Exception as e:
                last_err = str(e)
                logger.error(f"[NapCat] 撤回消息异常 attempt={attempt + 1}: {e}")
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
        logger.warning(f"[NapCat] 撤回最终失败 group={group_id} msg={message_id}: {last_err}")
        return False

    async def set_group_ban(
        self,
        group_id: int,
        user_id: int,
        duration: int = 600,
    ) -> bool:
        """
        禁言群成员
        
        Args:
            group_id: 群号（数字格式）
            user_id: 用户 QQ 号（数字格式）
            duration: 禁言时长（秒），0=解除禁言，默认600秒（10分钟）
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_ban",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "user_id": user_id,
                        "duration": duration,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(
                            f"[NapCat] 禁言成功: group={group_id} user={user_id} duration={duration}s"
                        )
                        return True
                    else:
                        logger.warning(f"[NapCat] 禁言失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 禁言异常: {e}")
            return False

    async def send_group_msg(self, group_id: int, text: str) -> bool:
        """
        通过 NapCat 发送群消息（作为小号发送）
        
        Args:
            group_id: 群号
            text: 文本内容
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/send_group_msg",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        return True
                    else:
                        logger.warning(f"[NapCat] 发送群消息失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 发送群消息异常: {e}")
            return False

    async def send_private_msg(self, user_id: int, text: str) -> bool:
        """
        发送私聊消息
        
        Args:
            user_id: 对方 QQ 号
            text: 文本内容
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/send_private_msg",
                    headers=self._headers(),
                    json={
                        "user_id": user_id,
                        "message": [{"type": "text", "data": {"text": text}}],
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        return True
                    else:
                        logger.warning(f"[NapCat] 发送私聊失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 发送私聊异常: {e}")
            return False

    async def kick_group_member(self, group_id: int, user_id: int, reject_add_request: bool = False) -> bool:
        """
        踢出群成员
        
        Args:
            group_id: 群号
            user_id: 用户 QQ 号
            reject_add_request: 是否拒绝后续加群请求
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_kick",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "user_id": user_id,
                        "reject_add_request": reject_add_request,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 踢出成员成功: group={group_id} user={user_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 踢出成员失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 踢出成员异常: {e}")
            return False

    async def set_group_whole_ban(self, group_id: int, enable: bool) -> bool:
        """全体禁言/解除"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_whole_ban",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "enable": enable,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 全体禁言{'开启' if enable else '解除'}成功: group={group_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 全体禁言失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 全体禁言异常: {e}")
            return False

    async def set_group_card(self, group_id: int, user_id: int, card: str) -> bool:
        """设置群名片"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_card",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "user_id": user_id,
                        "card": card,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 设置群名片成功: group={group_id} user={user_id} card={card}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 设置群名片失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 设置群名片异常: {e}")
            return False

    async def set_group_name(self, group_id: int, group_name: str) -> bool:
        """修改群名称"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_name",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "group_name": group_name,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 修改群名称成功: group={group_id} name={group_name}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 修改群名称失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 修改群名称异常: {e}")
            return False

    async def set_group_special_title(self, group_id: int, user_id: int, title: str) -> bool:
        """设置成员专属头衔"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_special_title",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "user_id": user_id,
                        "title": title,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 设置专属头衔成功: group={group_id} user={user_id} title={title}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 设置专属头衔失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 设置专属头衔异常: {e}")
            return False

    async def set_group_admin(self, group_id: int, user_id: int, enable: bool) -> bool:
        """设置/取消群管理员"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_admin",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "user_id": user_id,
                        "enable": enable,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        action = "设置管理员" if enable else "取消管理员"
                        logger.info(f"[NapCat] {action}成功: group={group_id} user={user_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 设置/取消管理员失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 设置/取消管理员异常: {e}")
            return False

    async def get_group_member_info(self, group_id: int, user_id: int, no_cache: bool = False) -> dict:
        """获取群成员信息（返回完整 data）"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_member_info",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "user_id": user_id,
                        "no_cache": no_cache,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        member_info = data.get("data", {})
                        logger.info(f"[NapCat] 获取群成员信息成功: group={group_id} user={user_id}")
                        return member_info
                    else:
                        logger.warning(f"[NapCat] 获取群成员信息失败: {data}")
                        return {}
        except Exception as e:
            logger.error(f"[NapCat] 获取群成员信息异常: {e}")
            return {}

    async def get_group_info(self, group_id: int, no_cache: bool = False) -> dict:
        """获取群信息"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_info",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "no_cache": no_cache,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        group_info = data.get("data", {})
                        logger.info(f"[NapCat] 获取群信息成功: group={group_id}")
                        return group_info
                    else:
                        logger.warning(f"[NapCat] 获取群信息失败: {data}")
                        return {}
        except Exception as e:
            logger.error(f"[NapCat] 获取群信息异常: {e}")
            return {}

    async def get_group_list(self) -> list:
        """获取 NapCat 已加入的群列表。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_list",
                    headers=self._headers(),
                    json={},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        group_list = data.get("data", [])
                        logger.info(f"[NapCat] 获取群列表成功: {len(group_list)} 个群")
                        return group_list
                    else:
                        logger.warning(f"[NapCat] 获取群列表失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取群列表异常: {e}")
            return []

    async def get_group_member_list(self, group_id: int, no_cache: bool = False) -> list:
        """获取群成员列表"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_member_list",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "no_cache": no_cache,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        member_list = data.get("data", [])
                        logger.info(f"[NapCat] 获取群成员列表成功: group={group_id} count={len(member_list)}")
                        return member_list
                    else:
                        logger.warning(f"[NapCat] 获取群成员列表失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取群成员列表异常: {e}")
            return []

    async def get_group_msg_history(self, group_id: int, count: int = 20) -> list:
        """获取群消息历史"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_msg_history",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "count": count,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        messages = data.get("data", [])
                        logger.info(f"[NapCat] 获取群消息历史成功: group={group_id} count={len(messages)}")
                        return messages
                    else:
                        logger.warning(f"[NapCat] 获取群消息历史失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取群消息历史异常: {e}")
            return []

    async def get_essence_msg_list(self, group_id: int) -> list:
        """获取精华消息列表"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_essence_msg_list",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        essence_list = data.get("data", [])
                        logger.info(f"[NapCat] 获取精华消息列表成功: group={group_id} count={len(essence_list)}")
                        return essence_list
                    else:
                        logger.warning(f"[NapCat] 获取精华消息列表失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取精华消息列表异常: {e}")
            return []

    async def set_essence_msg(self, group_id: int, message_id: int) -> bool:
        """设置精华消息"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_essence_msg",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "message_id": message_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 设置精华消息成功: group={group_id} msg={message_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 设置精华消息失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 设置精华消息异常: {e}")
            return False

    async def delete_essence_msg(self, group_id: int, message_id: int, message_seq: str = "") -> bool:
        """取消精华消息"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/delete_essence_msg",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "message_id": message_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 取消精华消息成功: group={group_id} msg={message_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 取消精华消息失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 取消精华消息异常: {e}")
            return False

    async def send_group_notice(self, group_id: int, text: str) -> bool:
        """发送群公告"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/send_group_notice",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "content": text,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 发送群公告成功: group={group_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 发送群公告失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 发送群公告异常: {e}")
            return False

    async def delete_group_notice(self, group_id: int, notice_id: str) -> bool:
        """删除群公告"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/delete_group_notice",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "notice_id": notice_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 删除群公告成功: group={group_id} notice={notice_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 删除群公告失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 删除群公告异常: {e}")
            return False

    async def get_group_notice_list(self, group_id: int) -> list:
        """获取群公告列表"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_notice_list",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        notice_list = data.get("data", [])
                        logger.info(f"[NapCat] 获取群公告列表成功: group={group_id} count={len(notice_list)}")
                        return notice_list
                    else:
                        logger.warning(f"[NapCat] 获取群公告列表失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取群公告列表异常: {e}")
            return []

    async def get_group_file_list(self, group_id: int) -> list:
        """获取群文件列表"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_files",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        file_list = data.get("data", [])
                        logger.info(f"[NapCat] 获取群文件列表成功: group={group_id} count={len(file_list)}")
                        return file_list
                    else:
                        logger.warning(f"[NapCat] 获取群文件列表失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取群文件列表异常: {e}")
            return []

    async def delete_group_file(self, group_id: int, file_id: str, busid: int = 0) -> bool:
        """删除群文件"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/delete_group_file",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "file_id": file_id,
                        "busid": busid,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[NapCat] 删除群文件成功: group={group_id} file={file_id}")
                        return True
                    else:
                        logger.warning(f"[NapCat] 删除群文件失败: {data}")
                        return False
        except Exception as e:
            logger.error(f"[NapCat] 删除群文件异常: {e}")
            return False

    async def get_group_honor_list(self, group_id: int, honor_type: str = "all") -> list:
        """获取群荣誉列表"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/get_group_honor_info",
                    headers=self._headers(),
                    json={
                        "group_id": group_id,
                        "honor_type": honor_type,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        honor_list = data.get("data", [])
                        logger.info(f"[NapCat] 获取群荣誉列表成功: group={group_id} type={honor_type} count={len(honor_list)}")
                        return honor_list
                    else:
                        logger.warning(f"[NapCat] 获取群荣誉列表失败: {data}")
                        return []
        except Exception as e:
            logger.error(f"[NapCat] 获取群荣誉列表异常: {e}")
            return []

    async def set_group_kick(self, group_id: int, user_id: int, reject_add_request: bool = False) -> bool:
        """踢出群成员（已有 kick_group_member，这里改名为 set_group_kick 别名）"""
        return await self.kick_group_member(group_id, user_id, reject_add_request)

    async def set_group_add_request(
        self,
        flag: str,
        sub_type: str = "add",
        approve: bool = True,
        reason: str = "",
    ) -> bool:
        """处理加群请求（通过/拒绝）"""
        try:
            payload = {
                "flag": flag,
                "sub_type": sub_type or "add",
                "approve": approve,
            }
            if not approve and reason:
                payload["reason"] = reason
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/set_group_add_request",
                    headers=self._headers(),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == "ok":
                        action = "通过" if approve else "拒绝"
                        logger.info(f"[NapCat] 加群申请已{action}: flag={flag}")
                        return True
                    logger.warning(f"[NapCat] 处理加群申请失败: {data}")
                    return False
        except Exception as e:
            logger.error(f"[NapCat] 处理加群申请异常: {e}")
            return False


# 全局单例
_bridge: NapCatBridge = None


def get_napcat_bridge() -> NapCatBridge:
    """获取全局 NapCat 桥接实例"""
    global _bridge
    if _bridge is None:
        # 从环境变量或 settings 读取配置
        from config import settings
        base_url = getattr(settings, "NAPCAT_API_URL", "") or "http://napcat:30101"
        token = getattr(settings, "NAPCAT_ACCESS_TOKEN", "") or ""
        _bridge = NapCatBridge(base_url=base_url, access_token=token)
    return _bridge
