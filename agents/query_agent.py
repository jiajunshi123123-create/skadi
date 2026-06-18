"""Query Agent - SQL执行 + 自愈重试

职责：接收Plan Agent生成的SQL，通过EXPLAIN校验后执行，支持错误自愈重试。
输出：查询结果字典（包含cols、rows、row_count等字段）
"""
import os
import re
import logging
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.agent_config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    QUERY_MODEL, QUERY_TEMPERATURE, QUERY_MAX_TOKENS,
    MAX_SELF_HEAL_RETRIES,
    PROMPTS_DIR
)
from tools.database_adapter import DatabaseAdapter
from utils.pre_sql_hook import pre_sql_hook, HookExitCode

logger = logging.getLogger(__name__)

# 各表的分区键映射，用于空结果验证查询
TABLE_PARTITION_KEYS = {
    'table_new_users': 'date',
    'table_dau_events': 'dt_utc',
    'table_core_behavior': 'dt_utc',
    'table_product_usage': 'dt',
}


class QueryAgent:
    """Query Agent - SQL执行与自愈"""

    def __init__(self):
        self.llm = ChatOpenAI(
            model=QUERY_MODEL,
            openai_api_key=DEEPSEEK_API_KEY,
            openai_api_base=DEEPSEEK_BASE_URL,
            temperature=QUERY_TEMPERATURE,
            max_tokens=QUERY_MAX_TOKENS,
        )
        self.system_prompt = self._load_prompt()
        self.max_retries = MAX_SELF_HEAL_RETRIES
        # 统一数据源适配器（替代旧 subprocess 调用）
        self.db = DatabaseAdapter.create()

    def _load_prompt(self) -> str:
        """加载Query Agent系统提示词"""
        prompt_path = os.path.join(PROMPTS_DIR, 'query_prompt.md')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read()

    async def execute(self, plan_task: dict) -> dict:
        """
        执行SQL查询，支持自愈重试。

        Args:
            plan_task: Plan Agent输出的任务字典，包含sql字段

        Returns:
            成功: {'success': True, 'cols': [...], 'rows': [...], 'row_count': N, 'sql_executed': '...', 'retries': N}
            失败: {'success': False, 'error': '...', 'sql_attempted': '...', 'retries': N}
        """
        sql = plan_task.get('sql', '')
        if not sql:
            return {
                'success': False,
                'error': 'Plan Task中缺少SQL语句',
                'sql_attempted': '',
                'retries': 0
            }

        last_error = None

        for attempt in range(self.max_retries + 1):
            logger.info(f"[QueryAgent] 执行SQL (尝试 {attempt + 1}/{self.max_retries + 1}): {sql[:80]}...")

            result = self._run_query(sql)

            if result['success']:
                # ---- 空结果验证：区分"真无数据"与"SQL逻辑错误" ----
                if result.get('row_count', 0) == 0 and not result.get('rows'):
                    logger.info("[QueryAgent] 查询返回0行，执行表数据验证...")
                    verification = self._verify_table_has_data(sql)
                    if verification['has_recent_data']:
                        # 表有近期数据但查询为空 → SQL日期范围或逻辑有误 → 触发自愈
                        last_error = (
                            f"SQL执行成功但返回0行。"
                            f"验证查询显示表 {verification['table_name']} 最新数据日期为 "
                            f"{verification['max_date']}，说明SQL的日期范围或过滤逻辑有误。"
                            f"请检查：1) 日期范围是否覆盖了有数据的区间；"
                            f"2) DATE类型字段（如date、dt）是否被DATETIME函数错误包装"
                            f"（如DATE_FORMAT、HOUR等）；"
                            f"3) 时区转换（UTC转北京时间）是否导致日期偏移。"
                        )
                        logger.warning(f"[QueryAgent] 空结果自愈触发: {last_error}")
                        if attempt < self.max_retries:
                            healed_sql = await self._self_heal(sql, last_error, plan_task)
                            if healed_sql and healed_sql != sql:
                                sql = healed_sql
                                logger.info(f"[QueryAgent] 空结果自愈修正SQL: {sql[:80]}...")
                                continue
                            else:
                                logger.warning("[QueryAgent] 空结果自愈未产生有效修正，返回空结果")
                    else:
                        logger.info(
                            f"[QueryAgent] 验证确认表无近期数据（max_date={verification['max_date']}），"
                            "空结果属正常"
                        )
                # ---- 空结果验证结束 ----

                result['sql_executed'] = sql
                result['retries'] = attempt
                logger.info(f"[QueryAgent] 查询成功，返回 {result.get('row_count', 0)} 行")
                # 自愈成功（重试后才成功）时，记录为 lesson
                if attempt > 0 and last_error:
                    try:
                        from learning.feedback_loop import feedback_loop
                        feedback_loop.on_sql_self_heal(
                            original_sql=plan_task.get('sql', ''),
                            error=last_error,
                            fixed_sql=sql,
                            user_query=''  # 这里没有 user_query 上下文
                        )
                    except Exception:
                        pass
                return result

            last_error = result['error']
            logger.warning(f"[QueryAgent] 查询失败 (尝试 {attempt + 1}): {last_error[:100]}")

            # 如果还有重试机会，尝试自愈
            if attempt < self.max_retries:
                healed_sql = await self._self_heal(sql, last_error, plan_task)
                if healed_sql and healed_sql != sql:
                    sql = healed_sql
                    logger.info(f"[QueryAgent] 自愈修正SQL: {sql[:80]}...")
                else:
                    # 自愈未能产生新SQL，终止重试
                    logger.warning("[QueryAgent] 自愈未产生有效修正，终止重试")
                    break

        return {
            'success': False,
            'error': last_error or '未知错误',
            'sql_attempted': sql,
            'retries': min(attempt + 1, self.max_retries)
        }

    def _run_query(self, sql: str) -> dict:
        """
        通过统一数据源适配器执行查询。
        适配器内置 SQL 安全检查和 EXPLAIN 校验。

        [历史] 旧实现通过 subprocess 调用 starrocks_query_safe.py，
        现已迁移为 DatabaseAdapter 直连方式，支持 MySQL/PG/StarRocks。
        """
        # === P1-3: PreSQLUse Hook — SQL执行前校验 ===
        hook_result = pre_sql_hook.validate(sql)
        if hook_result.exit_code == HookExitCode.BLOCK:
            logger.error(f"[QueryAgent] PreSQLHook 阻止执行: {hook_result.message}")
            return {
                'success': False,
                'error': f"PreSQLHook校验失败: {hook_result.message}",
                'cols': [], 'rows': [], 'row_count': 0
            }
        elif hook_result.exit_code == HookExitCode.WARN:
            logger.warning(
                f"[QueryAgent] PreSQLHook 警告({hook_result.checks_passed}P/{hook_result.checks_failed}F): "
                f"{'; '.join(hook_result.warnings[:2])}"
            )

        # 先做 EXPLAIN 预校验
        explain_result = self.db.explain(sql)
        if not explain_result['success']:
            return {
                'success': False,
                'error': f"EXPLAIN校验失败: {explain_result['error']}",
                'cols': [], 'rows': [], 'row_count': 0
            }

        # 执行实际查询
        return self.db.execute(sql)

    def _extract_table_name(self, sql: str) -> Optional[str]:
        """
        从 SQL 中提取主 FROM 后面的第一个表名，支持 schema.table 格式。
        """
        # 去掉行内注释和块注释，规范化空白
        sql_clean = re.sub(r'--[^\n]*', '', sql)
        sql_clean = re.sub(r'/\*.*?\*/', '', sql_clean, flags=re.DOTALL)
        sql_clean = ' '.join(sql_clean.split())

        # 匹配 FROM 后面的表名（支持 schema.table 和反引号/引号包裹）
        pattern = r'\bFROM\s+([`"]?[\w]+[`"]?(?:\.[`"]?[\w]+[`"]?)?)'  
        match = re.search(pattern, sql_clean, re.IGNORECASE)
        if match:
            return match.group(1).replace('`', '').replace('"', '')
        return None

    def _verify_table_has_data(self, sql: str) -> dict:
        """
        验证主查询访问的表是否存在近期数据。
        通过 DatabaseAdapter.get_max_partition() 获取最大分区值。

        Returns:
            {'has_recent_data': bool, 'max_date': str or None, 'table_name': str or None}
        """
        table_name = self._extract_table_name(sql)
        if not table_name:
            logger.warning("[QueryAgent] 无法从 SQL 中提取表名，跳过空结果验证")
            return {'has_recent_data': False, 'max_date': None, 'table_name': None}

        # 支持 schema.table 格式，取短表名查分区键
        short_name = table_name.split('.')[-1]
        partition_key = TABLE_PARTITION_KEYS.get(short_name)
        if not partition_key:
            logger.warning(
                f"[QueryAgent] 表 {table_name} 无分区键配置，跳过空结果验证"
            )
            return {'has_recent_data': False, 'max_date': None, 'table_name': table_name}

        logger.info(f"[QueryAgent] 执行验证查询: MAX({partition_key}) FROM {table_name}")
        max_date = self.db.get_max_partition(table_name, partition_key)

        has_data = max_date is not None and str(max_date).lower() not in ('none', 'null', '')

        return {
            'has_recent_data': has_data,
            'max_date': max_date if has_data else None,
            'table_name': table_name,
        }

    async def _self_heal(self, failed_sql: str, error: str, plan_task: dict) -> Optional[str]:
        """
        分析错误，调用LLM修正SQL。

        Returns:
            修正后的SQL字符串，或None（无法修正时）
        """
        heal_prompt = f"""SQL执行失败，请分析错误并修正SQL。

原始SQL:
```sql
{failed_sql}
```

错误信息:
{error}

查询意图: {plan_task.get('intent', '未知')}
目标表: {plan_task.get('table', '未知')}

数据字典参考:
- table_dau_events: dt_utc(DATETIME分区键), user_id, raw_event_name, std_event_name, event_value
- table_core_behavior: dt_utc(DATETIME分区键), user_id, event_cnt, user_cnt
- table_product_usage: dt(DATETIME分区键), uid(不是user_id), books_id(不是book_id), name(书名，事实表内嵌)
- table_new_users: date(DATE分区键), user_id

请只输出修正后的SQL（纯SQL，不要包含解释文字或代码块标记）。
如果无法修正，输出: CANNOT_FIX"""

        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=heal_prompt),
        ]

        try:
            response = await self.llm.ainvoke(messages)
            healed = response.content.strip()

            # 清理可能的代码块标记
            healed = re.sub(r'^```(?:sql)?\s*\n?', '', healed)
            healed = re.sub(r'\n?\s*```$', '', healed)
            healed = healed.strip()

            if 'CANNOT_FIX' in healed or not healed:
                return None

            # 基本安全检查：只允许SELECT
            if not healed.upper().lstrip().startswith('SELECT'):
                logger.warning(f"[QueryAgent] 自愈产生了非SELECT语句，拒绝")
                return None

            return healed

        except Exception as e:
            logger.error(f"[QueryAgent] 自愈调用LLM失败: {e}")
            return None
