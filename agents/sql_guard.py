"""每次执行前重写 SQL。自愈产生的新语句也要再过一遍，不能沿用上一次的放行结果。"""

from __future__ import annotations

from permission_manager import PermissionDenied, permission_manager


def prepare_sql(sql: str, user_role: dict) -> str:
    """按角色白名单和强制条件改写 SQL。没有角色或解析失败时拒绝。"""
    if not sql or not str(sql).strip():
        raise PermissionDenied("SQL为空")
    if not user_role:
        raise PermissionDenied("缺少角色，已拒绝执行")
    return permission_manager.enforce_sql(sql, user_role)
