"""图表、下载权限、定时报告和演示库隔离。"""

import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from analysis.compute import attach_computed_facts
from demo.generator import MISSING_DATE, SPIKE_DATE, daily_metric_result, generate_rows
from reports.charts import bundled_font_path, write_charts
from reports.deliver import deliver_dingtalk, deliver_feishu
from reports.files import resolve_file, save_report
from reports.schedule_commands import handle_schedule_command
from reports.schedule_parse import parse_schedule_command
from reports.schedules import MemoryScheduleStore, Schedule, is_due, run_due
from scripts.build_demo_report import build_demo_report
from scripts.load_demo_data import assert_demo_target


class ChartAndFileTests(unittest.TestCase):
    def test_font_is_bundled_and_charts_mark_anomalies(self):
        self.assertTrue(bundled_font_path())
        rows = generate_rows()
        result = daily_metric_result(rows, "dau")
        attach_computed_facts(result, "演示")
        with tempfile.TemporaryDirectory() as folder:
            paths = write_charts(result["computed_facts"], folder)
            self.assertTrue(any(path.endswith("series.png") for path in paths))
            with open(os.path.join(folder, "series.png"), "rb") as handle:
                self.assertEqual(handle.read(8), b"\x89PNG\r\n\x1a\n")

    def test_download_is_limited_to_the_owner_and_ttl_removes_files(self):
        facts = {"row_count": 2, "series": {"metric": "dau", "points": []}, "breakdowns": []}
        with tempfile.TemporaryDirectory() as folder:
            saved = save_report("usr-abc", "总结\n", [facts], root=folder, now=1_000)
            path = resolve_file("usr-abc", saved["report_id"], "report.md", root=folder, now=1_000)
            self.assertTrue(path)
            self.assertIsNone(resolve_file("usr-other", saved["report_id"], "report.md", root=folder, now=1_000))
            self.assertIsNone(resolve_file("usr-abc", saved["report_id"], "../meta.json", root=folder, now=1_000))
            self.assertIsNone(resolve_file("usr-abc", saved["report_id"], "report.md", root=folder, now=1_000 + 8 * 86400))

    def test_demo_report_mentions_planted_events(self):
        with tempfile.TemporaryDirectory() as folder:
            built = build_demo_report(folder)
            self.assertIn(SPIKE_DATE.isoformat(), built["text"])
            self.assertIn(MISSING_DATE.isoformat(), built["text"])
            self.assertTrue(os.path.isfile(os.path.join(folder, "weekly_report.docx")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "weekly_series.png")))


class ScheduleTests(unittest.TestCase):
    def test_parse_weekly_command(self):
        parsed = parse_schedule_command("每周一早上9点发我上周日活周报")
        self.assertEqual(parsed["cron"], "0 9 * * 1")
        self.assertEqual(parsed["report_spec"], "上周日活周报")
        self.assertEqual(parse_schedule_command("昨天日活多少"), None)

    def test_commands_only_touch_the_owner_private_chat(self):
        store = MemoryScheduleStore()
        user = type("U", (), {"user_id": "u-1"})()
        message = type("M", (), {"channel": "feishu", "user_ext_id": "ou_owner"})()
        reply = handle_schedule_command("每周一早上9点发我上周日活周报", user, message, store)
        self.assertIn("ou_owner", reply)
        other = type("U", (), {"user_id": "u-2"})()
        self.assertIn("没有找到", handle_schedule_command("取消定时报告 1", other, message, store))
        self.assertIn("已取消", handle_schedule_command("取消定时报告 1", user, message, store))

    def test_disabled_role_stops_delivery(self):
        store = MemoryScheduleStore()
        schedule = Schedule(
            id=1,
            owner_user_id="u-1",
            channel="feishu",
            target="ou_owner",
            cron="0 9 * * 1",
            report_spec="上周日活周报",
        )
        store.rows[1] = schedule
        sent = []
        now = datetime(2026, 11, 23, 9, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(is_due(schedule, now))
        done = run_due(store, lambda user_id: None, lambda spec, role: ("报告", {}), lambda *args: sent.append(args), now)
        self.assertEqual(sent, [])
        self.assertEqual(done[0]["status"], "disabled")
        self.assertFalse(store.rows[1].enabled)


class DemoIsolationTests(unittest.TestCase):
    def test_loader_refuses_production_database_names(self):
        previous = {key: os.environ.get(key) for key in ("DEMO_MODE", "DB_NAME", "DATA_DICTIONARY_PATH")}
        try:
            os.environ["DEMO_MODE"] = "1"
            os.environ["DB_NAME"] = "app_prod_db"
            with self.assertRaises(SystemExit):
                assert_demo_target()
            os.environ["DB_NAME"] = "skadi_demo"
            os.environ.pop("DEMO_MODE", None)
            with self.assertRaises(SystemExit):
                assert_demo_target()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_compose_files_do_not_share_env_files(self):
        with open("docker-compose.yml", encoding="utf-8") as handle:
            production = handle.read()
        with open("docker-compose.demo.yml", encoding="utf-8") as handle:
            demo = handle.read()
        self.assertNotIn("demo.env", production)
        self.assertNotIn("skadi_demo", production)
        self.assertNotIn("env_file: .env", demo)
        self.assertNotIn("data_dictionary.yml", demo.replace("data_dictionary.demo.yml", ""))


class DeliveryTests(unittest.TestCase):
    def test_feishu_uploads_image_and_docx_to_the_same_open_id(self):
        calls = []

        class Api:
            def upload_image(self, path):
                calls.append(("image", path))
                return "img-key"
            def send_image(self, open_id, image_key):
                calls.append(("send-image", open_id, image_key))
            def upload_file(self, path, name):
                calls.append(("file", name))
                return "file-key"
            def send_file(self, open_id, file_key):
                calls.append(("send-file", open_id, file_key))

        with tempfile.TemporaryDirectory() as folder:
            image = os.path.join(folder, "series.png")
            docx = os.path.join(folder, "report.docx")
            open(image, "wb").write(b"png")
            open(docx, "wb").write(b"docx")
            deliver_feishu(Api(), "ou_owner", {"paths": {"series.png": image, "report.docx": docx}})
        self.assertEqual(calls[1][1], "ou_owner")
        self.assertEqual(calls[3][1], "ou_owner")

    def test_dingtalk_uses_markdown_when_the_sdk_has_it(self):
        seen = []
        deliver_dingtalk(lambda title, body: seen.append(title), None, "分析报告", "正文", {"paths": {}})
        self.assertEqual(seen, ["分析报告"])
