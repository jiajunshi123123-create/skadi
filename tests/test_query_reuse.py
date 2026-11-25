"""权限重写、自愈越权、查询复用和时间参数。"""

import os
import unittest
from pathlib import Path

from agents.sql_guard import prepare_sql
from learning.case_record import build_case
from learning.embeddings import hash_embed
from learning.lessons import render_lessons, select_relevant
from learning.pattern_text import keywords_for_match
from learning.plan_context import format_similar_patterns
from learning.query_cache import MemoryQueryCacheRepo, QueryCache
from learning.time_params import normalize_question, render_sql, templatize_sql
from permission_manager import PermissionDenied, permission_manager
from utils.web_roles import match_role_id, resolve_web_role

ROOT = Path(__file__).resolve().parents[1]

ANALYST = {
    "role_id": "analyst",
    "role_name": "分析师",
    "allowed_tables": ["events"],
    "mandatory_filters": {"events": "region = 'east'"},
    "allow_user_detail": True,
    "max_query_days": 30,
}


class PermissionRewriteTests(unittest.TestCase):
    def test_cte_name_is_not_a_table_and_filter_lands_inside(self):
        sql = "WITH daily AS (SELECT region, cnt FROM events) SELECT cnt FROM daily"
        allowed, reason = permission_manager.check_sql_permission(sql, ANALYST)
        self.assertTrue(allowed, reason)
        self.assertEqual(permission_manager.extract_tables_from_sql(sql), ["events"])
        rewritten = permission_manager.enforce_sql(sql, ANALYST)
        self.assertIn("region = 'east'", rewritten)
        self.assertIn("events", rewritten.lower())
        # 过滤条件在 CTE 内部，不把 daily 当成需要授权的表。
        self.assertLess(rewritten.lower().index("region = 'east'"), rewritten.lower().rindex("daily"))

    def test_hidden_table_inside_cte_is_still_checked(self):
        sql = "WITH daily AS (SELECT salary FROM payroll) SELECT salary FROM daily"
        with self.assertRaises(PermissionDenied) as caught:
            permission_manager.enforce_sql(sql, ANALYST)
        self.assertIn("payroll", str(caught.exception))

    def test_self_heal_cannot_escalate_to_a_forbidden_table(self):
        executed = []

        def execute(sql):
            executed.append(sql)
            return sql

        safe = prepare_sql("SELECT cnt FROM events", ANALYST)
        execute(safe)
        with self.assertRaises(PermissionDenied):
            prepare_sql("SELECT salary FROM payroll", ANALYST)
        self.assertEqual(len(executed), 1)
        self.assertNotIn("payroll", executed[0].lower())

        source = (ROOT / "agents" / "query_agent.py").read_text(encoding="utf-8")
        run_query = source.split("def _run_query", 1)[1].split("\n    def ", 1)[0]
        self.assertLess(run_query.index("prepare_sql"), run_query.index("self.db.execute"))
        execute_body = source.split("async def execute", 1)[1].split("\n    def ", 1)[0]
        self.assertLess(execute_body.index("permission_denied"), execute_body.index("_self_heal"))


