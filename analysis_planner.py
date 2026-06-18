# -*- coding: utf-8 -*-
"""分析规划器 (Analysis Planner)

核⼼职责：将⽤户问题转化为科学的分析⽅案。

流程：
    ⽤户问题 → ⽅法论推理 → 数据需求映射 → 可⾏性判断 → 分析计划 + 缺⼝声明

区别于意图识别（"这是什么类型的问题"），分析规划器回答：
    "这个问题在科学上应该怎么回答？需要什么数据？当前能执⾏什么？缺少什么？"
"""

import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.agent_config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    PLAN_MODEL, PLAN_TEMPERATURE,
)
from knowledge.statistical_methods import (
    STATISTICAL_METHODS,
    StatisticalMethod,
    build_methods_summary,
    build_data_requirement_checklist,
    get_all_methods,
)
from config.data_dictionary_loader import get_data_dictionary_prompt

logger = logging.getLogger(__name__)


# ================================================================
# 输出数据结构
# ================================================================

@dataclass
class DataGap:
    """数据缺口"""
    field: str               # 缺失的字段/数据
    description: str         # 说明
    needed_for: List[str]    # 为什么需要（方法列表）


@dataclass
class PlannedMethod:
    """规划的分析方法"""
    method_id: str
    method_name: str
    reason: str              # 为什么选择这个方法
    data_available: bool     # 所需数据是否可⽤
    missing_data: List[str] = field(default_factory=list)


@dataclass
class AnalysisPlan:
    """分析计划"""
    user_question: str
    question_type: str                    # 分析问题类型
    methods: List[PlannedMethod]          # 规划的方法链
    executable_methods: List[PlannedMethod]  # 可执⾏的方法
    blocked_methods: List[PlannedMethod]     # 因数据缺失⽆法执⾏的方法
    data_gaps: List[DataGap]              # 数据缺口详情
    suggested_sql_hints: List[str]        # SQL生成提⽰
    summary: str                          # ⼈类可读的规划摘要


# ================================================================
# 分析规划器
# ================================================================

