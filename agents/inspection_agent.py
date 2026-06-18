"""数据核查探查智能体 (Data Inspection Agent)

职责：在Query Agent返回数据后，对数据进行全面的质量核查和探查，
     生成结构化核查报告，传递给Analysis Agent作为分析参考。

探查维度：
1. 完整性检查 - 空值率、缺失字段
2. 一致性检查 - 数据类型、值域范围
3. 准确性检查 - 与预期模式的偏差
4. 时效性检查 - 数据新鲜度
5. 统计画像 - 数值列的分布特征

位置：Query Agent → Inspection Agent → Analysis Agent
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 北京时间
TZ = timezone(timedelta(hours=8))


class InspectionAgent:
    """数据核查探查智能体"""

    # 探查配置
    NULL_THRESHOLD_WARN = 0.1       # 空值率超过10%告警
    NULL_THRESHOLD_ERROR = 0.3      # 空值率超过30%视为严重
    ROW_COUNT_LOW_THRESHOLD = 5     # 少于5行视为数据稀疏
    STALE_DAYS_WARN = 2             # 数据超过2天未更新告警

    # 日期列名候选（用于时效性检查）
    DATE_COLUMN_CANDIDATES = [
        'date', 'dt', 'dt_utc', 'stat_date', 'server_day',
        'd_date', 'created_time', 'create_time', 'day', 'report_date',
    ]

    def inspect(self, query_result: dict, user_query: str) -> dict:
        """
        对查询结果执行全面核查探查。

        Args:
            query_result: Query Agent 返回的结果字典
                {success, cols, rows, row_count, sql_executed, retries}
            user_query: 用户原始问题

        Returns:
            核查报告字典:
            {
                'checks_passed': bool,       # 是否通过所有检查
                'warning_count': int,        # 告警数
                'error_count': int,          # 错误数
                'completeness': {...},       # 完整性报告
                'consistency': {...},        # 一致性报告
                'freshness': {...},          # 时效性报告
                'statistical_profile': {...},# 统计画像
                'summary': str,              # 文字摘要
                'issues': [...],             # 问题列表
            }
        """
        cols = query_result.get('cols', [])
        rows = query_result.get('rows', [])
        row_count = query_result.get('row_count', 0)
        sql = query_result.get('sql_executed', '')

        issues = []
        checks = []

        # === 1. 完整性检查 ===
        completeness = self._check_completeness(cols, rows, row_count)
        checks.append(('completeness', completeness['status']))
        if completeness['issues']:
            issues.extend(completeness['issues'])

        # === 2. 一致性检查 ===
        consistency = self._check_consistency(cols, rows)
        checks.append(('consistency', consistency['status']))
        if consistency['issues']:
            issues.extend(consistency['issues'])

        # === 3. 时效性检查 ===
        freshness = self._check_freshness(cols, rows, user_query)
        checks.append(('freshness', freshness['status']))
        if freshness['issues']:
            issues.extend(freshness['issues'])

        # === 4. 统计画像 ===
        statistical_profile = self._compute_statistical_profile(cols, rows)

        # === 汇总 ===
        error_count = sum(1 for _, s in checks if s == 'error')
        warning_count = sum(1 for _, s in checks if s == 'warning')
        checks_passed = error_count == 0

        summary_parts = []
        if checks_passed:
            summary_parts.append("✅ 数据核查通过")
        else:
            summary_parts.append(f"⚠️ 数据核查发现 {error_count} 个错误, {warning_count} 个告警")
        if row_count == 0:
            summary_parts.append("查询返回0行数据")
        else:
            summary_parts.append(
                f"共 {row_count} 行, {len(cols)} 列"
            )
        if completeness['null_columns']:
            summary_parts.append(
                f"含空值列: {', '.join(completeness['null_columns'][:3])}"
            )
        if freshness.get('max_date'):
            summary_parts.append(f"最新数据日期: {freshness['max_date']}")

        summary = "; ".join(summary_parts)

        report = {
            'checks_passed': checks_passed,
            'warning_count': warning_count,
            'error_count': error_count,
            'completeness': completeness,
            'consistency': consistency,
            'freshness': freshness,
            'statistical_profile': statistical_profile,
            'summary': summary,
            'issues': issues,
        }

        logger.info(
            f"[InspectionAgent] 核查完成: passed={checks_passed}, "
            f"warnings={warning_count}, errors={error_count}"
        )
        return report

    # ================================================================
    # 完整性检查
    # ================================================================

    def _check_completeness(
        self, cols: list, rows: list, row_count: int
    ) -> dict:
        """检查数据完整性：空值率、行数"""
        issues = []
        null_columns = []

        if not rows:
            return {
                'status': 'error',
                'null_ratio': 1.0,
                'null_columns': [],
                'row_count': 0,
                'issues': [{'level': 'error', 'msg': '查询返回空数据集'}],
            }

        # 检查每列的空值率
        for col_idx, col_name in enumerate(cols):
            null_count = sum(
                1 for row in rows
                if col_idx >= len(row) or row[col_idx] is None or row[col_idx] == ''
            )
            null_ratio = null_count / max(len(rows), 1)

            if null_ratio >= self.NULL_THRESHOLD_ERROR:
                issues.append({
                    'level': 'error',
                    'msg': f'列 "{col_name}" 空值率 {null_ratio:.0%}（≥{self.NULL_THRESHOLD_ERROR:.0%}），数据严重缺失',
                })
                null_columns.append(col_name)
            elif null_ratio >= self.NULL_THRESHOLD_WARN:
                issues.append({
                    'level': 'warning',
                    'msg': f'列 "{col_name}" 空值率 {null_ratio:.0%}（≥{self.NULL_THRESHOLD_WARN:.0%}）',
                })
                null_columns.append(col_name)

        # 行数检查
        if row_count < self.ROW_COUNT_LOW_THRESHOLD:
            issues.append({
                'level': 'warning',
                'msg': f'数据行数较少（{row_count}行），分析结论置信度可能不足',
            })

        # 确定状态
        has_error = any(i['level'] == 'error' for i in issues)
        has_warning = any(i['level'] == 'warning' for i in issues)
        status = 'error' if has_error else ('warning' if has_warning else 'ok')

        return {
            'status': status,
            'null_columns': null_columns,
            'row_count': row_count,
            'issues': issues,
        }

    # ================================================================
    # 一致性检查
    # ================================================================

    def _check_consistency(self, cols: list, rows: list) -> dict:
        """检查数据一致性：值域、异常值"""
        issues = []

        if not rows:
            return {'status': 'error', 'issues': issues}

        for col_idx, col_name in enumerate(cols):
            col_name_lower = col_name.lower()

            # 检查比率/百分比列的值域
            if any(kw in col_name_lower for kw in ['rate', 'ratio', 'pct', '率', '比']):
                out_of_range = []
                for row in rows:
                    if col_idx < len(row):
                        val = row[col_idx]
                        if val is not None and isinstance(val, (int, float)):
                            if val < 0:
                                out_of_range.append(val)
                if out_of_range:
                    issues.append({
                        'level': 'warning',
                        'msg': f'列 "{col_name}" 存在 {len(out_of_range)} 个负值（比率类指标不应为负）',
                    })

            # 检查计数字段的合理性
            if any(kw in col_name_lower for kw in ['cnt', 'count', 'num', '数', '量', '人数']):
                negative_vals = []
                for row in rows:
                    if col_idx < len(row):
                        val = row[col_idx]
                        if val is not None and isinstance(val, (int, float)) and val < 0:
                            negative_vals.append(val)
                if negative_vals:
                    issues.append({
                        'level': 'error',
                        'msg': f'列 "{col_name}" 存在 {len(negative_vals)} 个负值（计数类指标不应为负）',
                    })

        has_error = any(i['level'] == 'error' for i in issues)
        has_warning = any(i['level'] == 'warning' for i in issues)
        status = 'error' if has_error else ('warning' if has_warning else 'ok')

        return {
            'status': status,
            'issues': issues,
        }

    # ================================================================
    # 时效性检查
    # ================================================================

    def _check_freshness(
        self, cols: list, rows: list, user_query: str
    ) -> dict:
        """检查数据时效性"""
        issues = []
        max_date = None
        stale_days = None

        if not rows:
            return {
                'status': 'ok',
                'max_date': None,
                'stale_days': None,
                'issues': [],
            }

        # 找到日期列
        date_col_idx = None
        for idx, col_name in enumerate(cols):
            col_lower = col_name.lower()
            for candidate in self.DATE_COLUMN_CANDIDATES:
                if candidate in col_lower:
                    date_col_idx = idx
                    break
            if date_col_idx is not None:
                break

        if date_col_idx is None:
            return {
                'status': 'ok',
                'max_date': None,
                'stale_days': None,
                'issues': [],
            }

        # 找最大日期
        import re
        from datetime import date as date_type

        max_val = None
        for row in rows:
            if date_col_idx < len(row):
                val = row[date_col_idx]
                if val is not None:
                    parsed = self._parse_date(val)
                    if parsed and (max_val is None or parsed > max_val):
                        max_val = parsed

        if max_val is None:
            return {
                'status': 'ok',
                'max_date': None,
                'stale_days': None,
                'issues': [],
            }

        max_date = str(max_val)
        if isinstance(max_val, date_type):
            today = datetime.now(TZ).date()
            stale_days = (today - max_val).days
        elif isinstance(max_val, datetime):
            now = datetime.now(TZ)
            stale_days = (now - max_val).days

        if stale_days is not None and stale_days > self.STALE_DAYS_WARN:
            issues.append({
                'level': 'warning',
                'msg': f'最新数据日期为 {max_date}，已滞后 {stale_days} 天，数据可能不是最新的',
            })

        return {
            'status': 'warning' if issues else 'ok',
            'max_date': max_date,
            'stale_days': stale_days,
            'issues': issues,
        }

    def _parse_date(self, val):
        """尝试解析各种日期格式"""
        import re
        from datetime import date as date_type, datetime as dt_type

        if isinstance(val, (date_type, dt_type)):
            return val

        if not isinstance(val, str):
            return None

        formats = [
            '%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
            '%Y%m%d', '%Y/%m/%d', '%d/%m/%Y',
        ]

        # 提取日期部分
        date_match = re.match(r'(\d{4}-\d{2}-\d{2})', str(val))
        if date_match:
            val = date_match.group(1)

        for fmt in formats:
            try:
                return dt_type.strptime(str(val)[:19], fmt).date()
            except (ValueError, TypeError):
                continue
        return None

    # ================================================================
    # 统计画像
    # ================================================================

    def _compute_statistical_profile(self, cols: list, rows: list) -> dict:
        """计算数值列的统计画像"""
        if not rows or not cols:
            return {'numeric_columns': [], 'profiles': {}}

        profiles = {}
        numeric_columns = []

        for col_idx, col_name in enumerate(cols):
            values = []
            for row in rows:
                if col_idx < len(row):
                    val = row[col_idx]
                    if val is not None and isinstance(val, (int, float)):
                        values.append(val)

            if not values:
                continue

            numeric_columns.append(col_name)
            sorted_vals = sorted(values)
            n = len(sorted_vals)

            profiles[col_name] = {
                'count': n,
                'min': sorted_vals[0],
                'max': sorted_vals[-1],
                'mean': round(sum(values) / n, 2),
                'median': sorted_vals[n // 2],
                'p25': sorted_vals[max(0, n // 4)],
                'p75': sorted_vals[min(n - 1, 3 * n // 4)],
            }

            # 标准差
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / n
            profiles[col_name]['std'] = round(variance ** 0.5, 2)

            # 变异系数
            if mean != 0:
                profiles[col_name]['cv'] = round(
                    (variance ** 0.5) / abs(mean), 3
                )

        return {
            'numeric_columns': numeric_columns,
            'profiles': profiles,
        }

    # ================================================================
    # 生成供 Analysis Agent 使用的上下文文本
    # ================================================================

    def format_for_analysis(self, report: dict) -> str:
        """将核查报告格式化为 Analysis Agent 可用的上下文文本"""
        if not report:
            return ""

        lines = ["## 🔍 数据核查报告", ""]

        # 总体状态
        lines.append(f"核查状态: {'✅ 通过' if report['checks_passed'] else '⚠️ 存在问题'}")
        lines.append(f"摘要: {report.get('summary', 'N/A')}")
        lines.append("")

        # 统计画像（关键指标）
        profile = report.get('statistical_profile', {})
        if profile.get('numeric_columns'):
            lines.append("### 数值列统计画像")
            for col_name, stats in profile.get('profiles', {}).items():
                lines.append(
                    f"- **{col_name}**: "
                    f"n={stats['count']}, "
                    f"min={stats['min']}, "
                    f"max={stats['max']}, "
                    f"均值={stats['mean']}, "
                    f"中位数={stats['median']}"
                )
                if 'cv' in stats:
                    lines.append(f"  标准差={stats['std']}, 变异系数={stats['cv']}")
            lines.append("")

        # 问题列表
        issues = report.get('issues', [])
        if issues:
            lines.append("### 发现的问题")
            for issue in issues:
                icon = '🔴' if issue['level'] == 'error' else '🟡'
                lines.append(f"- {icon} {issue['msg']}")
            lines.append("")

        # 提示
        lines.extend([
            "> 请在分析中注意以上数据质量问题。",
            "> 如存在空值或异常值，在解读数据时需标注不确定性。",
            "> 时效性告警时，请在分析中明确说明数据的截止日期。",
        ])

        return "\n".join(lines)


# 全局单例
inspection_agent = InspectionAgent()
