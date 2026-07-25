"""
异步 SQLite 数据库模块
用于存储群组设置、自动回复规则、定时任务等
"""

import aiosqlite
from config import settings


class Database:
    """数据库操作类"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or settings.DATABASE_PATH
        self._connection: aiosqlite.Connection | None = None

    async def connect(self):
        """建立数据库连接"""
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self):
        """关闭数据库连接"""
        if self._connection:
            await self._connection.close()

    async def _create_tables(self):
        """创建数据表"""
        await self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                welcome_msg TEXT,
                antispam INTEGER DEFAULT 0,
                block_forward INTEGER DEFAULT 0,
                banned_words TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS auto_reply (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                keyword TEXT,
                response TEXT,
                is_global INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, keyword)
            );

            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                text TEXT,
                cron TEXT,
                enabled INTEGER DEFAULT 1,
                is_random INTEGER DEFAULT 0,
                random_texts TEXT DEFAULT '',
                suffix TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS spam_users (
                user_id INTEGER,
                chat_id INTEGER,
                join_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS admin_users (
                chat_id INTEGER,
                user_id INTEGER,
                added_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS captcha_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                verified INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS bot_groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await self._connection.commit()

    # --- 群组设置 ---
    async def get_group_setting(self, chat_id: int):
        """获取群组设置"""
        async with self._connection.execute(
            "SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_group_setting(self, chat_id: int, **kwargs):
        """更新群组设置"""
        allowed = {"welcome_msg", "antispam", "block_forward", "banned_words"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return

        # 检查是否存在
        exists = await self.get_group_setting(chat_id)
        if exists:
            sets = ", ".join([f"{k} = ?" for k in fields])
            values = list(fields.values()) + [chat_id]
            await self._connection.execute(
                f"UPDATE group_settings SET {sets} WHERE chat_id = ?", values
            )
        else:
            cols = ", ".join(["chat_id"] + list(fields.keys()))
            placeholders = ", ".join(["?"] * (len(fields) + 1))
            values = [chat_id] + list(fields.values())
            await self._connection.execute(
                f"INSERT INTO group_settings ({cols}) VALUES ({placeholders})", values
            )
        await self._connection.commit()

    # --- 自动回复 ---
    async def get_auto_replies(self, chat_id: int = None, global_only: bool = False):
        """获取自动回复规则"""
        if global_only:
            async with self._connection.execute(
                "SELECT * FROM auto_reply WHERE is_global = 1", ()
            ) as cursor:
                rows = await cursor.fetchall()
        elif chat_id:
            async with self._connection.execute(
                "SELECT * FROM auto_reply WHERE chat_id = ? OR is_global = 1", (chat_id,)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._connection.execute("SELECT * FROM auto_reply") as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def add_auto_reply(self, keyword: str, response: str, chat_id: int = 0, is_global: bool = True):
        """添加自动回复规则"""
        await self._connection.execute(
            """INSERT OR REPLACE INTO auto_reply (chat_id, keyword, response, is_global)
               VALUES (?, ?, ?, ?)""",
            (chat_id, keyword.lower(), response, 1 if is_global else 0)
        )
        await self._connection.commit()

    async def delete_auto_reply(self, keyword: str, chat_id: int = None):
        """删除自动回复规则"""
        if chat_id is not None:
            await self._connection.execute(
                "DELETE FROM auto_reply WHERE keyword = ? AND chat_id = ?",
                (keyword.lower(), chat_id)
            )
        else:
            await self._connection.execute(
                "DELETE FROM auto_reply WHERE keyword = ?", (keyword.lower(),)
            )
        await self._connection.commit()

    async def delete_auto_reply_by_id(self, rule_id: int):
        """按 ID 删除自动回复规则"""
        await self._connection.execute(
            "DELETE FROM auto_reply WHERE id = ?", (rule_id,)
        )
        await self._connection.commit()

    # --- 定时消息 ---
    async def get_scheduled_messages(self, enabled_only: bool = True):
        """获取定时消息列表"""
        sql = "SELECT * FROM scheduled_messages"
        if enabled_only:
            sql += " WHERE enabled = 1"
        async with self._connection.execute(sql) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def add_scheduled_message(self, chat_id: int, text: str, cron: str,
                                  is_random: bool = False, random_texts: str = "",
                                  suffix: str = ""):
        """添加定时消息"""
        await self._connection.execute(
            "INSERT INTO scheduled_messages (chat_id, text, cron, is_random, random_texts, suffix) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, text, cron, 1 if is_random else 0, random_texts, suffix)
        )
        await self._connection.commit()

    async def update_scheduled_message(self, msg_id: int, **kwargs):
        """更新定时消息字段"""
        allowed = {"text", "cron", "enabled", "is_random", "random_texts", "suffix"}
        sets = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                values.append(v)
        if sets:
            values.append(msg_id)
            await self._connection.execute(
                f"UPDATE scheduled_messages SET {', '.join(sets)} WHERE id = ?",
                values
            )
            await self._connection.commit()

    async def delete_scheduled_message(self, msg_id: int):
        """删除定时消息"""
        await self._connection.execute(
            "DELETE FROM scheduled_messages WHERE id = ?", (msg_id,)
        )
        await self._connection.commit()

    async def toggle_scheduled_message(self, msg_id: int, enabled: bool):
        """启用/禁用定时消息"""
        await self._connection.execute(
            "UPDATE scheduled_messages SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, msg_id)
        )
        await self._connection.commit()

    # --- 反垃圾 ---
    async def add_spam_user(self, user_id: int, chat_id: int):
        """记录新入群用户"""
        await self._connection.execute(
            """INSERT OR REPLACE INTO spam_users (user_id, chat_id, verified)
               VALUES (?, ?, 0)""", (user_id, chat_id)
        )
        await self._connection.commit()

    async def verify_user(self, user_id: int, chat_id: int):
        """标记用户已验证"""
        await self._connection.execute(
            "UPDATE spam_users SET verified = 1 WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        await self._connection.commit()

    async def is_user_verified(self, user_id: int, chat_id: int) -> bool:
        """检查用户是否已验证"""
        async with self._connection.execute(
            "SELECT verified FROM spam_users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["verified"])

    async def remove_spam_user(self, user_id: int, chat_id: int):
        """移除用户记录"""
        await self._connection.execute(
            "DELETE FROM spam_users WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        await self._connection.commit()

    # --- 群管权限 ---
    async def add_group_admin(self, chat_id: int, user_id: int, added_by: int):
        """添加群管"""
        await self._connection.execute(
            "INSERT OR IGNORE INTO admin_users (chat_id, user_id, added_by) VALUES (?, ?, ?)",
            (chat_id, user_id, added_by)
        )
        await self._connection.commit()

    async def remove_group_admin(self, chat_id: int, user_id: int):
        """移除群管"""
        await self._connection.execute(
            "DELETE FROM admin_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        )
        await self._connection.commit()

    async def is_group_admin(self, chat_id: int, user_id: int) -> bool:
        """检查是否为群管"""
        async with self._connection.execute(
            "SELECT 1 FROM admin_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ) as cursor:
            return await cursor.fetchone() is not None

    # --- Captcha 验证 ---
    async def add_captcha_challenge(self, user_id: int, chat_id: int, message_id: int):
        """添加验证码挑战记录"""
        await self._connection.execute(
            """INSERT OR REPLACE INTO captcha_challenges (user_id, chat_id, message_id, verified)
               VALUES (?, ?, ?, 0)""",
            (user_id, chat_id, message_id)
        )
        await self._connection.commit()

    async def verify_captcha(self, user_id: int, chat_id: int) -> bool:
        """标记用户验证通过"""
        await self._connection.execute(
            "UPDATE captcha_challenges SET verified = 1 WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        await self._connection.commit()
        # 检查是否有更新
        async with self._connection.execute(
            "SELECT verified FROM captcha_challenges WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["verified"])

    async def is_captcha_verified(self, user_id: int, chat_id: int) -> bool:
        """检查用户是否已通过验证"""
        async with self._connection.execute(
            "SELECT verified FROM captcha_challenges WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["verified"])

    async def get_captcha_message_id(self, user_id: int, chat_id: int) -> int | None:
        """获取验证消息 ID"""
        async with self._connection.execute(
            "SELECT message_id FROM captcha_challenges WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row["message_id"] if row else None

    async def delete_captcha_challenge(self, user_id: int, chat_id: int):
        """删除验证记录"""
        await self._connection.execute(
            "DELETE FROM captcha_challenges WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        await self._connection.commit()

    # --- 机器人所在群组 ---
    async def add_bot_group(self, chat_id: int, title: str):
        """记录机器人所在群组"""
        await self._connection.execute(
            "INSERT OR REPLACE INTO bot_groups (chat_id, title) VALUES (?, ?)",
            (chat_id, title)
        )
        await self._connection.commit()

    async def remove_bot_group(self, chat_id: int):
        """移除机器人群组记录"""
        await self._connection.execute(
            "DELETE FROM bot_groups WHERE chat_id = ?", (chat_id,)
        )
        await self._connection.commit()

    async def get_bot_groups(self) -> list:
        """获取机器人所在群组列表"""
        async with self._connection.execute(
            "SELECT * FROM bot_groups ORDER BY added_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# 全局数据库实例
db = Database()
