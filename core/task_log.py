"""把完成的查询记入 tasks 表。失败只记日志，不改写给用户的回复。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Callable, Optional

logger = logging.getLogger("skadi.task_log")


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


def save_task(
    user_id: str,
    user_name: str,
    prompt: str,
    plan: dict,
    results: list,
    connect: Optional[Callable] = None,
) -> Optional[str]:
    """写入一条已完成任务。计划与结果按文本保存，与历史插入语句一致。"""
    opener = connect or _default_connect
    task_id = str(uuid.uuid4())[:8]
    plan = plan or {}
    results = results or []
    tags = [str(plan.get("type") or plan.get("intent") or "other")]
    summary = (
        f"{len(results)} steps, "
        f"{sum(int(item.get('total_rows') or 0) for item in results)} total rows"
    )
    conn = None
    try:
        conn = opener()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks
                    (task_id, user_id, user_name, user_question, plan_json, execution,
                     result_summary, tags, status, created_at, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'completed', NOW(), NOW())
                """,
                (
                    task_id,
                    user_id,
                    user_name,
                    prompt,
                    json.dumps(plan, ensure_ascii=False, default=str),
                    json.dumps(results, ensure_ascii=False, default=str),
                    summary,
                    tags,
                ),
            )
        conn.commit()
        logger.info("任务已写入 task_id=%s user_id=%s", task_id, user_id)
        return task_id
    except Exception:
        logger.exception("任务写入失败 user_id=%s", user_id)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                logger.exception("任务写入回滚失败 user_id=%s", user_id)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("任务写入连接关闭失败 user_id=%s", user_id)
