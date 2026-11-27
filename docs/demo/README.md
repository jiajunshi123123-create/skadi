# 演示报告样例

这些文件由 `python scripts/build_demo_report.py docs/demo` 从固定种子的演示数据生成，没有连接数据库。

- `weekly_report.md` / `weekly_report.docx`：总结、数据来源、计算方法、详细分析和建议
- `weekly_series.png`：全部日期的日活，红点是同星期几基线下的异常
- `weekly_breakdown.png`：渠道贡献

埋点：2026-09-15 全渠道尖峰，2026-08-20 至 2026-08-22「渠道合作」下跌，2026-07-08 整天没有数据。国庆周的低点是假期效应，不是额外埋点。

本地把这套数据放进 MySQL 并打开 Web：`make demo`。演示配置在 `config/demo.env.example`，和生产 `.env` 分开。
