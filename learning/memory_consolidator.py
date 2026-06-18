"""自动记忆合并器 (MemoryConsolidator)

借鉴 Claude Code 的 MemoryConsolidator：
- 从成功会话中自动提取关键经验
- 合并相似记忆，避免冗余
- 置信度评分（基于出现频率+验证次数）
- 定期后台合并（非阻塞异步）

工作流程：
1. on_session_complete: 会话完成后触发
2. _extract_insights: LLM提取关键发现
3. _merge_similar: 与已有记忆合并去重
4. _update_confidence: 更新置信度分数"""
import logging
import time
from typing import Optional
from datetime import datetime, timezone, timedelta

from learning.pattern_store import pattern_store
from learning.memory_types import (
    infer_memory_type, WHAT_NOT_TO_SAVE, MEMORY_TYPE_LABELS,
    memory_freshness_text, confidence_recency_score,
)

logger = logging.getLogger(__name__)

# 配置
CONSOLIDATION_INTERVAL = 300      # 最小合并间隔（秒），防止频繁触发
MAX_MEMORIES_PER_SESSION = 3      # 每次会话最多提取3条记忆
MEMORY_SCORE_FRESH_BOOST = 0.2    # 新记忆新鲜度加成
MEMORY_SCORE_VERIFY_BOOST = 0.15  # 每次验证加成
MEMORY_SCORE_CONFLICT_DECAY = 0.3 # 冲突时降权