class AnalysisPlanner:
    """分析规划器 — 问题驱动的⽅法论推理引擎"""

    def __init__(self):
        self.llm = ChatOpenAI(
            model=PLAN_MODEL,
            openai_api_key=DEEPSEEK_API_KEY,
            openai_api_base=DEEPSEEK_BASE_URL,
            temperature=PLAN_TEMPERATURE,
            max_tokens=2000,
        )
        self._methods_kb = {m.id: m for m in STATISTICAL_METHODS}
        self._methods_summary = build_methods_summary()

    async def plan(
        self,
        user_query: str,
        data_dictionary_text: str = "",
        available_tables: List[str] = None,
    ) -> AnalysisPlan:
        """
        分析⽤户问题，生成分析计划。

        Args:
            user_query: ⽤户⾃然语⾔问题
            data_dictionary_text: 数据字典内容（⽤于数据可⽤性判断）
            available_tables: 可⽤的表名列表

        Returns:
            AnalysisPlan 包含⽅法链、数据缺⼝、执⾏建议
        """
        # ——— 阶段1: LLM推理 — 选择分析⽅法 ———
        method_ids = await self._reason_methods(user_query)

        # ——— 阶段2: 数据可⽤性检查 ———
        planned_methods = []
        for mid in method_ids:
            method = self._methods_kb.get(mid)
            if not method:
                continue
            available, missing = self._check_data_availability(
                method, data_dictionary_text, available_tables or []
            )
            pm = PlannedMethod(
                method_id=mid,
                method_name=method.name,
                reason=method.description,
                data_available=available,
                missing_data=missing,
            )
            planned_methods.append(pm)

        # ——— 阶段3: 分类⽅法 ———
        executable = [m for m in planned_methods if m.data_available]
        blocked = [m for m in planned_methods if not m.data_available]

        # ——— 阶段4: 汇总数据缺⼝ ———
        gaps = self._summarize_gaps(blocked)

        # ——— 阶段5: 生成SQL提⽰ ———
        sql_hints = self._generate_sql_hints(executable, user_query)

        # ——— 阶段6: 构建摘要 ———
        summary = self._build_summary(user_query, executable, blocked, gaps)

        return AnalysisPlan(
            user_question=user_query,
            question_type=self._classify_question_type(planned_methods),
            methods=planned_methods,
            executable_methods=executable,
            blocked_methods=blocked,
            data_gaps=gaps,
            suggested_sql_hints=sql_hints,
            summary=summary,
        )

    async def _reason_methods(self, user_query: str) -> List[str]:
        """调⽤LLM推理应该使⽤哪些分析⽅法"""
        system_prompt = f"""你是⼀位资深数据分析⽅法论专家。

你的任务是：给定⽤户的数据分析问题，从可⽤⽅法库中选择最科学、最合适的分析⽅法链条。

选择原则：
1. 从问题本质出发选择⽅法，⽽⾮关键词匹配
2. ⽅法之间应有逻辑递进关系（如：先描述 → 再检验 → 后建模）
3. 优先选择最严谨的⽅法（如：有对照组时优先选AB测试/DID⽽⾮简单对⽐）
4. 只输出⽅法ID列表，⽤逗号分隔，不要任何解释

{self._methods_summary}

请只输出⽅法ID列表，⽤逗号分隔，例如: descriptive_stats,trend_analysis,comparison_two_sample"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"⽤户问题: {user_query}\n\n请选择最合适的分析⽅法链:"),
        ]

        try:
            response = await self.llm.ainvoke(messages)
            content = response.content.strip()
            # 解析ID列表
            ids = [s.strip() for s in content.replace("\n", ",").split(",")]
            valid_ids = [i for i in ids if i in self._methods_kb]
            logger.info(f"[AnalysisPlanner] LLM选择方法: {valid_ids}")
            return valid_ids if valid_ids else self._fallback_methods(user_query)
        except Exception as e:
            logger.error(f"[AnalysisPlanner] LLM推理失败: {e}")
            return self._fallback_methods(user_query)

    def _fallback_methods(self, user_query: str) -> List[str]:
        """LLM不可⽤时的关键词兜底"""
        query_lower = user_query.lower()
        fallback = []
        for m in STATISTICAL_METHODS:
            for condition in m.when_to_use:
                # 简单关键词匹配作为fallback
                for kw in ["趋势", "异常", "对⽐", "相关", "回归", "预测",
                          "分布", "实验", "留存", "转化", "因果", "分层"]:
                    if kw in user_query and kw in condition:
                        fallback.append(m.id)
                        break
        return list(dict.fromkeys(fallback))[:4] or ["descriptive_stats"]

    def _check_data_availability(
        self,
        method: StatisticalMethod,
        data_dict_text: str,
        available_tables: List[str],
    ) -> tuple:
        """检查某个⽅法所需数据是否可⽤"""
        missing = []
        for req in method.requires:
            if not req.required:
                continue
            found = False
            # 在数据字典中查找
            if data_dict_text:
                if req.name.lower() in data_dict_text.lower():
                    found = True
            # 检查替代字段
            if not found:
                for alt in req.alternatives:
                    if alt.lower() in data_dict_text.lower():
                        found = True
                        break
            if not found:
                missing.append(f"{req.name}({req.description})")
        return len(missing) == 0, missing

    def _summarize_gaps(self, blocked: List[PlannedMethod]) -> List[DataGap]:
        """汇总数据缺⼝"""
        gap_map: Dict[str, DataGap] = {}
        for pm in blocked:
            for missing_field in pm.missing_data:
                if missing_field not in gap_map:
                    gap_map[missing_field] = DataGap(
                        field=missing_field,
                        description="数据缺失",
                        needed_for=[pm.method_name],
                    )
                else:
                    gap_map[missing_field].needed_for.append(pm.method_name)
        return list(gap_map.values())

    def _generate_sql_hints(
        self, executable: List[PlannedMethod], user_query: str
    ) -> List[str]:
        """为可执⾏⽅法生成SQL⽣成提⽰"""
        hints = []
        for pm in executable:
            method = self._methods_kb.get(pm.method_id)
            if not method:
                continue
            # 提取计算提⽰
            if method.computation_hint:
                hints.append(f"[{method.name}] {method.computation_hint}")
        return hints

    def _build_summary(
        self,
        user_query: str,
        executable: List[PlannedMethod],
        blocked: List[PlannedMethod],
        gaps: List[DataGap],
    ) -> str:
        """构建⼈类可读的分析计划摘要"""
        lines = [f"## 分析计划: {user_query[:60]}\n"]

        if executable:
            lines.append("### ✅ 可执⾏的分析")
            for i, pm in enumerate(executable, 1):
                lines.append(f"{i}. **{pm.method_name}** — {pm.reason}")

        if blocked:
            lines.append("\n### ⚠️ 因数据缺失暂⽆法执⾏")
            for i, pm in enumerate(blocked, 1):
                missing_str = "、".join(pm.missing_data)
                lines.append(f"{i}. **{pm.method_name}** — 缺失: {missing_str}")

        if gaps:
            lines.append("\n### 📋 数据缺⼝声明")
            for gap in gaps:
                needed = "、".join(gap.needed_for)
                lines.append(f"- **{gap.field}** → 需要⽤于: {needed}")
            lines.append("\n建议补充上述数据后可执⾏完整分析链。")

        if not executable:
            lines.append("\n### ❌ 当前⽆法执⾏任何分析")
            lines.append("缺乏必要的底层数据。请先补充数据字典中对应的表/字段。")

        return "\n".join(lines)

    def _classify_question_type(self, methods: List[PlannedMethod]) -> str:
        """分类问题类型"""
        categories = set()
        for pm in methods:
            method = self._methods_kb.get(pm.method_id)
            if method:
                categories.add(method.category)
        if "causal" in categories:
            return "因果推断"
        if "comparative" in categories:
            return "⽐较分析"
        if "relational" in categories:
            return "关系建模"
        if "temporal" in categories:
            return "时间序列分析"
        if "segmentation" in categories:
            return "分群分类"
        return "描述性分析"


# ================================================================
# 全局单例
# ================================================================

analysis_planner = AnalysisPlanner()


# ================================================================
# 辅助函数 — 格式化规划结果供下游使⽤
# ================================================================

def format_plan_for_dingtalk(plan: AnalysisPlan) -> str:
    """将分析计划格式化为钉钉消息（简短版）"""
    lines = ["📋 **分析路线**"]
    for i, pm in enumerate(plan.executable_methods, 1):
        lines.append(f"  {i}. {pm.method_name}")
    if plan.blocked_methods:
        lines.append(f"\n⚠️ {len(plan.blocked_methods)}项分析因数据缺失暂⽆法执⾏")
    if plan.data_gaps:
        missing_fields = [g.field for g in plan.data_gaps]
        lines.append(f"   缺失数据: {', '.join(missing_fields[:3])}")
    return "\n".join(lines)


def format_plan_for_analysis(plan: AnalysisPlan) -> str:
    """将分析计划注⼊ Analysis Agent（分析框架指令）"""
    if not plan.executable_methods:
        return ""
    lines = ["# 分析⽅法框架\n"]
    for pm in plan.executable_methods:
        method = STATISTICAL_METHODS_DICT.get(pm.method_id)
        if method and method.prompt_guide:
            lines.append(method.prompt_guide)
    return "\n".join(lines)


# 快速查找字典
STATISTICAL_METHODS_DICT = {m.id: m for m in STATISTICAL_METHODS}
