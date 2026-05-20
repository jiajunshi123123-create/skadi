"""Plan Agent - 意图理解 + SQL生成

职责：接收用户自然语言查询，理解查询意图，生成可执行的SQL查询计划。
输出：结构化 PlanTask 字典（包含intent、sql、table、metrics等字段）
"""
import os
import re
import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.agent_config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    PLAN_MODEL, PLAN_TEMPERATURE, PLAN_MAX_TOKENS,
    PROMPTS_DIR
)
from config.data_dictionary_loader import get_data_dictionary_prompt

logger = logging.getLogger(__name__)

# Plan prompt 中用于占位的标记，会在加载时被动态生成的数据字典 Markdown 替换
DATA_DICTIONARY_PLACEHOLDER = '{DATA_DICTIONARY}'


class PlanAgent:
    """Plan Agent - 将自然语言转化为SQL执行计划"""

    def __init__(self):
        self.llm = ChatOpenAI(
            model=PLAN_MODEL,
            openai_api_key=DEEPSEEK_API_KEY,
            openai_api_base=DEEPSEEK_BASE_URL,
            temperature=PLAN_TEMPERATURE,
            max_tokens=PLAN_MAX_TOKENS,
        )
        self.system_prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        """加载 Plan Agent 系统提示词，并注入动态数据字典。

        提示词模板中使用 ``{DATA_DICTIONARY}`` 作为占位符，启动时由
        ``config/data_dictionary.yml`` 渲染出的 Markdown 替换。
        """
        prompt_path = os.path.join(PROMPTS_DIR, 'plan_prompt.md')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            template = f.read()

        data_dict_section = get_data_dictionary_prompt()
        if DATA_DICTIONARY_PLACEHOLDER in template:
            prompt = template.replace(DATA_DICTIONARY_PLACEHOLDER, data_dict_section)
            logger.info(
                "[PlanAgent] 已注入数据字典到 prompt (length=%d)",
                len(data_dict_section)
            )
        else:
            # 兼容旧版无占位符的 prompt：保留原文不动
            logger.warning(
                "[PlanAgent] plan_prompt.md 中未找到 %s 占位符，跳过数据字典注入",
                DATA_DICTIONARY_PLACEHOLDER
            )
            prompt = template
        return prompt

    async def plan(self, user_query: str, user_role: dict = None) -> dict:
        """
        接收用户查询，返回 PlanTask dict。

        Args:
            user_query: 用户的自然语言查询
            user_role: 用户角色信息字典（权限系统注入）

        Returns:
            PlanTask字典，包含:
            - intent: 查询意图描述
            - sql: 待执行的SQL
            - table: 主查询表名
            - metrics: 指标列表
            - time_range: 时间范围类型
            - needs_comparison: 是否需要对比
            
            或错误字典:
            - error: 错误信息
            - suggestion: 建议
        """
        system_content = self.system_prompt
        
        # L2: Prompt注入 - 将用户角色权限信息注入系统提示词
        if user_role and user_role.get('role_id') != 'admin':
            permission_context = self._build_permission_prompt(user_role)
            system_content = system_content + "\n\n" + permission_context

        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content=user_query),
        ]

        logger.info(f"[PlanAgent] 处理查询: {user_query[:50]}...")
        response = await self.llm.ainvoke(messages)
        content = response.content.strip()
        logger.debug(f"[PlanAgent] LLM原始输出: {content[:200]}...")

        return self._parse_plan_task(content)

    def _build_permission_prompt(self, user_role: dict) -> str:
        """构建权限提示上下文，注入到系统prompt"""
        allowed_tables = user_role.get('allowed_tables', [])
        role_name = user_role.get('role_name', '未知')
        max_days = user_role.get('max_query_days', 30)
        allow_detail = user_role.get('allow_user_detail', False)
        
        tables_str = "\n".join(f"  - {t}" for t in allowed_tables)
        
        prompt = f"""
---

## 权限约束（当前用户角色: {role_name}）

**你必须严格遵守以下权限规则，违反将导致SQL被拒绝执行：**

### 可查询表白名单（仅以下表可用）:
{tables_str}

### 限制条件:
- 查询时间范围不得超过 {max_days} 天
- {'允许' if allow_detail else '禁止'}查看用户级明细（user_id/uid级别数据）
- 如果用户的查询需求涉及白名单之外的表，直接返回 error 并告知用户无权限

### 强制规则:
- 生成的SQL只能引用上述白名单中的表
- 如果用户要求查看不在权限内的数据（如销售数据但用户是内容角色），返回:
  {{"error": "您当前角色({role_name})无权查询该数据", "suggestion": "请联系管理员调整权限"}}
"""
        if not allow_detail:
            prompt += """- 禁止生成包含 user_id 或 uid 明细的SQL（聚合查询 COUNT(DISTINCT ...) 除外）
"""
        return prompt

    def _parse_plan_task(self, content: str) -> dict:
        """
        解析LLM输出为PlanTask字典。
        支持：纯JSON、```json```包裹、混合文本中的JSON。
        """
        # 尝试1: 直接解析纯JSON
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 尝试2: 提取 ```json ... ``` 代码块
        json_block_match = re.search(
            r'```(?:json)?\s*\n?(.*?)\n?\s*```',
            content,
            re.DOTALL
        )
        if json_block_match:
            try:
                return json.loads(json_block_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试3: 查找第一个 { ... } 块
        brace_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # 所有解析方式都失败
        logger.error(f"[PlanAgent] 无法解析LLM输出为JSON: {content[:100]}...")
        return {
            'error': f'Plan Agent输出格式异常，无法解析为JSON',
            'raw_output': content[:500],
            'suggestion': '请重新描述您的查询需求'
        }