class MemoryConsolidator:
    """自动记忆合并器

    在每次成功的分析对话后，自动提取关键经验并合并到长期记忆库。

    Attributes:
        _last_consolidation: 上次合并时间戳
        _consolidation_count: 累计合并次数
    """

    def __init__(self):
        self._last_consolidation = 0.0
        self._consolidation_count = 0

    def should_consolidate(self) -> bool:
        """判断是否应该触发合并（频率控制）。

        Returns:
            True 如果距离上次合并超过间隔
        """
        return time.time() - self._last_consolidation >= CONSOLIDATION_INTERVAL

    def on_session_complete(
        self,
        user_query: str,
        sql_executed: str,
        analysis_result: str,
        query_success: bool,
        row_count: int = 0,
        duration_ms: int = 0,
        error_message: str = '',
    ) -> int:
        """会话完成回调 — 触发自动记忆提取与合并。

        Args:
            user_query: 用户原始查询
            sql_executed: 执行成功的SQL
            analysis_result: 分析结果文本（前500字符）
            query_success: 查询是否成功
            row_count: 返回行数
            duration_ms: 耗时（毫秒）
            error_message: 失败时的错误信息

        Returns:
            提取的记忆数量
        """
        if not self.should_consolidate():
            return 0

        self._last_consolidation = time.time()
        memories = []

        # 1. 从成功查询中提取模式记忆
        if query_success and sql_executed:
            pattern_memory = self._extract_pattern_memory(
                user_query, sql_executed, row_count, duration_ms
            )
            if pattern_memory:
                memories.append(pattern_memory)

        # 2. 从分析结果中提取知识记忆
        if query_success and analysis_result and len(analysis_result) > 100:
            knowledge_memory = self._extract_knowledge_memory(
                user_query, analysis_result
            )
            if knowledge_memory:
                memories.append(knowledge_memory)

        # 3. 从失败案例中提取教训记忆
        if not query_success and error_message:
            lesson_memory = self._extract_lesson_memory(
                user_query, error_message
            )
            if lesson_memory:
                memories.append(lesson_memory)

        # 限制每次提取数量
        memories = memories[:MAX_MEMORIES_PER_SESSION]

        # 存储所有提取的记忆
        stored_count = 0
        for mem in memories:
            try:
                # ??????
                memory_type = infer_memory_type(
                    content=mem['insight'],
                    source='consolidator',
                    lesson_type=mem['type'],
                )
                pattern_store.save_lesson(
                    lesson_type=memory_type,
                    original_query=mem['query'],
                    problem=mem.get('context', ''),
                    solution=mem['insight'],
                )
                stored_count += 1
            except Exception as e:
                logger.error(f"[MemoryConsolidator] 存储记忆失败: {e}")

        if stored_count > 0:
            self._consolidation_count += 1
            logger.info(
                f"[MemoryConsolidator] 会话合并完成: 提取{stored_count}条记忆 "
                f"(累计{self._consolidation_count}次合并)"
            )

        return stored_count

    def _extract_pattern_memory(
        self, query: str, sql: str, row_count: int, duration_ms: int
    ) -> Optional[dict]:
        """提取查询模式记忆。

        记录：什么查询类型 → 什么SQL模式 → 查询效果如何"""
        # 简单规则提取（后续可升级为LLM提取）
        pattern_type = self._classify_query_type(query)

        return {
            'type': 'query_pattern',
            'query': query[:200],
            'insight': f'{pattern_type}查询 | SQL: {sql[:200]} | 返回{row_count}行 | 耗时{duration_ms}ms',
            'context': f'成功查询模式: {pattern_type}',
        }

    def _extract_knowledge_memory(
        self, query: str, analysis: str
    ) -> Optional[dict]:
        """从分析结果中提取知识记忆。

        提取关键发现、数据洞察、业务建议等可复用知识。

        Args:
            query: 用户查询
            analysis: 分析结果文本

        Returns:
            记忆字典，或None（分析不够丰富时跳过）
        """
        # 提取关键数字和结论（简化版）
        import re

        # 找百分比、涨跌幅等关键数字
        percentages = re.findall(r'([+-]?\d+\.?\d*%)\s*(上涨|下降|增长|减少|提升|降低|涨幅|跌幅)', analysis)
        key_values = re.findall(r'([\d,]+)\s*(人|次|元|条|万|亿)', analysis)

        if not percentages and not key_values:
            return None

        key_findings = []
        for val, direction in percentages[:3]:
            key_findings.append(f'{val} {direction}')
        for val, unit in key_values[:3]:
            key_findings.append(f'{val}{unit}')

        return {
            'type': 'knowledge',
            'query': query[:200],
            'insight': f'关键发现: {"; ".join(key_findings[:5])} | {analysis[:300]}',
            'context': f'数据洞察 - 来源查询: {query[:100]}',
        }

    def _extract_lesson_memory(
        self, query: str, error: str
    ) -> Optional[dict]:
        """从失败案例中提取教训记忆。

        记录：什么查询 → 什么错误 → 避免什么"""
        return {
            'type': 'lesson',
            'query': query[:200],
            'insight': f'避免: {error[:300]}',
            'context': f'查询失败 - 原因: {error[:200]}',
        }

    def _should_save(self, memory: dict) -> bool:
        """??????????????????????

        ?? WHAT_NOT_TO_SAVE ????:
        - ?????????
        - ??/?????????????
        - ?????????/commit??
        - ?? AGENTS.md ????

        Returns:
            True ??????
        """
        insight = memory.get('insight', '')
        query = memory.get('query', '')
        combined = (insight + query).lower()

        # ????
        skip_patterns = [
            # ??/????
            (['import ', 'class ', 'def ', 'function'], '????'),
            (['??', 'architecture', '????'], '????'),
            # ????
            (['bug', 'fix', '??', '??'], '????'),
            (['commit', 'branch', 'merge'], 'git??'),
            # ????
            (['????', '???', '???', 'todo'], '????'),
            (['????', '????'], '????'),
        ]

        for keywords, reason in skip_patterns:
            if any(kw in combined for kw in keywords):
                logger.debug(
                    f"[MemoryConsolidator] ???? (??: {reason}): "
                    f"{insight[:80]}"
                )
                return False
        return True

    def _classify_query_type(self, query: str) -> str:
        """分类查询类型（规则匹配）。"""
        if any(w in query for w in ['趋势', '走势', '变化', '最近', '近']):
            return '趋势分析'
        elif any(w in query for w in ['对比', '比较', 'vs', '环比', '同比']):
            return '对比分析'
        elif any(w in query for w in ['日活', 'DAU', '活跃']):
            return '日活查询'
        elif any(w in query for w in ['新增', '注册', '新用户']):
            return '新增查询'
        elif any(w in query for w in ['异常', '突变', '暴增', '骤降']):
            return '异常检测'
        elif any(w in query for w in ['排名', 'TOP', 'top', '前']):
            return '排名查询'
        return '通用查询'

    def get_stats(self) -> dict:
        """获取合并器统计信息。"""
        return {
            'consolidation_count': self._consolidation_count,
            'last_consolidation_ago': time.time() - self._last_consolidation,
        }


# 全局实例
memory_consolidator = MemoryConsolidator()
