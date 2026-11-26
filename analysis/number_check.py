"""核对模型输出里的数字是否能追溯到计算结果或样本。"""

from __future__ import annotations

import re
from typing import Iterable

from analysis.compute import iter_numbers

NUMBER_RE = re.compile(
    r"(?<![\w.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+))(?:\.(\d+))?(%)?"
)
DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
LIST_MARKER_RE = re.compile(r"(?m)^\s*\d{1,2}[.、)]\s*")


def untraceable_numbers(text: str, *sources) -> list:
    """返回无法在事实或样本中找到来源的数字原文。"""
    allowed = _allowed_numbers(sources)
    flagged = []
    seen = set()
    cleaned = DATE_RE.sub(" ", text or "")
    cleaned = LIST_MARKER_RE.sub(" ", cleaned)
    for match in NUMBER_RE.finditer(cleaned):
        raw = match.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        if _is_method_token(cleaned, match):
            continue
        value = _parse_match(match)
        if value is None:
            continue
        if _matches(value, allowed, percent=bool(match.group(3))):
            continue
        flagged.append(raw)
    return flagged


def warning_block(flagged: list) -> str:
    shown = "、".join(flagged[:12])
    return (
        "\n\n⚠️ 数字核对\n"
        "以下数字没有出现在计算结果或样本中，不能当作结论："
        f"{shown}"
    )


def _allowed_numbers(sources: Iterable) -> list:
    allowed = []
    for source in sources:
        for number in iter_numbers(source):
            allowed.append(float(number))
            allowed.append(round(float(number), 2))
            allowed.append(round(float(number), 1))
            allowed.append(round(float(number), 0))
    return allowed


def _parse_match(match: re.Match):
    whole = match.group(1).replace(",", "")
    fraction = match.group(2)
    text = whole if fraction is None else f"{whole}.{fraction}"
    try:
        return float(text)
    except ValueError:
        return None


def _matches(value: float, allowed: list, percent: bool) -> bool:
    candidates = [value]
    if percent:
        candidates.append(value)
    for candidate in candidates:
        for known in allowed:
            tolerance = max(0.06, abs(known) * 0.015)
            if abs(candidate - known) <= tolerance:
                return True
            if abs(known) <= 2 and abs(candidate - known * 100) <= max(0.2, abs(known * 100) * 0.015):
                return True
    return False


def _is_method_token(text: str, match: re.Match) -> bool:
    """2σ、1.5 倍 IQR 这类方法常数不是业务数字。"""
    tail = text[match.end():match.end() + 3]
    if "σ" in tail or "sigma" in tail.lower():
        return True
    return False
