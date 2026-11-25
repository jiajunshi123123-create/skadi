"""把问题里的时间说法抽成参数，避免「昨天」和「前天」被当成两个完全不同的问题。"""

from __future__ import annotations

from typing import Optional

# 长的说法放前面，避免「近7天」被更短的规则截断。
TIME_RULES = (
    ("最近7天", "CURDATE() - INTERVAL 7 DAY"),
    ("近7天", "CURDATE() - INTERVAL 7 DAY"),
    ("最近30天", "CURDATE() - INTERVAL 30 DAY"),
    ("近30天", "CURDATE() - INTERVAL 30 DAY"),
    ("前天", "CURDATE() - INTERVAL 2 DAY"),
    ("昨天", "CURDATE() - INTERVAL 1 DAY"),
    ("昨日", "CURDATE() - INTERVAL 1 DAY"),
    ("今天", "CURDATE()"),
    ("今日", "CURDATE()"),
    ("本月", "DATE_FORMAT(CURDATE(), '%Y-%m-01')"),
    ("上月", "DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01')"),
    ("本周", "DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)"),
)

TIME_TOKEN = "{time}"
SQL_TOKEN = "{time_sql}"


def sql_fragment(phrase: str) -> Optional[str]:
    for name, fragment in TIME_RULES:
        if name == phrase:
            return fragment
    return None


def normalize_question(question: str) -> tuple[str, dict]:
    """返回 (带 {time} 的问题, 参数)。没有时间说法时参数为空。"""
    text = question or ""
    for phrase, fragment in TIME_RULES:
        if phrase in text:
            return text.replace(phrase, TIME_TOKEN, 1), {"time": phrase, "time_sql": fragment}
    return text, {}


def templatize_sql(sql: str, phrase: Optional[str]) -> str:
    """把 SQL 里对应的时间表达式换成 {time_sql}。对不上就保持原样。"""
    fragment = sql_fragment(phrase or "")
    if not fragment or not sql or fragment not in sql:
        return sql or ""
    return sql.replace(fragment, SQL_TOKEN, 1)


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
