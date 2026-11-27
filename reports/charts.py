"""用捆绑的思源黑体画时间序列和拆分图。"""

from __future__ import annotations

import os
from typing import Optional

_FONT_CONFIGURED = False


def bundled_font_path() -> Optional[str]:
    override = os.environ.get("SKADI_CJK_FONT", "").strip()
    if override and os.path.isfile(override):
        return override
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "NotoSansCJKsc-Regular.otf"))
    if os.path.isfile(here):
        return here
    return None


def _configure():
    global _FONT_CONFIGURED
    import matplotlib

    matplotlib.use("Agg", force=False)
    from matplotlib import font_manager, pyplot as plt

    if _FONT_CONFIGURED:
        return plt
    path = bundled_font_path()
    if path:
        font_manager.fontManager.addfont(path)
        family = font_manager.FontProperties(fname=path).get_name()
        plt.rcParams["font.family"] = family
        plt.rcParams["axes.unicode_minus"] = False
    _FONT_CONFIGURED = True
    return plt


def write_charts(facts: dict, directory: str) -> list:
    """按事实里的序列和拆分写 PNG。没有可画的数据时返回空列表。"""
    os.makedirs(directory, exist_ok=True)
    paths = []
    series_path = _write_series(facts, os.path.join(directory, "series.png"))
    if series_path:
        paths.append(series_path)
    breakdown_path = _write_breakdown(facts, os.path.join(directory, "breakdown.png"))
    if breakdown_path:
        paths.append(breakdown_path)
    return paths


def _write_series(facts: dict, path: str) -> Optional[str]:
    series = facts.get("series") or {}
    points = series.get("points") or []
    if len(points) < 2:
        return None
    plt = _configure()
    dates = [point["date"] for point in points]
    values = [point["value"] for point in points]
    anomaly_dates = {item.get("date") for item in facts.get("anomalies") or []}
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(range(len(values)), values, color="#1f4e79", linewidth=1.4, label=series.get("metric") or "指标")
    xs = [index for index, day in enumerate(dates) if day in anomaly_dates]
    ys = [values[index] for index in xs]
    if xs:
        ax.scatter(xs, ys, color="#c0392b", zorder=3, label="同星期几异常")
    step = max(1, len(dates) // 8)
    ax.set_xticks(list(range(0, len(dates), step)))
    ax.set_xticklabels([dates[index] for index in range(0, len(dates), step)], rotation=30, ha="right")
    ax.set_title(f"{series.get('metric') or '指标'} 按日")
    ax.legend(loc="best")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def _write_breakdown(facts: dict, path: str) -> Optional[str]:
    blocks = facts.get("breakdowns") or []
    if not blocks or not blocks[0].get("top"):
        return None
    block = blocks[0]
    plt = _configure()
    labels = [str(item.get("key")) for item in block["top"]]
    values = [item.get("value") or 0 for item in block["top"]]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.bar(range(len(labels)), values, color="#2e86ab")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_title(f"按{block.get('dimension')}拆分 {block.get('metric')}")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
