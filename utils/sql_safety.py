"""只读 SQL 防护。

只放行单条 SELECT、WITH ... SELECT（CTE）或 EXPLAIN。
拒绝多语句、写操作，以及 INTO OUTFILE / DUMPFILE、LOAD_FILE 等导出。

解析按 MySQL 默认 sql_mode（反斜杠转义字符串、双引号是字符串）。
连接侧应同时执行 SET SESSION sql_mode 与 SET SESSION TRANSACTION READ ONLY
（见 tools.database_adapter.apply_mysql_readonly_session）。两种引号解释
结果不一致时直接拒绝，避免 NO_BACKSLASH_ESCAPES 把字面量里的语句放出来。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

_MAX_SQL_CHARS = 100_000

# 出现在 SELECT/WITH/EXPLAIN 内部仍然危险的关键字。
# 不包含 SET：CAST(... AS CHAR CHARACTER SET utf8) 是合法只读写法。
# 不包含 REPLACE：REPLACE() 是字符串函数。
_FORBIDDEN_TOKENS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "RENAME",
    "CALL",
    "EXECUTE",
    "EXEC",
    "HANDLER",
    "LOCK",
    "UNLOCK",
    "PREPARE",
    "DEALLOCATE",
    "LOAD",
    "INSTALL",
    "UNINSTALL",
    "KILL",
    "INTO",
    "OUTFILE",
    "DUMPFILE",
    "INFILE",
}

_FORBIDDEN_FUNCTIONS = (
    "LOAD_FILE",
    "GET_LOCK",
    "RELEASE_LOCK",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "SLEEP",
    "BENCHMARK",
)

_ALLOWED_HEADS = {"SELECT", "WITH", "EXPLAIN"}
_METADATA_HEADS = {"SHOW", "DESCRIBE", "DESC"}

_CTE_RE = re.compile(
    r"(?:\bWITH\b|,)\s+(?:RECURSIVE\s+)?([A-Za-z_][\w]*)\s*(?:\([^)]*\))?\s+\bAS\s*\(",
    re.IGNORECASE,
)

_SIMPLE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOLLAR_QUOTE = re.compile(r"\$([A-Za-z0-9_]*)\$")


class SqlSafetyError(ValueError):
    """SQL 文本无法无歧义解析。"""


def check_readonly_sql(sql: str, *, allow_metadata: bool = False) -> Optional[str]:
    """不安全时返回中文错误，安全时返回 None。

    allow_metadata 仅供扫描器等内部调用放行单条 SHOW / DESCRIBE。
    用户查询路径必须保持默认 False。
    """
    if not isinstance(sql, str):
        return "SQL被安全检查拒绝：非法输入"
    if len(sql) > _MAX_SQL_CHARS:
        return "SQL被安全检查拒绝：语句过长"

    prepared = _prepare_text(sql)
    if prepared is None:
        return "SQL被安全检查拒绝：包含非法字符"
    if not prepared.strip():
        return "SQL被安全检查拒绝：空语句"

    interpretations = []
    failures = []
    for backslash_escapes in (True, False):
        try:
            interpretations.append(
                _normalize_sql(prepared, backslash_escapes=backslash_escapes)
            )
        except SqlSafetyError as exc:
            failures.append(str(exc))
            interpretations.append(None)

    if interpretations[0] is None and interpretations[1] is None:
        return f"SQL被安全检查拒绝：{failures[0]}"
    if (
        interpretations[0] is None
        or interpretations[1] is None
        or _canonical(interpretations[0]) != _canonical(interpretations[1])
    ):
        return "SQL被安全检查拒绝：引号转义存在歧义，请用 '' 表示单引号"
    return _assess(interpretations[0], allow_metadata=allow_metadata)


def normalize_for_analysis(sql: str) -> str:
    """去掉注释和字符串字面量，保留简单标识符，供表名 / CTE 提取。"""
    if not isinstance(sql, str):
        raise SqlSafetyError("非法输入")
    prepared = _prepare_text(sql)
    if prepared is None:
        raise SqlSafetyError("包含非法字符")
    return _normalize_sql(prepared, backslash_escapes=True)


def extract_cte_names(sql: str) -> set:
    """返回 CTE 名称（小写）。解析失败时返回空集，由安全检查另行拒绝。"""
    try:
        normalized = normalize_for_analysis(sql)
    except SqlSafetyError:
        return set()
    return {match.group(1).lower() for match in _CTE_RE.finditer(normalized)}


def _prepare_text(sql: str) -> Optional[str]:
    if "\x00" in sql:
        return None
    normalized = unicodedata.normalize("NFKC", sql)
    squashed = []
    for char in normalized:
        if char in "\n\r\t":
            squashed.append(char)
            continue
        if char.isspace():
            squashed.append(" ")
            continue
        if not char.isprintable():
            return None
        squashed.append(char)
    return "".join(squashed)


def _canonical(sql: str) -> str:
    return " ".join(sql.split())


def _assess(normalized: str, *, allow_metadata: bool) -> Optional[str]:
    statements = _split_statements(normalized)
    if len(statements) != 1:
        return "SQL被安全检查拒绝：只允许单条语句"
    statement = statements[0]
    tokens = _SIMPLE_IDENT.findall(statement)
    if not tokens:
        return "SQL被安全检查拒绝：无法识别的语句"

    head = tokens[0].upper()
    allowed = set(_ALLOWED_HEADS)
    if allow_metadata:
        allowed |= _METADATA_HEADS
    if head not in allowed:
        return "SQL被安全检查拒绝：只允许 SELECT、WITH（CTE）或 EXPLAIN 语句"

    for token in tokens:
        upper = token.upper()
        if upper in _FORBIDDEN_TOKENS:
            return f"SQL被安全检查拒绝：包含禁止关键字 {upper}"

    for func in _FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{func}\s*\(", statement, re.IGNORECASE):
            return f"SQL被安全检查拒绝：包含禁止函数 {func}"
    return None


def _split_statements(normalized: str) -> list:
    parts = [part.strip() for part in normalized.split(";")]
    return [part for part in parts if part]


def _normalize_sql(sql: str, *, backslash_escapes: bool, depth: int = 0) -> str:
    if depth > 20:
        raise SqlSafetyError("注释嵌套过深")

    out = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""

        if char == "-" and nxt == "-":
            after = sql[index + 2] if index + 2 < length else ""
            if after == "" or after.isspace():
                index += 2
                while index < length and sql[index] not in "\n\r":
                    index += 1
                out.append(" ")
                continue
            out.append(char)
            index += 1
            continue

        if char == "#":
            index += 1
            while index < length and sql[index] not in "\n\r":
                index += 1
            out.append(" ")
            continue

        if char == "/" and nxt == "*":
            end = sql.find("*/", index + 2)
            if end == -1:
                raise SqlSafetyError("未闭合的注释")
            body = sql[index + 2:end]
            if body.startswith("!"):
                body = body[1:]
                version = 0
                while version < len(body) and body[version].isdigit():
                    version += 1
                out.append(
                    _normalize_sql(
                        body[version:],
                        backslash_escapes=backslash_escapes,
                        depth=depth + 1,
                    )
                )
            else:
                out.append(" ")
            index = end + 2
            continue

        if char == "$":
            match = _DOLLAR_QUOTE.match(sql, index)
            if match:
                tag = match.group(0)
                end = sql.find(tag, index + len(tag))
                if end == -1:
                    raise SqlSafetyError("未闭合的美元引号")
                index = end + len(tag)
                out.append(" ")
                continue

        if char == "`":
            index += 1
            buf = []
            closed = False
            while index < length:
                if sql[index] == "`":
                    if index + 1 < length and sql[index + 1] == "`":
                        buf.append("`")
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                buf.append(sql[index])
                index += 1
            if not closed:
                raise SqlSafetyError("未闭合的标识符")
            body = "".join(buf)
            if _SIMPLE_IDENT.fullmatch(body):
                out.append(" " + body + " ")
            else:
                out.append(" ident ")
            continue

        if char in ("'", '"'):
            quote = char
            index += 1
            closed = False
            while index < length:
                if backslash_escapes and sql[index] == "\\":
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                index += 1
            if not closed:
                raise SqlSafetyError("未闭合的字符串")
            out.append(" ")
            continue

        out.append(char)
        index += 1

    return "".join(out)
