"""飞书事件解析、只处理私聊、按 message_id / event_id 去重，以及卡片回复。"""

import asyncio
import json
import unittest

from channels.base import MessageDeduper
from channels.feishu import (
    ROUTE_DUPLICATE,
    ROUTE_HANDLE,
    ROUTE_IGNORE,
    FeishuBot,
    FeishuSink,
    analyzing_card,
    decide_feishu_route,
    install_card_frame_dispatch,
    parse_card_action,
    parse_message_event,
    result_card,
)
from core.conversation import ANALYZING_TEXT, ConversationService
from core.identity import MemoryIdentityStore


def _event(chat_type="p2p", text="昨天日活多少", message_id="om_1", event_id="evt_1", sender_type="user"):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_user"},
                "sender_type": sender_type,
            },
            "message": {
                "message_id": message_id,
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "mentions": [],
            },
        },
    }


class _Inline:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


class _Api:
    def __init__(self):
        self.cards = []
        self.patches = []
        self.texts = []

    def send_card(self, open_id, card):
        self.cards.append((open_id, card))
        return "om_card"

    def patch_card(self, message_id, card):
        self.patches.append((message_id, card))

    def send_text(self, open_id, text):
        self.texts.append((open_id, text))
        return "om_text"


class _Conversation:
    def __init__(self):
        self.texts = []

    async def handle(self, message, send):
        self.texts.append(message.text)
        send(ANALYZING_TEXT)
        send("日活 100")


class ParseAndRouteTests(unittest.TestCase):
    def test_private_text_event(self):
        message, event_id = parse_message_event(_event())
        self.assertEqual(message.channel, "feishu")
        self.assertEqual(message.user_ext_id, "ou_user")
        self.assertEqual(message.text, "昨天日活多少")
        self.assertFalse(message.is_group)
        self.assertEqual(message.msg_id, "om_1")
        self.assertEqual(event_id, "evt_1")
        self.assertEqual(message.chat_id, "oc_chat")

    def test_group_is_ignored_and_does_not_consume_dedup(self):
        message, event_id = parse_message_event(_event(chat_type="group", text="@机器人 日活"))
        self.assertTrue(message.is_group)
        deduper = MessageDeduper()
        self.assertEqual(decide_feishu_route(message, event_id, deduper), ROUTE_IGNORE)
        private, private_event = parse_message_event(_event())
        self.assertEqual(decide_feishu_route(private, private_event, deduper), ROUTE_HANDLE)

    def test_dedup_by_message_id_and_event_id(self):
        deduper = MessageDeduper()
        first, first_event = parse_message_event(_event())
        self.assertEqual(decide_feishu_route(first, first_event, deduper), ROUTE_HANDLE)
        again, again_event = parse_message_event(_event(message_id="om_1", event_id="evt_other"))
        self.assertEqual(decide_feishu_route(again, again_event, deduper), ROUTE_DUPLICATE)

        other = MessageDeduper()
        parse_message_event(_event())
        original, original_event = parse_message_event(_event(message_id="om_a", event_id="evt_same"))
        self.assertEqual(decide_feishu_route(original, original_event, other), ROUTE_HANDLE)
        replay, replay_event = parse_message_event(_event(message_id="om_b", event_id="evt_same"))
        self.assertEqual(decide_feishu_route(replay, replay_event, other), ROUTE_DUPLICATE)

    def test_bot_sender_is_dropped(self):
        self.assertIsNone(parse_message_event(_event(sender_type="app")))

    def test_sdk_object_parses_the_same_way(self):
        try:
            from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
        except ImportError:
            self.skipTest("lark-oapi not installed")
        message, event_id = parse_message_event(P2ImMessageReceiveV1(_event()))
        self.assertEqual(message.user_ext_id, "ou_user")
        self.assertEqual(message.text, "昨天日活多少")
        self.assertEqual(event_id, "evt_1")
        self.assertFalse(message.is_group)


class CardTests(unittest.TestCase):
    def test_result_card_has_feedback_buttons(self):
        card = result_card("日活 100")
        actions = card["elements"][1]["actions"]
        self.assertEqual([item["value"]["feedback"] for item in actions], ["up", "down"])
        self.assertIn("正在分析", json.dumps(analyzing_card(), ensure_ascii=False))
        self.assertEqual(len(analyzing_card()["elements"]), 1)

    def test_card_action_becomes_thumbs(self):
        payload = {
            "header": {"event_id": "card_evt"},
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"feedback": "down"}},
                "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
            },
        }
        action = parse_card_action(payload)
        message = action.as_message()
        self.assertEqual(message.channel, "feishu")
        self.assertEqual(message.user_ext_id, "ou_user")
        self.assertEqual(message.text, "👎")
        self.assertFalse(message.is_group)
        self.assertIsNone(parse_card_action({"event": {"action": {"value": {"feedback": "maybe"}}}}))


