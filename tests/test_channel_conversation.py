"""渠道解析、身份拒绝、限流，以及钉钉不再走旧 SQL 回退。"""

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from channels.base import IncomingMessage, MessageDeduper
from channels.dingtalk import (
    EMPTY_REPLY,
    GROUP_REPLY,
    ROUTE_DUPLICATE,
    ROUTE_EMPTY,
    ROUTE_GROUP,
    ROUTE_HANDLE,
    decide_route,
    parse_dingtalk_payload,
)
from core.conversation import (
    AGENT_UNAVAILABLE_TEXT,
    ANALYZING_TEXT,
    FEEDBACK_TEXT,
    RATE_LIMIT_TEXT,
    RESET_TEXT,
    SUGGESTION_EXPIRED_TEXT,
    UNEXPECTED_ERROR_TEXT,
    ConversationService,
    _default_on_feedback,
    is_confirmation,
    is_follow_up,
)
from core.identity import MemoryIdentityStore, PgIdentityStore, ResolvedUser
from utils.web_auth import reset_for_tests

ROOT = Path(__file__).resolve().parents[1]


def _user(ext="staff-1", user_id="u-001"):
    return ResolvedUser(
        user_id=user_id,
        display_name="张三",
        role={"role_id": "analyst", "staff_id": user_id, "staff_name": "张三", "allowed_tables": ["t"]},
        external_id=ext,
        channel="dingtalk",
    )


def _message(text="昨天日活多少", ext="staff-1", msg_id="m1", is_group=False):
    return IncomingMessage(
        channel="dingtalk",
        user_ext_id=ext,
        chat_id="cid-1",
        is_group=is_group,
        mentioned=False,
        text=text,
        msg_id=msg_id,
        user_name="张三",
    )


