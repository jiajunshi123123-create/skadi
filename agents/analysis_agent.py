"""Analysis Agent - 数据分析 + 行动建议

职责：接收查询结果，进行深度业务分析，生成三段式回复（数据+分析+建议）。
输出：格式化的分析文本（供最终回复使用）
"""
import os
import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from config.agent_config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    ANALYSIS_MODEL, ANALYSIS_TEMPERATURE, ANALYSIS_MAX_TOKENS,
    PROMPTS_DIR
)

logger = logging.getLogger(__name__)


class AnalysisAgent:
    """Analysis Agent - 数据分析与行动建议生成"""

    def __init__(self):
        self.llm = ChatOpenAI(
            model=ANALYSIS_MODEL,
            openai_api_key=DEEPSEEK_API_KEY,
            openai_api_base=DEEPSEEK_BASE_URL,
            temperature=ANALYSIS_TEMPERATURE,
            max_tokens=ANALYSIS_MAX_TOKENS,
        )
        self.system_prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        """加载Analysis Agent系统提示词"""
        prompt_path = os.path.join(PROMPTS_DIR, 'analysis_prompt.md')
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read()

    def _detect_anomalies(self, query_result: dict) -> str:
        """M1: Pre-detect obvious data anomalies before LLM analysis.
        
        Returns anomaly summary string, or empty string if clean.
        """
        if not query_result or not query_result.get('rows'):
            return ""

        rows = query_result.get('rows', [])
        cols = query_result.get('cols', [])
        row_count = query_result.get('row_count', len(rows))
        anomalies = []

        # 1. Empty result check
        if row_count == 0:
            return "⚠️ Query returned 0 rows — no data found for the specified conditions."

        # 2. Very sparse data
        if row_count < 3 and row_count > 0:
            anomalies.append(f"- Data sparsity: only {row_count} rows returned")

        # 3. Check for all-null columns
        if cols and rows:
            for col_idx, col_name in enumerate(cols):
                all_null = all(
                    row[col_idx] is None or str(row[col_idx]).strip() == ''
                    for row in rows
                    if isinstance(row, (list, tuple)) and col_idx < len(row)
                )
                if all_null:
                    anomalies.append(f"- Column '{col_name}': all values are NULL")

        # 4. Check for 0 or negative values in numeric columns
        if cols and rows and len(rows) > 0:
            sample_row = rows[0]
            if isinstance(sample_row, (list, tuple)):
                for col_idx, col_name in enumerate(cols):
                    if col_idx >= len(sample_row):
                        continue
                    all_zero = all(
                        isinstance(row[col_idx], (int, float)) and row[col_idx] == 0
                        for row in rows
                        if isinstance(row, (list, tuple)) and col_idx < len(row) and row[col_idx] is not None
                    )
                    if all_zero and len(rows) > 0:
                        anomalies.append(f"- Column '{col_name}': all values are 0")

        if anomalies:
            return "Data quality concerns detected:\n" + "\n".join(anomalies)
        return ""

    async def analyze(
        self,
        user_query: str,
        query_result: dict,
        anomaly_summary: str = "",
        inspection_context: str = "",
        skills_context: str = ""
    ) -> str:
        """
        分析查询结果，生成三段式回复。

        Args:
            user_query: 用户原始问题
            query_result: Query Agent返回的结果字典
            anomaly_summary: 异常检测摘要（M1阶段）
            inspection_context: 数据核查探查报告（Inspection Agent）
            skills_context: 匹配的分析技能方法论指令

        Returns:
            格式化的分析文本（三段式：📊数据 → 📈分析 → 💡建议）
        """
        context = self._build_context(
            user_query, query_result,
            anomaly_summary=anomaly_summary,
            inspection_context=inspection_context,
            skills_context=skills_context
        )

        # 构建消息：系统提示 + 技能指令 + 核查报告 + 数据上下文
        system_content = self.system_prompt
        if skills_context:
            system_content += f"\n\n{skills_context}"

        messages = [
            SystemMessage(content=system_content),
            HumanMessage(content=context),
        ]

        logger.info(
            f"[AnalysisAgent] 开始分析，数据行数: {query_result.get('row_count', 0)}"
            f"，技能: {'有' if skills_context else '无'}"
            f"，核查报告: {'有' if inspection_context else '无'}"
        )

        try:
            response = await self.llm.ainvoke(messages)
            analysis = response.content.strip()
            logger.info(f"[AnalysisAgent] 分析完成，输出长度: {len(analysis)}")
            return analysis
        except Exception as e:
            logger.error(f"[AnalysisAgent] 分析失败: {e}")
            return self._fallback_analysis(query_result)

    def _build_context(
        self,
        query: str,
        result: dict,
        anomaly_summary: str = "",
        inspection_context: str = "",
        skills_context: str = ""
    ) -> str:
        """构建分析上下文，传递给LLM"""
        data_display = self._format_data(result)

        context = f"""用户问题：{query}

查询结果：
执行的SQL: {result.get('sql_executed', 'N/A')}
列名: {result.get('cols', [])}
数据行数: {result.get('row_count', 0)}
重试次数: {result.get('retries', 0)}

数据内容:
{data_display}
"""

        # 注入异常检测摘要
        if anomaly_summary:
            context += f"\n⚠️ 数据异常检测结果:\n{anomaly_summary}\n"

        # 注入核查报告
        if inspection_context:
            context += f"\n{inspection_context}\n"

        context += "\n请按照分析三步法（📊数据概览 → 📈趋势分析 → 💡行动建议）进行深度分析。"
        context += "\n注意：基于实际数据分析，不要编造任何数字。"

        return context

    def _format_data(self, result: dict) -> str:
        """格式化查询数据为可读文本"""
        cols = result.get('cols', [])
        rows = result.get('rows', [])

        if not rows:
            return "(无数据)"

        # 构建表格式输出
        lines = []

        # 表头
        if cols:
            lines.append(" | ".join(str(c) for c in cols))
            lines.append("-" * (len(lines[0]) if lines else 20))

        # 数据行（最多显示50行，避免上下文过长）
        display_rows = rows[:50]
        for row in display_rows:
            if isinstance(row, (list, tuple)):
                lines.append(" | ".join(str(v) for v in row))
            else:
                lines.append(str(row))

        if len(rows) > 50:
            lines.append(f"... 共 {len(rows)} 行，仅显示前50行")

        return "\n".join(lines)

    def _fallback_analysis(self, result: dict) -> str:
        """当LLM分析失败时的兜底输出"""
        cols = result.get('cols', [])
        rows = result.get('rows', [])
        row_count = result.get('row_count', 0)

        output_parts = ["📊 数据概览", "━━━━━━━━━━━━━━━━"]

        if rows:
            # 简单展示数据
            for i, row in enumerate(rows[:10]):
                if isinstance(row, (list, tuple)) and cols:
                    row_display = ", ".join(
                        f"{cols[j]}: {row[j]}" for j in range(min(len(cols), len(row)))
                    )
                    output_parts.append(row_display)
                else:
                    output_parts.append(str(row))

            if row_count > 10:
                output_parts.append(f"... 共 {row_count} 行数据")
        else:
            output_parts.append("查询未返回数据。")

        output_parts.extend([
            "",
            "📈 趋势分析",
            "━━━━━━━━━━━━━━━━",
            "（分析服务暂时不可用，请参考上方原始数据）",
            "",
            "💡 行动建议",
            "━━━━━━━━━━━━━━━━",
            "1. 如需深度分析，请稍后重试"
        ])

        return "\n".join(output_parts)
