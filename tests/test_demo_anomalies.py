"""演示数据里埋下的尖峰、渠道下跌和缺数日必须被计算层找到。"""

import unittest

from analysis.compute import compute_all
from demo.generator import (
    DROP_CHANNEL,
    DROP_DATES,
    MISSING_DATE,
    SPIKE_DATE,
    daily_metric_result,
    generate_rows,
)


class DemoAnomalyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = generate_rows()

    def test_generator_is_deterministic_and_skips_the_missing_day(self):
        again = generate_rows()
        self.assertEqual(len(self.rows), len(again))
        self.assertEqual(self.rows[0], again[0])
        self.assertEqual(self.rows[-1], again[-1])
        dates = {row["stat_date"] for row in self.rows}
        self.assertNotIn(MISSING_DATE, dates)
        self.assertIn(SPIKE_DATE, dates)
        self.assertGreater(len(self.rows), 10000)

    def test_planted_spike_and_missing_day_on_the_total_series(self):
        result = daily_metric_result(self.rows, "dau")
        facts = compute_all(result["cols"], result["rows"])
        flagged = {item["date"]: item for item in facts["anomalies"]}
        self.assertIn(SPIKE_DATE.isoformat(), flagged)
        self.assertEqual(flagged[SPIKE_DATE.isoformat()]["direction"], "high")
        self.assertGreater(abs(flagged[SPIKE_DATE.isoformat()]["z"]), 2)
        self.assertIn(MISSING_DATE.isoformat(), facts["missing_days"])

    def test_planted_channel_drop_is_detected_on_that_channel(self):
        result = daily_metric_result(self.rows, "dau", channel=DROP_CHANNEL)
        facts = compute_all(result["cols"], result["rows"])
        low_days = {
            item["date"]
            for item in facts["anomalies"]
            if item["direction"] == "low"
        }
        for day in DROP_DATES:
            self.assertIn(day.isoformat(), low_days)

    def test_weekend_baseline_is_below_weekday_baseline(self):
        result = daily_metric_result(self.rows, "dau")
        facts = compute_all(result["cols"], result["rows"])
        split = facts["weekday_weekend"]
        self.assertGreater(split["weekday"]["mean"], split["weekend"]["mean"])
        self.assertGreater(split["weekday"]["n"], 8)
        self.assertGreater(split["weekend"]["n"], 8)
