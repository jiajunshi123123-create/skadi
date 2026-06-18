# -*- coding: utf-8 -*-
"""统计分析⽅法论知识库

每⼀个条目代表⼀种标准统计/分析⽅法，包含：
- 适⽤场景 (when_to_use)
- 数据需求 (requires)
- 统计算法 (computation)
- 替代⽅案 (fallback)
- 分析指引 (prompt_guide)

分析规划器会基于这个知识库进⾏⽅法论推理，⽽⾮关键词匹配。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class DataRequirement:
    """单⼀数据需求"""
    name: str                          # 字段名/变量名
    description: str                   # 说明
    data_type: str                     # continuous / categorical / datetime / text
    required: bool = True              # 是否必须
    min_sample: int = 1                # 最⼩样本量
    alternatives: List[str] = field(default_factory=list)  # 可替代的字段名


@dataclass
class StatisticalMethod:
    """⼀种统计分析⽅法"""
    id: str
    name: str
    category: str                       # descriptive / comparative / relational / causal / temporal / segmentation
    description: str
    when_to_use: List[str]              # 适⽤场景判断条件
    requires: List[DataRequirement]     # 数据需求
    computation_hint: str = ""          # 计算⽅法提⽰
    fallback: Optional[str] = None      # 数据不⾜时的替代⽅法 ID
    prompt_guide: str = ""              # 注⼊ LLM 的分析指引
    priority: int = 0                   # 优先级 (同场景下优先选择)


# ================================================================
# 知识库定义
# ================================================================

STATISTICAL_METHODS: List[StatisticalMethod] = [
    # ── 描述性统计 ──
    StatisticalMethod(
        id="descriptive_stats",
        name="描述性统计",
        category="descriptive",
        description="计算均值、中位数、标准差、分位数等基础统计量，了解数据整体分布",
        when_to_use=[
            "用户询问'多少'、'平均'、'总体情况'等",
            "需要了解数据的基本分布特征",
            "作为其他分析的先导步骤",
        ],
        requires=[
            DataRequirement("value", "待分析的数值列", "continuous"),
        ],
        computation_hint="计算 mean, median, std, min, max, Q1, Q3, skewness, kurtosis",
        prompt_guide="""
## 描述性统计分析

### 1. 集中趋势
- 均值、中位数、众数 → 数据"典型值"在哪
- 均值 vs 中位数差距大 → 存在偏态或异常值

### 2. 离散程度
- 标准差、IQR(四分位距) → 数据分散程度
- 变异系数(CV = std/mean) → 跨指标可比

### 3. 分布形状
- 偏度 > 0 → 右偏(长尾在右)；< 0 → 左偏
- 峰度 > 3 → 厚尾；< 3 → 薄尾

### 4. 分位数画像
- P25/P50/P75/P90/P95 → 了解用户分层
""",
        priority=10,
    ),

    # ── 趋势分析 ──
    StatisticalMethod(
        id="trend_analysis",
        name="趋势分析",
        category="temporal",
        description="识别时间序列的变化方向、速率和模式",
        when_to_use=[
            "询问趋势、走势、变化方向",
            "需要了解指标随时间的变化",
            "含'最近N天/周/月'等时间范围",
        ],
        requires=[
            DataRequirement("date", "时间/日期列", "datetime"),
            DataRequirement("value", "待分析的数值列", "continuous"),
        ],
        computation_hint="移动平均(3/7日)、线性趋势拟合、环比变化率",
        prompt_guide="""
## 趋势分析方法

### 1. 趋势方向
- 观察首末值变化率，判断上升/下降/平稳
- 计算线性趋势斜率

### 2. 移动平均平滑
- 3日MA → 短期波动；7日MA → 中期趋势
- 去噪后识别真实转折点

### 3. 环比变化序列
- 逐期环比变化率
- 标注最大变化点及可能原因

### 4. 周期性识别
- 周周期(工作日/周末)、月周期(月初/月末)
""",
        priority=5,
    ),

    # ── 异常检测 ──
    StatisticalMethod(
        id="anomaly_detection",
        name="异常检测",
        category="descriptive",
        description="识别偏离正常模式的异常数据点，判断显著变化",
        when_to_use=[
            "询问'异常'、'暴跌'、'激增'、'突变'等",
            "需要判断某个值是否显著偏离",
            "监控类问题",
        ],
        requires=[
            DataRequirement("date", "时间/日期列", "datetime"),
            DataRequirement("value", "待检测的数值列", "continuous", min_sample=7),
        ],
        computation_hint="Z-score (|Z|>2为异常)、IQR (Q1-1.5*IQR / Q3+1.5*IQR)、移动窗口均值±2σ",
        fallback="descriptive_stats",
        prompt_guide="""
