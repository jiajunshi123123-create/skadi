"""Token估算器 - 中英文混合文本的Token数估算

借鉴 Claude Code 的 Token 预算机制，提供：
1. 快速估算（字符数 / N ≈ token数）
2. 精确估算（基于实际分词比例）
3. 上下文预算检查

中文: ~1.5-2 字符/token（DeepSeek tokenizer）
英文: ~4 字符/token
混合: 字符数 / 2.5 作为保守估算
"""

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Token估算系数（保守估算）
CHINESE_CHAR_PER_TOKEN = 1.8   # 中文字符/Token
ENGLISH_CHAR_PER_TOKEN = 4.0   # 英文字符/Token
MIXED_CHAR_PER_TOKEN = 2.5     # 混合文本保守估算

# 上下文预算阈值
DEFAULT_MAX_CONTEXT_TOKENS = 8000   # 默认上下文上限
WARNING_THRESHOLD = 0.7              # 70%时触发压缩警告
CRITICAL_THRESHOLD = 0.9             # 90%时强制压缩


@dataclass
class TokenBudget:
    """Token预算状态"""
    total_tokens: int
    max_tokens: int
    usage_ratio: float
    needs_compaction: bool      # 需要轻度压缩（超过70%）
    needs_aggressive: bool      # 需要激进压缩（超过90%）


def estimate_tokens(text: str) -> int:
    """快速估算文本的Token数。

    使用保守估算系数，确保估算值不会低于实际值。
    对于中英混合文本，使用 MIXED_CHAR_PER_TOKEN 系数。

    Args:
        text: 输入文本

    Returns:
        估算的Token数（保守上限）
    """
    if not text:
        return 0

    # 分别统计中英文字符
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', text))
    non_chinese = len(text) - chinese_chars

    # 中文按1.8字符/token，英文按4字符/token
    est_chinese = chinese_chars / CHINESE_CHAR_PER_TOKEN
    est_non_chinese = non_chinese / ENGLISH_CHAR_PER_TOKEN

    # 加上额外开销量（标点、换行等约10%）
    total = int((est_chinese + est_non_chinese) * 1.1)

    return max(total, 1)


def estimate_tokens_fast(text: str) -> int:
    """极速估算Token数（仅按混合系数）。

    适用于不需要精确估算的场景。

    Args:
        text: 输入文本

    Returns:
        估算的Token数
    """
    if not text:
        return 0
    return max(int(len(text) / MIXED_CHAR_PER_TOKEN), 1)


def check_token_budget(
    *texts: str,
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    extra_tokens: int = 0
) -> TokenBudget:
    """检查多段文本的总Token是否超出预算。

    借鉴 Claude Code 的两层级压缩策略：
    - 70%阈值：触发轻度压缩（snip）
    - 90%阈值：触发激进压缩（auto-compact）

    Args:
        *texts: 多段文本
        max_tokens: Token上限
        extra_tokens: 额外预留Token数

    Returns:
        TokenBudget 状态对象
    """
    total = sum(estimate_tokens_fast(t) for t in texts) + extra_tokens
    ratio = total / max(max_tokens, 1)

    return TokenBudget(
        total_tokens=total,
        max_tokens=max_tokens,
        usage_ratio=ratio,
        needs_compaction=ratio >= WARNING_THRESHOLD,
        needs_aggressive=ratio >= CRITICAL_THRESHOLD,
    )


def estimate_dict_tokens(data: dict, max_depth: int = 3) -> int:
    """估算字典类型数据的Token数。

    Args:
        data: 字典数据
        max_depth: 最大递归深度

    Returns:
        估算的Token数
    """
    if max_depth <= 0:
        return 0

    total = 0
    for key, value in data.items():
        total += estimate_tokens_fast(str(key))
        if isinstance(value, dict):
            total += estimate_dict_tokens(value, max_depth - 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:20]:  # 最多采样20个元素
                if isinstance(item, dict):
                    total += estimate_dict_tokens(item, max_depth - 1)
                else:
                    total += estimate_tokens_fast(str(item))
        else:
            total += estimate_tokens_fast(str(value))

    return total
