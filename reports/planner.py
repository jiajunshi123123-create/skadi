"""把周报、月报和需要基线的问题拆成有限的几个子问题。"""

from __future__ import annotations

from dataclasses import dataclass

REPORT_MARKERS = ("周报", "月报", "日报", "分析报告", "复盘", "经营报告", "数据报告")
BASELINE_MARKERS = ("基线", "异常", "同比")
MAX_SUBQUERIES = 4


@dataclass
class SubQuery:
    name: str
    question: str
    purpose: str


def is_report_request(text: str) -> bool:
    raw = text or ""
    return any(marker in raw for marker in REPORT_MARKERS)


def needs_baseline_query(text: str) -> bool:
    """报告，或明确要求基线、异常、同比时，才追加历史子查询。"""
    raw = text or ""
    return is_report_request(raw) or any(marker in raw for marker in BASELINE_MARKERS)


def plan_subqueries(user_query: str) -> list:
    question = (user_query or "").strip()
    planned = [SubQuery("current", question, "当前周期")]
    if needs_baseline_query(question):
        planned.append(SubQuery(
            "baseline",
            (
                f"为回答「{question}」，查询同一指标最近 30 天的按日明细，保留日期列。"
                "不要只返回一个汇总数，后续要用同星期几的均值做基线。"
            ),
            "历史基线",
        ))
    if is_report_request(question):
        planned.append(SubQuery(
            "breakdown",
            f"为报告「{question}」按渠道、区域和品类拆分核心指标，返回各分组的汇总值。",
            "维度拆解",
        ))
        planned.append(SubQuery(
            "previous",
            f"查询「{question}」的上一周期汇总，用来和本期对比。",
            "上一周期",
        ))
    return planned[:MAX_SUBQUERIES]
