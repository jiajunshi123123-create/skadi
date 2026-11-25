#!/usr/bin/env python3
"""钉钉 Stream 入口。

只做协议适配：解析消息、按路由回复、把私聊交给 ConversationService。
身份、权限、限流、追问和反馈都在 core.conversation。
分析失败时回复明确错误，不再回退到本地拼 SQL。
"""

from __future__ import annotations

import logging
import os

from dingtalk_stream import (
    AckMessage,
    ChatbotHandler,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)

from channels.base import MessageDeduper
from channels.dingtalk import (
    EMPTY_REPLY,
    GROUP_REPLY,
    ROUTE_DUPLICATE,
    ROUTE_EMPTY,
    ROUTE_GROUP,
    ROUTE_HANDLE,
    decide_route,
    incoming_from_dingtalk,
)
from core.conversation import ConversationService

LOG_DIR = os.environ.get("LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "dingtalk_bot.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("dingtalk_bot")


class BotHandler(ChatbotHandler):
    def __init__(self, conversation: ConversationService = None, deduper: MessageDeduper = None):
        super().__init__()
        self.conversation = conversation or ConversationService()
        self.deduper = deduper or MessageDeduper()

    async def process(self, callback_message):
        try:
            data = getattr(callback_message, "data", callback_message)
            cbm = ChatbotMessage.from_dict(data) if isinstance(data, dict) else callback_message
            incoming = incoming_from_dingtalk(data if isinstance(data, dict) else cbm)
            route = decide_route(incoming, self.deduper)

            def send(text: str) -> None:
                self.reply_text(text, cbm)

            if route == ROUTE_DUPLICATE:
                logger.info("忽略重复消息 msg_id=%s", incoming.msg_id)
                return AckMessage.STATUS_OK, "ok"
            if route == ROUTE_GROUP:
                logger.info("拒绝群聊 user=%s", incoming.user_name or incoming.user_ext_id)
                send(GROUP_REPLY)
                return AckMessage.STATUS_OK, "ok"
            if route == ROUTE_EMPTY:
                send(EMPTY_REPLY)
                return AckMessage.STATUS_OK, "ok"
            if route == ROUTE_HANDLE:
                await self.conversation.handle(incoming, send)
                return AckMessage.STATUS_OK, "ok"
            logger.error("未知路由 route=%s", route)
            send("消息无法处理，请稍后重试。")
        except Exception:
            logger.exception("钉钉消息处理失败")
            try:
                data = getattr(callback_message, "data", None)
                if isinstance(data, dict):
                    self.reply_text("处理失败，请稍后重试。", ChatbotMessage.from_dict(data))
            except Exception:
                logger.exception("钉钉错误回复发送失败")
        return AckMessage.STATUS_OK, "ok"


def main() -> None:
    key = os.environ.get("DINGTALK_BOT_APP_KEY", "")
    secret = os.environ.get("DINGTALK_BOT_APP_SECRET", "")
    if not key or not secret:
        raise SystemExit("缺少 DINGTALK_BOT_APP_KEY 或 DINGTALK_BOT_APP_SECRET")
    client = DingTalkStreamClient(Credential(key, secret))
    client.register_callback_handler("/v1.0/im/bot/messages/get", BotHandler())
    logger.info("钉钉机器人启动")
    client.start_forever()


if __name__ == "__main__":
    main()
