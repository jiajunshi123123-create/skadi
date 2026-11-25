#!/opt/venv/bin/python3
"""StarRocks安全查询 v2 — EXPLAIN校验 + 执行"""
import sys, pymysql
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.database_adapter import apply_mysql_readonly_session
from utils.sql_safety import check_readonly_sql

from config.env_settings import read_setting

DB = sys.argv[1] if len(sys.argv) > 1 else read_setting("DB_NAME", "STARROCKS_DB")
SQL = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
if not SQL:
    print("Usage: starrocks_query_safe.py <db> <SQL>")
    sys.exit(1)

HOST = read_setting("DB_HOST", "STARROCKS_HOST")
USER = read_setting("DB_USER", "STARROCKS_USER")
PASS = read_setting("DB_PASSWORD", "STARROCKS_PASSWORD")
PORT = int(read_setting("DB_PORT", "STARROCKS_PORT", default="9030"))
if not HOST or not USER or not DB:
    print("REJECTED: 缺少数据库地址、用户或库名，请设置 DB_*（STARROCKS_* 已弃用）")
    sys.exit(1)

CONN = {
    "host": HOST,
    "port": PORT,
    "user": USER,
    "password": PASS,
    "database": DB,
    "connect_timeout": 10,
    "read_timeout": 30,
}

def run(sql):
    conn = pymysql.connect(**CONN); cur = conn.cursor()
    try:
        apply_mysql_readonly_session(cur)
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]
        return cols, rows
    finally: cur.close(); conn.close()

# 1. Security — single SELECT / WITH / EXPLAIN, no INTO OUTFILE
_safety_error = check_readonly_sql(SQL)
if _safety_error:
    print(f"REJECTED: {_safety_error}"); sys.exit(1)

# 2. EXPLAIN
FIELD_ERRORS = ["Unknown column","doesn't exist","cannot be resolved","Unknown table"]
if not SQL.upper().strip().startswith("EXPLAIN"):
    try:
        cols, rows = run(f"EXPLAIN {SQL}")
        print(f"EXPLAIN OK ({len(rows)} nodes)")
    except pymysql.Error as e:
        err = str(e)
        if any(kw in err for kw in FIELD_ERRORS):
            print(f"EXPLAIN FAILED: {err[:300]}")
            print("HINT: Check field/table name, fix SQL and retry.")
            sys.exit(1)
        print(f"EXPLAIN WARN: {err[:200]}")

# 3. Execute
try:
    cols, rows = run(SQL)
    print(f"COLS: {cols}")
    print(f"ROWS: {len(rows)}")
    for row in rows[:50]: print(row)
    if len(rows) > 50: print(f"TRUNCATED: {len(rows)} rows, showing 50. Add filters or use GROUP BY.")
except pymysql.Error as e:
    print(f"EXEC FAILED: {str(e)[:500]}")
    sys.exit(1)
