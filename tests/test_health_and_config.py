"""启动配置校验、弃用变量回退，以及健康检查不泄露配置。"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from config.env_settings import read_setting, reset_warnings_for_tests
from config.startup_check import StartupConfigError, validate_entrypoint
from utils.heartbeat import start_heartbeat

ROOT = Path(__file__).resolve().parents[1]

_VALID = {
    "DEEPSEEK_API_KEY": "sk-test-not-real",
    "DB_HOST": "127.0.0.1",
    "DB_USER": "readonly_user",
    "DB_PASSWORD": "db-secret-value",
    "DB_NAME": "analytics",
    "PG_HOST": "127.0.0.1",
    "PG_DATABASE": "agent_experience",
    "PG_USER": "agent_user",
    "PG_PASSWORD": "pg-secret-value",
    "WEB_API_KEY": "user-key-0123456789abcdef",
    "WEB_API_KEY_ROLES": "user-key-0123456789abcdef=analyst",
    "DINGTALK_BOT_APP_KEY": "ding-app-key",
    "DINGTALK_BOT_APP_SECRET": "ding-app-secret",
    "FEISHU_APP_ID": "cli_app",
    "FEISHU_APP_SECRET": "feishu-secret",
}


class EnvAliasTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: os.environ.get(name)
            for name in ("PG_PASSWORD", "PGPASSWORD", "DB_HOST", "STARROCKS_HOST")
        }
        reset_warnings_for_tests()

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        reset_warnings_for_tests()

    def test_deprecated_name_is_used_and_warned(self):
        os.environ.pop("PG_PASSWORD", None)
        os.environ["PGPASSWORD"] = "from-old-name"
        with self.assertLogs("skadi.config", level="WARNING") as captured:
            value = read_setting("PG_PASSWORD", "PGPASSWORD")
            value_again = read_setting("PG_PASSWORD", "PGPASSWORD")
        self.assertEqual(value, "from-old-name")
        self.assertEqual(value_again, "from-old-name")
        self.assertEqual(len(captured.output), 1)
        self.assertIn("PGPASSWORD", captured.output[0])
        self.assertIn("PG_PASSWORD", captured.output[0])

    def test_canonical_name_wins_over_deprecated(self):
        os.environ["DB_HOST"] = "db.internal"
        os.environ["STARROCKS_HOST"] = "old.internal"
        with self.assertLogs("skadi.config", level="WARNING"):
            value = read_setting("DB_HOST", "STARROCKS_HOST", default="localhost")
        self.assertEqual(value, "db.internal")


class StartupValidationTests(unittest.TestCase):
    def setUp(self):
        names = list(_VALID) + ["PG_DB", "PGDATABASE", "PGPASSWORD", "STARROCKS_PASSWORD"]
        self._saved = {name: os.environ.get(name) for name in names}
        for name, value in _VALID.items():
            os.environ[name] = value
        reset_warnings_for_tests()

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_complete_config_passes_for_each_entrypoint(self):
        for name in ("web", "dingtalk", "feishu"):
            validate_entrypoint(name)

    def test_missing_values_are_listed_together_without_secrets(self):
        os.environ["DB_PASSWORD"] = ""
        os.environ["PG_PASSWORD"] = "your_password"
        os.environ.pop("FEISHU_APP_ID", None)
        with self.assertRaises(StartupConfigError) as caught:
            validate_entrypoint("feishu")
        missing = caught.exception.missing
        self.assertIn("DB_PASSWORD", missing)
        self.assertIn("PG_PASSWORD", missing)
        self.assertIn("FEISHU_APP_ID", missing)
        message = str(caught.exception)
        self.assertNotIn("pg-secret-value", message)
        self.assertNotIn("db-secret-value", message)
        self.assertNotIn("feishu-secret", message)

    def test_deprecated_password_satisfies_validation(self):
        os.environ.pop("PG_PASSWORD", None)
        os.environ["PGPASSWORD"] = "legacy-pg-password"
        with self.assertLogs("skadi.config", level="WARNING"):
            validate_entrypoint("dingtalk")


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_file_is_written_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "heartbeat")
            started = time.time()
            start_heartbeat(path, interval=60)
            self.assertTrue(os.path.exists(path))
            self.assertLess(time.time() - os.stat(path).st_mtime, 5)
            self.assertGreaterEqual(os.stat(path).st_mtime, started - 1)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "ok\n")
            self.assertNotIn("secret", Path(path).read_text(encoding="utf-8"))


class HealthEndpointTests(unittest.TestCase):
    def test_health_routes_are_public_and_do_not_echo_config(self):
        try:
            from fastapi.testclient import TestClient
            import web_server
        except Exception as exc:
            self.skipTest(f"web stack unavailable: {exc}")
        client = TestClient(web_server.app)
        for path in ("/health", "/healthz"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.json()
            self.assertEqual(body["status"], "ok")
            self.assertNotIn("password", body)
            self.assertNotIn("secret", body)
            self.assertNotIn("api_key", body)
            self.assertNotIn(os.environ.get("WEB_API_KEY", "missing-key"), response.text)

    def test_chat_runs_the_agent_off_the_request_thread(self):
        try:
            from fastapi.testclient import TestClient
            import web_server
        except Exception as exc:
            self.skipTest(f"web stack unavailable: {exc}")
        seen = []
        original = web_server.run_agent

        async def fake_run_agent(query, user_role=None, last_context=None):
            seen.append(threading.current_thread().name)
            return f"echo:{query}", {}

        web_server.run_agent = fake_run_agent
        os.environ["WEB_API_KEY"] = "user-key-0123456789abcdef"
        os.environ["WEB_API_KEY_ROLES"] = "user-key-0123456789abcdef=analyst"
        import utils.web_roles as web_roles
        previous = web_roles.resolve_web_role
        web_roles.resolve_web_role = lambda headers, loader=None: {
            "role_id": "analyst",
            "role_name": "分析师",
            "allowed_tables": ["events"],
            "mandatory_filters": {},
            "allow_user_detail": True,
            "staff_id": "web:analyst",
            "staff_name": "测试",
            "max_query_days": 30,
        }
        try:
            client = TestClient(web_server.app)
            response = client.post(
                "/api/chat",
                headers={"X-API-Key": "user-key-0123456789abcdef"},
                json={"query": "昨天日活"},
            )
        finally:
            web_server.run_agent = original
            web_roles.resolve_web_role = previous
        self.assertEqual(response.status_code, 200)
        self.assertIn("echo:昨天日活", response.text)
        self.assertTrue(seen)
        self.assertNotEqual(seen[0], threading.current_thread().name)
        source = (ROOT / "web_server.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.to_thread", source)
        dingtalk = (ROOT / "dingtalk_bot.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.to_thread", dingtalk)
