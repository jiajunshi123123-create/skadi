"""上下文压缩器 - 两层级智能压缩

借鉴 Claude Code 的 compaction.py 两层级策略：

Level 1 - Snip (轻度压缩):
    数据采样：保留关键行（首行、尾行、极值行）
    统计摘要替代明细：用mean/max/min/count替代原始行数据
    触发条件：Token使用超过70%阈值

Level 2 - Auto-Compact (激进压缩):
    表结构压缩：列名+统计信息，去除原始数据行
    只保留必要的查询元信息
    触发条件：Token使用超过90%阈值

同时提供 DingTalk 消息截断功能（20KB限制）。
"""

import logging
from typing import Optional
from dataclasses import dataclass, field

from utils.token_estimator import (
    estimate_tokens_fast,
    check_token_budget,
    DEFAULT_MAX_CONTEXT_TOKENS,
)

logger = logging.getLogger(__name__)

# ============================================================
# 配置常量
# ============================================================

# DingTalk 消息长度限制（约20KB = 20480字节）
DINGTALK_MAX_CHARS = 15000   # 保守起见用15000字符
DINGTALK_TRUNCATION_NOTICE = "\n\n... (内容过长，已截断。如需详细数据，请缩小查询范围)"

# 数据截断参数
MAX_ROWS_FULL = 500           # 完全展示时的最大行数
MAX_ROWS_SAMPLE = 100         # Snip模式下采样行数
MAX_ROWS_COMPACT = 20         # Auto-Compact模式下最大行数

# 统计摘要截断
MAX_STRING_VALUE_LEN = 50     # 单值最大字符数


@dataclass
class CompactionResult:
    """压缩结果"""
    original_size: int          # 原始Token估算
    compacted_size: int         # 压缩后Token估算
    level: str                  # 压缩级别: 'none' | 'snip' | 'auto-compact'
    data: dict                  # 压缩后的查询结果数据
    summary: str                # 人类可读的压缩摘要
    rows_kept: int              # 保留的行数
    rows_total: int             # 原始总行数


def compact_query_result(
    query_result: dict,
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
) -> CompactionResult:
    """对查询结果进行两层级压缩。

    Args:
        query_result: Query Agent 返回的结果字典
        max_tokens: Token预算上限

    Returns:
        CompactionResult 包含压缩后的数据和摘要
    """
    rows = query_result.get('rows', [])
    cols = query_result.get('cols', [])
    row_count = query_result.get('row_count', len(rows))

    if not rows:
        return CompactionResult(
            original_size=0, compacted_size=0, level='none',
            data=query_result, summary='(无数据)', rows_kept=0, rows_total=0
        )

    # 估算原始数据大小
    # 使用采样估算：前20行平均 * 总行数
    sample_text = _rows_to_text(rows[:20], cols) if len(rows) > 20 else _rows_to_text(rows, cols)
    if len(rows) > 20:
        avg_token_per_row = estimate_tokens_fast(sample_text) / 20
        original_size = int(avg_token_per_row * len(rows) * 1.2)  # 1.2倍安全系数
    else:
        original_size = estimate_tokens_fast(sample_text)

    # ============================================================
    # 决策：使用哪个压缩级别
    # ============================================================
    budget = check_token_budget(sample_text, max_tokens=max_tokens, extra_tokens=original_size - estimate_tokens_fast(sample_text))

    if not budget.needs_compaction:
        # 数据量在安全范围内，直接透传（但仍做基础截断）
        kept_rows = rows[:MAX_ROWS_FULL]
        result_data = {**query_result, 'rows': kept_rows}
        result_data['_truncated'] = len(rows) > MAX_ROWS_FULL

        return CompactionResult(
            original_size=original_size,
            compacted_size=estimate_tokens_fast(_rows_to_text(kept_rows, cols)),
            level='none',
            data=result_data,
            summary=f'展示全部 {min(row_count, MAX_ROWS_FULL)} 行数据',
            rows_kept=len(kept_rows),
            rows_total=row_count,
        )

    if budget.needs_aggressive:
        # ============================================================
        # Level 2: Auto-Compact — 激进压缩
        # ============================================================
        return _auto_compact(query_result, cols, rows, row_count, original_size)

    # ============================================================
    # Level 1: Snip — 轻度压缩
    # ============================================================
    return _snip_compact(query_result, cols, rows, row_count, original_size)


