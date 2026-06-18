"""模式存储 - 从成功查询中提取和存储可复用模式

与 PostgreSQL patterns/lessons 表交互，
记录成功的查询模式和经验教训，供后续查询时参考。

P1-2 增强: 多层级记忆系统
- 置信度评分 (confidence scoring)
- 冲突检测 (conflict detection)  
- 时效性排序 (recency ranking)
"""
import logging
import re
from typing import Optional
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
from learning.memory_types import (
    confidence_recency_score, memory_freshness_text,
    rank_memories_by_score, infer_memory_type,
)

logger = logging.getLogger(__name__)

# ============================================================
# P1-2: 记忆置信度与冲突检测配置
# ============================================================

# 置信度评分参数
CONFIDENCE_BASE = 0.3              # 基础置信度
CONFIDENCE_EACH_SUCCESS = 0.15     # 每次成功增加
CONFIDENCE_FRESH_MULTIPLIER = 1.2  # 新鲜记忆加权
CONFIDENCE_STALE_THRESHOLD_DAYS = 30  # 超过30天为陈旧
CONFIDENCE_STALE_DECAY = 0.8       # 陈旧记忆衰减系数

# 冲突检测参数
CONFLICT_SIMILARITY_THRESHOLD = 0.7  # 相似度阈值（高于此值视为冲突候选）
CONFLICT_MIN_CONFIDENCE_DIFF = 0.3   # 最小置信度差异（低于此值视为真正冲突）


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
        保存经验教训（带去重）。

        Args:
            lesson_type: 教训类别 ('sql_fix', 'analysis_improvement', 'query_pattern')
            original_query: 原始用户查询
            problem: 问题描述
            solution: 解决方案
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # Check for existing similar lesson to avoid duplicates
                    cur.execute(
                        "SELECT id, created_at FROM lessons "
                        "WHERE lesson_type = %s AND original_query = %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (lesson_type, original_query)
                    )
                    existing = cur.fetchone()
                    if existing:
                        # Update existing record instead of inserting duplicate
                        cur.execute(
                            "UPDATE lessons SET problem = %s, solution = %s, updated_at = NOW() "
                            "WHERE id = %s",
                            (problem, solution, existing[0])
                        )
                        logger.info(f"[PatternStore] Updated existing lesson (id={existing[0]}, age=N/A)")
                    else:
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

    # ============================================================
    # P1-2: 多层级记忆 — 置信度评分
    # ============================================================

    def calculate_confidence(
        self,
        success_count: int,
        days_since_created: float,
        days_since_last_used: float,
        has_analysis: bool = False,
    ) -> float:
        """计算模式置信度评分 (0.0 ~ 1.0)。

        置信度由以下因素综合决定：
        - 基础分: CONFIDENCE_BASE (0.3)
        - 使用次数: 每次成功 +CONFIDENCE_EACH_SUCCESS (上限0.9)
        - 时效性: 新鲜记忆乘1.2，陈旧记忆乘0.8
        - 分析质量: 有分析模板的 +0.1

        Args:
            success_count: 成功使用次数
            days_since_created: 创建至今的天数
            days_since_last_used: 最近使用距今的天数
            has_analysis: 是否有分析模板

        Returns:
            置信度分数 (0.0~1.0)
        """
        # 基础置信度 + 使用次数加成
        confidence = CONFIDENCE_BASE + min(success_count * CONFIDENCE_EACH_SUCCESS, 0.6)

        # 新鲜度调整
        if days_since_created < 7:
            confidence *= CONFIDENCE_FRESH_MULTIPLIER
        elif days_since_last_used > CONFIDENCE_STALE_THRESHOLD_DAYS:
            confidence *= CONFIDENCE_STALE_DECAY

        # 分析质量加成
        if has_analysis:
            confidence += 0.1

        return min(confidence, 1.0)

    def get_patterns_with_confidence(
        self, query: str, limit: int = 5
    ) -> list:
        """查找相似模式并附上置信度评分。

        相比 find_similar_pattern，返回结果包含：
        - confidence: 置信度评分
        - freshness: 时效性（天）
        - maturity: 成熟度（success_count加权）

        Args:
            query: 用户查询文本
            limit: 返回数量

        Returns:
            带置信度的模式列表，按置信度降序排列
        """
        patterns = self.find_similar_pattern(query, limit * 2)  # 获取更多候选

        if not patterns:
            return []

        now = datetime.now(timezone.utc)
        scored = []

        for p in patterns:
            success_count = p.get('success_count', 0)
            # 估算天数（PG created_at 可能为None）
            created_at = p.get('created_at')
            last_used = p.get('last_used')

            days_since_created = 0
            days_since_last_used = 0

            if created_at:
                days_since_created = (now - created_at).total_seconds() / 86400
            if last_used:
                days_since_last_used = (now - last_used).total_seconds() / 86400

            confidence = self.calculate_confidence(
                success_count=success_count,
                days_since_created=days_since_created,
                days_since_last_used=days_since_last_used,
                has_analysis=bool(p.get('analysis_template')),
            )

            scored.append({
                **p,
                'confidence': round(confidence, 3),
                'freshness_days': round(days_since_last_used, 1),
                'maturity': round(min(success_count * 0.1, 1.0), 2),
            })

        # 按置信度降序排列
        scored.sort(key=lambda x: x['confidence'], reverse=True)
        return scored[:limit]

    # ============================================================
    # P1-2: 多层级记忆 — 冲突检测
    # ============================================================

    def detect_conflicts(self, new_pattern: str, new_sql: str) -> list:
        """检测新SQL与已有模式之间的潜在冲突。

        当两个模式关键词相似但SQL结构不同时，可能存在口径冲突。
        例如：
        - 模式A: "单日查询+日活" → SELECT FROM dws_biz_dayi_user_login_daily_stats
        - 模式B: "单日查询+日活" → SELECT FROM qb_event_log  (冲突!)

        Args:
            new_pattern: 新模式关键词
            new_sql: 新模式SQL

        Returns:
            冲突列表 [{pattern_id, conflict_type, description}]
        """
        conflicts = []

        try:
            # 1. 查找相同模式的不同SQL
            existing = self.find_similar_pattern(new_pattern, limit=10)

            for ex in existing:
                existing_sql = ex.get('sql_template', '')

                # 提取表名进行比较
                new_tables = self._extract_table_names(new_sql)
                existing_tables = self._extract_table_names(existing_sql)

                # 计算 SQL 相似度
                similarity = self._calculate_sql_similarity(new_sql, existing_sql)

                # 表不同 → 表级冲突
                if new_tables and existing_tables and new_tables != existing_tables:
                    conflicts.append({
                        'pattern_id': ex.get('id', 'unknown'),
                        'pattern_text': ex.get('query_pattern', ''),
                        'conflict_type': 'table_mismatch',
                        'description': (
                            f'相同查询模式使用了不同表: '
                            f'{new_tables} vs {existing_tables}'
                        ),
                        'severity': 'high',
                    })
                # SQL相似但结构不同 → 结构冲突
                elif similarity > CONFLICT_SIMILARITY_THRESHOLD and similarity < 0.95:
                    conflicts.append({
                        'pattern_id': ex.get('id', 'unknown'),
                        'pattern_text': ex.get('query_pattern', ''),
                        'conflict_type': 'structure_divergence',
                        'description': (
                            f'相似查询模式下SQL结构有差异 '
                            f'(相似度: {similarity:.2f})'
                        ),
                        'severity': 'medium',
                    })

        except Exception as e:
            logger.error(f"[PatternStore] 冲突检测失败: {e}")

        return conflicts

    def _extract_table_names(self, sql: str) -> set:
        """从SQL中提取表名集合。"""
        # 匹配 FROM 和 JOIN 后的表名
        sql_clean = ' '.join(re.sub(r'--[^\n]*', '', sql).split())
        pattern = r'\b(?:FROM|JOIN)\s+([`"]?[\w]+[`"]?(?:\.[`"]?[\w]+[`"]?)?)'
        matches = re.findall(pattern, sql_clean, re.IGNORECASE)
        return {m.replace('`', '').replace('"', '') for m in matches}

    def _calculate_sql_similarity(self, sql1: str, sql2: str) -> float:
        """计算两条SQL的相似度（简化版 Jaccard 相似度）。

        基于SQL关键词（SELECT/FROM/WHERE/GROUP BY等）
        和表名的重合程度计算。

        Returns:
            0.0~1.0 之间的相似度
        """
        def tokenize(s: str) -> set:
            """将SQL分词为关键词集合"""
            # 提取SQL关键词（大写标准化）
            keywords = set(re.findall(
                r'\b(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|JOIN|UNION)\b',
                s, re.IGNORECASE
            ))
            # 提取表名
            tables = self._extract_table_names(s)
            # 提取函数名（聚合函数等）
            funcs = set(re.findall(
                r'\b(COUNT|SUM|AVG|MAX|MIN|DATE_FORMAT|DATE_TRUNC|CAST|COALESCE)\b',
                s, re.IGNORECASE
            ))
            return keywords | tables | funcs

        tokens1 = tokenize(sql1)
        tokens2 = tokenize(sql2)

        if not tokens1 or not tokens2:
            return 0.0

        intersection = tokens1 & tokens2
        union = tokens1 | tokens2

        return len(intersection) / len(union) if union else 0.0

    # ============================================================
    # P1-2: 多层级记忆 — 时效性排序
    # ============================================================

    def get_recent_lessons_ranked(
        self, lesson_type: str = None, limit: int = 10
    ) -> list:
        """获取经验教训并按多维排序（时效性+成功率+置信度）。

        排序优先级:
        1. 最近7天内的 +10分
        2. success_count > 5 的 +5分
        3. 有 analysis_template 的 +3分

        Args:
            lesson_type: 过滤类型
            limit: 返回数量

        Returns:
            排序后的经验教训列表
        """
        lessons = self.get_recent_lessons(lesson_type, limit * 2)

        if not lessons:
            return []

        now = datetime.now(timezone.utc)
        scored = []

        for lesson in lessons:
            score = 0
            created_at = lesson.get('created_at')

            # 时效性评分
            if created_at:
                days_ago = (now - created_at).total_seconds() / 86400
                if days_ago < 7:
                    score += 10
                elif days_ago < 30:
                    score += 5
                else:
                    score += 1

            scored.append({**lesson, '_rank_score': score})

        scored.sort(key=lambda x: x['_rank_score'], reverse=True)
        return scored[:limit]

    def find_best_pattern(
        self, query: str, min_confidence: float = 0.3
    ) -> Optional[dict]:
        """查找最佳匹配模式（综合置信度+时效性）。

        这是缓存快速通道的首选调用方法。
        在编排器 plan_node 中替换原始的 find_similar_pattern。

        Args:
            query: 用户查询文本
            min_confidence: 最低置信度阈值

        Returns:
            最佳匹配模式，或None（无合格匹配时）
        """
        candidates = self.get_patterns_with_confidence(query, limit=3)

        for candidate in candidates:
            # 检查成熟度：success_count >= 3 且 confidence >= min
            if (candidate.get('success_count', 0) >= 3 and
                    candidate.get('confidence', 0) >= min_confidence):
                return candidate

        return None


# 全局实例
pattern_store = PatternStore()
