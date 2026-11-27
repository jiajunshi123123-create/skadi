#!/usr/bin/env python3
"""用内存中的演示数据生成样例周报，不连接数据库。"""

from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.compute import attach_computed_facts
from demo.generator import (
    DROP_CHANNEL,
    MISSING_DATE,
    SPIKE_DATE,
    daily_metric_result,
    generate_rows,
)
from reports.files import save_report
from reports.synthesize import render_report


def build_demo_report(output_dir: str) -> dict:
    rows = generate_rows()
    history = daily_metric_result(rows, "dau")
    history["sql_executed"] = (
        "SELECT stat_date, SUM(dau) AS dau FROM demo_daily_metrics GROUP BY stat_date"
    )
    attach_computed_facts(history, "演示日活")
    channel = _channel_totals(rows)
    attach_computed_facts(channel, "按渠道")
    partner = daily_metric_result(rows, "dau", channel=DROP_CHANNEL)
    partner["sql_executed"] = (
        "SELECT stat_date, SUM(dau) AS dau FROM demo_daily_metrics "
        f"WHERE channel = '{DROP_CHANNEL}' GROUP BY stat_date"
    )
    attach_computed_facts(partner, "渠道合作")
    parts = [
        _part("当前周期", history),
        _part("维度拆解", channel),
        _part("渠道合作", partner),
    ]
    text = render_report("演示数据日活分析报告", parts, [
        "这份报告由种子固定的演示数据在内存中生成，没有连接生产库。",
        f"埋点：尖峰日 {SPIKE_DATE.isoformat()}，渠道下跌，缺数日 {MISSING_DATE.isoformat()}。",
    ])
    os.makedirs(output_dir, exist_ok=True)
    saved = save_report("demo-sample", text, [
        history["computed_facts"],
        channel["computed_facts"],
    ], root=output_dir)
    # save_report 把文件放在 owner/id 下。样例需要稳定的文件名，再复制出来。
    folder = saved["dir"]
    for name in ("report.md", "report.docx", "series.png", "breakdown.png"):
        source = os.path.join(folder, name)
        if os.path.isfile(source):
            target = os.path.join(output_dir, "weekly_" + name)
            with open(source, "rb") as incoming, open(target, "wb") as outgoing:
                outgoing.write(incoming.read())
    shutil.rmtree(os.path.join(output_dir, "demo-sample"), ignore_errors=True)
    return {"text": text, "dir": output_dir, "saved": saved}


def _channel_totals(rows: list) -> dict:
    totals = {}
    for row in rows:
        totals[row["channel"]] = totals.get(row["channel"], 0) + row["dau"]
    ordered = sorted(totals)
    return {
        "success": True,
        "cols": ["channel", "dau"],
        "rows": [[name, totals[name]] for name in ordered],
        "row_count": len(ordered),
        "sql_executed": "SELECT channel, SUM(dau) AS dau FROM demo_daily_metrics GROUP BY channel",
    }


def _part(purpose: str, result: dict) -> dict:
    return {
        "name": purpose,
        "purpose": purpose,
        "question": purpose,
        "success": True,
        "plan": {"sql": result.get("sql_executed")},
        "result": result,
    }


def main() -> None:
    output = sys.argv[1] if len(sys.argv) > 1 else os.path.join("docs", "demo")
    result = build_demo_report(output)
    print(result["dir"])


if __name__ == "__main__":
    main()
