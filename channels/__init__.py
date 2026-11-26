"""消息渠道适配层。业务处理在 core.conversation。"""

from channels.base import IncomingMessage, MessageDeduper

__all__ = ["IncomingMessage", "MessageDeduper"]
