"""QQ 群管命令处理器
支持通过群内指令执行管理操作"""
import logging
import re
import time
from napcat_bridge import get_napcat_bridge

logger = logging.getLogger("qq_group_manager")


def _parse_qq(text: str) -> int:
    """从文本中提取 QQ 号，支持 CQ码 @ 和纯数字（>=5位）。"""
    m = re.search(r'\[CQ:at,qq=(\d+)\]', text)
    if m:
        return int(m.group(1))
    parts = text.strip().split()
    for p in parts:
        if p.isdigit() and len(p) >= 5:
            return int(p)
    return 0


def _strip_prefix(text: str) -> str:
    """去掉指令前的 / 前缀（如果有）。"""
    return text.lstrip('/').lstrip()


async def _check_admin(sender_qq: int, group_num: int) -> bool:
    """检查发送者是否为群管理员/群主。"""
    try:
        from group_member_store import is_group_admin, _member_cache, refresh_member_cache
        if group_num not in _member_cache:
            await refresh_member_cache(group_num)
        return is_group_admin(group_num, sender_qq)
    except Exception as e:
        logger.warning("权限检查异常: %s", e)
        return False


async def _send_result(group_num: int, msg: str):
    """向群发送结果消息。"""
    try:
        bridge = get_napcat_bridge()
        await bridge.send_group_msg(group_num, msg)
    except Exception as e:
        logger.error("发送群消息失败: %s", e)


async def _fmt_user(qq: int, group_num: int) -> str:
    """格式化用户显示文本，尝试附带昵称/群名片。"""
    try:
        from group_member_store import _member_cache
        members = _member_cache.get(group_num, {})
        info = members.get(qq)
        if info:
            name = info.get('card') or info.get('nick') or ''
            if name:
                return f"{name}(QQ:{qq})"
    except Exception:
        pass
    return f"QQ:{qq}"


WRITE_COMMANDS = {
    '禁言', '解禁', '踢人', '全体禁言', '设置名片', '改群名',
    '设管理', '取消管理', '头衔', '发公告', '撤回', '设精华',
    '取消精华', '删公告', '删文件', '踢并拉黑',
}

QUERY_COMMANDS = {
    '成员列表', '禁言列表', '群信息', '搜索', '统计',
    '精华列表', '公告列表', '文件列表', '荣誉列表', '查成员', '帮助',
}

ALL_COMMANDS = WRITE_COMMANDS | QUERY_COMMANDS


def _match_command(raw_text: str):
    """匹配指令，返回 (指令名, 剩余参数) 或 (None, None)。
    必须以 / 开头，例如 /群信息、/禁言 123 30。
    """
    text = _strip_prefix(raw_text)
    if not text:
        return None, None
    for cmd in sorted(ALL_COMMANDS, key=len, reverse=True):
        if text == cmd:
            return cmd, ''
        if text.startswith(cmd + ' '):
            return cmd, text[len(cmd):].strip()
        # 允许 /禁言123456 这种无空格写法（参数以数字或 CQ@ 开头）
        if text.startswith(cmd) and len(text) > len(cmd):
            rest = text[len(cmd):]
            if rest[0].isdigit() or rest.startswith('[CQ:at'):
                return cmd, rest.strip()
    return None, None


async def _cmd_ban(rest, sender_qq, group_num):
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定要禁言的成员（QQ号或@）"
    duration = 30
    parts = re.split(r'\s+', rest.strip())
    for p in parts:
        if p.isdigit() and int(p) != qq:
            duration = int(p)
            break
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_ban(group_num, qq, duration * 60)
        user = await _fmt_user(qq, group_num)
        return f"[群管] 已禁言 {user} {duration}分钟"
    except Exception as e:
        return f"[群管] 禁言失败: {e}"


async def _cmd_unban(rest, sender_qq, group_num):
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定要解禁的成员（QQ号或@）"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_ban(group_num, qq, 0)
        user = await _fmt_user(qq, group_num)
        return f"[群管] 已解禁 {user}"
    except Exception as e:
        return f"[群管] 解禁失败: {e}"


