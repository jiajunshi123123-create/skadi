"""每次执行前重写 SQL。自愈产生的新语句也要再过一遍，不能沿用上一次的放行结果。"""

from __future__ import annotations

import logging

from permission_manager import PermissionDenied, permission_manager

logger = logging.getLogger("skadi.sql_guard")


def prepare_sql(sql: str, user_role: dict) -> str:
    """按角色白名单和强制条件改写 SQL。没有角色或解析失败时拒绝。"""
    if not sql or not str(sql).strip():
        raise PermissionDenied("SQL为空")
    if not user_role:
        raise PermissionDenied("缺少角色，已拒绝执行")
    return permission_manager.enforce_sql(sql, user_role)


def log_enforcement(
    user_role: dict,
    action: str,
    original_sql: str,
    enforced_sql: str = None,
    denied_reason: str = None,
) -> None:
    """把放行或拒绝写入 permission_audit_log。

    管理员的放行不记，和权限检查还在 query_node 时一致。
    拒绝（包括没有角色）仍然记录。审计写失败不影响查询。
    """
    role = user_role or {}
    if action == "query" and role.get("role_id") in (None, "", "admin"):
        return
    try:
        permission_manager.log_permission_audit(
            staff_id=role.get("staff_id") or "",
            staff_name=role.get("staff_name") or "",
            role_id=role.get("role_id") or "",
            action=action,
            original_sql=original_sql,
            enforced_sql=enforced_sql,
            denied_reason=denied_reason,
            tables_accessed=permission_manager.extract_tables_from_sql(original_sql or ""),
        )
    except Exception:
        logger.exception("权限审计写入失败 action=%s", action)