## 异常检测方法

### 1. 统计判定
- Z-score = (x - μ) / σ，|Z| > 2 视为异常
- IQR法: 超出 [Q1-1.5*IQR, Q3+1.5*IQR] 为异常值

### 2. 异常分级
- 轻度异常: 偏离基线 20%-50%
- 中度异常: 偏离基线 50%-100%
- 严重异常: 偏离基线 > 100%

### 3. 异常归因
- 检查是否同时段多个指标异常(系统性问题)
- 检查是否仅单指标异常(局部问题)
""",
        priority=5,
    ),

    # ── 分布分析 ──
    StatisticalMethod(
        id="distribution_analysis",
        name="分布分析",
        category="descriptive",
        description="分析数据在不同区间/类别上的分布特征",
        when_to_use=[
            "询问'分布'、'构成'、'占比'等",
            "需要了解数据在各维度上的分布",
            "含'分组'、'分段'需求",
        ],
        requires=[
            DataRequirement("category", "分组/分类字段", "categorical"),
            DataRequirement("value", "数值指标列", "continuous",
                          alternatives=["count"]),
        ],
        computation_hint="频率分布、累积分布、直方图分箱、帕累托分析",
        prompt_guide="""
## 分布分析方法

### 1. 频率分布
- 各类别占比、累计占比
- 识别头部集中度(帕累托原则)

### 2. 数值分布
- 分位数刻画(P25/P50/P75/P90)
- 偏度和峰度

### 3. 分布对比
- 多组分布的均值/方差比较
- 分布形态差异
""",
        priority=3,
    ),

    # ── 对比分析 (双样本) ──
    StatisticalMethod(
        id="comparison_two_sample",
        name="双样本对比分析",
        category="comparative",
        description="比较两组独立样本在某个指标上的差异是否显著",
        when_to_use=[
            "询问'对比'、'比较'、'差异'、'高于/低于'等",
            "需要比较两组数据的表现",
            "含'A vs B'、'实验/对照'、'前后对比'等",
        ],
        requires=[
            DataRequirement("group", "分组标识", "categorical"),
            DataRequirement("value", "比较的数值指标", "continuous", min_sample=30),
        ],
        computation_hint="""
- 独立样本t检验: scipy.stats.ttest_ind(group_a, group_b)
- 效应量 Cohen's d = (mean_a - mean_b) / pooled_std
- 若数据不满足正态: Mann-Whitney U检验
""",
        fallback="descriptive_stats",
        prompt_guide="""
## 双样本对比分析

### 1. 描述对比
- 两组均值、中位数、标准差
- 变化率 = (B-A)/A

### 2. 统计显著性检验
- 独立样本t检验: H0: 两组均值无差异
- p < 0.05 → 差异统计显著
- 效应量 Cohen's d: 0.2小/0.5中/0.8大

### 3. 业务解读
- 统计显著 ≠ 业务显著
- 结合业务阈值判断差异的实际意义
""",
        priority=5,
    ),

    # ── 方差分析 ──
    StatisticalMethod(
        id="anova",
        name="方差分析(ANOVA)",
        category="comparative",
        description="比较多组(≥3组)样本在某个指标上的差异",
        when_to_use=[
            "需要比较三组及以上数据",
            "含'不同渠道/版本/地区'等多分类对比",
        ],
        requires=[
            DataRequirement("group", "分组标识(≥3类)", "categorical"),
            DataRequirement("value", "比较的数值指标", "continuous", min_sample=30),
        ],
        computation_hint="scipy.stats.f_oneway(*groups)，若显著则事后检验(Tukey HSD)",
        fallback="comparison_two_sample",
        prompt_guide="""
## 方差分析(ANOVA)

### 1. 整体检验
- F统计量、p值 → 组间是否存在显著差异
- p < 0.05 → 至少有一组不同

### 2. 事后两两比较
- 确定具体哪些组之间有差异
- 控制多重比较错误率

### 3. 效应量
- η² (eta-squared): 组间差异解释了多大比例的变异
""",
        priority=3,
    ),

    # ── 卡方检验 ──
    StatisticalMethod(
        id="chi_square",
        name="卡方检验",
        category="comparative",
        description="检验两个分类变量之间是否独立",
        when_to_use=[
            "需要分析两个分类变量的关联",
            "含'比例/占比差异'等",
        ],
        requires=[
            DataRequirement("var_a", "分类变量A", "categorical"),
            DataRequirement("var_b", "分类变量B", "categorical"),
            DataRequirement("count", "频次计数", "continuous",
                          alternatives=["自动计数"]),
        ],
        computation_hint="""
