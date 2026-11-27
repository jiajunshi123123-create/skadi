"""报告拆分、权限边界和结构化正文。"""

import asyncio
import unittest
from datetime import date, timedelta

from analysis.compute import attach_computed_facts
from analysis.number_check import untraceable_numbers
from reports.execute import execute_report
from reports.planner import is_report_request, needs_baseline_query, plan_subqueries
from reports.synthesize import SECTIONS, render_report


def _series(start, values):
    rows = []
    for offset, value in enumerate(values):
        rows.append([(start + timedelta(days=offset)).isoformat(), value])
    return {
        "success": True,
        "cols": ["stat_date", "dau"],
        "rows": rows,
        "row_count": len(rows),
        "sql_executed": "SELECT stat_date, dau FROM demo_daily_metrics",
    }


class PlannerTests(unittest.TestCase):
    def test_weekly_report_is_capped_at_four_subqueries(self):
        planned = plan_subqueries("每周一早上发我上周日活周报")
        self.assertTrue(is_report_request("上周日活周报"))
        self.assertEqual(len(planned), 4)
        self.assertEqual([item.name for item in planned], ["current", "baseline", "breakdown", "previous"])

    def test_plain_question_stays_single_query(self):
        self.assertFalse(needs_baseline_query("昨天日活多少"))
        self.assertEqual(len(plan_subqueries("昨天日活多少")), 1)

    def test_anomaly_question_adds_a_baseline_query_only(self):
        planned = plan_subqueries("昨天日活有没有异常")
        self.assertEqual([item.name for item in planned], ["current", "baseline"])


class ExecuteReportTests(unittest.TestCase):
    def test_every_subquery_receives_the_role_and_permission_denial_stops(self):
        role = {"role_id": "analyst", "allowed_tables": ["demo_daily_metrics"]}
        calls = []

        async def plan(question, user_role=None):
            self.assertEqual(user_role["role_id"], "analyst")
            return {"sql": "SELECT 1", "intent": "demo"}

        async def query(plan_task, user_role=None):
            calls.append(user_role["role_id"])
            if len(calls) == 2:
                return {"success": False, "permission_denied": True, "error": "无权访问 demo_daily_metrics"}
            result = _series(date(2026, 6, 1), [100, 110, 90])
            return result

        payload = asyncio.run(execute_report("上周日活周报", role, plan, query))
        self.assertEqual(calls, ["analyst", "analyst"])
        self.assertTrue(payload["parts"][1]["permission_denied"])
        self.assertEqual(len(payload["parts"]), 2)
        self.assertTrue(any("权限" in note for note in payload["notes"]))

    def test_row_cap_stops_before_adopting_the_next_result(self):
        async def plan(question, user_role=None):
            return {"sql": "SELECT 1"}

        async def query(plan_task, user_role=None):
            return _series(date(2026, 6, 1), [1, 2, 3, 4, 5, 6])

        payload = asyncio.run(execute_report(
            "上周日活周报",
            {"role_id": "analyst"},
            plan,
            query,
            max_rows=10,
        ))
        self.assertEqual(payload["total_rows"], 6)
        self.assertEqual(len(payload["parts"]), 1)
        self.assertTrue(any("行数上限" in note for note in payload["notes"]))

    def test_cache_hit_skips_planning(self):
        planned = []

        async def plan(question, user_role=None):
            planned.append(question)
            return {"sql": "SELECT fresh"}

        async def query(plan_task, user_role=None):
            result = _series(date(2026, 6, 1), [10, 12])
            result["sql_executed"] = plan_task["sql"]
            return result

        def lookup(state):
            if state["user_query"].startswith("为回答"):
                return {"plan_task": {"sql": "SELECT cached", "intent": "cached"}}
            return None

        payload = asyncio.run(execute_report(
            "昨天日活有没有异常",
            {"role_id": "analyst"},
            plan,
            query,
            lookup=lookup,
        ))
        self.assertEqual(len(planned), 1)
        sqls = [part["result"]["sql_executed"] for part in payload["parts"]]
        self.assertIn("SELECT cached", sqls)


class RenderReportTests(unittest.TestCase):
    def test_report_uses_only_computed_numbers(self):
        result = _series(date(2026, 6, 1), [100] * 40 + [400])
        attach_computed_facts(result, "周报")
        parts = [{
            "name": "current",
            "purpose": "当前周期",
            "question": "日活周报",
            "success": True,
            "plan": {"sql": result["sql_executed"]},
            "result": result,
        }]
        text = render_report("日活周报", parts, ["已达到时间上限，后面的子查询没有执行。"])
        for section in SECTIONS:
            self.assertIn(f"## {section}", text)
        self.assertIn("400", text)
        self.assertNotIn("99999", text)
        facts = result["computed_facts"]
        # 方法说明里的 30 天、2σ、1.5 倍 IQR 是规则，不是业务数字。
        body = text.split("## 计算方法")[0] + text.split("## 详细分析")[1]
        flagged = untraceable_numbers(body, facts, result["sql_executed"])
        self.assertEqual(flagged, [])
