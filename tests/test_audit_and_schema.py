"""审计失败必须留下日志；init_db.sql 要覆盖代码实际读写的表。"""

import unittest
from pathlib import Path

from core.audit import write_audit_log

ROOT = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self, error=None):
        self.error = error
        self.statement = ""
        self.params = None

    def execute(self, statement, params):
        self.statement = statement
        self.params = params
        if self.error:
            raise self.error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Conn:
    def __init__(self, error=None):
        self.cursor_obj = _Cursor(error)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class AuditLogTests(unittest.TestCase):
    def test_write_failure_is_logged_and_not_swallowed(self):
        conn = _Conn(error=RuntimeError("disk full"))
        with self.assertLogs("skadi.audit", level="ERROR") as captured:
            ok = write_audit_log(
                user_id="u-1",
                user_name="张三",
                query="昨天日活",
                response="失败",
                status="error",
                connect=lambda: conn,
            )
        self.assertFalse(ok)
        self.assertTrue(conn.rolled_back)
        self.assertTrue(conn.closed)
        self.assertFalse(conn.committed)
        self.assertTrue(any("审计日志写入失败" in line for line in captured.output))
        self.assertTrue(any("u-1" in line for line in captured.output))

    def test_successful_insert_uses_shared_columns(self):
        conn = _Conn()
        ok = write_audit_log(
            user_id="u-1",
            query="q",
            response="r",
            user_name="张三",
            connect=lambda: conn,
        )
        self.assertTrue(ok)
        self.assertTrue(conn.committed)
        for column in ("user_id", "user_name", "query", "response", "source", "status", "action"):
            self.assertIn(column, conn.cursor_obj.statement)


class SchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.sql = (ROOT / "scripts" / "init_db.sql").read_text(encoding="utf-8")
        self.migrate = (ROOT / "scripts" / "migrate.sql").read_text(encoding="utf-8")

    def test_init_script_matches_code_tables(self):
        required = {
            "sessions": ("user_id", "plan", "question"),
            "tasks": ("task_id", "user_question", "plan_json", "execution", "tags"),
            "audit_logs": ("user_name", "query", "response", "source", "sql_executed", "agent_path", "action"),
            "patterns": ("query_pattern", "sql_template", "analysis_template", "success_count", "last_used"),
            "lessons": ("lesson_type", "original_query", "problem", "solution", "updated_at"),
            "role_definitions": ("allowed_tables", "mandatory_filters", "max_query_days", "allow_user_detail"),
            "user_permissions": ("staff_id", "role_id", "is_active", "expires_at"),
            "permission_audit_log": ("original_sql", "enforced_sql", "tables_accessed", "denied_reason"),
            "user_identities": ("channel", "external_id", "user_id"),
            "context_anchors": ("session_id", "anchor_text", "anchor_tokens", "original_tokens", "compressed_at"),
        }
        for table, columns in required.items():
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", self.sql)
            for column in columns:
                self.assertIn(column, self.sql)

    def test_migration_backfills_dingtalk_identities_and_keeps_legacy_tables(self):
        self.assertIn("sessions_legacy", self.migrate)
        self.assertIn("tasks_legacy", self.migrate)
        self.assertIn("INSERT INTO user_identities", self.migrate)
        self.assertIn("channel", self.migrate)
        notes = (ROOT / "scripts" / "MIGRATION.md").read_text(encoding="utf-8")
        self.assertIn("docker-entrypoint-initdb.d", notes)
        self.assertIn("user_identities", notes)
