"""PostgreSQL 工具 - 审计日志和经验知识库

提供审计日志记录、查询模式匹配和经验保存功能。
连接 PostgreSQL agent_experience 数据库。
"""
import logging
from typing import Optional
from datetime import datetime
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD

logger = logging.getLogger(__name__)


@contextmanager
def get_pg_connection():
    """获取PostgreSQL连接（上下文管理器）"""
    conn = None
    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_DB,
            user=PG_USER,
            password=PG_PASSWORD
        )
        yield conn
    except psycopg2.Error as e:
        logger.error(f"[PG] 连接失败: {e}")
        raise
    finally:
        if conn:
            conn.close()


def log_audit(
    user_id: str,
    query: str,
    response: str,
    duration_ms: int,
    tokens: int = 0,
    status: str = 'success',
    sql_executed: str = '',
    agent_path: str = 'plan→query→analysis'
) -> bool:
    """
    记录审计日志到 audit_logs 表。

    Args:
        user_id: 钉钉用户ID
        query: 用户原始查询
        response: 最终回复文本
        duration_ms: 处理耗时(毫秒)
        tokens: 消耗的token数量
        status: 处理状态 ('success'/'error'/'timeout')
        sql_executed: 实际执行的SQL
        agent_path: Agent调用路径

    Returns:
        是否记录成功
    """
    try:
        with get_pg_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_logs 
                    (user_id, query, response, duration_ms, tokens, status, sql_executed, agent_path, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    user_id, query, response[:5000], duration_ms,
                    tokens, status, sql_executed, agent_path,
                    datetime.now()
                ))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[PG] 审计日志写入失败: {e}")
        return False


def log_permission_event(
    staff_id: str,
    staff_name: str,
    role_id: str,
    action: str,
    original_sql: str = '',
    enforced_sql: str = '',
    denied_reason: str = '',
    tables_accessed: list = None,
    query_result_rows: int = 0,
    duration_ms: int = 0
) -> bool:
    """
    记录权限事件到 permission_audit_log 表。

    Args:
        staff_id: 用户钉钉staffId
        staff_name: 用户姓名
        role_id: 用户角色ID
        action: 'query'(正常)|'denied'(拒绝)|'error'(异常)
        original_sql: Plan Agent生成的原始SQL
        enforced_sql: 权限注入后的SQL
        denied_reason: 拒绝原因（仅action=denied时有值）
        tables_accessed: 涉及的表名列表
        query_result_rows: 返回行数
        duration_ms: 处理耗时

    Returns:
        是否记录成功
    """
    try:
        with get_pg_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO permission_audit_log
                    (staff_id, staff_name, role_id, action, original_sql, enforced_sql,
                     denied_reason, tables_accessed, query_result_rows, duration_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    staff_id, staff_name, role_id, action,
                    original_sql[:10000], enforced_sql[:10000],
                    denied_reason, tables_accessed or [],
                    query_result_rows, duration_ms
                ))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[PG] 权限审计写入失败: {e}")
        return False


# patterns/lessons 表的操作已统一到 learning.pattern_store 模块
# 请使用 from learning.pattern_store import pattern_store
#
# 历史上 pg_tool.py 曾经实现 get_similar_patterns / save_pattern / save_lesson，
# 但与 learning/pattern_store.py 对 patterns/lessons 表的字段假设不一致（schema 冲突），
# 已统一删除，唯一接口为 learning.pattern_store.pattern_store。
