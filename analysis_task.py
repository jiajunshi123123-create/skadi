# -*- coding: utf-8 -*-
"""分析任务追踪系统

借鉴 Claude Code 的 Task 系统:
- Task 数据模型 (PENDING → IN_PROGRESS → COMPLETED)
- 依赖关系 (blocks/blocked_by)
- JSON 文件持久化
- 与编排器集成，自动更新任务状态

用途:
- 将分析规划器的输出转化为可追踪的任务链
- 用户可以看到分析进度和阻塞原因
- 最终回复中展示任务完成状态
"""

from __future__ import annotations
import json
import threading
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ================================================================
# 数据模型
# ================================================================

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


STATUS_ICON = {
    TaskStatus.PENDING: "○",
    TaskStatus.IN_PROGRESS: "●",
    TaskStatus.COMPLETED: "✓",
    TaskStatus.BLOCKED: "⊘",
    TaskStatus.CANCELLED: "✗",
}

STATUS_LABEL = {
    TaskStatus.PENDING: "待执行",
    TaskStatus.IN_PROGRESS: "执行中",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.BLOCKED: "数据缺失",
    TaskStatus.CANCELLED: "已取消",
}


@dataclass
class AnalysisTask:
    """单个分析任务"""
    id: str
    subject: str                       # 任务名 (如 "趋势分析")
    description: str = ""              # 详细说明
    status: TaskStatus = TaskStatus.PENDING
    method_id: str = ""                # 对应的方法论ID
    blocks: list[str] = field(default_factory=list)     # 依赖此任务的其他任务ID
    blocked_by: list[str] = field(default_factory=list) # 此任务依赖的前置任务ID
    missing_data: list[str] = field(default_factory=list)  # BLOCKED时记录缺失数据
    result_summary: str = ""           # 完成后记录结果摘要
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def status_icon(self) -> str:
        return STATUS_ICON.get(self.status, "?")

    def status_label(self) -> str:
        return STATUS_LABEL.get(self.status, "未知")

    def one_line(self) -> str:
        icon = self.status_icon()
        label = self.status_label()
        blocked = f" [需: {', '.join(self.missing_data[:2])}]" if self.missing_data else ""
        return f"{icon} {self.subject} [{label}]{blocked}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status.value,
            "method_id": self.method_id,
            "blocks": self.blocks,
            "blocked_by": self.blocked_by,
            "missing_data": self.missing_data,
            "result_summary": self.result_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisTask":
        status = TaskStatus(data.get("status", "pending"))
        return cls(
            id=data["id"],
            subject=data.get("subject", ""),
            description=data.get("description", ""),
            status=status,
            method_id=data.get("method_id", ""),
            blocks=data.get("blocks", []),
            blocked_by=data.get("blocked_by", []),
            missing_data=data.get("missing_data", []),
            result_summary=data.get("result_summary", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


# ================================================================
# Task 存储 (JSON文件)
# ================================================================

_lock = threading.Lock()
_tasks: dict[str, AnalysisTask] = {}
_loaded = False

# 存储路径: 当前会话的任务文件
TASK_DIR = Path(".analysis_tasks")


def _task_file() -> Path:
    """任务JSON文件路径"""
    TASK_DIR.mkdir(parents=True, exist_ok=True)
    return TASK_DIR / "current_tasks.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    f = _task_file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for item in data.get("tasks", []):
                t = AnalysisTask.from_dict(item)
                _tasks[t.id] = t
        except Exception as e:
            logger.warning(f"[TaskStore] 加载任务文件失败: {e}")
    _loaded = True


def _save() -> None:
    f = _task_file()
    data = {"tasks": [t.to_dict() for t in _tasks.values()]}
    f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _next_id() -> str:
    if not _tasks:
        return "1"
    max_id = max((int(k) for k in _tasks if k.isdigit()), default=0)
    return str(max_id + 1)


# ── 公开 API ──

def create_task(
    subject: str,
    description: str = "",
    method_id: str = "",
    blocked_by: list[str] = None,
    missing_data: list[str] = None,
) -> AnalysisTask:
    """创建新任务"""
    with _lock:
        _load()
        task = AnalysisTask(
            id=_next_id(),
            subject=subject,
            description=description,
            method_id=method_id,
            blocked_by=blocked_by or [],
            missing_data=missing_data or [],
            status=TaskStatus.BLOCKED if missing_data else TaskStatus.PENDING,
        )
        _tasks[task.id] = task
        _save()
        return task


def update_task_status(task_id: str, status: TaskStatus, result_summary: str = "") -> Optional[AnalysisTask]:
    """更新任务状态"""
    with _lock:
        _load()
        task = _tasks.get(task_id)
        if not task:
            return None
        task.status = status
        task.updated_at = datetime.now().isoformat()
        if result_summary:
            task.result_summary = result_summary
        _save()
        return task


def get_task(task_id: str) -> Optional[AnalysisTask]:
    with _lock:
        _load()
        return _tasks.get(task_id)


def get_all_tasks() -> list[AnalysisTask]:
    with _lock:
        _load()
        return list(_tasks.values())


def get_tasks_by_status(status: TaskStatus) -> list[AnalysisTask]:
    return [t for t in get_all_tasks() if t.status == status]


def clear_tasks() -> None:
    """清空所有任务（新会话开始时调用）"""
    with _lock:
        _tasks.clear()
        _save()


# ── 从分析规划器创建任务 ──

def create_tasks_from_plan(analysis_plan: dict) -> list[AnalysisTask]:
    """从分析规划器的输出创建任务列表。

    Args:
        analysis_plan: analysis_planner 输出的 plan_dict

    Returns:
        创建的任务列表
    """
    clear_tasks()

    executable = analysis_plan.get("executable_methods", [])
    blocked = analysis_plan.get("blocked_methods", [])

    tasks = []

    # 可执行任务 — 创建为 PENDING
    for i, m in enumerate(executable):
        task = create_task(
            subject=m.get("name", f"步骤{i+1}"),
            description=m.get("reason", ""),
            method_id=m.get("id", ""),
        )
        tasks.append(task)

    # 阻塞任务 — 创建为 BLOCKED
    for i, m in enumerate(blocked):
        task = create_task(
            subject=m.get("name", f"步骤{len(executable)+i+1}"),
            description=m.get("reason", ""),
            method_id=m.get("id", ""),
            missing_data=m.get("missing_data", []),
        )
        tasks.append(task)

    logger.info(
        f"[TaskStore] 从分析规划创建 {len(tasks)} 个任务 "
        f"({len(executable)}可执行, {len(blocked)}阻塞)"
    )
    return tasks


# ── 格式化输出 ──

def format_task_progress() -> str:
    """生成任务进度文本（用于注入最终回复）"""
    tasks = get_all_tasks()
    if not tasks:
        return ""

    completed = get_tasks_by_status(TaskStatus.COMPLETED)
    in_progress = get_tasks_by_status(TaskStatus.IN_PROGRESS)
    blocked = get_tasks_by_status(TaskStatus.BLOCKED)

    lines = ["📋 分析任务进度"]

    for t in tasks:
        lines.append(f"  {t.one_line()}")

    # 统计
    total = len(tasks)
    done = len(completed)
    blocked_count = len(blocked)
    if done == total:
        lines.append(f"\n✅ 全部 {total} 项分析任务已完成")
    elif blocked_count > 0:
        lines.append(f"\n⚠️ {done}/{total} 完成, {blocked_count} 项因数据缺失阻塞")

    return "\n".join(lines)


def get_pending_count() -> int:
    return len(get_tasks_by_status(TaskStatus.PENDING))
