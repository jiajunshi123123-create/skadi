"""钉钉入站消息解析。不依赖 dingtalk_stream，方便单测。"""

from __future__ import annotations

from typing import Any, Mapping

from channels.base import IncomingMessage, MessageDeduper, as_mapping

GROUP_REPLY = "暂不支持群聊查询，请与我私聊提问。"
EMPTY_REPLY = "请发送文字消息。"

# decide_route 的结果
ROUTE_GROUP = "group"
ROUTE_DUPLICATE = "duplicate"
ROUTE_EMPTY = "empty"
ROUTE_HANDLE = "handle"


def _first(data: Mapping, *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        return str(value).strip()
    return default


def _text_from(data: Mapping) -> str:
    raw = data.get("text")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, Mapping):
        content = raw.get("content")
        return str(content).strip() if content else ""
    content = getattr(raw, "content", None)
    if content:
        return str(content).strip()
    return ""


def _is_group(value: Any) -> bool:
    return str(value).strip() == "2"


def parse_dingtalk_payload(data: Mapping) -> IncomingMessage:
    """把钉钉 Stream 回调 dict 转成 IncomingMessage。

    conversationType：1 单聊，2 群聊。身份优先用 senderStaffId，没有时用 senderId。
    """
    staff_id = _first(data, "senderStaffId", "sender_staff_id")
    sender_id = _first(data, "senderId", "sender_id")
    at_users = data.get("atUsers") or data.get("at_users") or []
    mentioned = bool(data.get("isInAtList") or data.get("is_in_at_list") or at_users)
    return IncomingMessage(
        channel="dingtalk",
        user_ext_id=staff_id or sender_id or "unknown",
        chat_id=_first(data, "conversationId", "conversation_id"),
        is_group=_is_group(data.get("conversationType", data.get("conversation_type"))),
        mentioned=mentioned,
        text=_text_from(data),
        msg_id=_first(data, "msgId", "messageId", "message_id"),
        user_name=_first(data, "senderNick", "sender_nick"),
        raw=dict(data),
    )


def incoming_from_dingtalk(source: Any) -> IncomingMessage:
    """接受原始 dict，或带 data / 属性的 ChatbotMessage。"""
    data = as_mapping(source)
    if data:
        return parse_dingtalk_payload(data)
    staff_id = str(getattr(source, "sender_staff_id", "") or "").strip()
    sender_id = str(getattr(source, "sender_id", "") or "").strip()
    text_obj = getattr(source, "text", None)
    content = ""
    if isinstance(text_obj, str):
        content = text_obj
    elif text_obj is not None:
        content = str(getattr(text_obj, "content", "") or "")
    conv = getattr(source, "conversation_type", None)
    return IncomingMessage(
        channel="dingtalk",
        user_ext_id=staff_id or sender_id or "unknown",
        chat_id=str(getattr(source, "conversation_id", "") or ""),
        is_group=_is_group(conv),
        mentioned=bool(getattr(source, "is_in_at_list", False)),
        text=content,
        msg_id=str(getattr(source, "message_id", "") or ""),
        user_name=str(getattr(source, "sender_nick", "") or ""),
    )


def decide_route(message: IncomingMessage, deduper: MessageDeduper) -> str:
    """群聊拒绝；其余消息按 ID 去重；空文本单独回复。"""
    if message.is_group:
        return ROUTE_GROUP
    if deduper.is_duplicate(message.msg_id):
        return ROUTE_DUPLICATE
    if not message.text:
        return ROUTE_EMPTY
    return ROUTE_HANDLE
