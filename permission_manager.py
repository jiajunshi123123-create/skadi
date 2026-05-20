"""权限管理器 - 企业级权限分隔系统核心

提供用户身份→角色映射、表白名单校验、强制WHERE条件注入、SQL权限校验等功能。
使用内存缓存避免频繁查询PG，缓存TTL可配置。
"""
import json
import time
import logging
import re
from typing import Optional
from datetime import datetime
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD

logger = logging.getLogger(__name__)

# 缓存配置
CACHE_TTL_SECONDS = 300  # 角色缓存5分钟


class PermissionDenied(Exception):
    """权限拒绝异常"""
    def __init__(self, message: str, table: str = None, reason: str = None):
        self.message = message
        self.table = table
        self.reason = reason
        super().__init__(message)


class PermissionManager:
    """权限管理核心类

    功能：
    1. get_user_role(staff_id) - 查PG获取用户角色及权限配置
    2. get_allowed_tables(role) - 返回角色的可查表白名单
    3. get_mandatory_filters(role) - 返回角色的强制WHERE条件
    4. check_sql_permission(sql, role) - 校验SQL是否符合角色权限
    5. enforce_sql(sql, user_role) - 返回注入权限条件后的SQL
    """

    def __init__(self):
        self._role_cache = {}       # {staff_id: {'role_data': {...}, 'expires_at': float}}
        self._role_def_cache = {}   # {role_id: {'data': {...}, 'expires_at': float}}

    @contextmanager
    def _get_connection(self):
        """获取PG连接（上下文管理器）"""
        conn = None
        try:
            conn = psycopg2.connect(
                host=PG_HOST,
                port=PG_PORT,
                dbname=PG_DB,
                user=PG_USER,
                password=PG_PASSWORD
            )
            yield conn
        except psycopg2.Error as e:
            logger.error(f"[PermissionManager] PG连接失败: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def get_user_role(self, staff_id: str) -> Optional[dict]:
        """
        根据钉钉 staffId 获取用户角色信息。

        Args:
            staff_id: 钉钉消息中的 senderStaffId

        Returns:
            成功: {
                'staff_id': '...',
                'staff_name': '...',
                'role_id': 'content',
                'role_name': '内容运营',
                'allowed_tables': ['table1', 'table2', ...],
                'mandatory_filters': {'table_name': 'condition'},
                'max_query_days': 60,
                'allow_user_detail': True
            }
            未找到: None（默认拒绝策略）
        """
        # 检查缓存
        cached = self._role_cache.get(staff_id)
        if cached and cached['expires_at'] > time.time():
            return cached['role_data']

        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("""
                        SELECT up.staff_id, up.staff_name, up.role_id, up.expires_at,
                               rd.role_name, rd.allowed_tables, rd.mandatory_filters,
                               rd.max_query_days, rd.allow_user_detail
                        FROM user_permissions up
                        JOIN role_definitions rd ON up.role_id = rd.role_id
                        WHERE up.staff_id = %s
                          AND up.is_active = TRUE
                          AND rd.is_active = TRUE
                          AND (up.expires_at IS NULL OR up.expires_at > NOW())
                    """, (staff_id,))
                    row = cur.fetchone()

            if not row:
                logger.warning(f"[PermissionManager] 未找到用户权限: staff_id={staff_id}")
                return None

            role_data = {
                'staff_id': row['staff_id'],
                'staff_name': row['staff_name'] or '',
                'role_id': row['role_id'],
                'role_name': row['role_name'],
                'allowed_tables': row['allowed_tables'] or [],
                'mandatory_filters': row['mandatory_filters'] or {},
                'max_query_days': row['max_query_days'] or 30,
                'allow_user_detail': row['allow_user_detail'] or False,
            }

            # 写入缓存
            self._role_cache[staff_id] = {
                'role_data': role_data,
                'expires_at': time.time() + CACHE_TTL_SECONDS
            }

            logger.info(f"[PermissionManager] 用户 {staff_id} -> 角色 {role_data['role_id']}")
            return role_data

        except Exception as e:
            logger.error(f"[PermissionManager] 查询用户角色异常: {e}")
            # 异常时默认拒绝（安全优先）
            return None

    def get_allowed_tables(self, user_role: dict) -> list:
        """获取角色可查询的表白名单"""
        return user_role.get('allowed_tables', [])

    def get_mandatory_filters(self, user_role: dict) -> dict:
        """获取角色的强制WHERE条件映射

        Returns:
            {'table_name': 'SQL条件表达式', ...}
        """
        filters = user_role.get('mandatory_filters', {})
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except (json.JSONDecodeError, TypeError):
                filters = {}
        return filters

    def extract_tables_from_sql(self, sql: str) -> list:
        """
        从SQL语句中提取所有引用的表名（FROM、JOIN）。

        使用 sqlparse 解析 + 正则兜底，确保提取准确。

        Args:
            sql: SQL语句

        Returns:
            去重后的表名列表
        """
        tables = set()

        # 方法1: 正则提取（兜底，覆盖大部分情况）
        pattern = r'(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
        matches = re.findall(pattern, sql, re.IGNORECASE)
        for m in matches:
            if m.upper() not in ('SELECT', 'WHERE', 'AND', 'OR', 'ON', 'AS', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'NATURAL', 'FULL'):
                tables.add(m.lower())

        # 方法2: sqlparse AST解析（更精确）
        try:
            import sqlparse
            from sqlparse.sql import IdentifierList, Identifier, Parenthesis
            from sqlparse.tokens import Keyword
            parsed = sqlparse.parse(sql)
            for statement in parsed:
                self._extract_tables_from_token(statement, tables)
        except Exception as e:
            logger.debug(f"[PermissionManager] sqlparse解析异常(使用正则兜底): {e}")

        return list(tables)

    def _extract_tables_from_token(self, token, tables: set):
        """递归从sqlparse token树中提取表名"""
        from_seen = False
        import sqlparse
        from sqlparse.sql import IdentifierList, Identifier, Parenthesis
        from sqlparse.tokens import Keyword
        for item in token.tokens:
            if item.ttype is Keyword and item.value.upper() in ('FROM', 'JOIN', 'INNER JOIN', 'LEFT JOIN', 'RIGHT JOIN', 'LEFT OUTER JOIN', 'RIGHT OUTER JOIN', 'FULL JOIN', 'CROSS JOIN'):
                from_seen = True
            elif from_seen:
                if isinstance(item, Identifier):
                    name = item.get_name()
                    if name:
                        tables.add(name.lower())
                    from_seen = False
                elif isinstance(item, IdentifierList):
                    for identifier in item.get_identifiers():
                        if isinstance(identifier, Identifier):
                            name = identifier.get_name()
                            if name:
                                tables.add(name.lower())
                    from_seen = False
                elif item.ttype is not sqlparse.tokens.Whitespace:
                    from_seen = False
            if isinstance(item, Parenthesis):
                self._extract_tables_from_token(item, tables)

    def check_sql_permission(self, sql: str, user_role: dict) -> tuple:
        """
        校验SQL语句是否符合用户角色权限。

        Args:
            sql: 待校验的SQL语句
            user_role: 用户角色字典

        Returns:
            (is_allowed: bool, denied_reason: str or None)
        """
        if not sql or not user_role:
            return False, "SQL或角色信息为空"

        # admin 角色直接放行
        if user_role.get('role_id') == 'admin':
            return True, None

        # 提取SQL中的表名
        tables = self.extract_tables_from_sql(sql)
        if not tables:
            return False, "无法从SQL中提取表名"

        # 校验表名白名单
        allowed = set(t.lower() for t in self.get_allowed_tables(user_role))
        for table in tables:
            if table.lower() not in allowed:
                return False, f"无权查询表 '{table}'，您的角色({user_role['role_name']})不包含此表的访问权限"

        # 校验用户明细权限
        if not user_role.get('allow_user_detail', False):
            sql_upper = sql.upper()
            has_distinct_user = 'DISTINCT USER_ID' in sql_upper or 'DISTINCT UID' in sql_upper
            has_group_by = 'GROUP BY' in sql_upper
            select_match = re.search(r'SELECT\s+(.*?)\s+FROM', sql, re.IGNORECASE | re.DOTALL)
            if select_match:
                select_clause = select_match.group(1).upper()
                if ('USER_ID' in select_clause or 'UID' in select_clause) and not has_distinct_user:
                    if not has_group_by:
                        return False, f"您的角色({user_role['role_name']})不允许查看用户级明细数据，请使用聚合查询"

        return True, None

    def enforce_sql(self, sql: str, user_role: dict) -> str:
        """
        对SQL注入强制权限条件，返回处理后的SQL。

        如果角色为admin，原样返回。
        如果权限校验失败，抛出PermissionDenied异常。

        Args:
            sql: 原始SQL
            user_role: 用户角色字典

        Returns:
            注入权限条件后的SQL字符串

        Raises:
            PermissionDenied: 当SQL访问了未授权的表
        """
        if not sql or not user_role:
            raise PermissionDenied("SQL或角色信息为空")

        # admin 直接放行
        if user_role.get('role_id') == 'admin':
            return sql

        # Step 1: 表名白名单校验
        is_allowed, denied_reason = self.check_sql_permission(sql, user_role)
        if not is_allowed:
            raise PermissionDenied(denied_reason)

        # Step 2: 注入强制过滤条件
        mandatory_filters = self.get_mandatory_filters(user_role)
        if not mandatory_filters:
            return sql

        # 提取SQL中使用的表名，对匹配的表注入条件
        tables_in_sql = self.extract_tables_from_sql(sql)
        conditions_to_inject = []
        for table in tables_in_sql:
            table_lower = table.lower()
            for filter_table, filter_condition in mandatory_filters.items():
                if filter_table.lower() == table_lower:
                    conditions_to_inject.append(filter_condition)

        if not conditions_to_inject:
            return sql

        # 注入WHERE条件
        enforced_sql = self._inject_where_conditions(sql, conditions_to_inject)
        logger.info(f"[PermissionManager] SQL权限注入: 追加 {len(conditions_to_inject)} 个条件")
        return enforced_sql

    def _inject_where_conditions(self, sql: str, conditions: list) -> str:
        """
        在SQL的WHERE子句中追加强制条件。

        策略：
        - 如果已有WHERE：在WHERE后的条件外包一层括号，再AND追加
        - 如果没有WHERE：在FROM子句后、GROUP BY/ORDER BY/LIMIT前插入WHERE
        """
        combined_condition = " AND ".join(f"({c})" for c in conditions)

        # 检查是否已有WHERE
        where_match = re.search(r'\bWHERE\b', sql, re.IGNORECASE)

        if where_match:
            # 已有WHERE：在WHERE关键字后的原始条件前加括号，然后AND追加
            where_pos = where_match.end()
            # 找到WHERE之后到GROUP BY/ORDER BY/LIMIT/结尾的范围
            tail_match = re.search(r'\b(GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|UNION)\b', sql[where_pos:], re.IGNORECASE)
            if tail_match:
                tail_start = where_pos + tail_match.start()
                original_condition = sql[where_pos:tail_start].strip()
                enforced = sql[:where_pos] + f" ({original_condition}) AND {combined_condition} " + sql[tail_start:]
            else:
                original_condition = sql[where_pos:].strip()
                enforced = sql[:where_pos] + f" ({original_condition}) AND {combined_condition}"
        else:
            # 没有WHERE：在FROM子句后插入
            tail_match = re.search(r'\b(GROUP\s+BY|ORDER\s+BY|LIMIT|HAVING|UNION)\b', sql, re.IGNORECASE)
            if tail_match:
                insert_pos = tail_match.start()
                enforced = sql[:insert_pos] + f" WHERE {combined_condition} " + sql[insert_pos:]
            else:
                enforced = sql + f" WHERE {combined_condition}"

        return enforced

    def clear_cache(self, staff_id: str = None):
        """清除缓存（手动刷新权限时使用）"""
        if staff_id:
            self._role_cache.pop(staff_id, None)
        else:
            self._role_cache.clear()
            self._role_def_cache.clear()
        logger.info(f"[PermissionManager] 缓存已清除: {'all' if not staff_id else staff_id}")

    def log_permission_audit(self, staff_id: str, staff_name: str, role_id: str,
                             action: str, original_sql: str = None, enforced_sql: str = None,
                             denied_reason: str = None, tables_accessed: list = None,
                             query_result_rows: int = 0, duration_ms: int = 0):
        """记录权限审计日志"""
        try:
            with self._get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO permission_audit_log
                        (staff_id, staff_name, role_id, action, original_sql, enforced_sql,
                         denied_reason, tables_accessed, query_result_rows, duration_ms)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        staff_id, staff_name, role_id, action,
                        original_sql, enforced_sql, denied_reason,
                        tables_accessed, query_result_rows, duration_ms
                    ))
                conn.commit()
        except Exception as e:
            logger.error(f"[PermissionManager] 审计日志写入失败: {e}")


# 全局单例
permission_manager = PermissionManager()
