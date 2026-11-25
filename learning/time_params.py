"""把问题里的时间说法抽成参数，避免同一类说法被当成完全不同的问题。

时间说法按窗口形状分族：单日、滚动 N 天、本周、本月、上月。
族名写进归一化问题（``{time:single_day}`` 这种），不同族不共用缓存。

同一族里要换一种说法，SQL 的下界和上界都必须先参数化。
只换下界会把「前天」扩成两天、「今天」收成空集、「最近7天」收成某一天。
对不上、或只参数化了一侧范围条件时，渲染返回 None，调用方不得复用。

渲染用规划提示词里的 StarRocks 写法：CURRENT_DATE()、DATE_SUB、DATE_ADD，
本周用 date_trunc，不用 WEEKDAY。识别时同时接受这些片段和旧的 MySQL 写法，
大小写不敏感。较短的片段如果落在更长的时间表达式里面，不替换。
"""

from __future__ import annotations

import re
from typing import Optional

TIME_TOKEN_PREFIX = "{time:"
SQL_TOKEN = "{time_sql}"
UPPER_TOKEN = "{time_upper}"


class _Phrase:
    """一种时间说法。words[0] 是规范名，其余是同一窗口的别名（今天/今日）。"""

    def __init__(
        self,
        phrase: str,
        family: str,
        lower: str,
        upper: str,
        words: tuple = (),
        lower_aliases: tuple = (),
        upper_aliases: tuple = (),
    ):
        self.phrase = phrase
        self.family = family
        self.lower = lower
        self.upper = upper
        self.words = (phrase, *words)
        self.lower_aliases = tuple(lower_aliases)
        self.upper_aliases = tuple(upper_aliases)

    def lower_forms(self) -> tuple:
        return (self.lower, *self.lower_aliases)

    def upper_forms(self) -> tuple:
        return (self.upper, *self.upper_aliases)


def _day(phrase: str, offset: int, words: tuple = ()) -> _Phrase:
    """单日半开区间 [当天 00:00, 次日 00:00)。offset=0 是今天，1 是昨天。"""
    if offset <= 0:
        lower = "CURRENT_DATE()"
        lower_aliases = ("CURDATE()",)
    else:
        lower = "DATE_SUB(CURRENT_DATE(), INTERVAL %d DAY)" % offset
        lower_aliases = (
            "DATE_SUB(CURDATE(), INTERVAL %d DAY)" % offset,
            "CURDATE() - INTERVAL %d DAY" % offset,
        )
    if offset <= 0:
        upper = "DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY)"
        upper_aliases = (
            "DATE_ADD(CURDATE(), INTERVAL 1 DAY)",
            "CURDATE() + INTERVAL 1 DAY",
        )
    elif offset == 1:
        upper = "CURRENT_DATE()"
        upper_aliases = ("CURDATE()",)
    else:
        upper = "DATE_SUB(CURRENT_DATE(), INTERVAL %d DAY)" % (offset - 1)
        upper_aliases = (
            "DATE_SUB(CURDATE(), INTERVAL %d DAY)" % (offset - 1),
            "CURDATE() - INTERVAL %d DAY" % (offset - 1),
        )
    return _Phrase(phrase, "single_day", lower, upper, words, lower_aliases, upper_aliases)


def _rolling(phrase: str, days: int, words: tuple = ()) -> _Phrase:
    """滚动 N 天。DATETIME 用半开上界，DATE 用 BETWEEN，上界表达式都是今天。"""
    lower = "DATE_SUB(CURRENT_DATE(), INTERVAL %d DAY)" % days
    lower_aliases = (
        "DATE_SUB(CURDATE(), INTERVAL %d DAY)" % days,
        "CURDATE() - INTERVAL %d DAY" % days,
    )
    return _Phrase(
        phrase,
        "rolling",
        lower,
        "CURRENT_DATE()",
        words,
        lower_aliases,
        ("CURDATE()",),
    )


