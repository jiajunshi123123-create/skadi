"""API 认证、会话归属和限流。"""

import os
import unittest
from pathlib import Path

from utils.web_auth import (
    AuthDecision,
    RateLimiter,
    authorize,
    caller_id_for_key,
    check_rate_limit,
    reset_for_tests,
    scoped_session_id,
)

USER_KEY = "user-key-0123456789abcdef"
ADMIN_KEY = "admin-key-0123456789abc"


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: os.environ.get(name)
            for name in (
                "WEB_API_KEY",
                "WEB_ADMIN_API_KEY",
                "WEB_RATE_LIMIT_PER_MINUTE",
                "WEB_ADMIN_RATE_LIMIT_PER_MINUTE",
                "WEB_AUTH_FAILURE_LIMIT_PER_MINUTE",
            )
        }
        os.environ["WEB_API_KEY"] = USER_KEY
        os.environ["WEB_ADMIN_API_KEY"] = ADMIN_KEY
        os.environ["WEB_RATE_LIMIT_PER_MINUTE"] = "2"
        os.environ["WEB_ADMIN_RATE_LIMIT_PER_MINUTE"] = "1"
        os.environ["WEB_AUTH_FAILURE_LIMIT_PER_MINUTE"] = "2"
        reset_for_tests()

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        reset_for_tests()

    def test_missing_or_placeholder_key_fails_closed(self):
        os.environ["WEB_API_KEY"] = ""
        reset_for_tests()
        decision = authorize({}, "127.0.0.1", admin=False)
        self.assertEqual(decision.status_code, 503)

        os.environ["WEB_API_KEY"] = "replace-with-a-long-random-key"
        reset_for_tests()
        decision = authorize({"X-API-Key": os.environ["WEB_API_KEY"]}, "127.0.0.1")
        self.assertEqual(decision.status_code, 503)

        os.environ["WEB_API_KEY"] = "short-key"
        reset_for_tests()
        decision = authorize({"X-API-Key": "short-key"}, "127.0.0.1")
        self.assertEqual(decision.status_code, 503)

    def test_user_key_accepted_from_header_or_bearer(self):
        header = authorize({"X-API-Key": USER_KEY}, "10.0.0.8")
        bearer = authorize({"Authorization": f"Bearer {USER_KEY}"}, "10.0.0.8")
        self.assertTrue(header.ok)
        self.assertTrue(bearer.ok)
        self.assertEqual(header.caller_id, bearer.caller_id)
        self.assertTrue(header.caller_id.startswith("usr-"))

    def test_comma_separated_keys_are_distinct_callers(self):
        other = "second-user-key-0123456789"
        os.environ["WEB_API_KEY"] = USER_KEY + "," + other
        reset_for_tests()
        first = authorize({"X-API-Key": USER_KEY}, "10.2.0.1")
        second = authorize({"X-API-Key": other}, "10.2.0.1")
        rejected = authorize({"X-API-Key": "replace-with-a-long-random-key"}, "10.2.0.2")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertNotEqual(first.caller_id, second.caller_id)
        self.assertNotEqual(
            scoped_session_id(first.caller_id, "s1"),
            scoped_session_id(second.caller_id, "s1"),
        )
        self.assertEqual(rejected.status_code, 401)

    def test_wrong_key_is_unauthorized_without_echoing_it(self):
        decision = authorize({"X-API-Key": "not-the-real-key-at-all"}, "10.0.0.9")
        self.assertEqual(decision.status_code, 401)
        self.assertNotIn("not-the-real-key", decision.error)

    def test_scan_requires_a_distinct_admin_key(self):
        user_only = authorize({"X-API-Key": USER_KEY}, "10.1.0.1", admin=True)
        self.assertEqual(user_only.status_code, 401)

        admin = authorize({"X-Admin-Key": ADMIN_KEY}, "10.1.0.1", admin=True)
        self.assertTrue(admin.ok)
        self.assertTrue(admin.caller_id.startswith("adm-"))
        self.assertNotEqual(admin.caller_id, caller_id_for_key(USER_KEY))

        os.environ["WEB_ADMIN_API_KEY"] = USER_KEY
        reset_for_tests()
        same = authorize({"X-Admin-Key": USER_KEY}, "10.1.0.2", admin=True)
        self.assertEqual(same.status_code, 503)

    def test_session_is_bound_to_the_caller(self):
        user_a = caller_id_for_key(USER_KEY)
        user_b = caller_id_for_key(ADMIN_KEY)
        self.assertEqual(scoped_session_id(user_a, "session-1"), f"{user_a}:session-1")
        self.assertNotEqual(
            scoped_session_id(user_a, "session-1"),
            scoped_session_id(user_b, "session-1"),
        )
        self.assertEqual(scoped_session_id(user_a, ""), f"{user_a}:default")
        self.assertEqual(scoped_session_id(user_a, None), f"{user_a}:default")
        with self.assertRaises(ValueError):
            scoped_session_id(user_a, "../other")
        with self.assertRaises(ValueError):
            scoped_session_id(user_a, "a" * 41)
        with self.assertRaises(ValueError):
            scoped_session_id(user_a, "bad:session")

    def test_rate_limit_and_auth_failure_lockout(self):
        self.assertIsNone(check_rate_limit("usr-abc", "user"))
        self.assertIsNone(check_rate_limit("usr-abc", "user"))
        blocked = check_rate_limit("usr-abc", "user")
        self.assertIsInstance(blocked, AuthDecision)
        self.assertEqual(blocked.status_code, 429)
        self.assertIsNone(check_rate_limit("usr-other", "user"))

        authorize({"X-API-Key": "wrong-key-wrong-key"}, "192.0.2.10")
        authorize({"X-API-Key": "wrong-key-wrong-key"}, "192.0.2.10")
        locked = authorize({"X-API-Key": USER_KEY}, "192.0.2.10")
        self.assertEqual(locked.status_code, 429)
        still_open = authorize({"X-API-Key": USER_KEY}, "192.0.2.11")
        self.assertTrue(still_open.ok)

    def test_limiter_window_blocks_at_the_limit(self):
        limiter = RateLimiter(limit=1, window_seconds=60)
        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))


class ConfigDefaultTests(unittest.TestCase):
    def test_source_has_no_production_database_defaults(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / "config" / "agent_config.py").read_text(encoding="utf-8")
        self.assertNotIn("aliyuncs.com", text)
        self.assertNotIn("WanWei_", text)
        self.assertNotIn("app_prod_db", text)
        self.assertIn("os.getenv('DB_USER', '')", text)
        example = (root / ".env.example").read_text(encoding="utf-8")
        self.assertIn("DB_USER=readonly_user", example)
        self.assertNotIn("DB_USER=root", example)
        self.assertIn("WEB_API_KEY=", example)
        self.assertIn("SQL_HOOK_STRICT_MODE=true", example)


if __name__ == "__main__":
    unittest.main()