class LessonAndPatternTests(unittest.TestCase):
    def test_only_matching_table_and_error_are_injected(self):
        lessons = [
            {"table_name": "events", "error_type": "syntax", "problem": "括号不配对", "solution": "补上括号"},
            {"table_name": "payroll", "error_type": "syntax", "problem": "工资表秘密", "solution": "不要看"},
            {"table_name": "events", "error_type": "empty_result", "problem": "日期空了", "solution": "放宽日期"},
        ]
        picked = select_relevant(lessons, "db.events", "syntax")
        text = render_lessons(picked)
        self.assertEqual(len(picked), 1)
        self.assertIn("括号不配对", text)
        self.assertNotIn("工资表秘密", text)
        self.assertNotIn("日期空了", text)

    def test_chinese_keywords_and_pattern_list(self):
        words = keywords_for_match("昨天日活多少")
        self.assertIn("日活", words)
        text = format_similar_patterns([
            {"query_pattern": "单日查询+日活", "sql_template": "SELECT dau FROM events"},
        ])
        self.assertIn("SELECT dau FROM events", text)
        self.assertIn("单日查询+日活", text)
        self.assertEqual(format_similar_patterns([]), "")

    def test_case_record_has_no_data_and_stable_role_id(self):
        first, content, metadata = build_case("analyst", "昨天日活", "SELECT 1")
        second, _, _ = build_case("analyst", "昨天日活", "SELECT 1")
        other, _, _ = build_case("sales", "昨天日活", "SELECT 1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotIn("分析摘要", content)
        self.assertIn("SELECT 1", content)
        self.assertEqual(metadata["role_id"], "analyst")


class QueryReuseTests(unittest.TestCase):
    def setUp(self):
        os.environ["EMBEDDING_BACKEND"] = "hash"
        self.cache = QueryCache(
            repo=MemoryQueryCacheRepo(),
            embedder=hash_embed,
            threshold=0.85,
            success_to_verify=3,
            down_threshold=2,
        )
        self.sql = "SELECT dau FROM events WHERE d = CURDATE() - INTERVAL 1 DAY"

    def test_time_substitution_and_promotion(self):
        normalized_yesterday, params_yesterday = normalize_question("昨天日活多少")
        normalized_before, params_before = normalize_question("前天日活多少")
        self.assertEqual(normalized_yesterday, "{time}日活多少")
        self.assertEqual(normalized_yesterday, normalized_before)
        template = templatize_sql(self.sql, "昨天")
        rendered = render_sql(template, params_yesterday, params_before)
        self.assertIn("INTERVAL 2 DAY", rendered)
        self.assertNotIn("INTERVAL 1 DAY", rendered)

        row_id = self.cache.remember("昨天日活多少", self.sql, "analyst", ["events"])
        can_read = lambda tables: tables == ["events"]
        self.assertIsNone(self.cache.lookup("前天日活多少", "analyst", can_read))
        self.cache.record_success(row_id)
        self.cache.record_success(row_id)
        hit = self.cache.lookup("前天日活多少", "analyst", can_read)
        self.assertIsNotNone(hit)
        self.assertIn("INTERVAL 2 DAY", hit.sql)
        self.assertNotIn("INTERVAL 1 DAY", hit.sql)

    def test_no_cross_role_and_no_table_permission(self):
        row_id = self.cache.remember("昨天日活多少", self.sql, "analyst", ["events"])
        self.cache.record_vote(row_id, "positive")
        self.assertIsNone(self.cache.lookup("昨天日活多少", "sales", lambda tables: True))
        self.assertIsNone(self.cache.lookup("昨天日活多少", "analyst", lambda tables: False))
        hit = self.cache.lookup("昨天日活多少", "analyst", lambda tables: True)
        self.assertIsNotNone(hit)

    def test_thumbs_down_demotes_then_disables(self):
        row_id = self.cache.remember("昨天日活多少", self.sql, "analyst", ["events"])
        self.cache.record_vote(row_id, "positive")
        self.assertEqual(self.cache.record_vote(row_id, "negative"), "candidate")
        self.assertIsNone(self.cache.lookup("昨天日活多少", "analyst", lambda tables: True))
        self.assertEqual(self.cache.record_vote(row_id, "negative"), "disabled")
        self.assertIsNone(self.cache.lookup("昨天日活多少", "analyst", lambda tables: True))


class WebRoleTests(unittest.TestCase):
    def test_unmapped_key_is_rejected(self):
        os.environ["WEB_API_KEY_ROLES"] = "user-key-0123456789abcdef=analyst"
        self.assertEqual(match_role_id("user-key-0123456789abcdef"), "analyst")
        self.assertIsNone(match_role_id("another-key-0123456789ab"))
        role = resolve_web_role(
            {"X-API-Key": "user-key-0123456789abcdef"},
            loader=lambda role_id: {"role_id": role_id, "allowed_tables": ["events"]},
        )
        self.assertEqual(role["role_id"], "analyst")
        self.assertIsNone(resolve_web_role(
            {"X-API-Key": "another-key-0123456789ab"},
            loader=lambda role_id: {"role_id": role_id},
        ))
        web = (ROOT / "web_server.py").read_text(encoding="utf-8")
        self.assertIn("resolve_web_role", web)
        self.assertIn("last_context=session.get('last_context')", web)