- scipy.stats.chi2_contingency(contingency_table)
- Cramér's V 衡量关联强度
""",
        fallback="distribution_analysis",
        prompt_guide="""
## 卡方检验

### 1. 列联表
- 交叉表展示两个变量的联合分布
- 行列百分比

### 2. 独立性检验
- H0: 两个变量独立
- p < 0.05 → 拒绝独立假设，存在关联

### 3. 关联强度
- Cramér's V: 0.1弱/0.3中/0.5强
""",
        priority=2,
    ),

    # ── 相关分析 ──
    StatisticalMethod(
        id="correlation_analysis",
        name="相关分析",
        category="relational",
        description="量化两个连续变量之间的线性关联程度",
        when_to_use=[
            "询问'关系'、'相关'、'关联'、'联动'等",
            "需要了解两个指标是否同向变化",
        ],
        requires=[
            DataRequirement("var_x", "变量X", "continuous"),
            DataRequirement("var_y", "变量Y", "continuous", min_sample=30),
        ],
        computation_hint="""
- Pearson r: numpy.corrcoef(x, y)[0,1]
- Spearman ρ: scipy.stats.spearmanr(x, y)
- 注意：相关≠因果
""",
        prompt_guide="""
## 相关分析

### 1. 相关系数
- Pearson r: 线性相关 (-1到1)
  - |r|>0.7 强 / 0.4-0.7 中 / 0.2-0.4 弱 / <0.2 可忽略
- Spearman ρ: 单调相关 (对异常值鲁棒)

### 2. 显著性检验
- p值 < 0.05 → 相关性统计显著

### 3. 散点图审视线
- 检查是否为非线性关系
- 识别异常点的影响

### 4. 因果推断线索
- 时间先后、剂量反应、排除混杂
- 相关不意味着因果
""",
        priority=5,
    ),

    # ── 回归分析 ──
    StatisticalMethod(
        id="regression_analysis",
        name="回归分析",
        category="relational",
        description="建立因变量与自变量之间的量化关系模型",
        when_to_use=[
            "询问'影响因素'、'驱动因素'、'预测'等",
            "需要量化变量间的关系强度",
        ],
        requires=[
            DataRequirement("y", "因变量(被预测的指标)", "continuous", min_sample=30),
            DataRequirement("x_vars", "多个自变量(候选影响因素)", "continuous",
                          alternatives=["categorical"]),
        ],
        computation_hint="""
- 线性回归: sklearn.linear_model.LinearRegression 或 statsmodels.OLS
- R² 衡量拟合优度
- 标准化系数比较变量重要性
""",
        fallback="correlation_analysis",
        prompt_guide="""
## 回归分析

### 1. 模型拟合
- Y = β₀ + β₁X₁ + β₂X₂ + ...
- R²: 模型解释了多少Y的变异

### 2. 系数解读
- β: X每变化1单位，Y的平均变化量
- 标准化β: 比较变量相对重要性
- p值: 变量是否统计显著

### 3. 模型诊断
- 残差是否随机分布
- 多重共线性(VIF)
- 异常值影响

### 4. 局限性声明
- 线性假设可能不成立
- 遗漏变量偏差
- 样本代表性问题
""",
        priority=3,
    ),

    # ── AB测试分析 ──
    StatisticalMethod(
        id="ab_test",
        name="A/B测试分析",
        category="comparative",
        description="严格评估实验组与对照组的差异，判断策略效果",
        when_to_use=[
            "询问'实验'、'AB测试'、'灰度'、'实验组'等",
            "需要评估某个策略/功能的效果",
        ],
        requires=[
            DataRequirement("group", "实验分组(实验/对照)", "categorical"),
            DataRequirement("metric", "评估指标", "continuous", min_sample=100),
        ],
        computation_hint="""
- 双样本t检验或比例检验
- 最小可检测效应量(MDE)
- 置信区间
""",
        prompt_guide="""
## A/B测试分析

### 1. 实验设计检查
- 随机分配是否合理
- 样本量是否充足

### 2. 效应评估
- 实验组 vs 对照组均值差异
- 相对提升 = (B-A)/A
- 置信区间

### 3. 显著性判断
- p值 < 0.05 → 差异统计显著
- 效应量大小判断业务价值