def _snip_compact(
    query_result: dict, cols: list, rows: list,
    row_count: int, original_size: int
) -> CompactionResult:
    """Level 1 轻度压缩：智能采样 + 统计摘要。

    保留：
    - 前 N 行（头部样本）
    - 后 N 行（尾部样本）
    - 数值列的统计摘要替代过长明细
    """
    logger.info(f"[Compactor] Level 1 Snip: {row_count}行 → 采样 {MAX_ROWS_SAMPLE}行")

    # 智能采样：前60% + 后40%
    head_count = int(MAX_ROWS_SAMPLE * 0.6)
    tail_count = MAX_ROWS_SAMPLE - head_count

    sampled = list(rows[:head_count])
    if len(rows) > head_count + tail_count:
        sampled.append(['...'] * len(cols) if cols else '...')
        sampled.extend(rows[-tail_count:])
    else:
        sampled.extend(rows[head_count:head_count + tail_count])

    # 生成统计摘要
    stats_summary = _generate_stats_summary(rows, cols)

    result_data = {
        **query_result,
        'rows': sampled,
        '_compaction': 'snip',
        '_stats_summary': stats_summary,
        '_truncated': True,
    }

    compacted_size = estimate_tokens_fast(_rows_to_text(sampled, cols))

    return CompactionResult(
        original_size=original_size,
        compacted_size=compacted_size,
        level='snip',
        data=result_data,
        summary=(
            f'数据已采样压缩: {row_count}行 → {len(sampled)}行 '
            f'(节省 {original_size - compacted_size} tokens)'
        ),
        rows_kept=len(sampled),
        rows_total=row_count,
    )


def _auto_compact(
    query_result: dict, cols: list, rows: list,
    row_count: int, original_size: int
) -> CompactionResult:
    """Level 2 激进压缩：仅保留统计摘要 + 少量样本行。

    保留：
    - 统计摘要（min/max/mean/count）
    - 极少量样本行（前10行）
    - 表结构信息
    """
    logger.info(f"[Compactor] Level 2 Auto-Compact: {row_count}行 → 仅保留统计摘要")

    # 生成完整统计摘要
    stats_summary = _generate_stats_summary(rows, cols)

    # 仅保留前几行作为样本
    compact_rows = rows[:MAX_ROWS_COMPACT]

    result_data = {
        **query_result,
        'rows': compact_rows,
        '_compaction': 'auto-compact',
        '_stats_summary': stats_summary,
        '_truncated': True,
        '_compaction_note': (
            f'原始数据 {row_count} 行已压缩为统计摘要。'
            f'分析时请基于统计摘要而非原始行数据进行判断。'
        ),
    }

    compacted_size = estimate_tokens_fast(_rows_to_text(compact_rows, cols))

    return CompactionResult(
        original_size=original_size,
        compacted_size=compacted_size,
        level='auto-compact',
        data=result_data,
        summary=(
            f'数据已激进压缩: {row_count}行 → '
            f'统计摘要+{len(compact_rows)}行样本 '
            f'(节省 {original_size - compacted_size} tokens)'
        ),
        rows_kept=len(compact_rows),
        rows_total=row_count,
    )


