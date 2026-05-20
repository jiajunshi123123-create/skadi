#!/opt/venv/bin/python3
"""StarRocks安全查询 v2 — EXPLAIN校验 + 执行"""
import os, sys, pymysql

DB = sys.argv[1] if len(sys.argv) > 1 else "your_database"
SQL = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
if not SQL: print("Usage: starrocks_query_safe.py <db> <SQL>"); sys.exit(1)

PASS = os.environ.get("STARROCKS_PASSWORD","")
if not PASS:
    try:
        with open(os.environ.get("ENV_FILE", "/opt/workspace/.env")) as f:
            for line in f:
                if line.startswith("STARROCKS_PASSWORD="): PASS = line.split("=",1)[1].strip(); break
    except: pass

CONN = {"host":os.environ.get("STARROCKS_HOST","your-starrocks-host"),"port":9030,"user":os.environ.get("STARROCKS_USER","data_user"),"password":PASS,"database":DB,"connect_timeout":10,"read_timeout":30}

def run(sql):
    conn = pymysql.connect(**CONN); cur = conn.cursor()
    try:
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]
        return cols, rows
    finally: cur.close(); conn.close()

# 1. Security
for kw in ["INSERT","UPDATE","DELETE","DROP","ALTER","CREATE","TRUNCATE","GRANT","REVOKE"]:
    if kw in SQL.upper().split():
        print(f"REJECTED: {kw}"); sys.exit(1)

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