### 4. 注意事项
- 新奇效应：初期效果可能高估
- 辛普森悖论：子组趋势可能反转
- 建议附置信区间，不做单点判断
""",
        priority=4,
    ),

    # ── 时间序列分解 ──
    StatisticalMethod(
        id="time_series_decomposition",
        name="时间序列分解",
        category="temporal",
        description="将时间序列拆解为趋势、季节性和残差三个成分",
        when_to_use=[
            "需要深层理解时间序列的结构",
            "含'季节性'、'周期性波动'等",
            "数据至少覆盖2个完整周期",
        ],
        requires=[
            DataRequirement("date", "时间列", "datetime", min_sample=14),
            DataRequirement("value", "数值列", "continuous", min_sample=14),
        ],
        computation_hint="""
- 加法模型: Y = Trend + Seasonal + Residual
- 乘法模型: Y = Trend × Seasonal × Residual
- statsmodels.tsa.seasonal_decompose
""",
        fallback="trend_analysis",
        prompt_guide="""
## 时间序列分解

### 1. 趋势成分
- 长期上升/下降趋势
- 趋势变化率

### 2. 季节性成分
- 识别周期模式(周/月/季)
- 季节性强度

### 3. 残差成分
- 去除趋势和季节后的随机波动
- 残差中的异常点 → 突发事件影响
""",
        priority=2,
    ),

    # ── 时间序列预测 ──
    StatisticalMethod(
        id="time_series_forecast",
        name="时间序列预测",
        category="temporal",
        description="基于历史数据预测未来趋势",
        when_to_use=[
            "询问'预测'、'预计'、'未来'等",
            "需要前瞻性判断",
        ],
        requires=[
            DataRequirement("date", "时间列", "datetime", min_sample=30),
            DataRequirement("value", "数值列", "continuous", min_sample=30),
        ],
        computation_hint="移动平均外推、指数平滑(Holt-Winters)、线性趋势外推",
        fallback="trend_analysis",
        prompt_guide="""
## 时间序列预测

### 1. 趋势外推
- 线性/指数趋势拟合
- 近30天平均日变化率推算

### 2. 季节性调整
- 识别并应用季节因子

### 3. 不确定性声明
- ⚠️ 必须声明：预测基于历史趋势假设不变
- 给出预测区间而非单点值
- 标注预测假设和局限性
""",
        priority=3,
    ),

    # ── 队列/留存分析 ──
    StatisticalMethod(
        id="cohort_analysis",
        name="队列分析",
        category="temporal",
        description="按某种特征分组后追踪各组随时间的行为变化",
        when_to_use=[
            "询问'留存'、'回访'、'复购'等",
            "需要按时间维度追踪用户群",
            "含'新用户/老用户'分组分析",
        ],
        requires=[
            DataRequirement("cohort_date", "队列定义日期(首次行为日期)", "datetime"),
            DataRequirement("activity_date", "行为发生日期", "datetime"),
            DataRequirement("user_id", "用户标识", "categorical"),
        ],
        computation_hint="按首次行为日期分组，计算各期留存率矩阵",
        fallback="trend_analysis",
        prompt_guide="""
## 队列分析

### 1. 队列定义
- 明确队列划分标准(按周/按月/按渠道)
- 展示各队列的样本量

### 2. 留存曲线
- Day1/Day7/Day30留存率
- 留存率衰减模式

### 3. 队列差异
- 不同队列留存率差异及趋势
- 识别高/低留存队列的特征
""",
        priority=3,
    ),

    # ── 漏斗分析 ──
    StatisticalMethod(
        id="funnel_analysis",
        name="漏斗分析",
        category="descriptive",
        description="分析用户在多步骤流程中的转化和流失",
        when_to_use=[
            "询问'转化'、'流失'、'漏斗'、'路径'等",
            "需要分析步骤间的转化率",
        ],
        requires=[
            DataRequirement("step", "流程步骤标识", "categorical"),
            DataRequirement("user_count", "各步骤用户数", "continuous",
                          alternatives=["从user_id去重计数"]),
        ],
        computation_hint="各步骤转化率 = 当前步骤/上一步骤，总体转化率 = 最终步骤/第一步",
        prompt_guide="""
## 漏斗分析

### 1. 步骤间转化
- 每步的绝对用户数
- 相邻步骤转化率
- 识别最大流失步骤

### 2. 漏斗对比
- 不同时段/渠道的漏斗对比

