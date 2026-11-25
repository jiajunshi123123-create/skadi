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
        normalized_rolling, _ = normalize_question("最近7天日活多少")
        self.assertEqual(normalized_yesterday, "{time:single_day}日活多少")
        self.assertEqual(normalized_yesterday, normalized_before)
        self.assertEqual(params_yesterday["time_family"], "single_day")
        self.assertNotEqual(normalized_yesterday, normalized_rolling)
        self.assertIn("{time:rolling}", normalized_rolling)
        # 等值条件表示整整一天，换成前天仍然是那一天，不会带上昨天。
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
        self.assertIsNone(self.cache.lookup("最近7天日活多少", "analyst", can_read))

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
        yesterday = (
            "SELECT COUNT(DISTINCT user_id) AS dau FROM events "
            "WHERE dt_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AND dt_utc < CURRENT_DATE()"
        )
        day_before = (
            "SELECT COUNT(DISTINCT user_id) AS dau FROM events "
            "WHERE dt_utc >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY) "
            "AND dt_utc < DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        )
        first = self.cache.observe_success("昨天日活多少", yesterday, "analyst", ["events"])
        second = self.cache.observe_success("前天日活多少", day_before, "analyst", ["events"])
        third = self.cache.observe_success("昨天日活多少", yesterday, "analyst", ["events"])
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(self.cache.repo.rows), 1)
        row = self.cache.repo.get(first)
        self.assertEqual(row["success_cnt"], 3)
        self.assertEqual(row["status"], "verified")
        self.assertNotIn("region", row["sql_template"])
        self.assertIn("{time_sql}", row["sql_template"])
        self.assertIn("{time_upper}", row["sql_template"])
        hit = self.cache.lookup("前天日活多少", "analyst", lambda tables: True)
        self.assertIsNotNone(hit)
        self.assertIn("DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)", hit.sql)
        self.assertIn("DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)", hit.sql)
        self.assertLess(hit.sql.index("INTERVAL 2 DAY"), hit.sql.index("INTERVAL 1 DAY"))
        self.assertNotIn("< CURRENT_DATE()", hit.sql)
        today = self.cache.lookup("今天日活多少", "analyst", lambda tables: True)
        self.assertIsNotNone(today)
        self.assertIn(">= CURRENT_DATE()", today.sql)
        self.assertIn("DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY)", today.sql)
        self.assertNotIn(">= CURRENT_DATE() AND dt_utc < CURRENT_DATE()", today.sql)

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
        self.assertIn("{time_upper}", template)
        self.assertNotIn("INTERVAL 1 DAY", template)
        self.assertNotIn("< CURRENT_DATE()", template)
        rendered = render_sql(template, {"time": "昨天"}, {"time": "前天"})
        self.assertIn("DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)", rendered)
        self.assertIn("< DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)", rendered)
        self.assertNotIn("< CURRENT_DATE()", rendered)
        today_sql = render_sql(template, {"time": "昨天"}, {"time": "今天"})
        self.assertIn("dt_utc >= CURRENT_DATE()", today_sql)
        self.assertIn("DATE_ADD(CURRENT_DATE(), INTERVAL 1 DAY)", today_sql)
        self.assertNotIn("dt_utc < CURRENT_DATE()", today_sql)
        self.assertIsNone(render_sql(template, {"time": "昨天"}, {"time": "最近7天"}))
        self.assertIsNone(render_sql(template, {"time": "昨天"}, {"time": "上月"}))

        # 昨天的半开区间不能被「今天」只换掉上界。对不上就保持原样，不生成可复用模板。
        untouched = templatize_sql(ranged, "今天")
        self.assertNotIn("{time_sql}", untouched)
        self.assertIn("DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)", untouched)
        self.assertIn("< CURRENT_DATE()", untouched)

        point_week = templatize_sql(
            "SELECT n FROM t WHERE date = DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)",
            "最近7天",
        )
        self.assertIsNone(render_sql(point_week, {"time": "最近7天"}, {"time": "最近30天"}))
        rolling = templatize_sql(
            "SELECT n FROM t WHERE dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND dt < CURRENT_DATE()",
            "最近7天",
        )
        rolling_30 = render_sql(rolling, {"time": "最近7天"}, {"time": "最近30天"})
        self.assertIn("INTERVAL 30 DAY", rolling_30)
        self.assertIn("< CURRENT_DATE()", rolling_30)
        between = templatize_sql(
            "SELECT n FROM t WHERE date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()",
            "最近7天",
        )
        between_30 = render_sql(between, {"time": "最近7天"}, {"time": "最近30天"})
        self.assertIn("BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND CURRENT_DATE()", between_30)

    def test_enforcing_cached_sql_injects_the_filter_once(self):
        raw = "SELECT dau FROM events WHERE d = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        once = permission_manager.enforce_sql(raw, ANALYST)
        self.assertEqual(once.lower().count("region"), 1)
        twice = permission_manager.enforce_sql(once, ANALYST)
        self.assertGreaterEqual(twice.lower().count("region"), 2)

    def test_different_sql_resets_the_count_instead_of_promoting(self):
        events = "SELECT dau FROM events WHERE d = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        orders = "SELECT cnt FROM orders WHERE d = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)"
        first = self.cache.observe_success("昨天日活多少", events, "analyst", ["events"])
        self.cache.observe_success("昨天日活多少", events, "analyst", ["events"])
        reset = self.cache.observe_success("昨天日活多少", orders, "analyst", ["orders"])
        self.assertEqual(first, reset)
        self.assertEqual(len(self.cache.repo.rows), 1)
        row = self.cache.repo.get(reset)
        self.assertEqual(row["success_cnt"], 1)
        self.assertEqual(row["status"], "candidate")
        self.assertIn("orders", row["sql_template"])
        self.assertEqual(row["tables"], ["orders"])
        self.cache.observe_success("昨天日活多少", orders, "analyst", ["orders"])
        self.cache.observe_success("昨天日活多少", orders, "analyst", ["orders"])
        row = self.cache.repo.get(reset)
        self.assertEqual(row["success_cnt"], 3)
        self.assertEqual(row["status"], "verified")
        hit = self.cache.lookup("昨天日活多少", "analyst", lambda tables: True)
        self.assertIn("orders", hit.sql)
        self.assertNotIn("events", hit.sql)

    def test_success_does_not_revive_a_disabled_row(self):
        row_id = self.cache.remember("昨天日活多少", self.sql, "analyst", ["events"])
        self.cache.record_vote(row_id, "positive")
        self.assertEqual(self.cache.record_vote(row_id, "negative"), "candidate")
        self.assertEqual(self.cache.record_vote(row_id, "negative"), "disabled")
        self.assertIsNone(self.cache.record_success(row_id))
        self.assertEqual(self.cache.repo.get(row_id)["status"], "disabled")
        self.assertEqual(self.cache.repo.get(row_id)["down_cnt"], 2)
        again = self.cache.observe_success("昨天日活多少", self.sql, "analyst", ["events"])
        self.assertIsNone(again)
        self.assertEqual(len(self.cache.repo.rows), 1)
        self.assertEqual(self.cache.repo.get(row_id)["status"], "disabled")

    def test_cache_writes_are_atomic_and_unique(self):
        init_sql = (ROOT / "scripts" / "init_db.sql").read_text(encoding="utf-8")
        migrate_sql = (ROOT / "scripts" / "migrate.sql").read_text(encoding="utf-8")
        notes = (ROOT / "scripts" / "MIGRATION.md").read_text(encoding="utf-8")
        for script in (init_sql, migrate_sql):
            self.assertIn("uq_query_cache_role_normalized_q", script)
            self.assertIn("ON query_cache(role_scope, normalized_q)", script)
        self.assertIn("DELETE FROM query_cache", migrate_sql)
        self.assertIn("ROW_NUMBER()", migrate_sql)
        self.assertIn("uq_query_cache_role_normalized_q", notes)

        from learning.query_cache import PgQueryCacheRepo

        class _Cursor:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=None):
                self.statements.append((statement, params))

            def fetchone(self):
                return (7, "candidate")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Conn:
            def __init__(self):
                self.cursor_obj = _Cursor()
                self.committed = False

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                self.committed = True

            def close(self):
                pass

        conn = _Conn()
        repo = PgQueryCacheRepo()
        with patch("learning.query_cache._connect", return_value=conn):
            repo.upsert_success({
                "normalized_q": "{time:single_day}日活多少",
                "params_template": {"time": "昨天"},
                "embedding": [1.0],
                "sql_template": "SELECT {time_sql}",
                "role_scope": "analyst",
                "tables": ["events"],
            }, 3)
        upsert_sql, _params = conn.cursor_obj.statements[-1]
        self.assertIn("ON CONFLICT (role_scope, normalized_q)", upsert_sql)
        self.assertIn("query_cache.success_cnt + 1", upsert_sql)
        self.assertIn("EXCLUDED.sql_template", upsert_sql)
        self.assertIn("query_cache.status = 'disabled'", upsert_sql)
        self.assertTrue(conn.committed)

        conn.cursor_obj.fetchone = lambda: ("verified",)
        with patch("learning.query_cache._connect", return_value=conn):
            status = repo.increment_success(7, 3, template="SELECT {time_sql}")
        inc_sql, _params = conn.cursor_obj.statements[-1]
        self.assertEqual(status, "verified")
        self.assertIn("success_cnt = success_cnt + 1", inc_sql)
        self.assertIn("status = CASE", inc_sql)
        self.assertIn("status <> 'disabled'", inc_sql)
        self.assertIn("sql_template = %s", inc_sql)

        conn.cursor_obj.fetchone = lambda: ("disabled",)
        with patch("learning.query_cache._connect", return_value=conn):
            voted = repo.apply_vote(7, "negative", 2)
        vote_sql, _params = conn.cursor_obj.statements[-1]
        self.assertEqual(voted, "disabled")
        self.assertIn("up_cnt = up_cnt +", vote_sql)
        self.assertIn("down_cnt = down_cnt +", vote_sql)
        self.assertIn("status = CASE", vote_sql)
        self.assertIn("status <> 'disabled'", vote_sql)


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
        self.assertIn("session.get('last_context')", web)


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