# 长的说法放前面，避免「近7天」截断「最近7天」。运行时仍按字面长度再排一次。
TIME_PHRASES = (
    _rolling("最近30天", 30, ("近30天",)),
    _rolling("最近7天", 7, ("近7天",)),
    _day("前天", 2),
    _day("昨天", 1, ("昨日",)),
    _day("今天", 0, ("今日",)),
    _Phrase(
        "本月",
        "month",
        "DATE_FORMAT(CURRENT_DATE(), '%Y-%m-01')",
        "DATE_ADD(DATE_FORMAT(CURRENT_DATE(), '%Y-%m-01'), INTERVAL 1 MONTH)",
        (),
        (
            "DATE_FORMAT(CURDATE(), '%Y-%m-01')",
            "date_trunc('month', CURRENT_DATE())",
            'date_trunc("month", CURRENT_DATE())',
        ),
        ("DATE_ADD(DATE_FORMAT(CURDATE(), '%Y-%m-01'), INTERVAL 1 MONTH)",),
    ),
    _Phrase(
        "上月",
        "prev_month",
        "DATE_FORMAT(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), '%Y-%m-01')",
        "DATE_FORMAT(CURRENT_DATE(), '%Y-%m-01')",
        (),
        ("DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01')",),
        ("DATE_FORMAT(CURDATE(), '%Y-%m-01')",),
    ),
    _Phrase(
        "本周",
        "week",
        "date_trunc('week', CURRENT_DATE())",
        "CURRENT_DATE()",
        (),
        (
            "DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)",
            "DATE_SUB(CURRENT_DATE(), INTERVAL WEEKDAY(CURRENT_DATE()) DAY)",
            'date_trunc("week", CURRENT_DATE())',
        ),
        ("CURDATE()",),
    ),
)


def _ordered_words() -> list:
    found = []
    for spec in TIME_PHRASES:
        for word in spec.words:
            found.append((word, spec))
    found.sort(key=lambda item: len(item[0]), reverse=True)
    return found


def _by_phrase(phrase: str) -> Optional[_Phrase]:
    if not phrase:
        return None
    for spec in TIME_PHRASES:
        if phrase in spec.words:
            return spec
    return None


def sql_fragment(phrase: str) -> Optional[str]:
    spec = _by_phrase(phrase)
    return spec.lower if spec else None


def family_of(phrase: str) -> Optional[str]:
    spec = _by_phrase(phrase)
    return spec.family if spec else None


def _all_forms() -> list:
    found = []
    for spec in TIME_PHRASES:
        found.extend(spec.lower_forms())
        found.extend(spec.upper_forms())
    return found


def normalize_question(question: str) -> tuple[str, dict]:
    """返回 (带族占位符的问题, 参数)。没有时间说法时参数为空。"""
    text = question or ""
    for word, spec in _ordered_words():
        if word in text:
            token = "{time:%s}" % spec.family
            params = {
                "time": spec.phrase,
                "time_family": spec.family,
                "time_sql": spec.lower,
                "time_upper": spec.upper,
            }
            return text.replace(word, token, 1), params
    return text, {}


def _iter_uncovered_spans(sql: str, fragment: str, universe: list):
    """fragment 的每一处出现，且它不是更长时间表达式的一部分。"""
    if not fragment or not sql:
        return
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
                if (
                    long_match.start() <= start
                    and long_match.end() >= end
                    and (long_match.start(), long_match.end()) != (start, end)
                ):
                    covered = True
                    break
            if covered:
                break
        if not covered:
            yield start, end


def _operator_before(sql: str, start: int) -> Optional[str]:
    prefix = sql[:start].rstrip()
    if re.search(r"(?i)\bBETWEEN$", prefix):
        return "BETWEEN"
    for op in (">=", "<=", "<>", "!=", ">", "<", "="):
        if prefix.endswith(op):
            return op
    return None


def _is_lower_slot(sql: str, start: int) -> bool:
    return _operator_before(sql, start) in (">=", ">", "=", "BETWEEN")


