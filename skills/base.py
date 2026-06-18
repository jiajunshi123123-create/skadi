"""分析技能基类 - 所有数据分析方法技能的统一接口

新增技能只需：
1. 继承 AnalysisSkill
2. 设置 name / description / keywords / category
3. 实现 prompt_snippet 属性（注入 LLM 的分析指令）
4. 可选实现 analyze_data()（Python 层面的数值计算）
"""

from typing import List, Optional


class AnalysisSkill:
    """分析技能基类

    每个技能代表一种数据分析方法论，通过关键词匹配自动激活。
    激活后，技能的 prompt_snippet 会被注入到 Analysis Agent 的上下文中，
    引导 LLM 按照该技能的分析框架进行深度分析。

    继承示例:
        class TrendSkill(AnalysisSkill):
            name = "趋势分析"
            description = "识别时间序列的上升/下降/平稳趋势"
            keywords = ["趋势", "走势", "变化", "增长", "下降"]
            category = "statistical"

            @property
            def prompt_snippet(self) -> str:
                return '''## 趋势分析方法
                1. 计算移动平均（3日/7日）
                2. 标注环比变化率
                ...'''
    """

    # ---- 子类必须覆盖 ----
    name: str = ""                 # 技能展示名
    description: str = ""          # 一句话描述
    keywords: List[str] = []       # 触发关键词（中文）
    category: str = "general"      # 分类: general / statistical / ml / testing

    # ---- 子类可选覆盖 ----
    priority: int = 0              # 优先级（越高越靠前）

    def match_score(self, query: str) -> float:
        """计算查询与本技能的匹配度 0.0~1.0

        默认基于关键词命中率计算，子类可覆盖实现更智能的匹配。
        """
        if not self.keywords:
            return 0.0
        query_lower = query.lower()
        hits = sum(1 for kw in self.keywords if kw.lower() in query_lower)
        if hits == 0:
            return 0.0
        # 命中率 + 优先级微调
        base = min(hits / max(len(self.keywords), 1), 1.0)
        return min(base + self.priority * 0.05, 1.0)

    @property
    def prompt_snippet(self) -> str:
        """返回注入 Analysis Agent 的分析方法指令

        这段文字会被追加到 Analysis Agent 的系统提示词中，
        引导 LLM 按照该技能的分析框架输出。
        """
        return ""

    def analyze_data(self, data: List[dict], cols: List[str]) -> dict:
        """可选：Python 层面的数据预计算

        Args:
            data: 查询结果行列表（每行为 dict）
            cols: 列名列表

        Returns:
            计算结果字典，会合并到 query_result 中供 Analysis Agent 参考
        """
        return {}

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self.name}>"
