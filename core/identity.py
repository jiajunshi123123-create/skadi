"""渠道外部身份 → 内部用户 → 角色。未配置则拒绝。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("skadi.identity")


@dataclass
class ResolvedUser:
    user_id: str
    display_name: str
    role: dict
    external_id: str
    channel: str


class IdentityStore:
    """按 (channel, external_id) 查找已授权用户。找不到返回 None。"""

    def resolve(self, channel: str, external_id: str) -> Optional[ResolvedUser]:
        raise NotImplementedError


class MemoryIdentityStore(IdentityStore):
    """测试或本地替身。键是 (channel, external_id)。"""

    def __init__(self, users: Optional[dict] = None):
        self.users = users or {}

    def resolve(self, channel: str, external_id: str) -> Optional[ResolvedUser]:
        found = self.users.get(((channel or "").lower(), external_id))
        if found is None:
            return None
        if isinstance(found, ResolvedUser):
            return found
        return None


class PgIdentityStore(IdentityStore):
    """user_identities.user_id 对齐 user_permissions.staff_id，再取角色。"""

    def __init__(self, connect: Optional[Callable] = None, permissions=None):
        self._connect = connect
        self._permissions = permissions

    def resolve(self, channel: str, external_id: str) -> Optional[ResolvedUser]:
        channel = (channel or "").strip().lower()
        external_id = (external_id or "").strip()
        if not channel or not external_id or external_id == "unknown":
            return None
        row = self._lookup(channel, external_id)
        if not row:
            logger.info("未登记的渠道身份 channel=%s external_id=%s", channel, external_id)
            return None
        user_id = row["user_id"]
        permissions = self._permission_api()
        try:
            role = permissions.get_user_role(user_id)
        except Exception:
            logger.exception("读取角色失败 user_id=%s", user_id)
            return None
        if not role:
            logger.info("身份已登记但无有效权限 user_id=%s channel=%s", user_id, channel)
            return None
        display = row.get("display_name") or role.get("staff_name") or ""
        return ResolvedUser(
            user_id=user_id,
            display_name=display,
            role=role,
            external_id=external_id,
            channel=channel,
        )

    def _permission_api(self):
        if self._permissions is not None:
            return self._permissions
        from permission_manager import permission_manager

        return permission_manager

    def _lookup(self, channel: str, external_id: str) -> Optional[dict]:
        opener = self._connect or _default_connect
        conn = None
        try:
            conn = opener()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ui.user_id,
                           COALESCE(NULLIF(ui.display_name, ''), up.staff_name, '') AS display_name
                    FROM user_identities ui
                    LEFT JOIN user_permissions up ON up.staff_id = ui.user_id
                    WHERE ui.channel = %s AND ui.external_id = %s
                    """,
                    (channel, external_id),
                )
                found = cur.fetchone()
            if not found:
                return None
            return {"user_id": found[0], "display_name": found[1] or ""}
        except Exception:
            logger.exception(
                "身份表查询失败 channel=%s external_id=%s", channel, external_id
            )
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.exception("身份查询连接关闭失败")


def _default_connect():
    import psycopg2

    from config.agent_config import PG_DB, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER

    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        dbname=PG_DB,
        user=PG_USER,
        password=PG_PASSWORD,
    )
