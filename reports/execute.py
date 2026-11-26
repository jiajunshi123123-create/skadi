"""按顺序执行报告子查询。每一条都重新做权限检查，并受条数、行数和时间上限约束。"""

from __future__ import annotations

import time
from typing import Callable, Optional

from analysis.compute import attach_computed_facts
from reports.planner import MAX_SUBQUERIES, plan_subqueries

MAX_TOTAL_ROWS = 20000
MAX_SECONDS = 90


async def execute_report(
    user_query: str,
    user_role: Optional[dict],
    plan,
    query,
    lookup: Optional[Callable] = None,
    remember: Optional[Callable] = None,
    now: Callable[[], float] = time.time,
    max_subqueries: int = MAX_SUBQUERIES,
    max_rows: int = MAX_TOTAL_ROWS,
    max_seconds: float = MAX_SECONDS,
) -> dict:
    """plan(question, user_role) 和 query(plan_task, user_role) 都是异步函数。"""
    specs = plan_subqueries(user_query)[:max_subqueries]
    started = now()
    parts = []
    notes = []
    total_rows = 0
    for spec in specs:
        if now() - started > max_seconds:
            notes.append("已达到时间上限，后面的子查询没有执行。")
            break
        state = {
            "user_query": spec.question,
            "user_role": user_role,
            "last_context": None,
        }
        cached = lookup(state) if lookup else None
        if cached and isinstance(cached.get("plan_task"), dict):
            plan_task = cached["plan_task"]
        else:
            plan_task = await plan(spec.question, user_role=user_role)
        if not isinstance(plan_task, dict):
            parts.append(_failed(spec, "规划没有返回结果"))
            continue
        if plan_task.get("error") and not plan_task.get("sql"):
            parts.append(_failed(spec, plan_task.get("error") or "规划失败"))
            continue
        if plan_task.get("direct_answer") and not plan_task.get("needs_sql", True):
            parts.append({
                "name": spec.name,
                "purpose": spec.purpose,
                "question": spec.question,
                "direct_answer": plan_task.get("direct_answer"),
                "success": True,
            })
            continue
        result = await query({**plan_task, "user_query": spec.question}, user_role=user_role)
        if not isinstance(result, dict):
            parts.append(_failed(spec, "查询没有返回结果"))
            continue
        if result.get("permission_denied"):
            parts.append(_failed(spec, result.get("error") or "权限不足", permission_denied=True))
            notes.append("权限不足，已停止后续子查询。")
            break
        if not result.get("success"):
            parts.append(_failed(spec, result.get("error") or "查询失败"))
            continue
        row_count = int(result.get("row_count") or len(result.get("rows") or []))
        if total_rows + row_count > max_rows:
            notes.append(
                f"行数上限是 {max_rows}，子查询「{spec.purpose}」会超出上限，已停止。"
            )
            break
        total_rows += row_count
        try:
            attach_computed_facts(result, spec.question)
        except Exception as exc:
            result["computed_facts"] = {"row_count": row_count, "note": f"计算失败: {exc}"}
        state["plan_task"] = plan_task
        if remember:
            remember(state, result)
        parts.append({
            "name": spec.name,
            "purpose": spec.purpose,
            "question": spec.question,
            "plan": plan_task,
            "result": result,
            "success": True,
        })
    return {"parts": parts, "notes": notes, "total_rows": total_rows}


def _failed(spec, error: str, permission_denied: bool = False) -> dict:
    return {
        "name": spec.name,
        "purpose": spec.purpose,
        "question": spec.question,
        "success": False,
        "error": error,
        "permission_denied": permission_denied,
    }


async def run_report_mode(user_query: str, user_role: Optional[dict]) -> tuple:
    """生产入口：复用现有的计划、查询、缓存。"""
    from agent_orchestrator import (
        _lookup_cached_plan,
        _remember_query_cache,
        plan_agent,
        query_agent,
    )
    from reports.synthesize import render_report

    async def plan(question, user_role=None):
        return await plan_agent.plan(question, user_role=user_role, last_context=None)

    async def query(plan_task, user_role=None):
        return await query_agent.execute(plan_task, user_role=user_role)

    def remember(state, result):
        _remember_query_cache(state, result)
        _record_case(state, result)

    payload = await execute_report(
        user_query,
        user_role,
        plan,
        query,
        lookup=_lookup_cached_plan,
        remember=remember,
    )
    text = render_report(user_query, payload["parts"], payload["notes"])
    primary = _primary_result(payload["parts"])
    metadata = {
        "report": True,
        "analysis": text,
        "plan_task": (primary.get("plan") if primary else {}) or {},
        "query_result": _public_result(primary.get("result") if primary else {}),
        "report_parts": [_public_part(part) for part in payload["parts"]],
        "user_query": user_query,
        "activated_skills": [],
    }
    sql = (metadata["query_result"] or {}).get("sql_executed") or ""
    if sql:
        text = text + f"\n<!--M3_SQL:{sql}-->"
    return text, metadata


def _record_case(state: dict, result: dict) -> None:
    """向量库只收问题和 SQL，不写查询结果或报告正文。"""
    try:
        from learning.feedback_loop import feedback_loop

        role = state.get("user_role") or {}
        feedback_loop.on_query_success(
            user_query=state.get("user_query") or "",
            sql=result.get("sql_before_enforce") or result.get("sql_executed") or "",
            result=result,
            analysis="",
            role_id=role.get("role_id") or "",
        )
    except Exception:
        return


def _primary_result(parts: list) -> dict:
    for part in parts:
        if part.get("success") and part.get("result"):
            return part
    return {}


def _public_result(result: dict) -> dict:
    if not result:
        return {}
    return {
        "success": result.get("success"),
        "sql_executed": result.get("sql_executed"),
        "row_count": result.get("row_count"),
        "cols": result.get("cols"),
        "computed_facts": result.get("computed_facts"),
        "permission_denied": result.get("permission_denied"),
        "error": result.get("error"),
    }


def _public_part(part: dict) -> dict:
    return {
        "name": part.get("name"),
        "purpose": part.get("purpose"),
        "question": part.get("question"),
        "success": part.get("success"),
        "error": part.get("error"),
        "permission_denied": part.get("permission_denied"),
        "direct_answer": part.get("direct_answer"),
        "result": _public_result(part.get("result") or {}),
    }
