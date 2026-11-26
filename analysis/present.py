"""把完整计算和少量样本整理成给模型看的文本。"""

from __future__ import annotations

import json


def format_result_for_llm(result: dict, facts: dict = None) -> str:
    """展示样本，并写明计算覆盖了全部行。"""
    facts = facts if facts is not None else (result.get("computed_facts") or {})
    total = facts.get("row_count")
    if total is None:
        total = result.get("facts_row_count") or result.get("row_count") or len(result.get("rows") or [])
    note = facts.get("sample_note") or (
        f"计算基于全部 {total} 行。若下面的表格短于这个行数，被省略的行已经计入计算结果。"
    )
    sample = facts.get("sample_rows")
    if sample:
        body = _render_records(sample)
    else:
        body = _render_raw_rows(result, limit=12)
    return f"{note}\n样本行数说明：结果共 {total} 行。\n{body}"


def format_facts_block(facts: dict) -> str:
    if not facts:
        return "（没有计算结果。数据不足时不要编造数字。）"
    payload = json.dumps(facts, ensure_ascii=False, indent=2, default=str)
    if len(payload) > 12000:
        payload = payload[:12000] + "\n... 计算结果已截断，被截掉的部分不能引用。"
    return payload


def _render_records(rows: list) -> str:
    if not rows:
        return "(无样本)"
    columns = list(rows[0].keys())
    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join("" if row.get(column) is None else str(row.get(column)) for column in columns))
    return "\n".join(lines)


def _render_raw_rows(result: dict, limit: int) -> str:
    cols = [str(column) for column in (result.get("cols") or [])]
    rows = result.get("rows") or []
    if not rows:
        return "(无数据)"
    lines = []
    if cols:
        lines.append(" | ".join(cols))
    shown = list(rows[:limit])
    if len(rows) > limit + 5:
        shown.extend(rows[-5:])
    for row in shown:
        if isinstance(row, dict):
            lines.append(" | ".join(str(row.get(column, "")) for column in (cols or row.keys())))
        elif isinstance(row, (list, tuple)):
            lines.append(" | ".join(str(value) for value in row))
        else:
            lines.append(str(row))
    if len(rows) > len(shown):
        lines.append(f"... 另有 {len(rows) - len(shown)} 行未展示，但已计入计算结果")
    return "\n".join(lines)
