"""把模式库的列表整理成提示词。调用方以前把 list 当成 dict，字段名也不对。"""

from __future__ import annotations


def format_similar_patterns(patterns) -> str:
    if not patterns:
        return ""
    if isinstance(patterns, dict):
        patterns = [patterns]
    if not isinstance(patterns, list):
        return ""
    lines = []
    for item in patterns[:3]:
        if not isinstance(item, dict):
            continue
        sql = item.get("sql_template") or ""
        if isinstance(sql, list):
            sql = sql[0] if sql else ""
        label = item.get("query_pattern") or ""
        lines.append(f"- {label}: {str(sql)[:300]}")
    if not lines:
        return ""
    return "相似历史查询:\n" + "\n".join(lines)


def format_lessons(lessons) -> str:
    if not lessons:
        return ""
    if isinstance(lessons, dict):
        lessons = [lessons]
    lines = []
    for item in (lessons or [])[:3]:
        if not isinstance(item, dict):
            continue
        text = item.get("problem") or item.get("solution") or ""
        if text:
            lines.append(f"- {str(text)[:150]}")
    if not lines:
        return ""
    return "历史教训:\n" + "\n".join(lines)