### 3. 优化建议
- 针对最大流失步骤的改进方向
""",
        priority=3,
    ),

    # ── 因果推断(DID) ──
    StatisticalMethod(
        id="causal_did",
        name="双重差分(DID)",
        category="causal",
        description="通过对比实验组和对照组在干预前后的变化差异来推断因果效应",
        when_to_use=[
            "需要推断某个事件/策略的因果效应",
            "有明确的干预时间点",
            "含'影响'、'导致'、'原因'等因果含义",
        ],
        requires=[
            DataRequirement("group", "实验组/对照组标识", "categorical"),
            DataRequirement("time", "干预前后时间标识", "categorical"),
            DataRequirement("metric", "评估指标", "continuous", min_sample=30),
        ],
        computation_hint="""
DID = (实验组后 - 实验组前) - (对照组后 - 对照组前)
需要平行趋势假设
""",
        fallback="comparison_two_sample",
        prompt_guide="""
## 双重差分(DID)分析

### 1. 平行趋势假设
- 干预前两组应有相似的趋势
- 如不满足，DID结果可能偏误

### 2. 因果效应估计
- DID估计量 = 处理效应
- 正数=正向效果，负数=负向效果

### 3. 稳健性
- 安慰剂检验
- 改变时间窗口的敏感性分析

### 4. ⚠️ 因果推断的局限性
- DID只能识别平均处理效应
- 不能排除其他同时期事件的影响
- 建议结合业务知识综合判断
""",
        priority=2,
    ),

    # ── 分组/聚类 ──
    StatisticalMethod(
        id="segmentation",
        name="用户分群",
        category="segmentation",
        description="基于行为/属性将用户划分为有意义的群组",
        when_to_use=[
            "询问'分层'、'分群'、'分类'、'画像'等",
            "需要识别不同类型的用户/产品",
        ],
        requires=[
            DataRequirement("features", "用于分群的多个特征列", "continuous"),
            DataRequirement("entity_id", "实体标识", "categorical"),
        ],
        computation_hint="""
- 规则分群: 基于业务阈值(RFM等)
- K-Means聚类: sklearn.cluster.KMeans
- 确定最优K值: 肘部法则
""",
        prompt_guide="""
## 用户分群分析

### 1. 分群维度选择
- 基于业务理解选择有意义的维度
- 避免高相关特征

### 2. 群组画像
- 每群的核心特征值
- 群规模及占比

### 3. 群间差异
- 关键指标的群间对比
- 差异化策略建议
""",
        priority=2,
    ),
]


# ================================================================
# 知识库查询⼯具
# ================================================================

def get_method(method_id: str) -> Optional[StatisticalMethod]:
    """按ID获取方法"""
    for m in STATISTICAL_METHODS:
        if m.id == method_id:
            return m
    return None


def get_methods_by_category(category: str) -> List[StatisticalMethod]:
    """按类别获取方法列表"""
    return [m for m in STATISTICAL_METHODS if m.category == category]


def get_all_methods() -> List[StatisticalMethod]:
    """获取所有方法"""
    return list(STATISTICAL_METHODS)


def build_methods_summary() -> str:
    """构建知识库摘要，用于注入 Prompt"""
    lines = ["# 可用分析方法库\n"]
    by_cat: Dict[str, list] = {}
    for m in STATISTICAL_METHODS:
        by_cat.setdefault(m.category, []).append(m)

    cat_names = {
        "descriptive": "描述性统计",
        "comparative": "比较检验",
        "relational": "关系建模",
        "temporal": "时间序列",
        "causal": "因果推断",
        "segmentation": "分群分类",
    }

    for cat, methods in by_cat.items():
        lines.append(f"\n## {cat_names.get(cat, cat)}")
        for m in methods:
            reqs = ", ".join(r.name for r in m.requires)
            lines.append(f"- **{m.name}** ({m.id})")
            lines.append(f"  适用: {'; '.join(m.when_to_use[:2])}")
            lines.append(f"  需要: {reqs}")
            if m.fallback:
                lines.append(f"  替代: {m.fallback}")

    return "\n".join(lines)


def build_data_requirement_checklist(methods: List[StatisticalMethod]) -> dict:
    """给定一组方法，汇总数据需求清单"""
    all_requirements = {}
    for method in methods:
        for req in method.requires:
            key = req.name
            if key not in all_requirements:
                all_requirements[key] = {
                    "description": req.description,
                    "data_type": req.data_type,
                    "required_by": [],
                    "required": req.required,
                    "min_sample": req.min_sample,
                    "alternatives": req.alternatives,
                }
            all_requirements[key]["required_by"].append(method.name)
    return all_requirements
