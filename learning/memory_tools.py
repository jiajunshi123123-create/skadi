# -*- coding: utf-8 -*-
"""记忆操作工具化

将记忆的保存/搜索/删除操作注册为 ToolRegistry 中的工具，
让 Plan/Query/Analysis Agent 可以主动调用记忆系统。

借鉴 Claude Code 的 MemorySave / MemoryDelete / MemorySearch 工具设计。
"""

from __future__ import annotations
import logging
from typing import Any

from utils.tool_registry import ToolDef
from learning.pattern_store import pattern_store
from learning.memory_types import (
    infer_memory_type, rank_memories_by_score,
    memory_freshness_text, build_memory_context, MEMORY_TYPE_LABELS,
)

logger = logging.getLogger(__name__)


# ================================================================
# 工具实现
# ================================================================

def _memory_save_tool(params: dict, config: dict = None) -> str:
    """保存一条记忆。

    params:
        memory_type: 记忆类型 (sql_pattern/sql_fix/feedback/project/reference)
        query: 关联的用户查询
        content: 记忆内容
        context: 上下文 (可选)
    """
    memory_type = params.get("memory_type", "sql_pattern")
    query = params.get("query", "")
    content = params.get("content", "")
    context = params.get("context", query[:200])

    if not content:
        return "错误: 记忆内容不能为空"

    # 推断类型（如果未明确指定）
    if memory_type == "sql_pattern" and content:
        memory_type = infer_memory_type(content=content, lesson_type=memory_type)

    type_label = MEMORY_TYPE_LABELS.get(memory_type, memory_type)
    try:
        pattern_store.save_lesson(
            lesson_type=memory_type,
            original_query=query[:300],
            problem=context[:300],
            solution=content[:2000],
        )
        return f"✅ 记忆已保存 [{type_label}]: {content[:100]}..."
    except Exception as e:
        logger.error(f"[MemoryTool] 保存失败: {e}")
        return f"❌ 保存记忆失败: {e}"


def _memory_search_tool(params: dict, config: dict = None) -> str:
    """搜索相关记忆。

    params:
        query: 搜索关键词
        limit: 返回条数 (默认5)
        min_confidence: 最低置信度 (默认0.3)
    """
    query = params.get("query", "")
    limit = int(params.get("limit", 5))
    min_confidence = float(params.get("min_confidence", 0.3))

    if not query:
        return "错误: 搜索关键词不能为空"

    try:
        patterns = pattern_store.get_patterns_with_confidence(query, limit=limit * 2)
        if not patterns:
            return f"未找到与 '{query}' 相关的记忆。"

        # 过滤低置信度
        filtered = [p for p in patterns if p.get("confidence", 0) >= min_confidence]

        if not filtered:
            return (
                f"找到 {len(patterns)} 条相关记忆，但置信度均低于 {min_confidence}。\n"
                f"最高置信度: {patterns[0].get('confidence', 0) if patterns else 0}"
            )

        # 构建输出
        lines = [f"🔍 搜索 '{query}' 找到 {len(filtered)} 条相关记忆:"]
        for i, p in enumerate(filtered[:limit]):
            conf = p.get("confidence", 0)
            conf_star = "●" if conf >= 0.7 else "○" if conf >= 0.4 else "◦"
            sql = p.get("common_sql", p.get("sql_template", ""))
            pattern_name = p.get("query_pattern", p.get("name", f"模式{i+1}"))
            freshness = p.get("_freshness_text", "")

            lines.append(f"\n{conf_star} [{conf:.0%}] {pattern_name}")
            if sql:
                lines.append(f"   SQL: {sql[:200]}")
            if freshness:
                lines.append(f"   {freshness}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[MemoryTool] 搜索失败: {e}")
        return f"❌ 搜索记忆失败: {e}"


def _memory_stats_tool(params: dict, config: dict = None) -> str:
    """查看记忆库统计信息"""
    try:
        stats = pattern_store.get_pattern_stats()
        return (
            f"📊 记忆库统计:\n"
            f"  SQL模式: {stats.get('patterns', 0)} 条\n"
            f"  经验教训: {stats.get('lessons', 0)} 条"
        )
    except Exception as e:
        return f"❌ 获取统计失败: {e}"


# ================================================================
# 工具定义
# ================================================================

MEMORY_SAVE_TOOL = ToolDef(
    name="memory_save",
    description="保存一条可复用的记忆（SQL模式/经验教训/业务知识）。Agent可在成功执行后调用以积累知识。",
    func=_memory_save_tool,
    read_only=False,
    category="memory",
    tags=["save", "learn", "memory"],
)

MEMORY_SEARCH_TOOL = ToolDef(
    name="memory_search",
    description="搜索历史记忆库，查找相关的SQL模式、经验教训或业务知识。用于计划阶段获取历史经验。",
    func=_memory_search_tool,
    read_only=True,
    category="memory",
    tags=["search", "recall", "memory"],
)

MEMORY_STATS_TOOL = ToolDef(
    name="memory_stats",
    description="查看记忆库的统计信息（模式数/教训数）。",
    func=_memory_stats_tool,
    read_only=True,
    category="memory",
    tags=["stats", "memory"],
)

# 所有记忆工具的列表
MEMORY_TOOLS = [MEMORY_SAVE_TOOL, MEMORY_SEARCH_TOOL, MEMORY_STATS_TOOL]


# ================================================================
# 注册到全局 ToolRegistry
# ================================================================

def register_memory_tools():
    """将所有记忆工具注册到全局 ToolRegistry"""
    from utils.tool_registry import ToolRegistry
    registry = ToolRegistry()
    for tool in MEMORY_TOOLS:
        registry.register(tool)
    logger.info(f"[MemoryTools] 已注册 {len(MEMORY_TOOLS)} 个记忆工具")
    return len(MEMORY_TOOLS)


# 自动注册（模块导入时）
_auto_registered = False


def ensure_registered():
    global _auto_registered
    if not _auto_registered:
        try:
            register_memory_tools()
            _auto_registered = True
        except Exception as e:
            logger.warning(f"[MemoryTools] 自动注册失败: {e}")
