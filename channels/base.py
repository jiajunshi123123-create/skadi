"""各渠道共用的入站消息和去重。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class IncomingMessage:
    """渠道无关的一条用户消息。

    channel: 渠道名，例如 dingtalk、feishu
    user_ext_id: 渠道侧用户 ID（钉钉 staffId、飞书 open_id）
    chat_id: 会话 ID
    is_group: 是否群聊
    mentioned: 群里是否 @ 了机器人
    text: 已去掉首尾空白的文本
    msg_id: 渠道消息 ID，用于去重
    """

    channel: str
    user_ext_id: str
    chat_id: str
    is_group: bool
    mentioned: bool
    text: str
    msg_id: str
    user_name: str = ""
    raw: Optional[dict] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.channel = (self.channel or "").strip().lower()
        self.user_ext_id = str(self.user_ext_id or "").strip()
        self.chat_id = str(self.chat_id or "").strip()
        self.text = (self.text or "").strip()
        self.msg_id = str(self.msg_id or "").strip()
        self.user_name = str(self.user_name or "").strip()
        if self.raw is not None and not isinstance(self.raw, dict):
            self.raw = {"value": self.raw}


class MessageDeduper:
    """按消息 ID 去重。进程内有效，超时后允许同一 ID 再处理一次。"""

    def __init__(self, ttl_seconds: float = 300.0, clock=time.time):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, msg_id: str) -> bool:
        """第一次见到返回 False 并记下；TTL 内再次见到返回 True。空 ID 不去重。"""
        msg_id = (msg_id or "").strip()
        if not msg_id:
            return False
        now = self.clock()
        with self._lock:
            expired = [key for key, seen_at in self._seen.items() if now - seen_at > self.ttl_seconds]
            for key in expired:
                self._seen.pop(key, None)
            if msg_id in self._seen:
                return True
            self._seen[msg_id] = now
            return False

    def forget(self, msg_id: str) -> None:
        with self._lock:
            self._seen.pop((msg_id or "").strip(), None)


def as_mapping(value: Any) -> dict:
    """把回调对象或 dict 收成普通 dict，供解析函数使用。"""
    if isinstance(value, dict):
        return value
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return data
    return {}
