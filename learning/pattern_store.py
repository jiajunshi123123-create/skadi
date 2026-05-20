"""模式存储 - 从成功查询中提取和存储可复用模式

与 PostgreSQL patterns/lessons 表交互，
记录成功的查询模式和经验教训，供后续查询时参考。
"""
import logging
from typing import Optional
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD

logger = logging.getLogger(__name__)


class PatternStore:
    """模式识别与存储"""

    def __init__(self):
        self.conn_params = {
            'host': PG_HOST,
            'port': PG_PORT,
            'dbname': PG_DB,
            'user': PG_USER,
            'password': PG_PASSWORD
        }

    @contextmanager
    def _get_conn(self):
        """获取数据库连接（上下文管理器）"""
        conn = None
        try:
            conn = psycopg2.connect(**self.conn_params)
            yield conn
        finally:
            if conn:
                conn.close()

    def save_pattern(self, query_pattern: str, sql_template: str,
                     analysis_template: str = None):
        """
        保存查询模式。如果已存在相同模式则更新使用计数。

        Args:
            query_pattern: 查询模式描述（如"单日查询+日活"）
            sql_template: 对应的SQL
            analysis_template: 对应的分析模板（可选）
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # 检查是否已存在相似模式
                    cur.execute(
                        "SELECT id, success_count FROM patterns WHERE query_pattern = %s",
                        (query_pattern,)
                    )
                    existing = cur.fetchone()
                    if existing:
                        cur.execute(
                            "UPDATE patterns SET success_count = success_count + 1, "
                            "last_used = NOW() WHERE id = %s",
                            (existing[0],)
                        )
                    else:
                        cur.execute(
                            "INSERT INTO patterns (query_pattern, sql_template, analysis_template) "
                            "VALUES (%s, %s, %s)",
                            (query_pattern, sql_template, analysis_template)
                        )
                conn.commit()
        except Exception as e:
            logger.error(f"[PatternStore] 保存模式失败: {e}")

    def find_similar_pattern(self, query: str, limit: int = 3) -> list:
        """
        查找相似的查询模式（基于关键词ILIKE匹配）。

        Args:
            query: 用户查询文本
            limit: 返回结果数量上限

        Returns:
            匹配的模式列表
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # 提取关键词进行模糊匹配
                    keywords = [
                        kw for kw in query.replace('？', '').replace('?', '').split()
                        if len(kw) > 1
                    ]
                    if not keywords:
                        return []

                    conditions = ' OR '.join(
                        ['query_pattern ILIKE %s'] * len(keywords)
                    )
                    params = [f'%{kw}%' for kw in keywords]

                    cur.execute(
                        f"SELECT query_pattern, sql_template, analysis_template, success_count "
                        f"FROM patterns WHERE {conditions} "
                        f"ORDER BY success_count DESC LIMIT %s",
                        params + [limit]
                    )
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"[PatternStore] 查找模式失败: {e}")
            return []

    def save_lesson(self, lesson_type: str, original_query: str,
                    problem: str, solution: str):
        """
        保存经验教训。

        Args:
            lesson_type: 教训类别 ('sql_fix', 'analysis_improvement', 'query_pattern')
            original_query: 原始用户查询
            problem: 问题描述
            solution: 解决方案
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO lessons (lesson_type, original_query, problem, solution) "
                        "VALUES (%s, %s, %s, %s)",
                        (lesson_type, original_query, problem, solution)
                    )
                conn.commit()
        except Exception as e:
            logger.error(f"[PatternStore] 保存教训失败: {e}")

    def get_recent_lessons(self, lesson_type: str = None, limit: int = 10) -> list:
        """
        获取最近的经验教训。

        Args:
            lesson_type: 过滤类别（None则返回所有类别）
            limit: 返回数量上限

        Returns:
            经验教训列表
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    if lesson_type:
                        cur.execute(
                            "SELECT lesson_type, original_query, problem, solution, created_at "
                            "FROM lessons WHERE lesson_type = %s "
                            "ORDER BY created_at DESC LIMIT %s",
                            (lesson_type, limit)
                        )
                    else:
                        cur.execute(
                            "SELECT lesson_type, original_query, problem, solution, created_at "
                            "FROM lessons ORDER BY created_at DESC LIMIT %s",
                            (limit,)
                        )
                    return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"[PatternStore] 获取教训失败: {e}")
            return []

    def get_pattern_stats(self) -> dict:
        """获取模式库统计信息"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM patterns")
                    pattern_count = cur.fetchone()[0]
                    cur.execute("SELECT COUNT(*) FROM lessons")
                    lesson_count = cur.fetchone()[0]
                    return {
                        'patterns': pattern_count,
                        'lessons': lesson_count
                    }
        except Exception as e:
            logger.error(f"[PatternStore] 获取统计失败: {e}")
            return {'patterns': 0, 'lessons': 0}


# 全局实例
pattern_store = PatternStore()
