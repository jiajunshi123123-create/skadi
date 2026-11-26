"""Web API 密钥到数据角色。密钥能通过认证但没有角色映射时，查询会被拒绝。"""

from __future__ import annotations

import logging
import os
from typing import Optional

from utils.web_auth import _keys_match, _presented_user_key

logger = logging.getLogger("skadi.web_roles")


def configured_role_pairs() -> list:
    """WEB_API_KEY_ROLES=密钥=role_id，多项用逗号分隔。"""
    pairs = []
    for part in os.getenv("WEB_API_KEY_ROLES", "").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, role_id = item.split("=", 1)
        key = key.strip()
        role_id = role_id.strip()
        if key and role_id:
            pairs.append((key, role_id))
    return pairs


def match_role_id(presented: str) -> Optional[str]:
    if not presented:
        return None
    matched = None
    for key, role_id in configured_role_pairs():
        if _keys_match(presented, key):
            matched = role_id
    return matched


def resolve_web_role(headers, loader=None) -> Optional[dict]:
    """返回角色字典。未配置映射或角色不存在时返回 None。"""
    role_id = match_role_id(_presented_user_key(headers))
    if not role_id:
        return None
    load = loader or _load_role
    try:
        role = load(role_id)
    except Exception:
        logger.exception("读取 Web 角色失败 role_id=%s", role_id)
        return None
    return role or None


def _load_role(role_id: str) -> Optional[dict]:
    from permission_manager import permission_manager

    return permission_manager.get_role_definition(role_id)