def _is_upper_slot(sql: str, start: int) -> bool:
    if _operator_before(sql, start) in ("<", "<="):
        return True
    prefix = sql[:start].rstrip()
    return bool(re.search(r"(?i)\bAND$", prefix))


def _overlaps(span: tuple, blocked: tuple) -> bool:
    start, end = span
    for other_start, other_end in blocked:
        if not (end <= other_start or start >= other_end):
            return True
    return False


def _find_bound(sql: str, forms: tuple, universe: list, predicate, blocked: tuple):
    for fragment in sorted(set(forms), key=len, reverse=True):
        for span in _iter_uncovered_spans(sql, fragment, universe):
            if _overlaps(span, blocked):
                continue
            if predicate(sql, span[0]):
                return span
    return None


def _splice(sql: str, span: tuple, token: str) -> str:
    start, end = span
    return sql[:start] + token + sql[end:]


def _collapse(sql: str) -> str:
    return re.sub(r"\s+", " ", sql or "").strip()


def templatize_sql(sql: str, phrase: Optional[str]) -> str:
    """把 SQL 里这对时间边界换成占位符。

    范围条件（>=、BETWEEN）会同时换下界和上界。
    等值条件（date = 某一天）只换那一个日期表达式，它本身就是整段窗口。
    找不到对应边界时保持原样，调用方不能拿它跨说法复用。
    """
    if not sql:
        return _collapse(sql)
    spec = _by_phrase(phrase or "")
    if spec is None:
        return _collapse(sql)
    universe = _all_forms()
    lower = _find_bound(sql, spec.lower_forms(), universe, _is_lower_slot, ())
    if lower is None:
        return _collapse(sql)
    if _operator_before(sql, lower[0]) == "=":
        return _collapse(_splice(sql, lower, SQL_TOKEN))
    upper = _find_bound(sql, spec.upper_forms(), universe, _is_upper_slot, (lower,))
    if upper is None:
        return _collapse(_splice(sql, lower, SQL_TOKEN))
    rewritten = sql
    for start, end, token in sorted(
        ((lower[0], lower[1], SQL_TOKEN), (upper[0], upper[1], UPPER_TOKEN)),
        key=lambda item: item[0],
        reverse=True,
    ):
        rewritten = rewritten[:start] + token + rewritten[end:]
    return _collapse(rewritten)


def _cross_phrase_safe(template: str, family: str) -> bool:
    """不同说法只有在窗口形状完整时才能互相填。"""
    has_lower = SQL_TOKEN in template
    has_upper = UPPER_TOKEN in template
    if has_lower and has_upper:
        return True
    if has_lower and not has_upper and family == "single_day":
        return _operator_before(template, template.find(SQL_TOKEN)) == "="
    return False


def render_sql(template: str, stored_params: dict, new_params: dict) -> Optional[str]:
    """用这次问题的时间填回模板。填不回去时返回 None，调用方不得复用这条 SQL。

    族不同直接放弃。同一族但说法不同时，范围条件必须上下界都已参数化；
    单日等值条件可以只替换那一个日期。做不到就返回 None，改走规划。
    """
    template = template or ""
    stored_params = stored_params or {}
    new_params = new_params or {}
    stored_time = stored_params.get("time")
    new_time = new_params.get("time")
    stored_spec = _by_phrase(stored_time or "")
    new_spec = _by_phrase(new_time or "")
    stored_family = stored_spec.family if stored_spec else stored_params.get("time_family")
    new_family = new_spec.family if new_spec else new_params.get("time_family")

    if not stored_time and not new_time:
        return template
    if not stored_spec or not new_spec or stored_family != new_family:
        return None
    if stored_spec.phrase != new_spec.phrase and not _cross_phrase_safe(template, new_spec.family):
        return None
    if SQL_TOKEN not in template and UPPER_TOKEN not in template:
        return template if stored_spec.phrase == new_spec.phrase else None
    rendered = template.replace(SQL_TOKEN, new_spec.lower).replace(UPPER_TOKEN, new_spec.upper)
    if SQL_TOKEN in rendered or UPPER_TOKEN in rendered:
        return None
    return rendered