class BotFlowTests(unittest.TestCase):
    def test_private_message_sends_card_then_patches_result(self):
        api = _Api()
        conversation = _Conversation()
        bot = FeishuBot(
            "app",
            "secret",
            conversation=conversation,
            api=api,
            executor=_Inline(),
        )
        route = bot.on_message_event(_event())
        self.assertEqual(route, ROUTE_HANDLE)
        self.assertEqual(conversation.texts, ["昨天日活多少"])
        self.assertEqual(api.cards[0][0], "ou_user")
        self.assertEqual(api.cards[0][1]["header"]["title"]["content"], "正在分析")
        self.assertEqual(api.patches[0][0], "om_card")
        patched = api.patches[0][1]
        self.assertIn("日活 100", patched["elements"][0]["text"]["content"])
        self.assertEqual(
            [item["value"]["feedback"] for item in patched["elements"][1]["actions"]],
            ["up", "down"],
        )
        self.assertEqual(api.texts, [])

    def test_patch_failure_falls_back_to_text(self):
        class _FailPatch(_Api):
            def patch_card(self, message_id, card):
                raise RuntimeError("patch failed")

        api = _FailPatch()
        sink = FeishuSink(api, "ou_user")
        sink.send(ANALYZING_TEXT)
        sink.send("日活 100")
        self.assertEqual(len(api.cards), 1)
        self.assertEqual(api.patches, [])
        self.assertEqual(api.texts, [("ou_user", "日活 100")])

    def test_group_and_duplicate_do_not_call_api(self):
        api = _Api()
        conversation = _Conversation()
        bot = FeishuBot("app", "secret", conversation=conversation, api=api, executor=_Inline())
        self.assertEqual(bot.on_message_event(_event(chat_type="group")), ROUTE_IGNORE)
        self.assertEqual(bot.on_message_event(_event()), ROUTE_HANDLE)
        self.assertEqual(bot.on_message_event(_event(event_id="evt_2")), ROUTE_DUPLICATE)
        self.assertEqual(len(api.cards), 1)
        self.assertEqual(conversation.texts, ["昨天日活多少"])

    def test_unmapped_user_gets_text_rejection_without_analysis_card(self):
        api = _Api()
        conversation = ConversationService(
            identity_store=MemoryIdentityStore({}),
            rate_limit=lambda key, scope: None,
        )
        bot = FeishuBot("app", "secret", conversation=conversation, api=api, executor=_Inline())
        bot.on_message_event(_event())
        self.assertEqual(api.cards, [])
        self.assertEqual(api.patches, [])
        self.assertIn("ou_user", api.texts[0][1])
        self.assertIn("暂无数据查询权限", api.texts[0][1])

    def test_card_button_is_wired_to_feedback(self):
        seen = []

        class FeedbackConversation:
            async def handle(self, message, send):
                seen.append(message.text)
                send("感谢您的反馈，我们会持续优化。")

        api = _Api()
        bot = FeishuBot(
            "app",
            "secret",
            conversation=FeedbackConversation(),
            api=api,
            executor=_Inline(),
        )
        toast = bot.on_card_action({
            "header": {"event_id": "card_evt"},
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"feedback": "up"}},
                "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
            },
        })
        self.assertEqual(seen, ["👍"])
        self.assertIn("已记录", toast["toast"]["content"])
        self.assertEqual(api.texts[0][1], "感谢您的反馈，我们会持续优化。")


class CardFrameDispatchTests(unittest.TestCase):
    def test_card_frame_reaches_event_handler(self):
        try:
            from lark_oapi.ws.client import Client
            from lark_oapi.ws.pb.pbbp2_pb2 import Frame
        except ImportError:
            self.skipTest("lark-oapi not installed")

        install_card_frame_dispatch()
        frame = Frame()
        frame.SeqID = 1
        frame.LogID = 1
        frame.service = 1
        frame.method = 1
        for key, value in (
            ("type", "card"),
            ("message_id", "m1"),
            ("trace_id", "t1"),
            ("sum", "1"),
            ("seq", "0"),
        ):
            header = frame.headers.add()
            header.key = key
            header.value = value
        frame.payload = b'{"schema":"2.0","header":{"event_id":"e","event_type":"card.action.trigger"}}'

        seen = []

        class Handler:
            def _do_without_validation(self, payload):
                seen.append(payload)
                return {"toast": {"type": "info", "content": "ok"}}

        client = Client.__new__(Client)
        client._event_handler = Handler()
        client._fmt_log = lambda *args: ""
        written = []

        async def _write(data):
            written.append(data)

        client._write_message = _write
        asyncio.run(client._handle_data_frame(frame))
        self.assertEqual(len(seen), 1)
        self.assertIn(b"card.action.trigger", seen[0])
        self.assertTrue(written)