def _load_orchestrator():
    """导入编排器时不依赖用例顺序，也不依赖真实的 API key。

    全量跑的时候 test_web_api_auth 会先导入 agents.plan_agent。那时密钥常量还是空的，
    PlanAgent() 会在编排器导入中途失败。这里在导入前把各个模块上的 ChatOpenAI 换成桩。
    """
    import sys
    from contextlib import ExitStack

    os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-not-real")
    os.environ.setdefault("EMBEDDING_BACKEND", "hash")
    import config.agent_config as agent_config
    import langchain_openai

    if not agent_config.DEEPSEEK_API_KEY:
        agent_config.DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]

    class _FakeLLM:
        def __init__(self, *args, **kwargs):
            pass

    loaded = [
        sys.modules[name]
        for name in (
            "agents.plan_agent",
            "agents.query_agent",
            "agents.analysis_agent",
            "analysis_planner",
        )
        if name in sys.modules
    ]
    with ExitStack() as stack:
        stack.enter_context(patch.object(langchain_openai, "ChatOpenAI", _FakeLLM))
        for mod in loaded:
            if hasattr(mod, "ChatOpenAI"):
                stack.enter_context(patch.object(mod, "ChatOpenAI", _FakeLLM))
            if hasattr(mod, "DEEPSEEK_API_KEY") and not mod.DEEPSEEK_API_KEY:
                mod.DEEPSEEK_API_KEY = agent_config.DEEPSEEK_API_KEY
        broken = sys.modules.get("agent_orchestrator")
        if broken is not None and not hasattr(broken, "_remember_query_cache"):
            del sys.modules["agent_orchestrator"]
        import agent_orchestrator
    return agent_orchestrator


