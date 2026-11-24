"""PreSQLHook：CTE 放行，严格模式可配置且默认开启。"""

import os
import unittest

from utils.pre_sql_hook import (
    ALLOWED_TABLES,
    HookExitCode,
    PreSQLHook,
    strict_mode_from_env,
)


class PreSqlHookTests(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("SQL_HOOK_STRICT_MODE")

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("SQL_HOOK_STRICT_MODE", None)
        else:
            os.environ["SQL_HOOK_STRICT_MODE"] = self._previous

    def test_strict_mode_defaults_to_on(self):
        os.environ.pop("SQL_HOOK_STRICT_MODE", None)
        self.assertTrue(strict_mode_from_env())
        self.assertTrue(PreSQLHook().strict_mode)
        os.environ["SQL_HOOK_STRICT_MODE"] = "false"
        self.assertFalse(PreSQLHook().strict_mode)
        os.environ["SQL_HOOK_STRICT_MODE"] = "0"
        self.assertFalse(PreSQLHook().strict_mode)
        os.environ["SQL_HOOK_STRICT_MODE"] = "true"
        self.assertTrue(PreSQLHook().strict_mode)

    def test_with_cte_is_allowed_and_cte_name_is_not_an_unknown_table(self):
        table = sorted(ALLOWED_TABLES)[0]
        sql = f"WITH daily AS (SELECT id FROM {table}) SELECT id FROM daily"
        hook = PreSQLHook(strict_mode=True)
        tables = hook._extract_tables(sql)
        self.assertIn(table, tables)
        self.assertNotIn("daily", {name.lower() for name in tables})
        result = hook.validate(sql)
        self.assertNotEqual(result.exit_code, HookExitCode.BLOCK, result.message)
        self.assertTrue(result.is_safe)

    def test_non_strict_with_query_is_not_rejected_for_being_a_cte(self):
        sql = "WITH daily AS (SELECT id FROM not_registered_xyz) SELECT id FROM daily"
        result = PreSQLHook(strict_mode=False).validate(sql)
        self.assertNotEqual(result.exit_code, HookExitCode.BLOCK, result.message)
        self.assertNotIn("仅允许SELECT", result.message)

    def test_into_outfile_blocks_even_when_not_strict(self):
        result = PreSQLHook(strict_mode=False).validate(
            "SELECT id FROM events INTO OUTFILE '/tmp/x'"
        )
        self.assertEqual(result.exit_code, HookExitCode.BLOCK)
        self.assertFalse(result.is_safe)

    def test_strict_mode_blocks_select_star(self):
        table = sorted(ALLOWED_TABLES)[0]
        result = PreSQLHook(strict_mode=True).validate(f"SELECT * FROM {table}")
        self.assertEqual(result.exit_code, HookExitCode.BLOCK)

    def test_quick_check_accepts_cte_and_rejects_export(self):
        hook = PreSQLHook(strict_mode=True)
        self.assertTrue(hook.quick_check("WITH c AS (SELECT 1 AS id) SELECT id FROM c"))
        self.assertFalse(hook.quick_check("SELECT 1 INTO DUMPFILE '/tmp/a'"))

    def test_drop_inside_a_string_is_not_a_write(self):
        table = sorted(ALLOWED_TABLES)[0]
        result = PreSQLHook(strict_mode=True).validate(
            f"SELECT 'DROP TABLE {table}' AS label FROM {table}"
        )
        self.assertNotEqual(result.exit_code, HookExitCode.BLOCK, result.message)


if __name__ == "__main__":
    unittest.main()
