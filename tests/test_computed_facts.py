"""计算层、数字核对，以及分析代理只引用算好的数字。"""

import os
import unittest
from datetime import date, timedelta

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-not-real")
os.environ.setdefault("EMBEDDING_BACKEND", "hash")

from analysis.compute import compute_all
from analysis.number_check import untraceable_numbers, warning_block
from analysis.present import format_result_for_llm
from skills.anomaly import AnomalySkill
from skills.comparison import ComparisonSkill
from skills.correlation import CorrelationSkill
from skills.distribution import DistributionSkill
from skills.regression import RegressionSkill
from skills.time_series import TimeSeriesSkill
from skills.trend import TrendSkill


def _daily(values, start=date(2026, 1, 5)):
    cols = ["stat_date", "dau"]
    rows = []
    for offset, value in enumerate(values):
        rows.append([(start + timedelta(days=offset)).isoformat(), value])
    return cols, rows


class ComputeFactsTests(unittest.TestCase):
    def test_summary_uses_every_row(self):
        cols, rows = _daily([10, 20, 30, 40])
        facts = compute_all(cols, rows)
        self.assertEqual(facts["row_count"], 4)
        self.assertTrue(facts["computed_on_full_result"])
        self.assertEqual(facts["metrics"]["dau"]["summary"]["sum"], 100)
        self.assertEqual(facts["metrics"]["dau"]["summary"]["mean"], 25)

    def test_mom_compares_adjacent_windows(self):
        values = [10] * 90 + [20] * 30
        cols, rows = _daily(values)
        facts = compute_all(cols, rows)
        mom = facts["metrics"]["dau"]["mom"]
        self.assertTrue(mom["available"])
        self.assertEqual(mom["current_sum"], 600)
        self.assertEqual(mom["previous_sum"], 300)
        self.assertAlmostEqual(mom["change_pct"], 100.0, places=2)
        self.assertFalse(facts["metrics"]["dau"]["yoy"]["available"])

    def test_weekday_and_weekend_baselines_are_separate(self):
        start = date(2026, 6, 1)  # 周一
        values = []
        day = start
        for _ in range(70):
            values.append(50 if day.weekday() >= 5 else 100)
            day += timedelta(days=1)
        cols, rows = _daily(values, start)
        facts = compute_all(cols, rows)
        split = facts["weekday_weekend"]
        self.assertEqual(split["weekday"]["mean"], 100)
        self.assertEqual(split["weekend"]["mean"], 50)
        self.assertGreater(split["weekday"]["n"], 8)
        self.assertGreater(split["weekend"]["n"], 8)

    def test_zscore_flags_spike_and_iqr_agrees(self):
        start = date(2026, 6, 1)
        values = []
        day = start
        spike_day = None
        for _ in range(84):
            noise = 1 if day.day % 2 == 0 else -1
            value = 100 + noise
            if day == date(2026, 7, 14):
                value = 500
                spike_day = day.isoformat()
            values.append(value)
            day += timedelta(days=1)
        cols, rows = _daily(values, start)
        facts = compute_all(cols, rows)
        flagged = {item["date"] for item in facts["anomalies"]}
        self.assertIn(spike_day, flagged)
        high = [item for item in facts["anomalies"] if item["date"] == spike_day][0]
        self.assertEqual(high["direction"], "high")
        self.assertGreater(abs(high["z"]), 2)
        iqr_dates = {item["date"] for item in facts["metrics"]["dau"]["iqr_outliers"]}
        self.assertIn(spike_day, iqr_dates)

    def test_missing_day_is_reported(self):
        cols, rows = _daily([10] * 10)
        del rows[4]
        facts = compute_all(cols, rows)
        self.assertIn("2026-01-09", facts["missing_days"])

    def test_breakdown_and_correlation(self):
        cols = ["stat_date", "channel", "dau", "gmv"]
        rows = []
        for offset in range(12):
            day = (date(2026, 3, 1) + timedelta(days=offset)).isoformat()
            rows.append([day, "App", 100 + offset, 200 + offset * 2])
            rows.append([day, "网页", 40, 80])
        facts = compute_all(cols, rows)
        breakdown = facts["breakdowns"][0]
        self.assertEqual(breakdown["dimension"], "channel")
        self.assertEqual(breakdown["top"][0]["key"], "App")
        self.assertGreater(breakdown["top"][0]["share_pct"], 50)
        self.assertEqual(len(facts["correlations"]), 1)
        self.assertGreater(facts["correlations"][0]["r"], 0.9)
        self.assertGreater(facts["metrics"]["dau"]["regression"]["r2"], 0.5)

    def test_sample_states_that_computation_used_every_row(self):
        cols, rows = _daily(list(range(80)))
        facts = compute_all(cols, rows)
        text = format_result_for_llm({"cols": cols, "rows": rows[:50], "row_count": 80}, facts)
        self.assertIn("全部 80 行", text)
        self.assertNotIn("仅显示前50行", text)
        self.assertLess(len(facts["sample_rows"]), 80)

    def test_skills_delegate_to_the_same_facts(self):
        cols, rows = _daily([10] * 40 + [80])
        records = [dict(zip(cols, row)) for row in rows]
        anomaly = AnomalySkill().analyze_data(records, cols)
        self.assertTrue(anomaly["anomalies"] or anomaly["missing_days"] is not None)
        self.assertIn("metrics", TrendSkill().analyze_data(records, cols))
        self.assertIn("weekday_weekend", TimeSeriesSkill().analyze_data(records, cols))
        self.assertIn("breakdowns", ComparisonSkill().analyze_data(records, cols))
        self.assertIn("mean", DistributionSkill().analyze_data(records, cols)["metrics"]["dau"]["distribution"])
        self.assertIn("correlations", CorrelationSkill().analyze_data(
            [dict(stat_date="2026-01-01", dau=index, gmv=index * 2) for index in range(1, 12)],
            ["stat_date", "dau", "gmv"],
        ))
        self.assertTrue(RegressionSkill().analyze_data(records, cols)["metrics"]["dau"]["regression"]["available"])