async def _cmd_kick(rest, sender_qq, group_num):
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定要踢出的成员（QQ号或@）"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_kick(group_num, qq)
        user = await _fmt_user(qq, group_num)
        return f"[群管] 已将 {user} 踢出群聊"
    except Exception as e:
        return f"[群管] 踢人失败: {e}"


async def _cmd_whole_ban(rest, sender_qq, group_num):
    rest = rest.strip()
    if rest in ('开启', '打开', 'on', 'true', '1'):
        enable = True
    elif rest in ('关闭', '关', 'off', 'false', '0'):
        enable = False
    else:
        return "[群管] 用法: 全体禁言 开启/关闭"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_whole_ban(group_num, enable)
        status = "开启" if enable else "关闭"
        return f"[群管] 已{status}全体禁言"
    except Exception as e:
        return f"[群管] 全体禁言操作失败: {e}"


async def _cmd_set_card(rest, sender_qq, group_num):
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定要设置名片的成员（QQ号）"
    cleaned = re.sub(r'\[CQ:at,qq=\d+\]', '', rest).strip()
    parts = cleaned.split(None, 1)
    if len(parts) >= 2 and parts[0].isdigit():
        new_card = parts[1]
    elif len(parts) == 1 and parts[0]:
        new_card = parts[0]
    else:
        return "[群管] 请指定新名片名称"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_card(group_num, qq, new_card)
        user = await _fmt_user(qq, group_num)
        return f'[群管] 已将 {user} 的名片设置为 "{new_card}"'
    except Exception as e:
        return f"[群管] 设置名片失败: {e}"


async def _cmd_set_group_name(rest, sender_qq, group_num):
    new_name = rest.strip()
    if not new_name:
        return "[群管] 请指定新群名"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_name(group_num, new_name)
        return f'[群管] 群名已修改为 "{new_name}"'
    except Exception as e:
        return f"[群管] 改群名失败: {e}"


async def _cmd_set_admin(rest, sender_qq, group_num, enable: bool):
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定目标成员（QQ号）"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_admin(group_num, qq, enable)
        user = await _fmt_user(qq, group_num)
        action = "设置" if enable else "取消"
        return f"[群管] 已{action} {user} 的管理员权限"
    except Exception as e:
        return f"[群管] 操作失败: {e}"


async def _cmd_special_title(rest, sender_qq, group_num):
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定目标成员（QQ号）"
    cleaned = re.sub(r'\[CQ:at,qq=\d+\]', '', rest).strip()
    parts = cleaned.split(None, 1)
    if len(parts) >= 2 and parts[0].isdigit():
        title = parts[1]
    elif len(parts) == 1 and parts[0]:
        title = parts[0]
    else:
        return "[群管] 请指定头衔内容"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_special_title(group_num, qq, title)
        user = await _fmt_user(qq, group_num)
        return f'[群管] 已将 {user} 的头衔设置为 "{title}"'
    except Exception as e:
        return f"[群管] 设置头衔失败: {e}"


async def _cmd_send_notice(rest, sender_qq, group_num):
    content = rest.strip()
    if not content:
        return "[群管] 请指定公告内容"
    try:
        bridge = get_napcat_bridge()
        await bridge.send_group_notice(group_num, content)
        return "[群管] 公告已发送"
    except Exception as e:
        return f"[群管] 发送公告失败: {e}"


async def _cmd_recall(rest, sender_qq, group_num):
    count = 1
    rest = rest.strip()
    if rest and rest.isdigit():
        count = int(rest)
    if count < 1:
        count = 1
    if count > 20:
        count = 20
    try:
        bridge = get_napcat_bridge()
        history = await bridge.get_group_msg_history(group_num, count=count)
        if not history:
            return "[群管] 未获取到可撤回的消息"
        if isinstance(history, dict):
            messages = history.get('messages', [])
        else:
            messages = history
        if not messages:
            return "[群管] 未获取到可撤回的消息"
        success = 0
        failed = 0
        for msg in messages:
            msg_id = msg.get('message_id') or msg.get('msg_id')
            if msg_id:
                try:
                    await bridge.delete_group_msg(group_num, msg_id)
                    success += 1
                except Exception:
                    failed += 1
        if failed == 0:
            return f"[群管] 已撤回 {success} 条消息"
        else:
            return f"[群管] 撤回完成: 成功 {success} 条, 失败 {failed} 条"
    except Exception as e:
        return f"[群管] 撤回失败: {e}"


