"""HTTP 级认证、扫描权限和会话隔离。依赖已安装的 FastAPI。"""

import os
import unittest

os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-not-real")
os.environ.setdefault("WEB_API_KEY", "user-key-0123456789abcdef,second-user-key-0123456789")
os.environ.setdefault("WEB_ADMIN_API_KEY", "admin-key-0123456789abc")

try:
    from fastapi.testclient import TestClient

    import web_server
    from utils.web_auth import reset_for_tests
except Exception as exc:  # pragma: no cover - 环境未装齐依赖时跳过
    TestClient = None
    web_server = None
    reset_for_tests = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipIf(TestClient is None, f"web stack unavailable: {_IMPORT_ERROR}")
class WebApiAuthTests(unittest.TestCase):
    def setUp(self):
        os.environ["WEB_API_KEY"] = "user-key-0123456789abcdef,second-user-key-0123456789"
        os.environ["WEB_ADMIN_API_KEY"] = "admin-key-0123456789abc"
        os.environ["WEB_RATE_LIMIT_PER_MINUTE"] = "30"
        os.environ["WEB_ADMIN_RATE_LIMIT_PER_MINUTE"] = "10"
        os.environ["WEB_AUTH_FAILURE_LIMIT_PER_MINUTE"] = "20"
        reset_for_tests()
        web_server._session_contexts.clear()

        async def fake_run_agent(query, user_role=None, last_context=None):
            return f"echo:{query}", {}

        self._original_run_agent = web_server.run_agent
        web_server.run_agent = fake_run_agent
        os.environ["WEB_API_KEY_ROLES"] = (
            "user-key-0123456789abcdef=analyst,"
            "second-user-key-0123456789=analyst"
        )
        import utils.web_roles as web_roles

        def fake_resolve(headers, loader=None):
            role_id = web_roles.match_role_id(web_roles._presented_user_key(headers))
            if not role_id:
                return None
            return {
                "role_id": role_id,
                "role_name": "分析师",
                "allowed_tables": ["events"],
                "mandatory_filters": {},
                "allow_user_detail": True,
                "staff_id": "web:" + role_id,
                "staff_name": "测试",
                "max_query_days": 30,
            }

        self._original_resolve = web_roles.resolve_web_role
        self._web_roles = web_roles
        web_roles.resolve_web_role = fake_resolve
        self.client = TestClient(web_server.app)
        self.client.__enter__()

    def tearDown(self):
        web_server.run_agent = self._original_run_agent
        self._web_roles.resolve_web_role = self._original_resolve
        self.client.__exit__(None, None, None)
        web_server._session_contexts.clear()
        reset_for_tests()

    def test_health_is_public_and_api_requires_a_key(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(self.client.post("/api/chat", json={"query": "hi"}).status_code, 401)
        self.assertEqual(self.client.get("/api/skills").status_code, 401)
        self.assertEqual(self.client.post("/api/scan").status_code, 401)

        skills = self.client.get("/api/skills", headers={"X-API-Key": "user-key-0123456789abcdef"})
        self.assertEqual(skills.status_code, 200)
        self.assertGreaterEqual(skills.json()["total"], 1)

    def test_sessions_are_scoped_to_each_key(self):
        first = self.client.post(
            "/api/chat",
            headers={"X-API-Key": "user-key-0123456789abcdef"},
            json={"query": "alpha", "session_id": "shared-name"},
        )
        second = self.client.post(
            "/api/chat",
            headers={"X-API-Key": "second-user-key-0123456789"},
            json={"query": "beta", "session_id": "shared-name"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn("echo:alpha", first.text)
        self.assertEqual(second.status_code, 200)
        self.assertIn("echo:beta", second.text)

        stored = web_server._session_contexts
        self.assertEqual(len(stored), 2)
        prefixes = {key.split(":", 1)[0] for key in stored}
        self.assertEqual(len(prefixes), 2)
        messages = {
            key: [item["content"] for item in value["conversation_messages"]]
            for key, value in stored.items()
        }
        self.assertIn(["alpha", "echo:alpha"], messages.values())
        self.assertIn(["beta", "echo:beta"], messages.values())

        invalid = self.client.post(
            "/api/chat",
            headers={"X-API-Key": "user-key-0123456789abcdef"},
            json={"query": "nope", "session_id": "../other"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(len(web_server._session_contexts), 2)

    def test_scan_is_admin_only(self):
        user = self.client.post(
            "/api/scan",
            headers={"X-API-Key": "user-key-0123456789abcdef"},
        )
        self.assertEqual(user.status_code, 401)

        import tools.db_scanner as scanner
        original = scanner.scan_database
        scanner.scan_database = lambda db: {"error": "stopped-in-test"}
        try:
            admin = self.client.post(
                "/api/scan",
                headers={"X-Admin-Key": "admin-key-0123456789abc"},
            )
        finally:
            scanner.scan_database = original
        self.assertEqual(admin.status_code, 200)
        self.assertIn("stopped-in-test", admin.text)

    def test_rate_limit(self):
        os.environ["WEB_RATE_LIMIT_PER_MINUTE"] = "1"
        reset_for_tests()
        headers = {"X-API-Key": "user-key-0123456789abcdef"}
        first = self.client.post("/api/chat", headers=headers, json={"query": "one"})
        second = self.client.post("/api/chat", headers=headers, json={"query": "two"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)


if __name__ == "__main__":
    unittest.main()
