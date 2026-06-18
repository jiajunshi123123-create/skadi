# -*- coding: utf-8 -*-
"""记忆类型分类与辅助函数

借鉴 Claude Code 的记忆系统设计:
- 4种记忆类型 (user/feedback/project/reference)
- 新鲜度过期警告
- 置信度×时效性排名
- 不应存储的记忆过滤
"""

from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from typing import Optional


# ================================================================
# 记忆类型分类
# ================================================================

MEMORY_TYPES = ["user", "feedback", "project", "reference", "sql_pattern", "sql_fix"]

MEMORY_TYPE_LABELS: dict[str, str] = {
    "user": "用户偏好",
    "feedback": "行为反馈",
    "project": "项目决策",
    "reference": "外部引用",
    "sql_pattern": "SQL模式",
    "sql_fix": "SQL修复",
}

MEMORY_TYPE_DESCRIPTIONS: dict[str, str] = {
    "user": (
        "用户的角色、目标、知识偏好。帮助后续会话适配用户风格。"
    ),
    "feedback": (
        "用户对AI工作方式的纠正或确认。先写规则，再写**原因**和**适用场景**。"
    ),
    "project": (
        "无法从代码或git历史推导的项目决策、截止日期、业务口径变更。"
        "先写事实/决定，再写**原因**和**影响范围**。始终使用绝对日期。"
    ),
    "reference": (
        "指向外部系统的指针（数据看板URL、文档链接、数据源地址）。"
    ),
    "sql_pattern": (
        "已验证成功的SQL查询模式，可直接复用。包含表名、口径说明。"
    ),
    "sql_fix": (
        "SQL错误修复经验。包含错误信息、修正方法。"
    ),
}

# ================================================================
# 不应存储的内容 (What NOT to save)
# ================================================================

WHAT_NOT_TO_SAVE = """
## 不应存入记忆的内容

以下内容**禁止**作为记忆保存，即使被要求:
- 代码模式、架构信息、文件路径 — 可从代码库推导
- Git历史、最近变更 — 使用 git log / git blame
- 调试方案或修复配方 — 修复已在代码中，commit有上下文
- 已在 AGENTS.md / CLAUDE.md 中记录的内容
- 临时任务状态、当前对话的中间过程

这些排除规则即使在用户明确要求保存时也适用。
若被要求保存PR列表或活动摘要，追问哪些是**令人意外**或**非显而易见**的部分。
"""

# ================================================================
# 数据模型
# ================================================================

@dataclass
class MemoryMetadata:
    """记忆条目的元数据"""
    memory_type: str = "sql_pattern"       # user|feedback|project|reference|sql_pattern|sql_fix
    confidence: float = 0.3                # 置信度 0.0~1.0
    source: str = "auto"                   # user|model|tool|consolidator
    created_at: str = ""                   # ISO 日期
    last_used_at: str = ""                 # 最近使用日期
    occurrence_count: int = 0              # 出现次数
    stale_days: float = 0.0                # 距今多少天
    conflict_group: str = ""               # 冲突组标签


# ================================================================
# 新鲜度 / 时效性
# ================================================================

def memory_age_days(mtime_s: float) -> int:
    """距今多少天（向下取整）"""
    return max(0, math.floor((time.time() - mtime_s) / 86_400))


def memory_age_text(mtime_s: float) -> str:
    """人类可读的年龄: '今天', '昨天', 或 'N天前'"""
    d = memory_age_days(mtime_s)
    if d == 0:
        return "今天"
    if d == 1:
        return "昨天"
    return f"{d}天前"


def memory_freshness_text(mtime_s: float) -> str:
    """返回时效性警告文本（超过1天时）。

    记忆是特定时间点的观察，不是实时状态。
    关于代码行为或表的断言可能已过期，使用前应验证。
    """
    d = memory_age_days(mtime_s)
    if d <= 1:
        return ""
    if d <= 7:
        return (
            f"⚠️ 此记忆已 {d} 天未使用。"
            "若涉及表结构或业务口径，建议验证后使用。"
        )
    if d <= 30:
        return (
            f"⚠️ 此记忆已 {d} 天未使用，可能已过期。"
            "SQL模式或数据假设建议重新验证。"
        )
    return (
        f"⛔ 此记忆已 {d} 天未使用，很可能已过期。"
        "强烈建议在执行前验证表结构和数据可用性。"
    )


def memory_is_stale(mtime_s: float, threshold_days: int = 30) -> bool:
    """判断记忆是否已过期"""
    return memory_age_days(mtime_s) > threshold_days


# ================================================================
# 置信度 × 时效性 排名
# ================================================================

