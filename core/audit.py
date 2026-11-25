"""审计日志写入。失败时记录日志，不再静默吞掉。"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("skadi.audit")


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


def write_audit_log(
    user_id: str,
    query: str,
    response: str,
    duration_ms: int = 0,
    tokens: int = 0,
    status: str = "success",
    user_name: str = "",
    source: str = "langgraph-orchestrator",
    sql_executed: str = "",
    agent_path: str = "",
    action: str = "agent_call",
    connect: Optional[Callable] = None,
) -> bool:
    """写入 audit_logs。连接或 SQL 失败时打错误日志并返回 False。"""
    opener = connect or _default_connect
    conn = None
    try:
        conn = opener()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs
                    (user_id, user_name, query, response, source, duration_ms,
                     tokens, status, sql_executed, agent_path, action)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    (user_id or "")[:128],
                    (user_name or "")[:256],
                    (query or "")[:2000],
                    (response or "")[:5000],
                    (source or "")[:64],
                    int(duration_ms or 0),
                    int(tokens or 0),
                    (status or "")[:32],
                    (sql_executed or "")[:10000],
                    (agent_path or "")[:128],
                    (action or "agent_call")[:32],
                ),
            )
        conn.commit()
        return True
    except Exception:
        logger.exception(
            "审计日志写入失败 user_id=%s status=%s source=%s",
            user_id,
            status,
            source,
        )
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logger.exception("审计日志回滚失败 user_id=%s", user_id)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("审计日志连接关闭失败 user_id=%s", user_id)
