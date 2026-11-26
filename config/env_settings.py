"""每个配置只保留一个正式名字。旧名字还能读，但会打一次弃用警告。"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("skadi.config")

_warned: set[str] = set()


def read_setting(canonical: str, *deprecated: str, default: str = "") -> str:
    """优先读正式变量。正式值为空时才回退到旧名字。"""
    current = os.getenv(canonical)
    current_set = current is not None and current.strip() != ""
    for name in deprecated:
        legacy = os.getenv(name)
        if legacy is None or str(legacy).strip() == "":
            continue
        if name not in _warned:
            logger.warning("环境变量 %s 已弃用，请改用 %s", name, canonical)
            _warned.add(name)
        if not current_set:
            return str(legacy).strip()
    if current_set:
        return current.strip()
    return default


def reset_warnings_for_tests() -> None:
    _warned.clear()
