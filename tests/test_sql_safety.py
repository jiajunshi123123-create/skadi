"""只读 SQL 防护的单元测试。"""

import unittest

from tools.database_adapter import DatabaseAdapter, apply_mysql_readonly_session
from utils.sql_safety import check_readonly_sql, extract_cte_names


class SqlSafetyTests(unittest.TestCase):
    def test_allows_select_with_and_explain(self):
        allowed = [
            "SELECT id FROM events",
            "  select 1",
            "SELECT 1;",
            "WITH daily AS (SELECT id FROM events) SELECT id FROM daily",
            "WITH RECURSIVE c AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM c WHERE n < 3) SELECT n FROM c",
            "EXPLAIN SELECT id FROM events",
            "EXPLAIN WITH c AS (SELECT 1 AS id) SELECT id FROM c",
            "SELECT ';drop table events' AS label",
            "SELECT 'INTO OUTFILE' AS label",
            "SELECT 'DROP TABLE events' AS label",
            "SELECT REPLACE(name, 'a', 'b') FROM events",
            "SELECT CAST(name AS CHAR CHARACTER SET utf8mb4) FROM events",
            "SELECT id FROM events WHERE note = 'a; b'",
            "SELECT 'it''s' AS label",
            "SELECT 1 /* INTO OUTFILE '/tmp/x' */",
            "SELECT id FROM events ORDER BY id DESC",
            "-- comment\nSELECT 1",
        ]
        for sql in allowed:
            self.assertIsNone(check_readonly_sql(sql), sql)

    def test_blocks_writes_exports_and_stacked_statements(self):
        blocked = [
            "SELECT id FROM events INTO OUTFILE '/tmp/x'",
            "SELECT id FROM events INTO DUMPFILE '/tmp/x'",
            "SELECT id INTO OUTFILE '/tmp/x' FROM events",
            "select id from events into outfile '/tmp/x'",
            "SELECT * FROM events INTO @var",
            "SELECT LOAD_FILE('/etc/passwd')",
            "SELECT 1; DROP TABLE events",
            "SELECT 1; SELECT 2",
            "SELECT 1; -- comment\nDROP TABLE events",
            "INSERT INTO events VALUES (1)",
            "WITH c AS (SELECT 1) INSERT INTO events SELECT * FROM c",
            "WITH c AS (DELETE FROM events RETURNING id) SELECT id FROM c",
            "UPDATE events SET id = 1",
            "DROP TABLE events",
            "EXPLAIN DELETE FROM events",
            "EXPLAIN INSERT INTO events VALUES (1)",
            "SELECT 1 FROM events FOR UPDATE",
            "SELECT 1 /*! INTO OUTFILE '/tmp/x' */",
            "SELECT 1 /*!50000 INTO OUTFILE '/tmp/x' */",
            "SELECT SLEEP(10)",
            "SHOW TABLES",
            "DESCRIBE events",
            "",
            "   ",
            "SELECT 1\x00",
        ]
        for sql in blocked:
            self.assertIsNotNone(check_readonly_sql(sql), sql)

    def test_metadata_opt_in_is_explicit(self):
        self.assertIsNone(check_readonly_sql("SHOW TABLES", allow_metadata=True))
        self.assertIsNone(check_readonly_sql("DESCRIBE events", allow_metadata=True))
        self.assertIsNotNone(
            check_readonly_sql("SHOW TABLES; DROP TABLE events", allow_metadata=True)
        )
        self.assertIsNotNone(
            check_readonly_sql("SELECT id FROM events INTO OUTFILE '/tmp/x'", allow_metadata=True)
        )

    def test_unicode_whitespace_cannot_split_keywords(self):
        sql = "SELECT id FROM events INTO\u00a0OUTFILE '/tmp/x'"
        self.assertIsNotNone(check_readonly_sql(sql))

    def test_backslash_quote_is_ambiguous(self):
        sql = "SELECT 'it\\'s' AS label"
        result = check_readonly_sql(sql)
        self.assertIsNotNone(result)
        self.assertIn("歧义", result)

    def test_cte_names_are_extracted_without_string_decoys(self):
        sql = "WITH daily AS (SELECT id FROM events WHERE note = 'WITH secret AS (') SELECT id FROM daily"
        self.assertEqual(extract_cte_names(sql), {"daily"})

    def test_adapter_uses_the_same_guard(self):
        self.assertIsNone(DatabaseAdapter._check_sql_safety(
            "WITH c AS (SELECT 1 AS id) SELECT id FROM c"
        ))
        self.assertIsNotNone(DatabaseAdapter._check_sql_safety(
            "SELECT 1 INTO OUTFILE '/tmp/x'"
        ))
        self.assertIsNotNone(DatabaseAdapter._check_sql_safety("SHOW TABLES"))
        self.assertIsNone(DatabaseAdapter._check_sql_safety("SHOW TABLES", allow_metadata=True))


class _Cursor:
    def __init__(self, fail_on=()):
        self.statements = []
        self.fail_on = fail_on
        self.rolled_back = False
        self.connection = self

    def execute(self, sql, *args):
        if any(part in sql for part in self.fail_on):
            raise RuntimeError("unsupported")
        self.statements.append(sql)

    def rollback(self):
        self.rolled_back = True


class MysqlReadonlySessionTests(unittest.TestCase):
    def test_sets_sql_mode_then_read_only(self):
        cursor = _Cursor()
        self.assertTrue(apply_mysql_readonly_session(cursor))
        self.assertEqual(len(cursor.statements), 2)
        self.assertIn("sql_mode", cursor.statements[0])
        self.assertIn("READ ONLY", cursor.statements[1])
        self.assertNotIn("NO_BACKSLASH_ESCAPES", cursor.statements[0])

    def test_unsupported_server_is_not_fatal(self):
        cursor = _Cursor(fail_on=("READ ONLY",))
        self.assertFalse(apply_mysql_readonly_session(cursor))
        self.assertTrue(cursor.rolled_back)
        self.assertTrue(any("sql_mode" in sql for sql in cursor.statements))


if __name__ == "__main__":
    unittest.main()