def confidence_recency_score(
    confidence: float,
    mtime_s: float,
    half_life_days: float = 30.0,
) -> float:
    """计算 置信度 × 时效性 综合得分。

    公式: confidence * exp(-age_days / half_life_days)
    半衰期默认30天: 30天前的记忆权重降为原始的 ~0.5。

    Args:
        confidence: 置信度 0.0~1.0
        mtime_s: 最后修改时间 (epoch秒)
        half_life_days: 时效性半衰期（天）

    Returns:
        综合得分 0.0~1.0
    """
    age_days = memory_age_days(mtime_s)
    recency = math.exp(-age_days / max(half_life_days, 1.0))
    return confidence * recency


def rank_memories_by_score(
    memories: list[dict],
    half_life_days: float = 30.0,
) -> list[dict]:
    """对记忆列表按置信度×时效性排名。

    每个dict需包含:
    - confidence: float (0~1)
    - mtime_s: float (epoch秒) 或 created_at/last_used

    返回按得分降序排列的新列表，每项增加 '_rank_score' 字段。
    """
    scored = []
    for m in memories:
        # 确定时间戳
        mtime = m.get("mtime_s", 0)
        if not mtime and m.get("created_at"):
            try:
                from datetime import datetime as dt
                mtime = dt.fromisoformat(str(m["created_at"])).timestamp()
            except Exception:
                mtime = 0

        confidence = float(m.get("confidence", 0.3))
        score = confidence_recency_score(confidence, mtime, half_life_days)
        scored.append({**m, "_rank_score": round(score, 4), "_age_days": memory_age_days(mtime)})

    scored.sort(key=lambda x: x["_rank_score"], reverse=True)
    return scored


# ================================================================
# 类型推断
# ================================================================

def infer_memory_type(
    content: str = "",
    source: str = "auto",
    lesson_type: str = "",
) -> str:
    """根据内容特征推断记忆类型。

    优先级: 显式lesson_type > 内容特征 > source

    Args:
        content: 记忆内容文本
        source: 来源 (user|model|tool|consolidator)
        lesson_type: 旧的lesson_type字段

    Returns:
        记忆类型: user|feedback|project|reference|sql_pattern|sql_fix
    """
    # 显式类型优先
    if lesson_type:
        type_map = {
            "sql_fix": "sql_fix",
            "analysis_improvement": "feedback",
            "query_pattern": "sql_pattern",
            "best_practice": "project",
        }
        if lesson_type in type_map:
            return type_map[lesson_type]

    # 来源推断
    if source == "user":
        return "user"

    # 内容特征推断
    if content:
        content_lower = content.lower()
        # 用户偏好
        if any(kw in content_lower for kw in ["偏好", "习惯", "风格", "喜欢", "不要", "请用"]):
            return "feedback"
        # 项目决策
        if any(kw in content_lower for kw in ["口径", "规则", "标准", "决策", "规范", "统一"]):
            return "project"
        # 外部引用
        if any(kw in content_lower for kw in ["链接", "url", "地址", "看板", "文档"]):
            return "reference"
        # SQL相关
        if any(kw in content_lower for kw in ["sql", "select", "查询", "表", "字段"]):
            return "sql_pattern"

    return "sql_pattern"


# ================================================================
# 记忆摘要构建 (用于注入 Prompt)
# ================================================================

def build_memory_context(
    memories: list[dict],
    max_items: int = 10,
    include_freshness: bool = True,
) -> str:
    """将记忆列表构建为可注入系统Prompt的文本。

    Args:
        memories: 记忆条目列表（需包含 name/type/content/confidence等）
        max_items: 最多展示条目数
        include_freshness: 是否附加时效性警告

    Returns:
        格式化的记忆上下文文本
    """
    if not memories:
        return ""

    lines = ["## 历史记忆 (按相关性排序)\n"]
    for i, m in enumerate(memories[:max_items]):
        mtype = m.get("memory_type", m.get("type", "unknown"))
        mtype_label = MEMORY_TYPE_LABELS.get(mtype, mtype)
        name = m.get("name", m.get("query_pattern", f"记忆{i+1}"))
        content = m.get("content", m.get("solution", ""))
        confidence = m.get("confidence", 0.3)

        # 截断过长内容
        if len(content) > 200:
            content = content[:200] + "..."

        conf_indicator = "●" if confidence >= 0.7 else "○" if confidence >= 0.4 else "◦"
        lines.append(f"- {conf_indicator} [{mtype_label}] {name}")
        if content:
            lines.append(f"  {content}")

        # 新鲜度警告
        if include_freshness:
            freshness = m.get("_freshness_text", "")
            if not freshness:
                mtime = m.get("mtime_s", 0)
                if mtime:
                    freshness = memory_freshness_text(mtime)
            if freshness:
                lines.append(f"  {freshness}")

    return "\n".join(lines)


# 全局单例级缓存
_memory_context_cache: dict = {}
