"""PreSQLUse Hook - SQL执行前校验钩子

借鉴 Claude Code 的 Hook 系统 (PreToolUse)：
- 退出码 0: 通过，允许执行
- 退出码 1: 警告，记录但允许执行
- 退出码 2: 阻止，拒绝执行

校验级别：
1. 安全校验: 禁止非SELECT语句
2. 模式校验: 危险SQL模式检测 (DROP/DELETE/ALTER)
3. 规模校验: 预估扫描行数告警 (基于EXPLAIN)
4. 白名单校验: 仅允许查询已注册的表
5. 复杂度假校验: 检测笛卡尔积/过度JOIN
"""

import re
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)


class HookExitCode(IntEnum):
    """Hook退出码"""
    PASS = 0      # 通过，允许执行
    WARN = 1      # 警告，记录但允许执行
    BLOCK = 2     # 阻止，拒绝执行


@dataclass
class HookResult:
    """Hook校验结果"""
    exit_code: HookExitCode
    message: str
    warnings: list = field(default_factory=list)
    checks_passed: int = 0
    checks_failed: int = 0
    estimated_rows: int = 0       # EXPLAIN预估行数
    is_safe: bool = True          # 是否安全


# ============================================================
# 危险模式定义
# ============================================================

# 绝对禁止的操作
FORBIDDEN_PATTERNS = [
    (r'\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW)\b', 'DROP操作'),
    (r'\bDELETE\s+FROM\b', 'DELETE操作'),
    (r'\bTRUNCATE\s+(TABLE\s+)?', 'TRUNCATE操作'),
    (r'\bALTER\s+(TABLE|DATABASE|SCHEMA)\b', 'ALTER操作'),
    (r'\bCREATE\s+(TABLE|DATABASE|SCHEMA)\b', 'CREATE操作'),
    (r'\bINSERT\s+INTO\b', 'INSERT操作'),
    (r'\bUPDATE\s+\w+\s+SET\b', 'UPDATE操作'),
    (r'\bGRANT\b', 'GRANT操作'),
    (r'\bREVOKE\b', 'REVOKE操作'),
]

# 警告模式（允许但需记录）
WARNING_PATTERNS = [
    (r'CROSS\s+JOIN', '笛卡尔积(CROSS JOIN) — 可能导致性能问题'),
    (r'JOIN.*JOIN.*JOIN.*JOIN', '超过3个JOIN — 注意查询性能'),
    (r'UNION\s+ALL.*UNION\s+ALL.*UNION\s+ALL', '超过2个UNION ALL — 考虑优化'),
    (r'SELECT\s+\*', 'SELECT * — 建议指定需要的列'),
    (r'\bLIKE\s+[\'"]%%.*%%[\'"]', 'LIKE双模糊匹配 — 索引失效风险'),
    (r'WHERE.*IN\s*\([^)]{200,}\)', 'IN子句过长 — 建议改用JOIN或子查询'),
]

# 白名单：优先从数据字典动态加载；不可用时回退内置最小白名单
def _load_allowed_tables_from_dd() -> set:
    try:
        from config.data_dictionary_loader import load_data_dictionary
        dd = load_data_dictionary()
        if dd and 'tables' in dd:
            tables = {t['name'] for t in dd['tables'] if t.get('role') != '禁用'}
            if tables:
                return tables
    except Exception:
        pass
    return {
        'dws_edu_ubb_bhv_app_daily_active_user',
        'dwd_edu_mrg_usr_new_user',
        'dws_edu_dayi_user_login_daily_stats',
        'dws_edu_mrg_usr_new_user_count',
        'qb_event_log_202604',
        'qb_event_log_202605',
        'qb_event_log_202606',
    }

ALLOWED_TABLES = _load_allowed_tables_from_dd()

# 表名别名映射（edu前缀 = 生产环境）
TABLE_ALIASES = {
    'dayi': 'dws_edu_dayi_user_login_daily_stats',
    'new_users': 'dws_edu_mrg_usr_new_user_count',
    'core_behavior': 'dwd_edu_bhv_maidian_user_core_behavior_daily',
}


