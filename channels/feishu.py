"""飞书 / Lark 私聊适配。

使用 lark-oapi 的长连接（WebSocket）订阅 im.message.receive_v1。
群聊直接忽略。分析在后台线程执行：先发出「正在分析」卡片，完成后再 PATCH 同一张卡片。
卡片上的赞/踩按钮走 card.action.trigger，记入现有反馈流程。

身份使用 user_identities(channel='feishu', external_id=open_id)。未登记的用户由
ConversationService 拒绝。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from channels.base import IncomingMessage, MessageDeduper
from core.conversation import ANALYZING_TEXT, ConversationService

logger = logging.getLogger("skadi.feishu")

ROUTE_IGNORE = "ignore"
ROUTE_DUPLICATE = "duplicate"
ROUTE_HANDLE = "handle"

CARD_TEXT_LIMIT = 3500
_CARD_PATCHED = False


def _get(obj: Any, *keys: str) -> Any:
    current = obj
    for key in keys:
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def _loads_content(content: Any) -> Any:
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return content


def message_text(message_type: str, content: Any) -> str:
    """只取文本消息。图片、文件等返回空字符串，由会话层提示用户发文字。"""
    parsed = _loads_content(content)
    if message_type and message_type != "text":
        return ""
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, Mapping):
        return str(parsed.get("text") or "").strip()
    return ""


def parse_message_event(payload: Any) -> Optional[tuple[IncomingMessage, str]]:
    """解析 im.message.receive_v1。非用户发送返回 None。群聊仍返回消息，由路由忽略。"""
    sender_type = str(_get(payload, "event", "sender", "sender_type") or "user").strip().lower()
    if sender_type and sender_type != "user":
        return None
    open_id = str(_get(payload, "event", "sender", "sender_id", "open_id") or "").strip()
    message_id = str(_get(payload, "event", "message", "message_id") or "").strip()
    chat_id = str(_get(payload, "event", "message", "chat_id") or "").strip()
    chat_type = str(_get(payload, "event", "message", "chat_type") or "").strip().lower()
    message_type = str(_get(payload, "event", "message", "message_type") or "text").strip().lower()
    mentions = _get(payload, "event", "message", "mentions") or []
    event_id = str(_get(payload, "header", "event_id") or "").strip()
    if not open_id and not message_id:
        return None
    return (
        IncomingMessage(
            channel="feishu",
            user_ext_id=open_id or "unknown",
            chat_id=chat_id,
            is_group=chat_type != "p2p",
            mentioned=bool(mentions),
            text=message_text(message_type, _get(payload, "event", "message", "content")),
            msg_id=message_id,
            user_name="",
        ),
        event_id,
    )


def dedup_keys(message: IncomingMessage, event_id: str) -> list[str]:
    keys = []
    if message.msg_id:
        keys.append(f"msg:{message.msg_id}")
    if event_id:
        keys.append(f"event:{event_id}")
    return keys


def decide_feishu_route(message: IncomingMessage, event_id: str, deduper: MessageDeduper) -> str:
    """群聊忽略且不占去重表。私聊按 message_id 与 event_id 去重。"""
    if message.is_group or message.channel != "feishu":
        return ROUTE_IGNORE
    if any(deduper.is_duplicate(key) for key in dedup_keys(message, event_id)):
        return ROUTE_DUPLICATE
    return ROUTE_HANDLE


def _clip(text: str) -> str:
    body = text or ""
    if len(body) <= CARD_TEXT_LIMIT:
        return body
    return body[:CARD_TEXT_LIMIT] + "\n\n…内容过长，已截断。"


def analyzing_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "正在分析"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "已收到，正在分析，请稍候。"},
            }
        ],
    }


def result_card(text: str, *, with_buttons: bool = True) -> dict:
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": _clip(text) or "（空回复）"}},
    ]
    if with_buttons:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "👍 有用"},
                    "type": "primary",
                    "value": {"feedback": "up"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "👎 没用"},
                    "type": "danger",
                    "value": {"feedback": "down"},
                },
            ],
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "分析结果"},
        },
        "elements": elements,
    }


def card_action_toast(content: str, level: str = "info") -> dict:
    return {"toast": {"type": level, "content": content}}


@dataclass
class CardAction:
    open_id: str
    feedback: str
    event_id: str
    open_message_id: str
    open_chat_id: str

    def as_message(self) -> IncomingMessage:
        text = "👍" if self.feedback == "up" else "👎"
        msg_id = self.event_id or f"{self.open_message_id}:feedback"
        return IncomingMessage(
            channel="feishu",
            user_ext_id=self.open_id,
            chat_id=self.open_chat_id,
            is_group=False,
            mentioned=False,
            text=text,
            msg_id=msg_id,
        )


def parse_card_action(payload: Any) -> Optional[CardAction]:
    """解析卡片按钮回调。只认 feedback=up/down。"""
    value = _get(payload, "event", "action", "value")
    if isinstance(value, str):
        value = _loads_content(value)
    if not isinstance(value, Mapping):
        value = {}
    feedback = str(value.get("feedback") or "").strip().lower()
    if feedback in {"positive", "up", "thumbsup"}:
        feedback = "up"
    elif feedback in {"negative", "down", "thumbsdown"}:
        feedback = "down"
    else:
        return None
    open_id = str(_get(payload, "event", "operator", "open_id") or "").strip()
    if not open_id:
        return None
    return CardAction(
        open_id=open_id,
        feedback=feedback,
        event_id=str(_get(payload, "header", "event_id") or "").strip(),
        open_message_id=str(_get(payload, "event", "context", "open_message_id") or "").strip(),
        open_chat_id=str(_get(payload, "event", "context", "open_chat_id") or "").strip(),
    )


class FeishuApi:
    """发卡片、改卡片、发文本。测试可替换整个对象。"""

    def __init__(self, app_id: str, app_secret: str, client=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self._client = client

    def _sdk(self):
        if self._client is None:
            import lark_oapi as lark

            self._client = (
                lark.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .build()
            )
        return self._client

    def send_card(self, open_id: str, card: dict) -> str:
        return self._send(open_id, "interactive", json.dumps(card, ensure_ascii=False))

    def send_text(self, open_id: str, text: str) -> str:
        body = json.dumps({"text": _clip(text)}, ensure_ascii=False)
        return self._send(open_id, "text", body)

    def patch_card(self, message_id: str, card: dict) -> None:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self._sdk().im.v1.message.patch(request)
        _ensure_success(response, "更新卡片")

    def _send(self, open_id: str, msg_type: str, content: str) -> str:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self._sdk().im.v1.message.create(request)
        _ensure_success(response, "发送消息")
        message_id = ""
        data = getattr(response, "data", None)
        if data is not None:
            message_id = str(getattr(data, "message_id", "") or "")
        if not message_id:
            raise RuntimeError("飞书发送消息成功但没有返回 message_id")
        return message_id


def _ensure_success(response, action: str) -> None:
    if response is not None and getattr(response, "success", lambda: False)():
        return
    code = getattr(response, "code", None)
    msg = getattr(response, "msg", None)
    raise RuntimeError(f"飞书{action}失败 code={code} msg={msg}")


class FeishuSink:
    """第一条「正在分析」发卡片，随后的回复 PATCH 这张卡片并带上赞/踩。"""

    def __init__(self, api: FeishuApi, open_id: str):
        self.api = api
        self.open_id = open_id
        self.card_id: Optional[str] = None

    def send(self, text: str) -> None:
        try:
            if text == ANALYZING_TEXT:
                self.card_id = self.api.send_card(self.open_id, analyzing_card())
                return
            if self.card_id:
                try:
                    self.api.patch_card(self.card_id, result_card(text, with_buttons=True))
                    return
                except Exception:
                    logger.exception("飞书更新卡片失败，改为发送文本 open_id=%s", self.open_id)
                    self.api.send_text(self.open_id, text)
                    return
            self.api.send_text(self.open_id, text)
        except Exception:
            logger.exception("飞书回复失败 open_id=%s", self.open_id)


class FeishuBot:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        conversation: Optional[ConversationService] = None,
        deduper: Optional[MessageDeduper] = None,
        api: Optional[FeishuApi] = None,
        executor=None,
        worker_count: int = 4,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.conversation = conversation or ConversationService()
        self.deduper = deduper or MessageDeduper()
        self.api = api or FeishuApi(app_id, app_secret)
        self.executor = executor or ThreadPoolExecutor(
            max_workers=max(1, worker_count),
            thread_name_prefix="feishu",
        )

    def on_message_event(self, payload: Any) -> str:
        parsed = parse_message_event(payload)
        if parsed is None:
            return ROUTE_IGNORE
        message, event_id = parsed
        route = decide_feishu_route(message, event_id, self.deduper)
        if route != ROUTE_HANDLE:
            logger.info(
                "飞书消息未处理 route=%s chat_type_group=%s msg_id=%s",
                route,
                message.is_group,
                message.msg_id,
            )
            return route
        # 长连接回调跑在已有事件循环上，分析放到线程里，避免阻塞心跳。
        self.executor.submit(self._run, message)
        return route

    def on_card_action(self, payload: Any) -> dict:
        action = parse_card_action(payload)
        if action is None:
            return card_action_toast("无法识别这次反馈", "error")
        message = action.as_message()
        if decide_feishu_route(message, action.event_id, self.deduper) == ROUTE_DUPLICATE:
            return card_action_toast("已收到")
        self.executor.submit(self._run, message)
        return card_action_toast("已记录，感谢反馈")

    def _run(self, message: IncomingMessage) -> None:
        sink = FeishuSink(self.api, message.user_ext_id)
        try:
            asyncio.run(self.conversation.handle(message, sink.send))
        except Exception:
            logger.exception("飞书分析任务失败 open_id=%s", message.user_ext_id)
            sink.send("分析失败，请稍后重试。本次没有改用其他查询方式。")

    def on_sdk_message(self, data: Any) -> None:
        self.on_message_event(data)

    def on_sdk_card(self, data: Any):
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        return P2CardActionTriggerResponse(self.on_card_action(data))

    def serve(self) -> None:
        import lark_oapi as lark

        install_card_frame_dispatch()
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_sdk_message)
            .register_p2_card_action_trigger(self.on_sdk_card)
            .build()
        )
        client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info("飞书长连接启动，仅处理私聊")
        client.start()


def install_card_frame_dispatch() -> None:
    """lark-oapi 1.8.0 的长连接把 CARD 帧直接丢掉，按钮回调因此到不了处理器。

    卡片回调的 payload 仍是 schema 2.0 事件，转成 event 帧后走原来的分发即可。
    """
    global _CARD_PATCHED
    if _CARD_PATCHED:
        return
    from lark_oapi.ws.client import Client, _get_by_key
    from lark_oapi.ws.const import HEADER_TYPE
    from lark_oapi.ws.enum import MessageType

    original = Client._handle_data_frame

    async def _handle_data_frame(self, frame):
        try:
            frame_type = _get_by_key(frame.headers, HEADER_TYPE)
        except Exception:
            return await original(self, frame)
        if frame_type == MessageType.CARD.value:
            for header in frame.headers:
                if header.key == HEADER_TYPE:
                    header.value = MessageType.EVENT.value
                    break
        return await original(self, frame)

    Client._handle_data_frame = _handle_data_frame
    _CARD_PATCHED = True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
    workers = os.environ.get("FEISHU_WORKERS", "").strip()
    count = int(workers) if workers.isdigit() and int(workers) > 0 else 4
    FeishuBot(app_id, app_secret, worker_count=count).serve()


if __name__ == "__main__":
    main()