class ParseAndRouteTests(unittest.TestCase):
    def test_private_payload_prefers_staff_id(self):
        msg = parse_dingtalk_payload({
            "conversationType": "1",
            "conversationId": "cid",
            "senderStaffId": "staff-9",
            "senderId": "sender-9",
            "senderNick": "李四",
            "msgId": "msg-9",
            "text": {"content": "  昨天日活  "},
            "isInAtList": False,
        })
        self.assertEqual(msg.channel, "dingtalk")
        self.assertEqual(msg.user_ext_id, "staff-9")
        self.assertEqual(msg.text, "昨天日活")
        self.assertFalse(msg.is_group)
        self.assertEqual(msg.msg_id, "msg-9")
        self.assertEqual(msg.user_name, "李四")

    def test_group_payload_is_flagged(self):
        msg = parse_dingtalk_payload({
            "conversationType": 2,
            "senderStaffId": "staff-2",
            "text": {"content": "@机器人 日活"},
            "msgId": "g1",
            "isInAtList": True,
        })
        self.assertTrue(msg.is_group)
        self.assertTrue(msg.mentioned)
        deduper = MessageDeduper()
        self.assertEqual(decide_route(msg, deduper), ROUTE_GROUP)
        # 群聊不占用去重表，避免同一 msgId 的单聊被误伤。
        self.assertFalse(deduper.is_duplicate("g1"))

    def test_duplicate_and_empty(self):
        deduper = MessageDeduper(ttl_seconds=100, clock=lambda: 1000)
        first = _message(text="日活", msg_id="same")
        again = _message(text="日活", msg_id="same")
        blank = _message(text="  ", msg_id="blank")
        self.assertEqual(decide_route(first, deduper), ROUTE_HANDLE)
        self.assertEqual(decide_route(again, deduper), ROUTE_DUPLICATE)
        self.assertEqual(decide_route(blank, deduper), ROUTE_EMPTY)
        self.assertIn("私聊", GROUP_REPLY)
        self.assertIn("文字", EMPTY_REPLY)

    def test_dedup_expires(self):
        now = {"t": 0}
        deduper = MessageDeduper(ttl_seconds=10, clock=lambda: now["t"])
        self.assertFalse(deduper.is_duplicate("a"))
        self.assertTrue(deduper.is_duplicate("a"))
        now["t"] = 11
        self.assertFalse(deduper.is_duplicate("a"))


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.feedback = []
        self.sessions = {}
        self.tasks = []
        self.deleted = []

    def _delete_session(self, user_id):
        self.deleted.append(user_id)
        self.sessions.pop(user_id, None)

    def _service(self, users=None, runner=None, use_real_rate_limit=False):
        if users is None:
            users = {("dingtalk", "staff-1"): _user()}
        if runner is None:
            runner = self._ok_runner
        return ConversationService(
            identity_store=MemoryIdentityStore(users),
            agent_runner=runner,
            rate_limit=None if use_real_rate_limit else (lambda key, scope: None),
            save_session=lambda user_id, user_name, plan, question: self.sessions.__setitem__(user_id, (plan, question)),
            get_session=lambda user_id: self.sessions.get(user_id),
            delete_session=self._delete_session,
            save_task=lambda **kwargs: self.tasks.append(kwargs),
            on_feedback=lambda user, polarity, ctx: self.feedback.append((user.user_id, polarity, ctx.get("last_user_query"))),
            clock=lambda: 1_000_000,
        )

    async def _ok_runner(self, prompt, user_role=None, last_context=None, user_id="", user_name=""):
        self.calls.append({
            "prompt": prompt,
            "role": None if user_role is None else user_role.get("role_id"),
            "last_context": last_context,
            "user_id": user_id,
        })
        return (
            "日活 100\n<!--M3_SQL:SELECT 1-->",
            {
                "plan_task": {"intent": "日活"},
                "query_result": {"success": True, "row_count": 1, "sql_executed": "SELECT 1"},
                "analysis": "昨天日活 100",
                "suggested_query": "最近7天日活趋势",
            },
        )

    def test_unmapped_user_is_rejected_without_agent(self):
        sent = []
        service = self._service(users={})
        asyncio.run(service.handle(_message(), sent.append))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(sent), 1)
        self.assertIn("staff-1", sent[0])
        self.assertIn("暂无数据查询权限", sent[0])

    def test_success_strips_sql_marker_and_keeps_context(self):
        sent = []
        service = self._service()
        asyncio.run(service.handle(_message(), sent.append))
        self.assertEqual(sent[0], ANALYZING_TEXT)
        self.assertEqual(sent[1], "日活 100")
        self.assertNotIn("M3_SQL", sent[1])
        self.assertEqual(self.calls[0]["role"], "analyst")
        self.assertEqual(self.calls[0]["user_id"], "u-001")
        stored = self.sessions["u-001"][0]
        self.assertEqual(stored["last_sql"], "SELECT 1")
        self.assertEqual(stored["last_user_query"], "昨天日活多少")
        self.assertEqual(self.tasks[0]["user_id"], "u-001")
        self.assertEqual(self.tasks[0]["prompt"], "昨天日活多少")

    def test_exception_replies_with_clear_error_and_stops(self):
        async def boom(*args, **kwargs):
            self.calls.append("ran")
            raise RuntimeError("db timeout")

        sent = []
        asyncio.run(self._service(runner=boom).handle(_message(), sent.append))
        self.assertEqual(self.calls, ["ran"])
        self.assertEqual(sent[-1], UNEXPECTED_ERROR_TEXT)
        self.assertNotIn("db timeout", sent[-1])

    def test_agent_service_prefix_does_not_fall_back(self):
        async def bad(*args, **kwargs):
            self.calls.append("ran")
            return "Agent service unavailable", {}

        sent = []
        asyncio.run(self._service(runner=bad).handle(_message(), sent.append))
        self.assertEqual(self.calls, ["ran"])
        self.assertEqual(sent[-1], AGENT_UNAVAILABLE_TEXT)

    def test_feedback_uses_previous_question(self):
        sent = []
        service = self._service()
        asyncio.run(service.handle(_message(), sent.append))
        asyncio.run(service.handle(_message(text="👍", msg_id="m2"), sent.append))
        self.assertEqual(self.feedback, [("u-001", "positive", "昨天日活多少")])
        self.assertEqual(sent[-1], FEEDBACK_TEXT)
        self.assertEqual(len(self.calls), 1)

    def test_confirmation_replaces_prompt(self):
        sent = []
        service = self._service()
        asyncio.run(service.handle(_message(), sent.append))
        asyncio.run(service.handle(_message(text="是", msg_id="m2"), sent.append))
        self.assertEqual(self.calls[1]["prompt"], "最近7天日活趋势")

    def test_confirmation_without_pending_is_a_normal_message(self):
        sent = []
        asyncio.run(self._service().handle(_message(text="是"), sent.append))
        self.assertEqual(self.calls[0]["prompt"], "是")
        self.assertNotIn(SUGGESTION_EXPIRED_TEXT, sent)

    def test_short_queries_are_not_treated_as_confirmations(self):
        for text in ("1月日活", "昨天好吗", "是谁在查", "py报错"):
            self.assertFalse(is_confirmation(text), text)
            sent = []
            self.calls.clear()
            asyncio.run(self._service().handle(_message(text=text, msg_id=text), sent.append))
            self.assertEqual(self.calls[0]["prompt"], text)
            self.assertNotIn(SUGGESTION_EXPIRED_TEXT, sent)
        self.assertTrue(is_confirmation("是"))
        self.assertTrue(is_confirmation("好的"))
        self.assertTrue(is_confirmation("OK"))
        self.assertFalse(is_confirmation("请帮我确认一下这张表"))

    def test_hao_de_confirms_pending_suggestion_instead_of_feedback(self):
        sent = []
        service = self._service()
        asyncio.run(service.handle(_message(), sent.append))
        asyncio.run(service.handle(_message(text="好的", msg_id="m2"), sent.append))
        self.assertEqual(self.calls[1]["prompt"], "最近7天日活趋势")
        self.assertEqual(self.feedback, [])
        self.assertNotIn(FEEDBACK_TEXT, sent)

    def test_hao_de_without_pending_is_positive_feedback(self):
        sent = []
        asyncio.run(self._service().handle(_message(text="好的"), sent.append))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.feedback, [("u-001", "positive", None)])
        self.assertEqual(sent, [FEEDBACK_TEXT])

    def test_expired_suggestion_is_not_executed(self):
        sent = []
        service = self._service()
        service._suggestions["u-001"] = {
            "suggested_query": "最近7天日活趋势",
            "saved_at": 1_000_000 - service.suggestion_ttl - 1,
        }
        asyncio.run(service.handle(_message(text="是"), sent.append))
        self.assertEqual(self.calls, [])
        self.assertEqual(sent, [SUGGESTION_EXPIRED_TEXT])

    def test_context_keeps_cache_id_from_plan_or_query_result(self):
        async def runner(prompt, user_role=None, last_context=None, user_id="", user_name=""):
            self.calls.append(prompt)
            return (
                "日活 100",
                {
                    "plan_task": {"intent": "日活"},
                    "query_result": {
                        "success": True,
                        "row_count": 1,
                        "sql_executed": "SELECT 1",
                        "cache_id": 9,
                    },
                },
            )

        service = self._service(runner=runner)
        asyncio.run(service.handle(_message(), [].append))
        self.assertEqual(service._contexts["u-001"]["cache_id"], 9)

    def test_default_feedback_records_a_cache_vote(self):
        votes = []
        fake = types.ModuleType("learning.feedback_loop")

        class _Loop:
            def on_analysis_feedback(self, **kwargs):
                return None

        fake.feedback_loop = _Loop()
        previous = sys.modules.get("learning.feedback_loop")
        sys.modules["learning.feedback_loop"] = fake
        try:
            with patch(
                "learning.query_cache.query_cache.record_vote",
                side_effect=lambda row_id, polarity: votes.append((row_id, polarity)),
            ):
                _default_on_feedback(
                    _user(),
                    "positive",
                    {"cache_id": 9, "last_user_query": "昨天日活", "last_analysis": "100"},
                )
        finally:
            if previous is None:
                sys.modules.pop("learning.feedback_loop", None)
            else:
                sys.modules["learning.feedback_loop"] = previous
        self.assertEqual(votes, [(9, "positive")])

    def test_reset_clears_follow_up_context(self):
        sent = []
        service = self._service()
        asyncio.run(service.handle(_message(), sent.append))
        asyncio.run(service.handle(_message(text="重置", msg_id="m2"), sent.append))
        self.assertEqual(self.deleted, ["u-001"])
        self.assertNotIn("u-001", service._contexts)
        self.assertEqual(sent[-1], RESET_TEXT)
        asyncio.run(service.handle(_message(text="按渠道", msg_id="m3"), sent.append))
        self.assertIsNone(self.calls[-1]["last_context"])

    def test_follow_up_passes_saved_context(self):
        sent = []
        service = self._service()
        asyncio.run(service.handle(_message(), sent.append))
        asyncio.run(service.handle(_message(text="按渠道", msg_id="m2"), sent.append))
        ctx = self.calls[-1]["last_context"]
        self.assertEqual(ctx["last_sql"], "SELECT 1")
        self.assertTrue(is_follow_up("按渠道"))
        self.assertFalse(is_follow_up("昨天日活多少"))
        self.assertTrue(is_confirmation("是"))
        self.assertFalse(is_confirmation("请帮我确认一下这张表"))

    def test_follow_up_restores_session_from_store(self):
        self.sessions["u-001"] = (
            {"last_sql": "SELECT 2", "last_result_summary": "旧结果", "last_analysis": "旧分析"},
            "前天新增",
        )
        sent = []
        asyncio.run(self._service().handle(_message(text="为什么"), sent.append))
        self.assertEqual(self.calls[0]["last_context"]["last_sql"], "SELECT 2")
        self.assertEqual(self.calls[0]["last_context"]["last_user_query"], "前天新增")

    def test_per_user_rate_limit(self):
        os.environ["BOT_RATE_LIMIT_PER_MINUTE"] = "1"
        reset_for_tests()
        sent = []
        service = self._service(use_real_rate_limit=True)
        try:
            asyncio.run(service.handle(_message(msg_id="a"), sent.append))
            asyncio.run(service.handle(_message(text="再问一次", msg_id="b"), sent.append))
        finally:
            reset_for_tests()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sent[-1], RATE_LIMIT_TEXT)


