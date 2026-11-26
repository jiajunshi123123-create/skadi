"""生成约六个月的演示经营数据。种子固定，结果可复现。

故意埋了三个问题，用来核对异常检测：
- 2026-09-15 全渠道日活放大，形成尖峰
- 2026-08-20 至 2026-08-22「渠道合作」日活跌到约一成
- 2026-07-08 整天没有数据
另外带有工作日/周末差异、国庆假期效应和缓慢增长。
"""

from __future__ import annotations

import random
from datetime import date, timedelta

DEMO_SEED = 42
END_DATE = date(2026, 11, 25)
HISTORY_DAYS = 182
SPIKE_DATE = date(2026, 9, 15)
SPIKE_FACTOR = 2.8
DROP_DATES = {date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22)}
DROP_CHANNEL = "渠道合作"
DROP_FACTOR = 0.12
MISSING_DATE = date(2026, 7, 8)
HOLIDAY_START = date(2026, 10, 1)
HOLIDAY_END = date(2026, 10, 7)

CHANNELS = ("App", "网页", "小程序", "渠道合作")
REGIONS = ("华东", "华北", "华南", "西部")
CATEGORIES = ("服饰", "数码", "食品", "家居")
USER_TYPES = ("新客", "老客")

CHANNEL_WEIGHT = {"App": 0.42, "网页": 0.23, "小程序": 0.22, "渠道合作": 0.13}
REGION_WEIGHT = {"华东": 0.34, "华北": 0.26, "华南": 0.24, "西部": 0.16}
CATEGORY_WEIGHT = {"服饰": 0.30, "数码": 0.28, "食品": 0.24, "家居": 0.18}
USER_WEIGHT = {"新客": 0.28, "老客": 0.72}
WEEKDAY_FACTOR = {0: 1.02, 1: 1.06, 2: 1.05, 3: 1.03, 4: 0.98, 5: 0.74, 6: 0.68}
CATEGORY_AOV = {"服饰": 180, "数码": 640, "食品": 46, "家居": 220}
BASE_DAU = 120000


def generate_rows(seed: int = DEMO_SEED) -> list:
    """返回 demo_daily_metrics 粒度的行。缺数日不产生任何行。"""
    rng = random.Random(seed)
    start = END_DATE - timedelta(days=HISTORY_DAYS - 1)
    rows = []
    day = start
    index = 0
    while day <= END_DATE:
        if day != MISSING_DATE:
            rows.extend(_day_rows(day, index, rng))
        day += timedelta(days=1)
        index += 1
    return rows


def daily_metric_result(rows: list, metric: str = "dau", channel: str = None) -> dict:
    """聚合成按日一行，供计算层使用。"""
    totals = {}
    for row in rows:
        if channel and row["channel"] != channel:
            continue
        key = row["stat_date"]
        totals[key] = totals.get(key, 0) + row[metric]
    ordered = sorted(totals)
    return {
        "cols": ["stat_date", metric],
        "rows": [[day.isoformat(), totals[day]] for day in ordered],
        "row_count": len(ordered),
    }


def iter_sql_inserts(rows: list, batch_size: int = 200) -> list:
    """MySQL / StarRocks 都能执行的 INSERT。数值来自生成器，不拼外部输入。"""
    statements = []
    for offset in range(0, len(rows), batch_size):
        chunk = rows[offset:offset + batch_size]
        values = []
        for row in chunk:
            values.append(
                "('{date}','{channel}','{region}','{category}','{user_type}',{dau},{orders},{gmv})".format(
                    date=row["stat_date"].isoformat(),
                    channel=row["channel"],
                    region=row["region"],
                    category=row["category"],
                    user_type=row["user_type"],
                    dau=int(row["dau"]),
                    orders=int(row["orders"]),
                    gmv=f"{row['gmv']:.2f}",
                )
            )
        statements.append(
            "INSERT INTO demo_daily_metrics "
            "(stat_date, channel, region, category, user_type, dau, orders, gmv) VALUES\n"
            + ",\n".join(values)
            + ";"
        )
    return statements


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS demo_daily_metrics (
    stat_date DATE NOT NULL,
    channel VARCHAR(32) NOT NULL,
    region VARCHAR(32) NOT NULL,
    category VARCHAR(32) NOT NULL,
    user_type VARCHAR(16) NOT NULL,
    dau BIGINT NOT NULL,
    orders BIGINT NOT NULL,
    gmv DECIMAL(18,2) NOT NULL
)
""".strip()


def _day_rows(day: date, index: int, rng: random.Random) -> list:
    trend = 1.0 + 0.35 * (index / float(HISTORY_DAYS - 1))
    weekday = WEEKDAY_FACTOR[day.weekday()]
    holiday = 0.62 if HOLIDAY_START <= day <= HOLIDAY_END else 1.0
    holiday_gmv = 1.35 if HOLIDAY_START <= day <= HOLIDAY_END else 1.0
    spike = SPIKE_FACTOR if day == SPIKE_DATE else 1.0
    rows = []
    for channel in CHANNELS:
        channel_factor = DROP_FACTOR if channel == DROP_CHANNEL and day in DROP_DATES else 1.0
        for region in REGIONS:
            for category in CATEGORIES:
                for user_type in USER_TYPES:
                    noise = 1.0 + (rng.random() - 0.5) * 0.06
                    dau = BASE_DAU * CHANNEL_WEIGHT[channel] * REGION_WEIGHT[region]
                    dau *= CATEGORY_WEIGHT[category] * USER_WEIGHT[user_type]
                    dau *= trend * weekday * holiday * spike * channel_factor * noise
                    dau_value = max(1, int(round(dau)))
                    order_rate = 0.16 if user_type == "新客" else 0.31
                    orders = max(1, int(round(dau_value * order_rate * (1.0 + (rng.random() - 0.5) * 0.04))))
                    gmv = round(orders * CATEGORY_AOV[category] * holiday_gmv * (1.0 + (rng.random() - 0.5) * 0.05), 2)
                    rows.append({
                        "stat_date": day,
                        "channel": channel,
                        "region": region,
                        "category": category,
                        "user_type": user_type,
                        "dau": dau_value,
                        "orders": orders,
                        "gmv": gmv,
                    })
    return rows
