"""权限重写、自愈越权、查询复用和时间参数。"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.query_agent import QueryAgent
from agents.sql_guard import prepare_sql
from learning.case_record import build_case
from learning.embeddings import hash_embed
from learning.lessons import render_lessons, select_relevant
from learning.pattern_text import keywords_for_match
from learning.plan_context import format_similar_patterns
from learning.query_cache import MemoryQueryCacheRepo, QueryCache
from learning.time_params import normalize_question, render_sql, sql_fragment, templatize_sql
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

    def test_repeated_success_promotes_one_row_without_a_prior_hit(self):
        starrocks = (
            "SELECT COUNT(DISTINCT user_id) AS dau FROM events "
            "WHERE dt_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AND dt_utc < CURRENT_DATE()"
        )
        first = self.cache.observe_success("昨天日活多少", starrocks, "analyst", ["events"])
        second_sql = starrocks.replace("INTERVAL 1 DAY", "INTERVAL 2 DAY")
        second = self.cache.observe_success("前天日活多少", second_sql, "analyst", ["events"])
        third = self.cache.observe_success("昨天日活多少", starrocks, "analyst", ["events"])
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(self.cache.repo.rows), 1)
        row = self.cache.repo.get(first)
        self.assertEqual(row["success_cnt"], 3)
        self.assertEqual(row["status"], "verified")
        self.assertNotIn("region", row["sql_template"])
        self.assertIn("{time_sql}", row["sql_template"])
        hit = self.cache.lookup("前天日活多少", "analyst", lambda tables: True)
        self.assertIsNotNone(hit)
        self.assertIn("INTERVAL 2 DAY", hit.sql)
        self.assertNotIn("INTERVAL 1 DAY", hit.sql)

    def test_starrocks_time_sql_is_parameterized_without_weekday(self):
        self.assertNotIn("WEEKDAY", (sql_fragment("本周") or "").upper())
        self.assertIn("CURRENT_DATE()", sql_fragment("本周"))
        week_sql = "SELECT w FROM events WHERE d >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)"
        week_template = templatize_sql(week_sql, "本周")
        self.assertIn("{time_sql}", week_template)
        rendered_week = render_sql(week_template, {"time": "本周"}, {"time": "本周"})
        self.assertNotIn("WEEKDAY", rendered_week.upper())

        ranged = (
            "SELECT dau FROM events "
            "WHERE dt_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AND dt_utc < CURRENT_DATE()"
        )
        template = templatize_sql(ranged, "昨天")
        self.assertIn("{time_sql}", template)
        self.assertNotIn("INTERVAL 1 DAY", template)
        self.assertIn("< CURRENT_DATE()", template)
        rendered = render_sql(template, {"time": "昨天"}, {"time": "前天"})
        self.assertIn("INTERVAL 2 DAY", rendered)
        self.assertNotIn("INTERVAL 1 DAY", rendered)

        today = templatize_sql(ranged, "今天")
        self.assertIn("DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)", today)
        self.assertTrue(today.endswith("{time_sql}"))
        self.assertNotIn("DATE_SUB({time_sql}", today)

    def test_enforcing_cached_sql_injects_the_filter_once(self):
        raw = "SELECT dau FROM events WHERE d = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        once = permission_manager.enforce_sql(raw, ANALYST)
        self.assertEqual(once.lower().count("region"), 1)
        twice = permission_manager.enforce_sql(once, ANALYST)
        self.assertGreaterEqual(twice.lower().count("region"), 2)


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


class _StubDb:
    def __init__(self):
        self.explained = []
        self.executed = []

    def explain(self, sql):
        self.explained.append(sql)
        return {"success": True}

    def execute(self, sql):
        self.executed.append(sql)
        return {"success": True, "cols": ["id"], "rows": [[1]], "row_count": 1}


class PermissionAuditTests(unittest.TestCase):
    def _agent(self):
        agent = QueryAgent.__new__(QueryAgent)
        agent.db = _StubDb()
        return agent

    def test_allowed_query_is_audited_and_keeps_sql_before_filters(self):
        from utils.pre_sql_hook import ALLOWED_TABLES

        table = sorted(ALLOWED_TABLES)[0]
        role = {
            "role_id": "analyst",
            "role_name": "分析师",
            "staff_id": "u-1",
            "staff_name": "张三",
            "allowed_tables": [table],
            "mandatory_filters": {table: "region = 'east'"},
            "allow_user_detail": True,
        }
        agent = self._agent()
        calls = []
        with patch.object(permission_manager, "log_permission_audit", side_effect=lambda **kwargs: calls.append(kwargs)):
            result = agent._run_query(f"SELECT id FROM {table}", role)
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["action"], "query")
        self.assertEqual(calls[0]["staff_id"], "u-1")
        self.assertNotIn("region", calls[0]["original_sql"])
        self.assertIn("region = 'east'", calls[0]["enforced_sql"])
        self.assertEqual(result["sql_before_enforce"], f"SELECT id FROM {table}")
        self.assertIn("region = 'east'", result["sql_executed"])
        self.assertEqual(len(agent.db.executed), 1)

    def test_denied_query_is_audited_and_not_executed(self):
        from utils.pre_sql_hook import ALLOWED_TABLES

        table = sorted(ALLOWED_TABLES)[0]
        role = {
            "role_id": "analyst",
            "role_name": "分析师",
            "staff_id": "u-1",
            "staff_name": "张三",
            "allowed_tables": ["some_other_table"],
            "mandatory_filters": {},
            "allow_user_detail": True,
        }
        agent = self._agent()
        calls = []
        with patch.object(permission_manager, "log_permission_audit", side_effect=lambda **kwargs: calls.append(kwargs)):
            result = agent._run_query(f"SELECT id FROM {table}", role)
        self.assertTrue(result["permission_denied"])
        self.assertEqual(calls[0]["action"], "denied")
        self.assertIn(table.lower(), calls[0]["denied_reason"].lower())
        self.assertEqual(agent.db.executed, [])
        self.assertEqual(agent.db.explained, [])

    def test_admin_success_is_not_audited(self):
        from utils.pre_sql_hook import ALLOWED_TABLES

        table = sorted(ALLOWED_TABLES)[0]
        agent = self._agent()
        calls = []
        with patch.object(permission_manager, "log_permission_audit", side_effect=lambda **kwargs: calls.append(kwargs)):
            result = agent._run_query(
                f"SELECT id FROM {table}",
                {"role_id": "admin", "staff_id": "root"},
            )
        self.assertTrue(result["success"])
        self.assertEqual(calls, [])


class CacheRoundTripTests(unittest.TestCase):
    def test_success_writes_cache_id_back_and_stores_unfiltered_sql(self):
        os.environ["EMBEDDING_BACKEND"] = "hash"
        import langchain_openai
        import agents.query_agent as query_agent_mod

        class _FakeLLM:
            def __init__(self, *args, **kwargs):
                pass

        with patch.object(langchain_openai, "ChatOpenAI", _FakeLLM), \
             patch.object(query_agent_mod, "ChatOpenAI", _FakeLLM):
            from agent_orchestrator import _remember_query_cache, format_output

        cache = QueryCache(
            repo=MemoryQueryCacheRepo(),
            embedder=hash_embed,
            threshold=0.85,
            success_to_verify=3,
            down_threshold=2,
        )
        raw = (
            "SELECT dau FROM events "
            "WHERE d = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        )
        state = {
            "user_query": "昨天日活多少",
            "user_role": {"role_id": "analyst"},
            "plan_task": {"sql": raw, "intent": "日活"},
            "query_result": {
                "success": True,
                "row_count": 1,
                "sql_executed": raw + " AND region = 'east'",
                "sql_before_enforce": raw,
            },
            "analysis": "日活 100",
            "start_time": 0,
        }
        with patch("learning.query_cache.query_cache", cache):
            cache_id = _remember_query_cache(state, state["query_result"])
            output = __import__("asyncio").run(format_output(dict(state)))
        self.assertEqual(cache_id, 1)
        self.assertEqual(state["plan_task"]["cache_id"], 1)
        self.assertEqual(state["query_result"]["cache_id"], 1)
        stored = cache.repo.get(1)["sql_template"]
        self.assertNotIn("region", stored)
        self.assertIn("{time_sql}", stored)
        self.assertEqual(output["plan_task"]["cache_id"], 1)
        self.assertEqual(output["query_result"]["cache_id"], 1)