def _generate_stats_summary(rows: list, cols: list) -> str:
    """为数值列生成统计摘要。

    对每列尝试计算 min/max/mean/count，
    非数值列仅显示唯一值数量。

    Args:
        rows: 数据行列表
        cols: 列名列表

    Returns:
        格式化的统计摘要文本
    """
    if not rows or not cols:
        return ''

    lines = ['📊 数据统计摘要:', '']

    for ci, col_name in enumerate(cols):
        values = []
        for row in rows:
            if isinstance(row, (list, tuple)):
                if ci < len(row):
                    values.append(row[ci])
            elif isinstance(row, dict):
                values.append(row.get(col_name))

        if not values:
            continue

        # 尝试数值统计
        numeric_vals = []
        for v in values:
            try:
                if v is not None:
                    numeric_vals.append(float(v))
            except (ValueError, TypeError):
                pass

        if len(numeric_vals) >= len(values) * 0.5:
            # 数值列：提供统计信息
            import statistics
            try:
                lines.append(
                    f'  {col_name}: '
                    f'min={min(numeric_vals):.1f}, '
                    f'max={max(numeric_vals):.1f}, '
                    f'均值={statistics.mean(numeric_vals):.1f}, '
                    f'总数={len(values)}'
                )
            except Exception:
                lines.append(
                    f'  {col_name}: min={min(numeric_vals):.1f}, '
                    f'max={max(numeric_vals):.1f}, 总数={len(values)}'
                )
        else:
            # 非数值列：唯一值数量
            unique_count = len(set(str(v)[:MAX_STRING_VALUE_LEN] for v in values if v is not None))
            lines.append(f'  {col_name}: {unique_count}个不同值, 总数={len(values)}')

    return '\n'.join(lines)


def _rows_to_text(rows: list, cols: list) -> str:
    """将行数据转换为文本（用于Token估算）。

    Args:
        rows: 数据行
        cols: 列名

    Returns:
        文本表示
    """
    parts = [' | '.join(str(c) for c in cols)] if cols else []
    for row in rows[:20]:  # 估算时仅采样前20行
        if isinstance(row, (list, tuple)):
            parts.append(' | '.join(str(v)[:MAX_STRING_VALUE_LEN] for v in row))
        else:
            parts.append(str(row)[:200])
    return '\n'.join(parts)


def truncate_for_dingtalk(text: str, max_chars: int = DINGTALK_MAX_CHARS) -> str:
    """为 DingTalk 消息做智能截断。

    DingTalk 消息限制约20KB，保护业务回复不被截断。
    采用三段式截断：保留开头信息 + 核心数据 + 结尾建议。

    Args:
        text: 原始回复文本
        max_chars: 最大字符数

    Returns:
        截断后的文本
    """
    if len(text) <= max_chars:
        return text

    logger.info(f"[Compactor] DingTalk消息截断: {len(text)}字符 → {max_chars}字符")

    # 三段式截断：按 emoji 分隔符分段
    sections = _split_by_sections(text)

    if len(sections) <= 1:
        # 无法分段，简单截断
        return text[:max_chars - len(DINGTALK_TRUNCATION_NOTICE)] + DINGTALK_TRUNCATION_NOTICE

    # 保留策略：
    # - 第一段（📊 数据）：保留完整（通常较短）
    # - 中间段（📈 分析）：按比例截断
    # - 最后段（💡 建议 + ⏱️ 用时）：保留完整

    header = sections[0]
    footer = sections[-1] if len(sections) > 1 else ''
    middle = '\n'.join(sections[1:-1]) if len(sections) > 2 else ''

    header_len = len(header)
    footer_len = len(footer)
    notice_len = len(DINGTALK_TRUNCATION_NOTICE)

    # 安全检查: header本身已超限 → 强制截断
    if header_len + notice_len > max_chars:
        # 先保留footer（用时长），header截断
        reserved = footer_len + notice_len + 100
        header_limit = max_chars - reserved
        if header_limit < 200:
            header_limit = 200
            reserved = max_chars - header_limit - notice_len
        header = header[:header_limit] + '\n... (内容过长，已截断)'
        middle = ''
        result = f'{header}\n{footer[:reserved - notice_len] if footer_len > reserved - notice_len else footer}{DINGTALK_TRUNCATION_NOTICE}'
        return result

    available = max_chars - header_len - footer_len - notice_len - 10

    if available > 0 and middle:
        middle = middle[:available] + '\n... (中间分析内容过长，已截断)'
    elif available <= 0 and middle:
        # 中间部分过长，严重截断
        middle = middle[:max(available + 500, 100)] + '\n... (分析内容已压缩)'

    return f'{header}\n{middle}\n{footer}{DINGTALK_TRUNCATION_NOTICE}'