class IdentityStoreTests(unittest.TestCase):
    def test_pg_store_requires_identity_row_and_role(self):
        class Cursor:
            def __init__(self, row):
                self.row = row
            def execute(self, sql, params):
                self.params = params
            def fetchone(self):
                return self.row
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        class Conn:
            def __init__(self, row):
                self.cursor_obj = Cursor(row)
            def cursor(self):
                return self.cursor_obj
            def close(self):
                pass

        class Permissions:
            def __init__(self, role):
                self.role = role
                self.asked = None
            def get_user_role(self, user_id):
                self.asked = user_id
                return self.role

        perms = Permissions({"role_id": "analyst", "staff_name": "张三"})
        store = PgIdentityStore(connect=lambda: Conn(("u-001", "张三")), permissions=perms)
        resolved = store.resolve("dingtalk", "staff-1")
        self.assertEqual(resolved.user_id, "u-001")
        self.assertEqual(perms.asked, "u-001")

        missing = PgIdentityStore(connect=lambda: Conn(None), permissions=perms)
        self.assertIsNone(missing.resolve("Feishu", "ou_1"))

        no_role = PgIdentityStore(
            connect=lambda: Conn(("u-001", "")),
            permissions=Permissions(None),
        )
        self.assertIsNone(no_role.resolve("dingtalk", "staff-1"))


class LegacyRemovalTests(unittest.TestCase):
    def test_dingtalk_entrypoint_has_no_sql_fallback_or_hardcoded_hosts(self):
        text = (ROOT / "dingtalk_bot.py").read_text(encoding="utf-8")
        for forbidden in (
            "execute_plan",
            "gen_plan",
            "your-starrocks-host",
            "your_database",
            "Agent fallback",
            "localhost",
            "PLAN_PROMPT",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("ConversationService", text)
        forward = (ROOT / "agent_forward.py").read_text(encoding="utf-8")
        self.assertNotIn("except: pass", forward)
        self.assertNotIn('"host": os.getenv("PG_HOST", "localhost")', forward)
        legacy_sql = (ROOT / "scripts" / "starrocks_query_safe.py").read_text(encoding="utf-8")
        self.assertNotIn("your-starrocks-host", legacy_sql)
        self.assertNotIn("your_database", legacy_sql)
        self.assertNotIn("except: pass", legacy_sql)