async def _cmd_member_list(rest, sender_qq, group_num):
    try:
        from group_member_store import _member_cache, refresh_member_cache
        try:
            await refresh_member_cache(group_num)
        except Exception:
            pass
        members = _member_cache.get(group_num, {})
        if not members:
            return "[群管] 无法获取成员列表，缓存为空"
        total = len(members)
        lines = [f"[群管] 群成员 (共 {total} 人):\n"]
        sorted_members = sorted(members.items(), key=lambda x: x[0])
        display = sorted_members[:20]
        for i, (qq, info) in enumerate(display, 1):
            card = info.get('card') or ''
            nick = info.get('nick') or ''
            name = card or nick or str(qq)
            role = info.get('role', 'member')
            role_tag = ''
            if role == 'owner':
                role_tag = ' [群主]'
            elif role == 'admin':
                role_tag = ' [管理]'
            lines.append(f"{i}. {name}(QQ:{qq}){role_tag}")
        if total > 20:
            lines.append(f"\n... 仅显示前 20 人，共 {total} 人")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取成员列表失败: {e}"


async def _cmd_ban_list(rest, sender_qq, group_num):
    try:
        from group_member_store import _member_cache, refresh_member_cache
        try:
            await refresh_member_cache(group_num)
        except Exception:
            pass
        members = _member_cache.get(group_num, {})
        if not members:
            return "[群管] 无法获取成员信息"
        banned = []
        for qq, info in members.items():
            shut_up = info.get('shut_up_timestamp', 0)
            if shut_up and shut_up > 0:
                card = info.get('card') or ''
                nick = info.get('nick') or ''
                name = card or nick or str(qq)
                banned.append((qq, name, shut_up))
        if not banned:
            return "[群管] 当前没有被禁言的成员"
        lines = [f"[群管] 禁言列表 (共 {len(banned)} 人):\n"]
        for i, (qq, name, ts) in enumerate(banned, 1):
            if ts > 9999999999:
                ts = ts / 1000
            remain = int(ts - time.time())
            if remain > 0:
                hours, mins = divmod(remain // 60, 60)
                if hours > 0:
                    time_str = f"{hours}小时{mins}分钟"
                else:
                    time_str = f"{mins}分钟"
            else:
                time_str = "已到期"
            lines.append(f"{i}. {name}(QQ:{qq}) - 剩余 {time_str}")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取禁言列表失败: {e}"


async def _cmd_group_info(rest, sender_qq, group_num):
    try:
        bridge = get_napcat_bridge()
        info = await bridge.get_group_info(group_num)
        if not info:
            return "[群管] 获取群信息失败"
        group_name = info.get('group_name', '未知')
        member_count = info.get('member_count', '?')
        max_member = info.get('max_member', '?')
        group_id = info.get('group_id', group_num)
        lines = [
            "[群管] 群信息:",
            f"  群号: {group_id}",
            f"  群名: {group_name}",
            f"  成员数: {member_count}/{max_member}",
        ]
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取群信息失败: {e}"


async def _cmd_search(rest, sender_qq, group_num):
    keyword = rest.strip()
    if not keyword:
        return "[群管] 请指定搜索关键词"
    try:
        from group_member_store import _member_cache, refresh_member_cache
        try:
            await refresh_member_cache(group_num)
        except Exception:
            pass
        members = _member_cache.get(group_num, {})
        if not members:
            return "[群管] 无法获取成员信息"
        results = []
        keyword_lower = keyword.lower()
        for qq, info in members.items():
            qq_str = str(qq)
            card = (info.get('card') or '').lower()
            nick = (info.get('nick') or '').lower()
            if keyword in qq_str or keyword_lower in card or keyword_lower in nick:
                name = info.get('card') or info.get('nick') or qq_str
                results.append((qq, name))
        if not results:
            return f'[群管] 未找到匹配 "{keyword}" 的成员'
        lines = [f'[群管] 搜索 "{keyword}" (找到 {len(results)} 人):\n']
        for i, (qq, name) in enumerate(results[:20], 1):
            lines.append(f"{i}. {name}(QQ:{qq})")
        if len(results) > 20:
            lines.append(f"\n... 仅显示前 20 条结果")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 搜索失败: {e}"


async def _cmd_essence(rest, sender_qq, group_num):
    rest = rest.strip()
    if not rest or not rest.isdigit():
        return "[群管] 请指定消息ID"
    msg_id = int(rest)
    try:
        bridge = get_napcat_bridge()
        await bridge.set_essence_msg(group_num, msg_id)
        return f"[群管] 已将消息 {msg_id} 设为精华"
    except Exception as e:
        return f"[群管] 设精华失败: {e}"


async def _cmd_member_stats(rest, sender_qq, group_num):
    """成员统计：总人数、除去机器人后的人数"""
    try:
        from group_member_store import _member_cache, refresh_member_cache
        try:
            await refresh_member_cache(group_num)
        except Exception:
            pass
        members = _member_cache.get(group_num, {})
        if not members:
            return "[群管] 无法获取成员列表，缓存为空"

        total = len(members)
        # 排除机器人账号（NapCat 小号本身 + 已知其他机器人）
        bot_qqs = set()
        try:
            from config import settings
            napcat_qq = getattr(settings, 'NAP_CAT_QQ', '')
            if napcat_qq and napcat_qq.isdigit():
                bot_qqs.add(int(napcat_qq))
        except Exception:
            pass
        # 尝试从 NapCat 运行时获取 self_qq（动态获取）
        try:
            from napcat_ws import _napcat_self_qq
            if _napcat_self_qq:
                bot_qqs.add(_napcat_self_qq)
        except Exception:
            pass
        human_count = total - len(bot_qqs & set(members.keys()))

        # 统计管理员/群主数
        admins = 0
        owners = 0
        for qq, info in members.items():
            role = info.get('role', '')
            if role == 'owner':
                owners += 1
            elif role == 'admin':
                admins += 1

        lines = [
            "[群管] 群成员统计:",
            f"  总人数: {total}",
            f"  其中机器人: {total - human_count}",
            f"  实际人数: {human_count}",
            f"  成员: {human_count - admins - owners}",
            f"  管理: {admins}",
            f"  群主: {owners}",
        ]
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 统计失败: {e}"


async def _cmd_remove_essence(rest, sender_qq, group_num):
    """取消精华消息。"""
    rest = rest.strip()
    if not rest:
        return "[群管] 请指定消息ID或消息seq"
    try:
        bridge = get_napcat_bridge()
        msg_id = None
        seq = ""
        # 尝试解析为消息ID（纯数字）
        if rest.isdigit():
            msg_id = int(rest)
        else:
            seq = rest
        await bridge.delete_essence_msg(group_num, msg_id or 0, message_seq=seq)
        return f"[群管] 已取消消息 {rest} 的精华"
    except Exception as e:
        return f"[群管] 取消精华失败: {e}"


async def _cmd_essence_list(rest, sender_qq, group_num):
    """查看精华消息列表。"""
    try:
        bridge = get_napcat_bridge()
        essence_list = await bridge.get_essence_msg_list(group_num)
        if not essence_list:
            return "[群管] 当前没有精华消息"
        lines = [f"[群管] 精华消息 (共 {len(essence_list)} 条):\n"]
        for i, item in enumerate(essence_list[:20], 1):
            sender_nick = item.get('sender_nick') or item.get('nick') or '未知'
            sender_id = item.get('sender_uin') or item.get('sender_id') or '?'
            content = item.get('content') or ''
            if isinstance(content, list):
                # CQ 码消息：提取纯文本部分
                text_parts = []
                for seg in content:
                    if isinstance(seg, dict) and seg.get('type') == 'text':
                        text_parts.append(seg.get('data', {}).get('text', ''))
                    elif isinstance(seg, str):
                        text_parts.append(seg)
                content = ''.join(text_parts)
            if not isinstance(content, str):
                content = str(content)
            # 截断显示
            preview = content[:60].replace('\n', ' ') if content else '(非文本消息)'
            lines.append(f"{i}. {sender_nick}(QQ:{sender_id}): {preview}")
        if len(essence_list) > 20:
            lines.append(f"\n... 仅显示前 20 条")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取精华列表失败: {e}"


async def _cmd_notice_list(rest, sender_qq, group_num):
    """查看群公告列表。"""
    try:
        bridge = get_napcat_bridge()
        notice_list = await bridge.get_group_notice_list(group_num)
        if not notice_list:
            return "[群管] 当前没有群公告"
        lines = [f"[群管] 群公告 (共 {len(notice_list)} 条):\n"]
        for i, item in enumerate(notice_list[:10], 1):
            notice_id = item.get('notice_id') or '?'
            sender = item.get('sender_nick') or item.get('sender_id') or '?'
            content = (item.get('plain', '') or '')[:80].replace('\n', ' ')
            lines.append(f"{i}. [{notice_id}] {sender}: {content}")
        if len(notice_list) > 10:
            lines.append(f"\n... 仅显示前 10 条，使用 /删公告 <ID> 删除")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取公告列表失败: {e}"


async def _cmd_delete_notice(rest, sender_qq, group_num):
    """删除指定公告。"""
    rest = rest.strip()
    if not rest:
        return "[群管] 请指定公告ID（用 /公告列表 查看）"
    try:
        bridge = get_napcat_bridge()
        await bridge.delete_group_notice(group_num, rest)
        return f"[群管] 已删除公告 {rest}"
    except Exception as e:
        return f"[群管] 删除公告失败: {e}"


async def _cmd_file_list(rest, sender_qq, group_num):
    """查看群文件列表。"""
    try:
        bridge = get_napcat_bridge()
        file_list = await bridge.get_group_file_list(group_num)
        if not file_list:
            return "[群管] 当前没有群文件"
        files = file_list if isinstance(file_list, list) else file_list.get('files', [])
        if not files:
            return "[群管] 当前没有群文件"
        lines = [f"[群管] 群文件 (共 {len(files)} 个):\n"]
        for i, f in enumerate(files[:20], 1):
            name = f.get('file_name') or f.get('name') or '未知'
            size = f.get('size', 0) or 0
            uploader = f.get('uploader_nick') or f.get('uploader_name') or f.get('uploader_id') or '?'
            fid = f.get('file_id') or f.get('id') or ''
            busid = f.get('busid', 0) or 0
            # 格式化文件大小
            if size >= 1048576:
                size_str = f"{size / 1048576:.1f}MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f}KB"
            else:
                size_str = f"{size}B"
            lines.append(f"{i}. {name} ({size_str}) by {uploader}")
            if fid:
                lines.append(f"   文件ID: {fid} busid: {busid}")
        if len(files) > 20:
            lines.append(f"\n... 仅显示前 20 个")
        lines.append("\n提示: 使用 /删文件 <文件ID> <busid> 删除文件")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取文件列表失败: {e}"


async def _cmd_delete_file(rest, sender_qq, group_num):
    """删除指定文件。"""
    rest = rest.strip()
    if not rest:
        return "[群管] 用法: /删文件 <文件ID> <busid>\n提示: 用 /文件列表 查看文件ID和busid"
    parts = rest.split()
    if len(parts) < 1:
        return "[群管] 请指定文件ID"
    file_id = parts[0]
    busid = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    try:
        bridge = get_napcat_bridge()
        await bridge.delete_group_file(group_num, file_id, busid)
        return f"[群管] 已删除文件 {file_id}"
    except Exception as e:
        return f"[群管] 删除文件失败: {e}"


async def _cmd_honor_list(rest, sender_qq, group_num):
    """查看群荣誉列表（龙王、活跃等）。"""
    honor_type = 'all'
    rest = rest.strip().lower()
    if rest in ('talkative', '龙王', '龙'):
        honor_type = 'talkative'
    elif rest in ('performer', '活跃', '闪耀'):
        honor_type = 'performer'
    elif rest in ('legend', '传奇'):
        honor_type = 'legend'
    elif rest in ('strong_newbie', '新人', '新秀'):
        honor_type = 'strong_newbie'
    elif rest in ('emotion', '氛围'):
        honor_type = 'emotion'
    try:
        bridge = get_napcat_bridge()
        honor_list = await bridge.get_group_honor_list(group_num, honor_type)
        if not honor_list:
            return f"[群管] 暂无荣誉记录"
        lines = [f"[群管] 群荣誉:\n"]
        for item in honor_list[:15]:
            nick = item.get('nick') or item.get('nickname') or '未知'
            qq = item.get('uin') or item.get('user_id') or '?'
            desc = item.get('description') or item.get('honor_name') or ''
            lines.append(f"  {nick}(QQ:{qq}) {desc}")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 获取荣誉列表失败: {e}"


async def _cmd_kick_ban(rest, sender_qq, group_num):
    """踢出成员并拒绝再次加群。"""
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定要踢出的成员（QQ号或@）"
    try:
        bridge = get_napcat_bridge()
        await bridge.set_group_kick(group_num, qq, reject_add_request=True)
        user = await _fmt_user(qq, group_num)
        return f"[群管] 已将 {user} 踢出并拒绝再次加群"
    except Exception as e:
        return f"[群管] 踢出并拉黑失败: {e}"


async def _cmd_member_info(rest, sender_qq, group_num):
    """查询单个成员详细信息。"""
    qq = _parse_qq(rest)
    if not qq:
        return "[群管] 请指定要查询的成员（QQ号或@）"
    try:
        bridge = get_napcat_bridge()
        info = await bridge.get_group_member_info(group_num, qq, no_cache=True)
        if not info:
            return f"[群管] 未找到成员 QQ:{qq} 的信息"
        card = info.get('card') or info.get('group_card') or ''
        nick = info.get('nickname') or info.get('nick') or ''
        role = info.get('role', 'member')
        role_map = {'owner': '群主', 'admin': '管理员', 'member': '普通成员'}
        role_str = role_map.get(role, role)
        join_time = info.get('join_time', 0)
        last_sent = info.get('last_sent_time', 0)
        level = info.get('level', '') or ''
        title = info.get('special_title') or ''
        shut_up = info.get('shut_up_timestamp', 0)

        lines = [f"[群管] 成员信息: {nick}(QQ:{qq})", f"  群名片: {card or '无'}"]
        lines.append(f"  身份: {role_str}")
        if level:
            lines.append(f"  等级: {level}")
        if title:
            lines.append(f"  头衔: {title}")
        if join_time:
            from datetime import datetime
            jt = datetime.fromtimestamp(join_time).strftime("%Y-%m-%d %H:%M") if join_time > 1000000000 else '未知'
            lines.append(f"  入群时间: {jt}")
        if last_sent:
            from datetime import datetime
            lt = datetime.fromtimestamp(last_sent).strftime("%Y-%m-%d %H:%M") if last_sent > 1000000000 else '未知'
            lines.append(f"  最后发言: {lt}")
        if shut_up and shut_up > 0:
            now_ts = time.time()
            if shut_up > 9999999999:
                shut_up = shut_up / 1000
            remain = int(shut_up - now_ts)
            if remain > 0:
                hours, mins = divmod(remain // 60, 60)
                lines.append(f"  禁言剩余: {hours}小时{mins}分钟" if hours else f"  禁言剩余: {mins}分钟")
            else:
                lines.append(f"  禁言状态: 已到期")
        return '\n'.join(lines)
    except Exception as e:
        return f"[群管] 查询成员信息失败: {e}"


async def _cmd_help(rest, sender_qq, group_num):
    """显示所有可用指令。"""
    lines = [
        "[群管] 可用指令:",
        "",
        "--- 写入指令（需管理员）---",
        "  /禁言 <QQ> [分钟]    禁言成员（默认30分钟）",
        "  /解禁 <QQ>           解除禁言",
        "  /踢人 <QQ>           踢出成员",
        "  /踢并拉黑 <QQ>       踢出且拒绝再次加群",
        "  /全体禁言 开启/关闭  全体禁言切换",
        "  /设置名片 <QQ> <名>  设置群名片",
        "  /改群名 <名称>       修改群名称",
        "  /头衔 <QQ> <头衔>    设置成员头衔",
        "  /设管理 <QQ>         设置管理员",
        "  /取消管理 <QQ>       取消管理员",
        "  /发公告 <内容>       发送群公告",
        "  /撤回 [数量]         撤回最近消息（默认1条，最多20）",
        "  /设精华 <消息ID>     设置精华消息",
        "  /取消精华 <消息ID>   取消精华",
        "  /删公告 <公告ID>     删除指定公告",
        "  /删文件 <ID> [busid] 删除指定文件",
        "",
        "--- 查询指令（所有人可用）---",
        "  /成员列表            查看群成员（前20人）",
        "  /禁言列表            查看被禁言成员",
        "  /群信息              查看群基本信息",
        "  /搜索 <关键词>       搜索成员",
        "  /统计                成员统计",
        "  /精华列表            查看精华消息",
        "  /公告列表            查看群公告",
        "  /文件列表            查看群文件",
        "  /荣誉列表 [类型]    查看群荣誉（龙王/活跃/传奇等）",
        "  /查成员 <QQ>         查询成员详细信息",
        "  /帮助                显示本帮助",
    ]
    return '\n'.join(lines)


COMMAND_MAP = {
    '禁言':     _cmd_ban,
    '解禁':     _cmd_unban,
    '踢人':     _cmd_kick,
    '全体禁言': _cmd_whole_ban,
    '设置名片': _cmd_set_card,
    '改群名':   _cmd_set_group_name,
    '头衔':     _cmd_special_title,
    '发公告':   _cmd_send_notice,
    '撤回':     _cmd_recall,
    '成员列表': _cmd_member_list,
    '禁言列表': _cmd_ban_list,
    '群信息':   _cmd_group_info,
    '搜索':     _cmd_search,
    '设精华':   _cmd_essence,
    '统计':     _cmd_member_stats,
    '取消精华': _cmd_remove_essence,
    '精华列表': _cmd_essence_list,
    '公告列表': _cmd_notice_list,
    '删公告':   _cmd_delete_notice,
    '文件列表': _cmd_file_list,
    '删文件':   _cmd_delete_file,
    '荣誉列表': _cmd_honor_list,
    '踢并拉黑': _cmd_kick_ban,
    '查成员':   _cmd_member_info,
    '帮助':     _cmd_help,
}

SPECIAL_COMMANDS = {
    '设管理':   lambda rest, sqq, gn: _cmd_set_admin(rest, sqq, gn, enable=True),
    '取消管理': lambda rest, sqq, gn: _cmd_set_admin(rest, sqq, gn, enable=False),
}


async def handle_group_command(text: str, sender_qq: int, group_num: int, nick: str) -> bool:
    if not text or not text.strip():
        return False
    cmd_name, rest = _match_command(text)
    if cmd_name is None:
        return False
    logger.info("群管指令: cmd=%s, sender=%s, group=%s, rest=%r",
                cmd_name, sender_qq, group_num, rest)
    if cmd_name in WRITE_COMMANDS:
        is_admin = await _check_admin(sender_qq, group_num)
        if not is_admin:
            await _send_result(group_num, "[群管] 权限不足，仅群主/管理员可执行此操作")
            return True
    try:
        if cmd_name in SPECIAL_COMMANDS:
            result = await SPECIAL_COMMANDS[cmd_name](rest, sender_qq, group_num)
        elif cmd_name in COMMAND_MAP:
            handler = COMMAND_MAP[cmd_name]
            result = await handler(rest, sender_qq, group_num)
        else:
            result = f"[群管] 未知指令: {cmd_name}"
        await _send_result(group_num, result)
    except Exception as e:
        logger.error("群管指令执行异常: cmd=%s, error=%s", cmd_name, e, exc_info=True)
        await _send_result(group_num, f"[群管] 指令执行异常: {e}")
    return True
