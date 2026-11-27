"""用已经算好的事实拼一份结构化报告。这里不再做新的算术。"""

from __future__ import annotations

SECTIONS = ("总结", "数据来源", "计算方法", "详细分析", "建议")


def render_report(user_query: str, parts: list, notes: list = None) -> str:
    notes = list(notes or [])
    blocks = []
    blocks.append(f"# {user_query.strip() or '分析报告'}")
    blocks.append("")
    blocks.append("## 总结")
    blocks.append(_summary(parts))
    blocks.append("")
    blocks.append("## 数据来源")
    blocks.append(_sources(parts))
    blocks.append("")
    blocks.append("## 计算方法")
    blocks.append(_methods(parts))
    blocks.append("")
    blocks.append("## 详细分析")
    blocks.append(_details(parts))
    blocks.append("")
    blocks.append("## 建议")
    blocks.append(_advice(parts))
    if notes:
        blocks.append("")
        blocks.append("## 执行说明")
        blocks.extend(f"- {note}" for note in notes)
    return "\n".join(blocks).rstrip() + "\n"


def _successes(parts: list) -> list:
    return [part for part in parts if part.get("success") and part.get("result")]


def _facts_of(part: dict) -> dict:
    return ((part.get("result") or {}).get("computed_facts") or {})


def _primary(parts: list) -> dict:
    usable = _successes(parts)
    return usable[0] if usable else {}


def _metric_block(facts: dict) -> tuple:
    metrics = facts.get("metrics") or {}
    if not metrics:
        return None, {}
    series_metric = (facts.get("series") or {}).get("metric")
    name = series_metric if series_metric in metrics else next(iter(metrics))
    return name, metrics[name]


def _summary(parts: list) -> str:
    part = _primary(parts)
    if not part:
        reasons = [item.get("error") or "失败" for item in parts] or ["没有成功的子查询"]
        return "这次没有形成可用结论。" + "；".join(reasons)
    facts = _facts_of(part)
    name, block = _metric_block(facts)
    if not name:
        return f"「{part.get('purpose')}」返回了 {facts.get('row_count', 0)} 行，但没有可计算的数值列。"
    trend = block.get("trend") or {}
    mom = block.get("mom") or {}
    sentences = [
        f"主指标是 {name}，计算使用了全部 {facts.get('row_count', 0)} 行。"
    ]
    if trend:
        sentences.append(
            f"从 {trend.get('first_date')} 的 {trend.get('first')} 到 {trend.get('last_date')} 的 {trend.get('last')}，"
            f"变化 {trend.get('change')}（{trend.get('change_pct')}%），方向为{trend.get('direction')}。"
        )
    if mom.get("available"):
        sentences.append(
            f"近 {mom.get('window_days')} 天合计 {mom.get('current_sum')}，"
            f"前 {mom.get('window_days')} 天合计 {mom.get('previous_sum')}，"
            f"环比 {mom.get('change_pct')}%。"
        )
    elif mom.get("reason"):
        sentences.append(mom["reason"] + "。")
    anomalies = facts.get("anomalies") or []
    if anomalies:
        top = anomalies[0]
        sentences.append(
            f"同星期几基线下绝对值最大的异常是 {top.get('date')}（{top.get('weekday')}），"
            f"数值 {top.get('value')}，基线均值 {top.get('baseline_mean')}，z={top.get('z')}，方向 {top.get('direction')}。"
        )
    else:
        sentences.append("按同星期几基线，没有 |z| 大于 2 的点。")
    missing = facts.get("missing_days") or []
    if missing:
        shown = "、".join(missing[:8])
        sentences.append(f"日期序列缺少这些天：{shown}。")
    return "".join(sentences)


def _sources(parts: list) -> str:
    if not parts:
        return "没有子查询。"
    lines = []
    for part in parts:
        if part.get("direct_answer"):
            lines.append(f"- {part.get('purpose')}：不需要 SQL。{part.get('direct_answer')}")
            continue
        result = part.get("result") or {}
        facts = _facts_of(part)
        if not part.get("success"):
            lines.append(f"- {part.get('purpose')}：失败。{part.get('error')}")
            continue
        sql = result.get("sql_executed") or (part.get("plan") or {}).get("sql") or "（SQL 未返回）"
        lines.append(
            f"- {part.get('purpose')}：返回 {result.get('row_count', facts.get('row_count', 0))} 行，"
            f"日期 {facts.get('date_min')} 至 {facts.get('date_max')}。SQL：{sql}"
        )
    return "\n".join(lines)


