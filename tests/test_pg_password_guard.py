"""Postgres 启动包装拒绝弱密码。"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "require_strong_pg_password.sh"


class PgPasswordGuardTests(unittest.TestCase):
    def _run(self, password, with_entrypoint=False):
        env = os.environ.copy()
        if password is None:
            env.pop("POSTGRES_PASSWORD", None)
        else:
            env["POSTGRES_PASSWORD"] = password
        with tempfile.TemporaryDirectory() as tmp:
            if with_entrypoint:
                entry = Path(tmp) / "docker-entrypoint.sh"
                entry.write_text("#!/bin/sh\necho booted\n", encoding="utf-8")
                entry.chmod(entry.stat().st_mode | stat.S_IEXEC)
                env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
            completed = subprocess.run(
                ["sh", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
            )
        return completed

    def test_rejects_missing_placeholder_and_short_passwords(self):
        for password in (None, "", "changeme", "change_me", "password", "short-pass"):
            completed = self._run(password)
            self.assertNotEqual(completed.returncode, 0, password)
            self.assertTrue(completed.stderr.strip(), password)

    def test_strong_password_reaches_the_official_entrypoint(self):
        completed = self._run("a-strong-password", with_entrypoint=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("booted", completed.stdout)


if __name__ == "__main__":
    unittest.main()