class CacheRoundTripTests(unittest.TestCase):
    def test_success_writes_cache_id_back_and_stores_unfiltered_sql(self):
        os.environ["EMBEDDING_BACKEND"] = "hash"
        orchestrator = _load_orchestrator()
        _remember_query_cache = orchestrator._remember_query_cache
        format_output = orchestrator.format_output

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

    def test_follow_up_does_not_read_or_write_the_cache(self):
        orchestrator = _load_orchestrator()
        calls = []

        class _Guard:
            def lookup(self, *args, **kwargs):
                calls.append("lookup")
                return None

            def observe_success(self, *args, **kwargs):
                calls.append("observe")
                return 1

        state = {
            "user_query": "按渠道拆分",
            "user_role": {"role_id": "analyst", "allowed_tables": ["events"]},
            "last_context": {"last_sql": "SELECT 1 FROM events", "last_user_query": "昨天日活"},
            "plan_task": {"sql": "SELECT channel FROM events"},
            "query_result": {
                "success": True,
                "sql_before_enforce": "SELECT channel FROM events",
            },
        }
        with patch("learning.query_cache.query_cache", _Guard()):
            self.assertIsNone(orchestrator._lookup_cached_plan(state))
            self.assertIsNone(orchestrator._remember_query_cache(state, state["query_result"]))
        self.assertEqual(calls, [])
        self.assertNotIn("cache_id", state["plan_task"])