class NumberCheckTests(unittest.TestCase):
    def test_flags_invented_numbers_and_accepts_facts(self):
        facts = {"metrics": {"dau": {"summary": {"mean": 25.0, "sum": 100}}}}
        self.assertEqual(untraceable_numbers("日活均值是 25。", facts), [])
        flagged = untraceable_numbers("日活均值是 25，预计明天 99999。", facts)
        self.assertEqual(flagged, ["99999"])
        self.assertIn("99999", warning_block(flagged))

    def test_ignores_dates_list_markers_and_sigma(self):
        facts = {"date": "2026-01-05", "z": 3.2}
        text = "1. 2026-01-05 偏离 3.2，超过 2σ。"
        self.assertEqual(untraceable_numbers(text, facts), [])


class AnalysisAgentNumberGuardTests(unittest.TestCase):
    def test_regenerates_once_when_numbers_are_not_in_facts(self):
        from agents.analysis_agent import AnalysisAgent

        agent = AnalysisAgent.__new__(AnalysisAgent)
        agent.system_prompt = "只引用计算结果"
        replies = ["明天会到 99999。", "均值是 25。"]

        class FakeLLM:
            async def ainvoke(self, messages):
                class Response:
                    content = replies.pop(0)
                return Response()

        agent.llm = FakeLLM()
        import asyncio

        facts = {"metrics": {"dau": {"summary": {"mean": 25}}}}
        result = {"cols": ["dau"], "rows": [[25]], "row_count": 1, "computed_facts": facts}
        text = asyncio.run(agent.analyze("看日活", result, computed_facts=facts))
        self.assertIn("25", text)
        self.assertNotIn("99999", text)
        self.assertEqual(replies, [])

    def test_appends_warning_when_the_rewrite_is_still_wrong(self):
        from agents.analysis_agent import AnalysisAgent

        agent = AnalysisAgent.__new__(AnalysisAgent)
        agent.system_prompt = "只引用计算结果"
        replies = ["编造 88888。", "还是 77777。"]

        class FakeLLM:
            async def ainvoke(self, messages):
                class Response:
                    content = replies.pop(0)
                return Response()

        agent.llm = FakeLLM()
        import asyncio

        facts = {"metrics": {"dau": {"summary": {"mean": 25}}}}
        result = {"cols": ["dau"], "rows": [[25]], "row_count": 1, "computed_facts": facts}
        text = asyncio.run(agent.analyze("看日活", result, computed_facts=facts))
        self.assertIn("数字核对", text)
        self.assertIn("77777", text)
