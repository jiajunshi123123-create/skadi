"""把问题里的时间说法抽成参数，避免「昨天」和「前天」被当成两个完全不同的问题。

渲染用规划提示词里的 StarRocks 写法：CURRENT_DATE()、DATE_SUB，本周用 date_trunc，不用 WEEKDAY。
识别时同时接受这些片段，以及旧的 MySQL 写法（CURDATE / WEEKDAY），大小写不敏感。
较短的片段如果落在更长的时间表达式里面（例如 DATE_SUB 里的 CURRENT_DATE()），不替换。
"""

from __future__ import annotations

import re
from typing import Optional

# 长的说法放前面，避免「近7天」被更短的规则截断。
# (说法, 写回 SQL 时用的表达式, 识别时额外接受的旧写法)
TIME_RULES = (
    (
        "最近30天",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
        ("CURDATE() - INTERVAL 30 DAY", "DATE_SUB(CURDATE(), INTERVAL 30 DAY)"),
    ),
    (
        "近30天",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
        ("CURDATE() - INTERVAL 30 DAY", "DATE_SUB(CURDATE(), INTERVAL 30 DAY)"),
    ),
    (
        "最近7天",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
        ("CURDATE() - INTERVAL 7 DAY", "DATE_SUB(CURDATE(), INTERVAL 7 DAY)"),
    ),
    (
        "近7天",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
        ("CURDATE() - INTERVAL 7 DAY", "DATE_SUB(CURDATE(), INTERVAL 7 DAY)"),
    ),
    (
        "前天",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)",
        ("CURDATE() - INTERVAL 2 DAY", "DATE_SUB(CURDATE(), INTERVAL 2 DAY)"),
    ),
    (
        "昨天",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)",
        ("CURDATE() - INTERVAL 1 DAY", "DATE_SUB(CURDATE(), INTERVAL 1 DAY)"),
    ),
    (
        "昨日",
        "DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)",
        ("CURDATE() - INTERVAL 1 DAY", "DATE_SUB(CURDATE(), INTERVAL 1 DAY)"),
    ),
    ("今天", "CURRENT_DATE()", ("CURDATE()",)),
    ("今日", "CURRENT_DATE()", ("CURDATE()",)),
    (
        "本月",
        "DATE_FORMAT(CURRENT_DATE(), '%Y-%m-01')",
        ("DATE_FORMAT(CURDATE(), '%Y-%m-01')", "date_trunc('month', CURRENT_DATE())"),
    ),
    (
        "上月",
        "DATE_FORMAT(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), '%Y-%m-01')",
        ("DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01')",),
    ),
    (
        "本周",
        "date_trunc('week', CURRENT_DATE())",
        (
            "DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)",
            "DATE_SUB(CURRENT_DATE(), INTERVAL WEEKDAY(CURRENT_DATE()) DAY)",
            'date_trunc("week", CURRENT_DATE())',
        ),
    ),
)

TIME_TOKEN = "{time}"
SQL_TOKEN = "{time_sql}"


def sql_fragment(phrase: str) -> Optional[str]:
    for name, fragment, _aliases in TIME_RULES:
        if name == phrase:
            return fragment
    return None


def _candidates(phrase: str) -> tuple:
    for name, fragment, aliases in TIME_RULES:
        if name == phrase:
            return (fragment, *aliases)
    return ()


def _all_fragments() -> list:
    found = []
    for _name, fragment, aliases in TIME_RULES:
        found.append(fragment)
        found.extend(aliases)
    return found


def normalize_question(question: str) -> tuple[str, dict]:
    """返回 (带 {time} 的问题, 参数)。没有时间说法时参数为空。"""
    text = question or ""
    for phrase, fragment, _aliases in TIME_RULES:
        if phrase in text:
            return text.replace(phrase, TIME_TOKEN, 1), {"time": phrase, "time_sql": fragment}
    return text, {}


def _span_outside_longer(sql: str, fragment: str, universe: list) -> Optional[tuple[int, int]]:
    """找到 fragment 的第一处出现，且它不是更长时间表达式的一部分。"""
    if not fragment or not sql:
        return None
    pattern = re.compile(re.escape(fragment), re.IGNORECASE)
    longer = [
        re.compile(re.escape(item), re.IGNORECASE)
        for item in universe
        if len(item) > len(fragment)
    ]
    for match in pattern.finditer(sql):
        start, end = match.span()
        covered = False
        for long_re in longer:
            for long_match in long_re.finditer(sql):
                if long_match.start() <= start and long_match.end() >= end and (long_match.start(), long_match.end()) != (start, end):
                    covered = True
                    break
            if covered:
                break
        if not covered:
            return start, end
    return None


def templatize_sql(sql: str, phrase: Optional[str]) -> str:
    """把 SQL 里对应的时间表达式换成 {time_sql}。对不上就保持原样。"""
    if not sql:
        return sql or ""
    candidates = _candidates(phrase or "")
    if not candidates:
        return sql
    universe = _all_fragments()
    for fragment in sorted(set(candidates), key=len, reverse=True):
        span = _span_outside_longer(sql, fragment, universe)
        if span is None:
            continue
        start, end = span
        return sql[:start] + SQL_TOKEN + sql[end:]
    return sql


def render_sql(template: str, stored_params: dict, new_params: dict) -> Optional[str]:
    """用这次问题的时间填回模板。填不回去时返回 None，调用方不得复用这条 SQL。"""
    template = template or ""
    stored_time = (stored_params or {}).get("time")
    new_time = (new_params or {}).get("time")
    if SQL_TOKEN in template:
        fragment = sql_fragment(new_time or "")
        if not fragment:
            return None
        return template.replace(SQL_TOKEN, fragment)
    if stored_time == new_time:
        return template
    return None
