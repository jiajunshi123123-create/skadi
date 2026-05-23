"""
PostgreSQL 会话存储模块
用于多轮对话场景下的会话上下文持久化。
dingtalk_bot.py 调用本模块保存/读取/删除用户会话。

建表 DDL:
    CREATE TABLE IF NOT EXISTS sessions (
        user_id     VARCHAR(128) PRIMARY KEY,
        user_name   VARCHAR(256),
        plan        JSONB,
        question    TEXT,
        created_at  TIMESTAMP DEFAULT NOW(),
        updated_at  TIMESTAMP DEFAULT NOW()
    );
"""
import json
import logging
from typing import Optional, Tuple

import psycopg2
from psycopg2.extras import Json

from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD

logger = logging.getLogger(__name__)

# PG 连接配置（从 config.agent_config 统一管理）
PG_CONFIG = {
    "host": PG_HOST,
    "port": PG_PORT,
    "dbname": PG_DB,
    "user": PG_USER,
    "password": PG_PASSWORD,
}


def _get_conn():
    """获取 PG 连接"""
    try:
        return psycopg2.connect(**PG_CONFIG)
    except psycopg2.Error as e:
        logger.error(f"[SessionStore] PG 连接失败: {e}")
        raise


def save_session(user_id: str, user_name: str, plan: dict, question: str) -> None:
    """
    保存或更新用户会话。
    使用 UPSERT (ON CONFLICT) 策略：同一 user_id 只保留最新一条。

    参数:
        user_id: 用户唯一标识（如 IM sender_id）
        user_name: 用户昵称
        plan: Plan Agent 输出的结构化计划 (dict)
        question: 用户原始问题
    """
    sql = """
        INSERT INTO sessions (user_id, user_name, plan, question, created_at, updated_at)
        VALUES (%s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            user_name = EXCLUDED.user_name,
            plan = EXCLUDED.plan,
            question = EXCLUDED.question,
            updated_at = NOW()
    """
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, user_name, Json(plan), question))
        conn.commit()
        logger.info(f"[SessionStore] 保存会话: user={user_id}, question={question[:30]}...")
    except psycopg2.Error as e:
        logger.error(f"[SessionStore] 保存会话失败: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def get_session(user_id: str) -> Optional[Tuple[dict, str]]:
    """
    获取用户最近的会话数据。

    参数:
        user_id: 用户唯一标识
    返回:
        (plan_dict, question_str) 或 None（无会话时）
    """
    sql = "SELECT plan, question FROM sessions WHERE user_id = %s"
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
            row = cur.fetchone()
            if row:
                plan = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
                question = row[1] or ''
                return (plan, question)
            return None
    except psycopg2.Error as e:
        logger.error(f"[SessionStore] 获取会话失败: {e}")
        return None
    finally:
        if conn:
            conn.close()


def delete_session(user_id: str) -> None:
    """
    删除用户会话（用户发送"重置/清除记忆"时触发）。

    参数:
        user_id: 用户唯一标识
    """
    sql = "DELETE FROM sessions WHERE user_id = %s"
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, (user_id,))
        conn.commit()
        logger.info(f"[SessionStore] 删除会话: user={user_id}")
    except psycopg2.Error as e:
        logger.error(f"[SessionStore] 删除会话失败: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
