"""自愈经验按表名和错误类型取用，不把别的表的教训塞进提示词。"""

from __future__ import annotations

import logging

logger = logging.getLogger("skadi.lessons")


def classify_error(message: str) -> str:
    text = message or ""
    lowered = text.lower()
    if "unknown column" in lowered or "doesn't exist" in lowered or "未知列" in text:
        return "unknown_column"
    if "syntax" in lowered or "you have an error" in lowered or "语法" in text:
        return "syntax"
    if "返回0行" in text or "0行" in text or "empty" in lowered:
        return "empty_result"
    if "权限" in text or "permission" in lowered:
        return "permission"
    return "execution"


def select_relevant(lessons: list, table_name: str, error_type: str) -> list:
    table = (table_name or "").lower().split(".")[-1]
    wanted = error_type or ""
    picked = []
    for lesson in lessons or []:
        lesson_table = (lesson.get("table_name") or "").lower().split(".")[-1]
        lesson_error = lesson.get("error_type") or ""
        if not table or lesson_table != table:
            continue
        if lesson_error != wanted:
            continue
        picked.append(lesson)
    return picked


def render_lessons(lessons: list) -> str:
    if not lessons:
        return ""
    lines = ["相关修复经验（仅当前表和错误类型）:"]
    for lesson in lessons[:3]:
        lines.append(f"- 问题: {(lesson.get('problem') or '')[:200]}")
        lines.append(f"  修正: {(lesson.get('solution') or '')[:300]}")
    return "\n".join(lines)


def advice_for_error(table_name: str, error: str, lessons: list = None) -> str:
    error_type = classify_error(error)
    rows = lessons
    if rows is None:
        rows = _load(table_name, error_type)
    return render_lessons(select_relevant(rows, table_name, error_type))


def _load(table_name: str, error_type: str) -> list:
    try:
        from learning.pattern_store import pattern_store

        return pattern_store.get_lessons_for_table(table_name, error_type)
    except Exception:
        logger.exception("读取自愈经验失败 table=%s error_type=%s", table_name, error_type)
        return []