def _methods(parts: list) -> str:
    facts = _facts_of(_primary(parts))
    split = facts.get("weekday_weekend") or {}
    lines = [
        "- 环比：最近 30 个自然日的合计，对比再往前 30 个自然日的合计。历史天数不够时不计算。",
        "- 同比：最近 30 天对比约 365 天前的同一窗口。没有去年同期数据时不计算。",
        "- 异常：每个点与「同一个星期几、且不含当天」的均值和标准差比较，|z| > 2 记为异常。该星期几的其余样本少于 8 天时不做这天的判定。",
        "- 工作日（周一至周五）和周末分开统计均值，不混在一个基线里。",
        "- IQR：小于 Q1-1.5×IQR 或大于 Q3+1.5×IQR 的点另列为离群点。",
        "- 以上全部在该子查询返回的完整行上计算，不只用展示给模型的样本。",
    ]
    weekday = split.get("weekday") or {}
    weekend = split.get("weekend") or {}
    if weekday.get("n"):
        lines.append(
            f"- 主指标工作日基线：均值 {weekday.get('mean')}，标准差 {weekday.get('std')}，样本 {weekday.get('n')} 天。"
        )
    if weekend.get("n"):
        lines.append(
            f"- 主指标周末基线：均值 {weekend.get('mean')}，标准差 {weekend.get('std')}，样本 {weekend.get('n')} 天。"
        )
    name, block = _metric_block(facts)
    if name and (block.get("by_weekday") or {}):
        lines.append("- 分星期几的均值：")
        for label, stats in block["by_weekday"].items():
            lines.append(f"  - {label}：均值 {stats.get('mean')}，标准差 {stats.get('std')}，样本 {stats.get('n')} 天。")
    return "\n".join(lines)


def _details(parts: list) -> str:
    lines = []
    for part in _successes(parts):
        facts = _facts_of(part)
        name, block = _metric_block(facts)
        lines.append(f"### {part.get('purpose')}")
        if not name:
            lines.append("没有数值指标。")
            continue
        summary = block.get("summary") or {}
        lines.append(
            f"{name}：合计 {summary.get('sum')}，均值 {summary.get('mean')}，"
            f"标准差 {summary.get('std')}，最小 {summary.get('min')}，最大 {summary.get('max')}。"
        )
        moving = block.get("moving_averages") or {}
        if moving.get("ma7") is not None:
            lines.append(f"七日移动平均的最新值是 {moving.get('ma7')}，三日移动平均的最新值是 {moving.get('ma3')}。")
        regression = block.get("regression") or {}
        if regression.get("available"):
            lines.append(
                f"对时间的线性斜率是每天 {regression.get('slope_per_day')}，R²={regression.get('r2')}。"
            )
        yoy = block.get("yoy") or {}
        if yoy.get("available"):
            lines.append(f"同比变化 {yoy.get('change_pct')}%。")
        elif yoy.get("reason"):
            lines.append(yoy["reason"] + "。")
        for item in (facts.get("anomalies") or [])[:5]:
            lines.append(
                f"异常 {item.get('date')} {item.get('weekday')}：值 {item.get('value')}，"
                f"同星期几均值 {item.get('baseline_mean')}，z={item.get('z')}，{item.get('severity')}。"
            )
        for day in (facts.get("missing_days") or [])[:8]:
            lines.append(f"缺数日：{day}。")
        for breakdown in facts.get("breakdowns") or []:
            lines.append(f"按 {breakdown.get('dimension')} 拆分 {breakdown.get('metric')}，合计 {breakdown.get('total')}：")
            for row in breakdown.get("top") or []:
                lines.append(f"- {row.get('key')}：{row.get('value')}，占比 {row.get('share_pct')}%。")
        for pair in facts.get("correlations") or []:
            lines.append(f"{pair.get('left')} 与 {pair.get('right')} 的相关系数 r={pair.get('r')}（n={pair.get('n')}）。")
    if not lines:
        return "没有可展开的成功查询。"
    return "\n".join(lines)


def _advice(parts: list) -> str:
    facts = _facts_of(_primary(parts))
    anomalies = facts.get("anomalies") or []
    missing = facts.get("missing_days") or []
    lines = []
    if not _successes(parts):
        return "先确认账号是否有表权限，以及问题里的时间范围能否查到数据。本次没有改用无权限检查的查询。"
    if anomalies:
        top = anomalies[0]
        lines.append(
            f"优先核对 {top.get('date')} 的 {top.get('metric') or (facts.get('series') or {}).get('metric')}："
            f"它相对同星期几基线的 z={top.get('z')}，方向是 {top.get('direction')}。"
        )
    if missing:
        lines.append(f"先补查缺数日 {missing[0]}，缺数当天不要当成业务下跌。")
    if not anomalies and not missing:
        lines.append("本窗口没有超过同星期几 2σ 的点，也没有缺日。建议按现有口径继续观察，不要用固定百分比当阈值。")
    lines.append("分组贡献只说明结构，不说明原因。若要解释某个异常日，需要另查该日的渠道或区域明细。")
    return "\n".join(lines)
