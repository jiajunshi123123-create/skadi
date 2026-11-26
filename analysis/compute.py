"""在完整结果集上计算分析事实。

模型只负责措辞。均值、环比、移动平均、同星期几基线、异常点和贡献度都在这里算出。
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

DATE_NAME_HINTS = ("date", "dt", "day", "日期", "时间", "stat_date")
ID_NAME_HINTS = ("id", "user_id", "staff_id", "编号")
METRIC_PREFERENCE = (
    "dau", "gmv", "orders", "amount", "revenue", "uv", "pv",
    "日活", "金额", "订单", "收入", "销量", "用户数",
)
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
Z_THRESHOLD = 2.0
MIN_WEEKDAY_PEERS = 8
TOP_N = 8


def attach_computed_facts(result: dict, query: str = "") -> dict:
    """把完整结果的计算结果写回 query_result，供后续压缩和模型引用。"""
    cols = list(result.get("cols") or [])
    rows = list(result.get("rows") or [])
    facts = compute_all(cols, rows)
    facts["user_query"] = query or ""
    result["computed_facts"] = facts
    result["facts_row_count"] = facts.get("row_count", len(rows))
    return facts


def compute_all(cols: list, rows: list) -> dict:
    """对全部行计算统计事实。行可以是 list 或 dict。"""
    records = rows_to_records(cols, rows)
    if not records:
        return {
            "row_count": 0,
            "computed_on_full_result": True,
            "note": "无数据",
            "metrics": {},
            "missing_days": [],
            "anomalies": [],
            "breakdowns": [],
            "correlations": [],
            "sample_rows": [],
        }
    frame = _prepare(records)
    date_col = _detect_date_column(frame)
    metric_cols = _detect_metric_columns(frame, date_col)
    dimension_cols = _detect_dimensions(frame, date_col, metric_cols)
    daily = _daily_frame(frame, date_col, metric_cols) if date_col else None

    metrics = {}
    anomalies = []
    for metric in metric_cols:
        block = _metric_block(frame, daily, date_col, metric, dimension_cols)
        metrics[metric] = block
        for item in block.get("anomalies") or []:
            anomalies.append(dict(item, metric=metric))

    missing = []
    if daily is not None and len(daily.index):
        missing = _missing_days(daily.index)

    facts = {
        "row_count": int(len(frame)),
        "computed_on_full_result": True,
        "date_column": date_col,
        "date_min": _date_text(_min_date(daily if daily is not None else frame, date_col)),
        "date_max": _date_text(_max_date(daily if daily is not None else frame, date_col)),
        "metric_columns": metric_cols,
        "dimension_columns": dimension_cols,
        "missing_days": missing,
        "metrics": metrics,
        "anomalies": anomalies,
        "breakdowns": _breakdowns(frame, metric_cols, dimension_cols),
        "correlations": _correlations(frame, metric_cols),
        "weekday_weekend": _weekday_weekend(daily, metric_cols),
        "sample_rows": _sample_rows(frame, date_col, metric_cols, anomalies),
        "sample_note": _sample_note(len(frame)),
        "series": _primary_series(daily, metric_cols),
    }
    return facts


def rows_to_records(cols: list, rows: list) -> list:
    records = []
    for row in rows or []:
        if isinstance(row, dict):
            records.append(dict(row))
            continue
        if isinstance(row, (list, tuple)):
            item = {}
            for index, value in enumerate(row):
                key = cols[index] if index < len(cols) else f"col_{index}"
                item[str(key)] = value
            records.append(item)
    return records


def trend_facts(data: list, cols: list) -> dict:
    facts = compute_all(cols, data if data and isinstance(data[0], dict) else _records_or_rows(data, cols))
    metrics = {}
    for name, block in (facts.get("metrics") or {}).items():
        metrics[name] = {
            "summary": block.get("summary"),
            "trend": block.get("trend"),
            "moving_averages": block.get("moving_averages"),
            "mom": block.get("mom"),
        }
    return {"row_count": facts.get("row_count", 0), "metrics": metrics}


def time_series_facts(data: list, cols: list) -> dict:
    facts = _facts_from_skill_input(data, cols)
    metrics = {}
    for name, block in (facts.get("metrics") or {}).items():
        metrics[name] = {
            "trend": block.get("trend"),
            "moving_averages": block.get("moving_averages"),
            "by_weekday": block.get("by_weekday"),
            "regression": block.get("regression"),
        }
    return {
        "row_count": facts.get("row_count", 0),
        "weekday_weekend": facts.get("weekday_weekend"),
        "metrics": metrics,
    }


def anomaly_facts(data: list, cols: list) -> dict:
    facts = _facts_from_skill_input(data, cols)
    return {
        "row_count": facts.get("row_count", 0),
        "missing_days": facts.get("missing_days") or [],
        "anomalies": facts.get("anomalies") or [],
        "weekday_weekend": facts.get("weekday_weekend"),
    }


def comparison_facts(data: list, cols: list) -> dict:
    facts = _facts_from_skill_input(data, cols)
    metrics = {}
    for name, block in (facts.get("metrics") or {}).items():
        metrics[name] = {"mom": block.get("mom"), "yoy": block.get("yoy")}
    return {
        "row_count": facts.get("row_count", 0),
        "metrics": metrics,
        "breakdowns": facts.get("breakdowns") or [],
    }


def distribution_facts(data: list, cols: list) -> dict:
    facts = _facts_from_skill_input(data, cols)
    metrics = {}
    for name, block in (facts.get("metrics") or {}).items():
        metrics[name] = {"summary": block.get("summary"), "distribution": block.get("distribution")}
    return {"row_count": facts.get("row_count", 0), "metrics": metrics}


def correlation_facts(data: list, cols: list) -> dict:
    facts = _facts_from_skill_input(data, cols)
    return {
        "row_count": facts.get("row_count", 0),
        "correlations": facts.get("correlations") or [],
    }


def regression_facts(data: list, cols: list) -> dict:
    facts = _facts_from_skill_input(data, cols)
    metrics = {}
    for name, block in (facts.get("metrics") or {}).items():
        metrics[name] = {"regression": block.get("regression")}
    return {"row_count": facts.get("row_count", 0), "metrics": metrics}


def _facts_from_skill_input(data: list, cols: list) -> dict:
    if data and isinstance(data[0], dict):
        columns = list(cols or data[0].keys())
        rows = [[item.get(column) for column in columns] for item in data]
        return compute_all(columns, rows)
    return compute_all(cols, data)


def _records_or_rows(data: list, cols: list) -> list:
    return data


def _prepare(records: list) -> pd.DataFrame:
    frame = pd.DataFrame(records)
    for column in frame.columns:
        frame[column] = frame[column].map(_unwrap)
    return frame


def _unwrap(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text == "" or text.lower() in {"null", "none", "nan"}:
            return None
        return text
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value


def _detect_date_column(frame: pd.DataFrame) -> Optional[str]:
    best = None
    best_score = 0.0
    for column in frame.columns:
        parsed = _parse_dates(frame[column])
        ratio = float(parsed.notna().mean()) if len(parsed) else 0.0
        name = str(column).lower()
        hinted = any(hint in name for hint in DATE_NAME_HINTS)
        score = ratio + (0.2 if hinted else 0.0)
        if hinted and ratio >= 0.5 and score > best_score:
            best, best_score = column, score
        elif ratio >= 0.8 and score > best_score:
            best, best_score = column, score
    return str(best) if best is not None else None


def _detect_metric_columns(frame: pd.DataFrame, date_col: Optional[str]) -> list:
    found = []
    for column in frame.columns:
        if str(column) == date_col:
            continue
        name = str(column).lower()
        if any(hint in name for hint in ID_NAME_HINTS):
            continue
        numeric = pd.to_numeric(frame[column], errors="coerce")
        ratio = float(numeric.notna().mean()) if len(numeric) else 0.0
        if ratio >= 0.7 and int(numeric.notna().sum()) >= 1:
            found.append(str(column))
    preferred = [name for name in found if any(hint in name.lower() for hint in METRIC_PREFERENCE)]
    ordered = preferred + [name for name in found if name not in preferred]
    return ordered[:6]


def _detect_dimensions(frame: pd.DataFrame, date_col: Optional[str], metrics: list) -> list:
    dims = []
    for column in frame.columns:
        name = str(column)
        if name == date_col or name in metrics:
            continue
        series = frame[column].dropna().astype(str)
        if series.empty:
            continue
        unique = int(series.nunique())
        if 1 < unique <= 30:
            dims.append(name)
    return dims[:4]


def _parse_dates(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")
    text = series.map(lambda value: None if value is None else str(value).strip()[:10].replace("/", "-"))
    return pd.to_datetime(text, errors="coerce", format="%Y-%m-%d")


def _daily_frame(frame: pd.DataFrame, date_col: str, metrics: list) -> pd.DataFrame:
    dates = _parse_dates(frame[date_col])
    working = frame.copy()
    working["_day"] = dates.dt.normalize()
    working = working.dropna(subset=["_day"])
    if working.empty or not metrics:
        return pd.DataFrame(columns=metrics)
    for metric in metrics:
        working[metric] = pd.to_numeric(working[metric], errors="coerce")
    grouped = working.groupby("_day", sort=True)[metrics].sum(min_count=1)
    return grouped


def _metric_block(frame, daily, date_col, metric: str, dimensions: list) -> dict:
    series = pd.to_numeric(frame[metric], errors="coerce").dropna()
    summary = _summary(series)
    distribution = _distribution(series)
    trend = None
    moving = None
    mom = {"available": False, "reason": "没有日期列，无法计算环比"}
    yoy = {"available": False, "reason": "没有日期列，无法计算同比"}
    by_weekday = {}
    regression = {"available": False, "reason": "没有日期列，无法做时间回归"}
    anomalies = []
    iqr_outliers = []
    if daily is not None and metric in daily.columns:
        values = daily[metric].dropna()
        trend = _trend(values)
        moving = _moving_averages(values)
        mom = _window_change(values, 30, "环比")
        yoy = _yoy(values)
        by_weekday = _by_weekday(values)
        regression = _regression(values)
        anomalies = _weekday_anomalies(values)
        iqr_outliers = _iqr_outliers(values)
    return {
        "summary": summary,
        "distribution": distribution,
        "trend": trend,
        "moving_averages": moving,
        "mom": mom,
        "yoy": yoy,
        "by_weekday": by_weekday,
        "regression": regression,
        "anomalies": anomalies,
        "iqr_outliers": iqr_outliers,
    }


def _summary(series: pd.Series) -> dict:
    if series.empty:
        return {"count": 0}
    values = series.astype(float)
    return {
        "count": int(values.count()),
        "sum": _num(values.sum()),
        "mean": _num(values.mean()),
        "std": _num(values.std(ddof=1)) if len(values) > 1 else 0.0,
        "min": _num(values.min()),
        "max": _num(values.max()),
        "median": _num(values.median()),
        "q1": _num(values.quantile(0.25)),
        "q3": _num(values.quantile(0.75)),
    }


def _distribution(series: pd.Series) -> dict:
    if series.empty:
        return {"count": 0}
    values = series.astype(float)
    mean = float(values.mean())
    median = float(values.median())
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    cv = abs(std / mean) if mean else None
    if mean > median * 1.05:
        shape = "右偏"
    elif mean < median * 0.95:
        shape = "左偏"
    else:
        shape = "大致对称"
    return {
        "mean": _num(mean),
        "median": _num(median),
        "std": _num(std),
        "cv": _num(cv) if cv is not None else None,
        "p90": _num(values.quantile(0.90)),
        "p95": _num(values.quantile(0.95)),
        "shape": shape,
    }


def _trend(values: pd.Series) -> dict:
    ordered = values.sort_index()
    first = float(ordered.iloc[0])
    last = float(ordered.iloc[-1])
    change = last - first
    change_pct = (change / first * 100.0) if first else None
    if change_pct is None:
        direction = "无法判断"
    elif change_pct > 3:
        direction = "上升"
    elif change_pct < -3:
        direction = "下降"
    else:
        direction = "平稳"
    return {
        "points": int(len(ordered)),
        "first_date": _date_text(ordered.index[0]),
        "last_date": _date_text(ordered.index[-1]),
        "first": _num(first),
        "last": _num(last),
        "change": _num(change),
        "change_pct": _num(change_pct) if change_pct is not None else None,
        "direction": direction,
    }


def _moving_averages(values: pd.Series) -> dict:
    ordered = values.sort_index().astype(float)
    result = {}
    for window in (3, 7):
        if len(ordered) < window:
            result[f"ma{window}"] = None
            continue
        rolling = ordered.rolling(window).mean()
        result[f"ma{window}"] = _num(rolling.iloc[-1])
        tail = []
        for day, value in list(rolling.dropna().items())[-14:]:
            tail.append({"date": _date_text(day), "value": _num(value)})
        result[f"ma{window}_tail"] = tail
    return result


def _window_change(values: pd.Series, days: int, label: str) -> dict:
    ordered = values.sort_index()
    end = ordered.index.max()
    current_start = end - pd.Timedelta(days=days - 1)
    previous_end = current_start - pd.Timedelta(days=1)
    previous_start = previous_end - pd.Timedelta(days=days - 1)
    current = ordered[(ordered.index >= current_start) & (ordered.index <= end)]
    previous = ordered[(ordered.index >= previous_start) & (ordered.index <= previous_end)]
    if len(current) < max(7, days // 4) or len(previous) < max(7, days // 4):
        return {
            "available": False,
            "reason": f"历史不足，无法计算{days}天{label}",
            "current_points": int(len(current)),
            "previous_points": int(len(previous)),
        }
    current_sum = float(current.sum())
    previous_sum = float(previous.sum())
    change_pct = ((current_sum - previous_sum) / previous_sum * 100.0) if previous_sum else None
    return {
        "available": True,
        "window_days": days,
        "current_start": _date_text(current_start),
        "current_end": _date_text(end),
        "previous_start": _date_text(previous_start),
        "previous_end": _date_text(previous_end),
        "current_sum": _num(current_sum),
        "previous_sum": _num(previous_sum),
        "current_mean": _num(current.mean()),
        "previous_mean": _num(previous.mean()),
        "change_pct": _num(change_pct) if change_pct is not None else None,
    }


def _yoy(values: pd.Series) -> dict:
    ordered = values.sort_index()
    end = ordered.index.max()
    current_start = end - pd.Timedelta(days=29)
    previous_end = end - pd.Timedelta(days=365)
    previous_start = current_start - pd.Timedelta(days=365)
    current = ordered[(ordered.index >= current_start) & (ordered.index <= end)]
    previous = ordered[(ordered.index >= previous_start) & (ordered.index <= previous_end)]
    if len(previous) < 14:
        return {"available": False, "reason": "数据不足，无法计算同比（需要约一年前的同期数据）"}
    current_sum = float(current.sum())
    previous_sum = float(previous.sum())
    change_pct = ((current_sum - previous_sum) / previous_sum * 100.0) if previous_sum else None
    return {
        "available": True,
        "current_sum": _num(current_sum),
        "previous_sum": _num(previous_sum),
        "change_pct": _num(change_pct) if change_pct is not None else None,
    }


def _by_weekday(values: pd.Series) -> dict:
    result = {}
    for weekday, name in enumerate(WEEKDAY_NAMES):
        subset = values[values.index.weekday == weekday].astype(float)
        if subset.empty:
            continue
        result[name] = {
            "mean": _num(subset.mean()),
            "std": _num(subset.std(ddof=1)) if len(subset) > 1 else 0.0,
            "n": int(len(subset)),
        }
    return result


def _weekday_weekend(daily: Optional[pd.DataFrame], metrics: list) -> dict:
    if daily is None or daily.empty or not metrics:
        return {}
    metric = metrics[0]
    values = daily[metric].dropna()
    weekend = values[values.index.weekday >= 5]
    weekday = values[values.index.weekday < 5]
    return {
        "metric": metric,
        "weekday": _level(weekday),
        "weekend": _level(weekend),
    }


def _level(series: pd.Series) -> dict:
    if series.empty:
        return {"n": 0, "mean": None, "std": None}
    return {
        "n": int(len(series)),
        "mean": _num(series.mean()),
        "std": _num(series.std(ddof=1)) if len(series) > 1 else 0.0,
    }


def _regression(values: pd.Series) -> dict:
    ordered = values.dropna().sort_index().astype(float)
    if len(ordered) < 8:
        return {"available": False, "reason": "点数不足，无法做线性回归"}
    origin = ordered.index.min()
    x = np.array([(day - origin).days for day in ordered.index], dtype=float)
    y = ordered.to_numpy(dtype=float)
    if np.allclose(y, y[0]):
        return {"available": True, "slope_per_day": 0.0, "intercept": _num(y[0]), "r2": 1.0, "n": int(len(y))}
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 1.0
    return {
        "available": True,
        "slope_per_day": _num(slope),
        "intercept": _num(intercept),
        "r2": _num(r2),
        "n": int(len(y)),
    }


def _weekday_anomalies(values: pd.Series) -> list:
    ordered = values.dropna().sort_index().astype(float)
    anomalies = []
    dates = list(ordered.index)
    numbers = list(ordered.to_numpy(dtype=float))
    for index, (day, value) in enumerate(zip(dates, numbers)):
        peers = [
            numbers[other]
            for other, other_day in enumerate(dates)
            if other != index and other_day.weekday() == day.weekday()
        ]
        if len(peers) < MIN_WEEKDAY_PEERS:
            continue
        mean = float(np.mean(peers))
        std = float(np.std(peers, ddof=1))
        if std <= 0:
            continue
        z_score = (value - mean) / std
        if abs(z_score) < Z_THRESHOLD:
            continue
        anomalies.append({
            "date": _date_text(day),
            "value": _num(value),
            "baseline_mean": _num(mean),
            "baseline_std": _num(std),
            "z": _num(z_score),
            "method": "weekday_zscore",
            "direction": "high" if z_score > 0 else "low",
            "severity": "显著异常" if abs(z_score) >= 3 else "轻度异常",
            "weekday": WEEKDAY_NAMES[day.weekday()],
        })
    anomalies.sort(key=lambda item: abs(item["z"]), reverse=True)
    return anomalies[:30]


def _iqr_outliers(values: pd.Series) -> list:
    ordered = values.dropna().sort_index().astype(float)
    if len(ordered) < 8:
        return []
    q1 = float(ordered.quantile(0.25))
    q3 = float(ordered.quantile(0.75))
    iqr = q3 - q1
    if iqr <= 0:
        return []
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    outliers = []
    for day, value in ordered.items():
        number = float(value)
        if number < low or number > high:
            outliers.append({
                "date": _date_text(day),
                "value": _num(number),
                "method": "iqr",
                "direction": "high" if number > high else "low",
                "fence_low": _num(low),
                "fence_high": _num(high),
            })
    return outliers[:30]


def _missing_days(index: pd.DatetimeIndex) -> list:
    if len(index) < 2:
        return []
    start = index.min()
    end = index.max()
    expected = pd.date_range(start, end, freq="D")
    present = set(pd.DatetimeIndex(index).normalize())
    missing = [day for day in expected if day not in present]
    return [_date_text(day) for day in missing[:60]]


def _breakdowns(frame: pd.DataFrame, metrics: list, dimensions: list) -> list:
    if not metrics or not dimensions:
        return []
    metric = metrics[0]
    numeric = pd.to_numeric(frame[metric], errors="coerce")
    working = frame.copy()
    working[metric] = numeric
    blocks = []
    for dimension in dimensions:
        grouped = (
            working.dropna(subset=[metric])
            .groupby(working[dimension].astype(str))[metric]
            .sum()
            .sort_values(ascending=False)
        )
        total = float(grouped.sum())
        top = []
        for key, value in grouped.head(TOP_N).items():
            share = (float(value) / total * 100.0) if total else None
            top.append({"key": str(key), "value": _num(value), "share_pct": _num(share) if share is not None else None})
        blocks.append({"dimension": dimension, "metric": metric, "total": _num(total), "top": top})
    return blocks


def _correlations(frame: pd.DataFrame, metrics: list) -> list:
    if len(metrics) < 2:
        return []
    numeric = frame[metrics].apply(pd.to_numeric, errors="coerce").dropna()
    if len(numeric) < 8:
        return []
    matrix = numeric.corr(method="pearson")
    pairs = []
    for left_index, left in enumerate(metrics):
        for right in metrics[left_index + 1:]:
            value = matrix.loc[left, right]
            if value is None or (isinstance(value, float) and math.isnan(value)):
                continue
            pairs.append({"left": left, "right": right, "r": _num(float(value)), "n": int(len(numeric))})
    pairs.sort(key=lambda item: abs(item["r"]), reverse=True)
    return pairs[:12]


def _primary_series(daily: Optional[pd.DataFrame], metrics: list) -> dict:
    """主指标的按日序列，最多 200 点，给报告作图用。"""
    if daily is None or daily.empty or not metrics:
        return {"metric": None, "points": []}
    metric = metrics[0]
    ordered = daily[metric].dropna().sort_index()
    if len(ordered) > 200:
        ordered = ordered.iloc[-200:]
    points = [{"date": _date_text(day), "value": _num(value)} for day, value in ordered.items()]
    return {"metric": metric, "points": points}


def _sample_rows(frame: pd.DataFrame, date_col: Optional[str], metrics: list, anomalies: list) -> list:
    view = frame.copy()
    if date_col and date_col in view.columns:
        view["_sort"] = _parse_dates(view[date_col])
        view = view.sort_values("_sort")
    head = view.head(8)
    tail = view.tail(5)
    chosen = pd.concat([head, tail]).drop_duplicates()
    rows = []
    for _, record in chosen.iterrows():
        item = {}
        for column, value in record.items():
            if column == "_sort":
                continue
            item[str(column)] = _jsonable(value)
        rows.append(item)
    return rows[:20]


def _sample_note(row_count: int) -> str:
    if row_count <= 20:
        return f"计算基于全部 {row_count} 行，下列样本覆盖全部行。"
    return (
        f"计算基于全部 {row_count} 行。"
        "交给模型的表格只是代表性样本（开头和结尾），未展示的行已经计入均值、基线和异常点。"
    )


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, 4)


def _date_text(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        return value[:10]
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(value)[:10]


def _min_date(frame, date_col):
    if date_col is None:
        return None
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame.index.min()
    if date_col in getattr(frame, "columns", []):
        parsed = _parse_dates(frame[date_col])
        return parsed.min()
    return None


def _max_date(frame, date_col):
    if date_col is None:
        return None
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame.index.max()
    if date_col in getattr(frame, "columns", []):
        parsed = _parse_dates(frame[date_col])
        return parsed.max()
    return None


def _jsonable(value: Any):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, (datetime, pd.Timestamp)):
        return _date_text(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _num(value)
    if isinstance(value, (int, str)):
        return value
    return str(value)


def iter_numbers(value: Any) -> Iterable[float]:
    """遍历事实里出现过的数字，供引用核对使用。"""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float, np.integer, np.floating)):
        number = _num(value)
        if number is not None:
            yield number
        return
    if isinstance(value, str):
        for match in re.finditer(r"\d+(?:\.\d+)?", value):
            yield float(match.group(0))
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_numbers(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_numbers(item)
