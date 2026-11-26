#!/usr/bin/env python3
"""把演示数据写入 MySQL。拒绝连到名称不是 skadi_demo 的库。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from demo.generator import CREATE_TABLE_SQL, generate_rows, iter_sql_inserts


def assert_demo_target() -> None:
    if os.environ.get("DEMO_MODE") != "1":
        raise SystemExit("拒绝加载：需要 DEMO_MODE=1。这个脚本不能对生产库执行。")
    name = os.environ.get("DB_NAME") or os.environ.get("STARROCKS_DB") or ""
    if name != "skadi_demo":
        raise SystemExit(f"拒绝加载：DB_NAME 必须是 skadi_demo，当前是 {name!r}。")
    dictionary = os.environ.get("DATA_DICTIONARY_PATH", "")
    if dictionary and not dictionary.endswith("data_dictionary.demo.yml"):
        raise SystemExit("拒绝加载：DATA_DICTIONARY_PATH 必须指向 data_dictionary.demo.yml。")


def main() -> None:
    assert_demo_target()
    import pymysql

    connection = pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        autocommit=True,
    )
    rows = generate_rows()
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TABLE_SQL)
            cursor.execute("DELETE FROM demo_daily_metrics")
            for statement in iter_sql_inserts(rows):
                cursor.execute(statement)
    finally:
        connection.close()
    print(f"已写入 skadi_demo.demo_daily_metrics，共 {len(rows)} 行。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"演示数据没有写入：{exc}", file=sys.stderr)
        sys.exit(1)