class PreSQLHook:
    """SQL执行前校验钩子。

    借鉴 Claude Code 的 PreToolUse Hook 模式：
    - 在SQL执行前进行多层校验
    - 返回 HookResult 决定是否允许执行

    使用方式:
        hook = PreSQLHook()
        result = hook.validate(sql)
        if result.exit_code == HookExitCode.BLOCK:
            return error_response(result.message)
    """

    def __init__(self, strict_mode: bool = False):
        """初始化钩子。

        Args:
            strict_mode: 严格模式，警告也会阻止执行
        """
        self.strict_mode = strict_mode
        self._validation_count = 0

    def validate(
        self,
        sql: str,
        user_role: Optional[dict] = None,
        explain_result: Optional[dict] = None,
    ) -> HookResult:
        """对SQL进行全面校验。

        按顺序执行所有校验，收集所有警告和错误。

        Args:
            sql: 待执行的SQL语句
            user_role: 用户角色信息（可选）
            explain_result: EXPLAIN 预校验结果（可选）

        Returns:
            HookResult 包含校验结果
        """
        self._validation_count += 1
        warnings = []
        checks_passed = 0
        checks_failed = 0
        exit_code = HookExitCode.PASS

        sql_upper = sql.upper().strip()

        # ---- 校验1: 基本SQL类型检查 ----
        if not sql_upper.startswith('SELECT') and not sql_upper.startswith('EXPLAIN'):
            return HookResult(
                exit_code=HookExitCode.BLOCK,
                message=f'🚫 仅允许SELECT查询，当前SQL: {sql[:50]}...',
                is_safe=False,
            )
        checks_passed += 1

        # ---- 校验2: 危险操作检测 ----
        for pattern, description in FORBIDDEN_PATTERNS:
            if re.search(pattern, sql, re.IGNORECASE):
                checks_failed += 1
                return HookResult(
                    exit_code=HookExitCode.BLOCK,
                    message=f'🚫 检测到禁止操作: {description}',
                    warnings=warnings,
                    checks_passed=checks_passed,
                    checks_failed=checks_failed,
                    is_safe=False,
                )
        checks_passed += 1

        # ---- 校验3: 警告模式检测 ----
        for pattern, description in WARNING_PATTERNS:
            if re.search(pattern, sql, re.IGNORECASE):
                warnings.append(description)
                logger.warning(f"[PreSQLHook] 警告: {description} | SQL: {sql[:80]}...")

        if warnings and self.strict_mode:
            checks_failed += 1
            exit_code = HookExitCode.BLOCK
        elif warnings:
            checks_failed += 1
            exit_code = HookExitCode.WARN
        else:
            checks_passed += 1

        # ---- 校验4: 白名单表校验 ----
        tables = self._extract_tables(sql)
        unknown_tables = tables - ALLOWED_TABLES

        # 尝试别名解析
        resolved_unknown = set()
        for t in unknown_tables:
            if t.lower() in TABLE_ALIASES:
                resolved_unknown.add(t)  # 别名已解析，不算未知

        unknown_tables -= resolved_unknown

        if unknown_tables:
            unknown_list = ', '.join(sorted(unknown_tables))
            warn_msg = f'⚠️ 查询了未知表: {unknown_list}（可能未在数据字典中注册）'
            warnings.append(warn_msg)
            logger.warning(f"[PreSQLHook] {warn_msg}")
            if self.strict_mode:
                checks_failed += 1
                exit_code = HookExitCode.BLOCK
            else:
                checks_failed += 1
                exit_code = max(exit_code, HookExitCode.WARN)
        else:
            checks_passed += 1

        # ---- 校验5: EXPLAIN结果检查 ----
        estimated_rows = 0
        if explain_result and explain_result.get('success'):
            estimated_rows = explain_result.get('estimated_rows', 0)
            if estimated_rows > 1000000:  # 超过100万行告警
                warn_msg = f'⚠️ 预估扫描 {estimated_rows:,} 行数据，可能较慢'
                warnings.append(warn_msg)
                checks_failed += 1
                exit_code = max(exit_code, HookExitCode.WARN)
            elif estimated_rows > 500000:
                warn_msg = f'⚠️ 预估扫描 {estimated_rows:,} 行数据'
                warnings.append(warn_msg)
                checks_failed += 1
                exit_code = max(exit_code, HookExitCode.WARN)
            else:
                checks_passed += 1
        else:
            checks_passed += 1

        # 汇总消息
        if exit_code == HookExitCode.BLOCK:
            warnings_text = '; '.join(warnings[:3])
            message = (
                '🚫 SQL执行被阻止\n'
                f'校验结果: {checks_passed}通过 / {checks_failed}失败\n'
                f'原因: {warnings_text}'
            )
        elif exit_code == HookExitCode.WARN:
            warnings_text = '; '.join(warnings[:3])
            message = (
                '⚠️ SQL有潜在风险但允许执行\n'
                f'校验结果: {checks_passed}通过 / {checks_failed}警告\n'
                f'注意: {warnings_text}'
            )
        else:
            message = f'✅ SQL校验通过 ({checks_passed}项)'

        return HookResult(
            exit_code=exit_code,
            message=message,
            warnings=warnings,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            estimated_rows=estimated_rows,
            is_safe=(exit_code != HookExitCode.BLOCK),
        )

    def quick_check(self, sql: str) -> bool:
        """快速安全检查（仅检查危险操作）。

        适用于EXPLAIN之前的快速过滤。

        Args:
            sql: SQL语句

        Returns:
            True 如果安全
        """
        sql_upper = sql.upper().strip()
        if not sql_upper.startswith('SELECT') and not sql_upper.startswith('EXPLAIN'):
            return False

        for pattern, _ in FORBIDDEN_PATTERNS:
            if re.search(pattern, sql, re.IGNORECASE):
                return False

        return True

    def _extract_tables(self, sql: str) -> set:
        """从SQL中提取表名集合。"""
        sql_clean = ' '.join(re.sub(r'--[^\n]*', '', sql).split())
        sql_clean = re.sub(r'/\*.*?\*/', '', sql_clean, flags=re.DOTALL)
        pattern = r'\b(?:FROM|JOIN)\s+([`"]?[\w]+[`"]?(?:\.[`"]?[\w]+[`"]?)?)'
        matches = re.findall(pattern, sql_clean, re.IGNORECASE)
        return {m.replace('`', '').replace('"', '').split('.')[-1] for m in matches}

    def get_stats(self) -> dict:
        """获取Hook统计。"""
        return {
            'validation_count': self._validation_count,
            'strict_mode': self.strict_mode,
        }


# 全局实例（非严格模式，允许警告通过）
pre_sql_hook = PreSQLHook(strict_mode=False)
