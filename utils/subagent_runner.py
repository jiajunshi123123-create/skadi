"""子代理并行分析 (Sub-Agent Parallel Analysis)

借鉴 Claude Code 的 multi_agent/subagent.py:
- ThreadPoolExecutor 并行执行多个分析任务
- 每个子代理独立运行，有自己的上下文
- 深度限制防止递归爆炸
- 协作式取消机制
- 结果合并与去重

使用场景:
1. 多技能并行: 趋势分析 + 异常检测 + 对比分析 同时运行
2. 分维度分析: 按渠道/产品/地区分维度并行分析
3. 竞品对比: 同时查询多个产品线

架构:
    Orchestrator
    ├── SubAgent 1: 趋势分析
    ├── SubAgent 2: 异常检测  
    └── SubAgent 3: 对比分析
         ↓ (parallel execution)
    ResultMerger: 合并去重排序
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError as FutureTimeoutError
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================
# 配置
# ============================================================
DEFAULT_MAX_WORKERS = 4       # 最大并发子代理数
DEFAULT_MAX_DEPTH = 2         # 最大递归深度（子代理不再嵌套子代理）
DEFAULT_SUBAGENT_TIMEOUT = 60 # 单个子代理超时（秒）


class SubAgentStatus(Enum):
    """子代理执行状态"""
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    TIMEOUT = 'timeout'
    CANCELLED = 'cancelled'


@dataclass
class SubAgentTask:
    """子代理任务定义"""
    id: str
    name: str
    func: Callable
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    timeout: int = DEFAULT_SUBAGENT_TIMEOUT
    priority: int = 0          # 优先级（越高越先执行）
    depth: int = 0             # 递归深度

    # 运行时状态
    status: SubAgentStatus = SubAgentStatus.PENDING
    result: Any = None
    error: str = ''
    duration_ms: int = 0
    start_time: float = 0.0


@dataclass
class ParallelResult:
    """并行分析结果汇总"""
    results: list               # 各子代理结果列表
    success_count: int
    failure_count: int
    timeout_count: int
    total_duration_ms: int
    merged_summary: str = ''    # 合并后的分析摘要


class SubAgentRunner:
    """子代理并行执行器

    特性:
    - ThreadPoolExecutor 并行调度
    - 超时保护 + 协作取消
    - 深度限制（子代理不递归嵌套）
    - 优先级排序
    - 异常隔离（单代理失败不影响其他）

    使用方式:
        runner = SubAgentRunner(max_workers=3)

        tasks = [
            SubAgentTask(id='t1', name='趋势分析', func=trend_func, args=(data,)),
            SubAgentTask(id='t2', name='异常检测', func=anomaly_func, args=(data,)),
        ]

        result = runner.run_parallel(tasks)
        print(f'成功: {result.success_count}, 耗时: {result.total_duration_ms}ms')
    """

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS, max_depth: int = DEFAULT_MAX_DEPTH):
        self.max_workers = max_workers
        self.max_depth = max_depth
        self._cancelled = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._stats = {
            'total_runs': 0,
            'total_tasks': 0,
            'total_duration_ms': 0,
        }

    def cancel_all(self):
        """取消所有正在运行的子代理。"""
        self._cancelled = True
        logger.info("[SubAgentRunner] 取消所有子代理")

    def reset_cancel(self):
        """重置取消标记。"""
        self._cancelled = False

    def run_parallel(self, tasks: list[SubAgentTask]) -> ParallelResult:
        """并行执行多个子代理任务。

        Args:
            tasks: 子代理任务列表

        Returns:
            ParallelResult 汇总结果
        """
        if not tasks:
            return ParallelResult(
                results=[], success_count=0, failure_count=0,
                timeout_count=0, total_duration_ms=0, merged_summary='(无任务)'
            )

        # 深度检查
        valid_tasks = []
        for task in tasks:
            if task.depth > self.max_depth:
                logger.warning(
                    f"[SubAgentRunner] 跳过任务 '{task.name}': "
                    f"深度{task.depth}超过限制{self.max_depth}"
                )
                continue
            valid_tasks.append(task)

        if not valid_tasks:
            return ParallelResult(
                results=[], success_count=0, failure_count=0,
                timeout_count=0, total_duration_ms=0,
                merged_summary='(所有任务被深度限制跳过)'
            )

        # 优先级排序
        valid_tasks.sort(key=lambda t: t.priority, reverse=True)

        # 限制并发数
        actual_workers = min(self.max_workers, len(valid_tasks))

        logger.info(
            f"[SubAgentRunner] 启动 {len(valid_tasks)} 个子代理 "
            f"(workers={actual_workers}, max_depth={self.max_depth})"
        )

        t_start = time.time()

        # 并行执行
        results = []
        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            self._executor = executor
            futures: dict[Future, SubAgentTask] = {}

            for task in valid_tasks:
                if self._cancelled:
                    task.status = SubAgentStatus.CANCELLED
                    results.append(task)
                    continue

                future = executor.submit(self._run_single, task)
                futures[future] = task

            # 收集结果
            for future, task in futures.items():
                try:
                    task.result = future.result(timeout=task.timeout)
                    task.status = SubAgentStatus.COMPLETED
                except FutureTimeoutError:
                    task.status = SubAgentStatus.TIMEOUT
                    task.error = f'超时({task.timeout}s)'
                    logger.warning(f"[SubAgentRunner] 子代理 '{task.name}' 超时")
                except Exception as e:
                    task.status = SubAgentStatus.FAILED
                    task.error = str(e)
                    logger.error(f"[SubAgentRunner] 子代理 '{task.name}' 失败: {e}")

                task.duration_ms = int((time.time() - task.start_time) * 1000)
                results.append(task)

        total_duration = int((time.time() - t_start) * 1000)

        # 统计
        success = sum(1 for t in results if t.status == SubAgentStatus.COMPLETED)
        failed = sum(1 for t in results if t.status == SubAgentStatus.FAILED)
        timed_out = sum(1 for t in results if t.status == SubAgentStatus.TIMEOUT)

        # 更新统计
        self._stats['total_runs'] += 1
        self._stats['total_tasks'] += len(valid_tasks)
        self._stats['total_duration_ms'] += total_duration

        logger.info(
            f"[SubAgentRunner] 并行执行完成: "
            f"{success}成功/{failed}失败/{timed_out}超时, "
            f"耗时{total_duration}ms"
        )

        # 合并结果
        merged = self._merge_results(results)

        return ParallelResult(
            results=results,
            success_count=success,
            failure_count=failed,
            timeout_count=timed_out,
            total_duration_ms=total_duration,
            merged_summary=merged,
        )

    def run_single(self, task: SubAgentTask) -> SubAgentTask:
        """同步执行单个子代理（兼容现有代码）。

        Args:
            task: 子代理任务

        Returns:
            更新后的任务（含结果）
        """
        results = self.run_parallel([task])
        if results.results:
            return results.results[0]
        return task

    def _run_single(self, task: SubAgentTask) -> Any:
        """内部单任务执行（由线程池调用）。"""
        task.start_time = time.time()
        task.status = SubAgentStatus.RUNNING

        if self._cancelled:
            task.status = SubAgentStatus.CANCELLED
            return None

        logger.info(f"[SubAgent] 开始执行: {task.name} (depth={task.depth})")

        try:
            result = task.func(*task.args, **task.kwargs)
            return result
        except Exception as e:
            logger.error(f"[SubAgent] '{task.name}' 执行异常: {e}")
            raise

    def _merge_results(self, tasks: list[SubAgentTask]) -> str:
        """合并多个子代理的分析结果。

        按优先级排序，去重关键发现，生成统一摘要。

        Args:
            tasks: 完成的子代理任务列表

        Returns:
            合并后的分析摘要
        """
        completed = [t for t in tasks if t.status == SubAgentStatus.COMPLETED]

        if not completed:
            failed_names = [t.name for t in tasks if t.status != SubAgentStatus.COMPLETED]
            return f'(分析失败: {", ".join(failed_names) if failed_names else "无"}，请重试)'

        parts = []
        for task in sorted(completed, key=lambda t: t.priority, reverse=True):
            result_str = str(task.result)[:500] if task.result else '(无结果)'
            parts.append(f"## {task.name}\n{result_str}\n")

        return '\n---\n'.join(parts)

    def get_stats(self) -> dict:
        """获取执行统计。"""
        return dict(self._stats)

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled


# 全局实例
subagent_runner = SubAgentRunner()