def _split_by_sections(text: str) -> list:
    """按 emoji 章节标题分段。

    识别 📊 📈 💡 ⚠️ 等常用分段标记。
    """
    # 常见分段标记
    section_markers = r'(?:^|\n)(?=[📊📈📉💡⚠️🔒⏱️📋🔍📌🎯])'

    parts = re.split(section_markers, text, flags=re.MULTILINE)

    # 清理空段
    return [p.strip() for p in parts if p.strip()]


def compact_analysis_context(
    user_query: str,
    query_result: dict,
    anomaly_summary: str = '',
    inspection_context: str = '',
    skills_context: str = '',
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> dict:
    """综合分析上下文的Token预算并压缩。

    这是编排器 level 调用的主入口。
    会检查所有上下文的Token总量，超出阈值时自动压缩。

    Args:
        user_query: 用户问题
        query_result: 查询结果
        anomaly_summary: 异常检测摘要
        inspection_context: 核查报告
        skills_context: 技能上下文
        max_tokens: Token上限

    Returns:
        {
            'compact_result': CompactionResult,
            'query_result_compacted': dict,   # 压缩后的查询结果
            'context_truncated': bool,         # 上下文是否被截断
        }
    """
    # 先估算总Token用量
    total_estimate = sum([
        estimate_tokens_fast(user_query),
        estimate_tokens_fast(str(query_result.get('rows', []))),
        estimate_tokens_fast(anomaly_summary),
        estimate_tokens_fast(inspection_context),
        estimate_tokens_fast(skills_context),
    ])

    logger.info(
        f"[Compactor] 分析上下文Token估算: {total_estimate}/{max_tokens} "
        f"({total_estimate/max(max_tokens,1)*100:.0f}%)"
    )

    # 压缩查询结果数据
    compact_result = compact_query_result(query_result, max_tokens)

    # 如果压缩后仍然紧张，截断核查报告和技能上下文
    context_truncated = False
    if compact_result.level in ('snip', 'auto-compact'):
        remaining = max_tokens - compact_result.compacted_size
        if remaining < 1000:
            # 截断核查报告和技能上下文
            context_truncated = True
            if inspection_context:
                inspection_context = inspection_context[:200] + '\n... (上下文不足，核查报告已截断)'
            if skills_context:
                skills_context = skills_context[:300] + '\n... (上下文不足，技能指令已截断)'
            logger.warning(
                f"[Compactor] 上下文严重紧张，已截断核查报告和技能上下文 "
                f"(剩余Token预算: {remaining})"
            )

    return {
        'compact_result': compact_result,
        'query_result_compacted': compact_result.data,
        'anomaly_summary': anomaly_summary,
        'inspection_context': inspection_context,
        'skills_context': skills_context,
        'context_truncated': context_truncated,
    }

# ============================================================
# Conversation-Level Compression (Memory Anchor to PG)
# ============================================================

CONVERSATION_COMPACT_THRESHOLD = 0.60   # 60% of max context triggers compression
CONVERSATION_ANCHOR_KEEP_LAST = 3       # Keep last N messages after compression

def compact_conversation(
    messages: list,
    session_id: str,
    model_max_tokens: int = 128000,
    llm_client=None,
) -> dict:
    """对话级上下文压缩：LLM 摘要 → 记忆锚点 → PG 存储。

    当对话 token 用量超过 60% 阈值时触发：
    1. 调用 LLM 将所有历史消息压缩为结构化摘要
    2. 摘要（锚点）存入 PG context_anchors 表
    3. 返回压缩后上下文：锚点 + 最近 N 条消息

    Args:
        messages: [{"role": "user/assistant", "content": "..."}]
        session_id: 会话 ID
        model_max_tokens: 模型最大上下文窗口
        llm_client: LLM 客户端（需支持 chat.completions.create）

    Returns:
        {
            "compressed": True/False,
            "anchor_text": "压缩后的摘要",
            "new_context": [压缩后的消息列表],
            "saved_to_pg": True/False,
        }
    """
    import json, logging
    from utils.token_estimator import estimate_tokens_fast

    logger = logging.getLogger(__name__)

    # 估算当前总 token 量
    total_text = "".join([m.get("content", "") for m in messages])
    total_tokens = estimate_tokens_fast(total_text)
    threshold_tokens = int(model_max_tokens * CONVERSATION_COMPACT_THRESHOLD)

    if total_tokens < threshold_tokens:
        return {
            "compressed": False,
            "anchor_text": "",
            "new_context": messages,
            "saved_to_pg": False,
            "current_tokens": total_tokens,
            "threshold_tokens": threshold_tokens,
        }

    logger.info(
        f"[ConvCompactor] 触发对话压缩: {total_tokens}/{model_max_tokens} tokens "
        f"({total_tokens/model_max_tokens*100:.0f}%) > {CONVERSATION_COMPACT_THRESHOLD*100:.0f}%"
    )

    # 分离：待压缩的历史 + 保留的最近消息
    keep_messages = messages[-CONVERSATION_ANCHOR_KEEP_LAST:] if len(messages) > CONVERSATION_ANCHOR_KEEP_LAST else []
    compress_messages = messages[:-CONVERSATION_ANCHOR_KEEP_LAST] if len(messages) > CONVERSATION_ANCHOR_KEEP_LAST else messages

    # 调用 LLM 生成压缩锚点
    anchor_text = _generate_anchor(compress_messages, llm_client, total_tokens)

    # 存入 PG
    saved = _save_anchor_to_pg(session_id, anchor_text, total_tokens)

    # 构建新上下文：锚点 + 最近消息
    new_context = [{"role": "system", "content": f"[对话记忆锚点]\n{anchor_text}"}]
    new_context.extend(keep_messages)

    logger.info(
        f"[ConvCompactor] 压缩完成: {len(compress_messages)}条消息 → "
        f"锚点({len(anchor_text)}字符) + {len(keep_messages)}条最近消息"
    )

    return {
        "compressed": True,
        "anchor_text": anchor_text,
        "new_context": new_context,
        "saved_to_pg": saved,
        "current_tokens": total_tokens,
        "threshold_tokens": threshold_tokens,
    }


def _generate_anchor(messages: list, llm_client, total_tokens: int) -> str:
    """调用 LLM 将对话历史压缩为结构化锚点摘要。

    锚点格式：
    - 关键分析发现
    - 数据查询记录（SQL/表名）
    - 用户偏好/上下文
    - 决策与结论
    """
    import logging
    logger = logging.getLogger(__name__)

    # 构建压缩 prompt
    conv_text = ""
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")[:500]  # 每条消息截取前500字符
        conv_text += f"[{role}]: {content}\n"

    prompt = f"""你是一个对话压缩器。请将以下数据分析对话历史压缩为一个结构化的"记忆锚点"。

压缩要求：
1. 保留所有关键分析发现和结论
2. 保留数据查询记录（SQL语句、表名、查询条件）
3. 保留用户偏好和分析上下文
4. 忽略问候语和过渡性对话
5. 输出不超过500字

对话历史（Token用量: {total_tokens}）：
{conv_text}

请输出压缩后的记忆锚点（JSON格式）：
{{"key_findings": "关键发现", "queries": "数据查询记录", "context": "用户上下文", "conclusions": "结论"}}
"""

    if llm_client is None:
        # Fallback: 简单截断摘要
        logger.warning("[ConvCompactor] LLM客户端不可用，使用简单截断摘要")
        return _fallback_anchor(messages)

    try:
        # 调用 DeepSeek API
        response = llm_client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        anchor_json = response.choices[0].message.content.strip()

        # 尝试解析 JSON，失败则直接使用文本
        try:
            import json
            anchor_data = json.loads(anchor_json)
            anchor_text = (
                f"关键发现: {anchor_data.get('key_findings', '')}\n"
                f"数据查询: {anchor_data.get('queries', '')}\n"
                f"用户上下文: {anchor_data.get('context', '')}\n"
                f"结论: {anchor_data.get('conclusions', '')}"
            )
        except json.JSONDecodeError:
            anchor_text = anchor_json

        return anchor_text

    except Exception as e:
        logger.error(f"[ConvCompactor] LLM压缩失败: {e}")
        return _fallback_anchor(messages)


def _fallback_anchor(messages: list) -> str:
    """无 LLM 时的降级压缩：提取最后几条消息的关键内容。"""
    parts = []
    for m in messages[-5:]:
        content = m.get("content", "")
        role = m.get("role", "user")
        if role == "user":
            parts.append(f"用户提问: {content[:100]}")
        else:
            # 提取分析结论（通常以 📊 或代码块开头）
            lines = content.split("\n")
            key_lines = [l for l in lines[:20] if l.strip() and not l.startswith("```")]
            parts.append(f"分析摘要: {'; '.join(key_lines[:3])}")

    return "\n".join(parts)


def _save_anchor_to_pg(session_id: str, anchor_text: str, original_tokens: int) -> bool:
    """将记忆锚点保存到 PostgreSQL context_anchors 表。"""
    import logging
    logger = logging.getLogger(__name__)

    try:
        import psycopg2
        from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
        from utils.token_estimator import estimate_tokens_fast

        anchor_tokens = estimate_tokens_fast(anchor_text)

        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASSWORD
        )
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO context_anchors (session_id, anchor_text, anchor_tokens, original_tokens)
                VALUES (%s, %s, %s, %s)
            """, (session_id, anchor_text, anchor_tokens, original_tokens))
        conn.commit()
        conn.close()

        logger.info(
            f"[ConvCompactor] 锚点已存入PG: session={session_id}, "
            f"anchor_tokens={anchor_tokens}, original_tokens={original_tokens}"
        )
        return True

    except Exception as e:
        logger.error(f"[ConvCompactor] PG存储失败: {e}")
        return False


def load_anchors_from_pg(session_id: str, limit: int = 3) -> list:
    """从 PG 加载会话的历史记忆锚点。

    Args:
        session_id: 会话 ID
        limit: 返回最近 N 个锚点

    Returns:
        [{"anchor_text": "...", "compressed_at": "..."}]
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        import psycopg2
        from config.agent_config import PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD

        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DB,
            user=PG_USER, password=PG_PASSWORD
        )
        with conn.cursor() as cur:
            cur.execute("""
                SELECT anchor_text, original_tokens, compressed_at
                FROM context_anchors
                WHERE session_id = %s
                ORDER BY compressed_at DESC
                LIMIT %s
            """, (session_id, limit))
            rows = cur.fetchall()
        conn.close()

        anchors = [
            {
                "anchor_text": row[0],
                "original_tokens": row[1],
                "compressed_at": row[2].isoformat() if row[2] else None,
            }
            for row in rows
        ]
        logger.info(f"[ConvCompactor] 从PG加载 {len(anchors)} 个锚点: session={session_id}")
        return anchors

    except Exception as e:
        logger.error(f"[ConvCompactor] PG加载锚点失败: {e}")
        return []


import re
