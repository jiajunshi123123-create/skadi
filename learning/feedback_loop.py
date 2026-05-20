"""自学习循环 - 从成功查询中学习并更新知识库

自学习流程:
1. 查询成功后，提取查询模式（问题类型+表名+指标）
2. 将模式存入 PostgreSQL patterns 表
3. 将成功案例向量化写入 ChromaDB
4. SQL自愈成功时，记录为 lesson
5. 分析质量反馈时，记录改进建议
"""
import logging
import re
from typing import Optional

from learning.pattern_store import pattern_store
from tools.rag_tool import rag_tool

logger = logging.getLogger(__name__)


class FeedbackLoop:
    """
    自学习反馈循环。

    在主查询流程的关键节点被调用：
    - on_query_success: 查询成功后
    - on_sql_self_heal: SQL自愈成功后
    - on_analysis_feedback: 分析质量反馈时
    """

    def on_query_success(self, user_query: str, sql: str,
                         result: dict, analysis: str):
        """
        查询成功回调 — 提取并存储模式。

        Args:
            user_query: 用户原始问题
            sql: 执行的SQL
            result: 查询结果
            analysis: 生成的分析文本
        """
        # 1. 提取查询模式
        pattern = self._extract_pattern(user_query)

        # 2. 存入 PostgreSQL patterns 表
        try:
            pattern_store.save_pattern(
                query_pattern=pattern,
                sql_template=sql,
                analysis_template=analysis[:500] if analysis else None
            )
            logger.info(f"[FeedbackLoop] 模式已保存: {pattern}")
        except Exception as e:
            logger.error(f"[FeedbackLoop] 保存模式失败: {e}")

        # 3. 向量化写入 ChromaDB（作为历史案例）
        try:
            doc_id = f'case_{abs(hash(user_query)) % 100000:05d}'
            content = (
                f'历史查询案例:\n'
                f'问题: {user_query}\n'
                f'SQL: {sql}\n'
                f'分析摘要: {analysis[:300] if analysis else "无"}'
            )
            rag_tool.add_document(
                doc_id=doc_id,
                content=content,
                metadata={
                    'type': 'historical_case',
                    'pattern': pattern
                }
            )
            logger.info(f"[FeedbackLoop] 案例已写入知识库: {doc_id}")
        except Exception as e:
            logger.error(f"[FeedbackLoop] 写入知识库失败: {e}")

    def on_sql_self_heal(self, original_sql: str, error: str,
                         fixed_sql: str, user_query: str):
        """
        SQL自愈成功回调 — 记录为经验教训。

        Args:
            original_sql: 出错的原始SQL
            error: 错误信息
            fixed_sql: 修正后的SQL
            user_query: 用户原始查询
        """
        try:
            pattern_store.save_lesson(
                lesson_type='sql_fix',
                original_query=user_query,
                problem=f'SQL错误: {error}\n原始SQL: {original_sql}',
                solution=f'修正后SQL: {fixed_sql}'
            )
            logger.info(f"[FeedbackLoop] SQL自愈经验已记录")
        except Exception as e:
            logger.error(f"[FeedbackLoop] 保存SQL自愈lesson失败: {e}")

    def on_analysis_feedback(self, user_query: str, analysis: str,
                             feedback: str):
        """
        分析质量反馈回调（预留接口，未来可对接用户评价）。

        Args:
            user_query: 用户查询
            analysis: 原始分析文本
            feedback: 反馈/改进建议
        """
        try:
            pattern_store.save_lesson(
                lesson_type='analysis_improvement',
                original_query=user_query,
                problem=f'原分析: {analysis[:200]}',
                solution=f'改进建议: {feedback}'
            )
            logger.info(f"[FeedbackLoop] 分析反馈已记录")
        except Exception as e:
            logger.error(f"[FeedbackLoop] 保存分析反馈失败: {e}")

    def get_context_for_query(self, user_query: str) -> dict:
        """
        为新查询获取上下文信息（从知识库和模式库中检索）。

        Args:
            user_query: 用户查询文本

        Returns:
            包含相关知识和历史模式的上下文字典
        """
        context = {
            'rag_results': [],
            'similar_patterns': [],
            'recent_lessons': []
        }

        # 从 ChromaDB 检索相关知识
        try:
            context['rag_results'] = rag_tool.search(user_query, n_results=3)
        except Exception as e:
            logger.error(f"[FeedbackLoop] RAG检索失败: {e}")

        # 从 patterns 表查找相似模式
        try:
            context['similar_patterns'] = pattern_store.find_similar_pattern(user_query)
        except Exception as e:
            logger.error(f"[FeedbackLoop] 模式查找失败: {e}")

        # 获取最近的经验教训
        try:
            context['recent_lessons'] = pattern_store.get_recent_lessons(limit=5)
        except Exception as e:
            logger.error(f"[FeedbackLoop] 获取教训失败: {e}")

        return context

    def _extract_pattern(self, query: str) -> str:
        """
        从用户查询中提取模式标识（简化版，后续可用LLM增强）。

        Args:
            query: 用户原始查询

        Returns:
            模式标识字符串（如"单日查询+日活"）
        """
        keywords = []

        # 时间模式
        if any(w in query for w in ['昨天', '昨日', '前天']):
            keywords.append('单日查询')
        elif any(w in query for w in ['最近', '近', '趋势', '走势', '变化']):
            keywords.append('趋势查询')
        elif any(w in query for w in ['对比', '比较', 'vs', '环比', '同比']):
            keywords.append('对比查询')
        elif any(w in query for w in ['今天', '今日']):
            keywords.append('单日查询')

        # 指标模式
        if any(w in query for w in ['日活', '活跃', 'DAU', 'dau']):
            keywords.append('日活')
        if any(w in query for w in ['新增', '注册', '新用户']):
            keywords.append('新增用户')
        if any(w in query for w in ['图书', '答疑']):
            keywords.append('图书答疑')
        if any(w in query for w in ['核心', '行为', '做题', '阅读']):
            keywords.append('核心行为')
        if any(w in query for w in ['渠道', '来源']):
            keywords.append('渠道分析')

        return '+'.join(keywords) if keywords else '通用查询'


# 全局实例
feedback_loop = FeedbackLoop()
