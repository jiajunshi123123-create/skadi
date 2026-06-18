"""LangGraph 三Agent编排器 - Enterprise数据AI Agent系统核心

编排流程:
    用户消息 → Plan Node (意图理解+SQL生成)
             → Query Node (EXPLAIN校验+执行+自愈)
             → Analysis Node (趋势分析+行动建议)
             → 三段式回复输出

每个节点都有错误兜底，异常时快速跳转到格式化输出。
"""
import asyncio
import time
import logging
from typing import TypedDict, Optional, Annotated

from langgraph.graph import StateGraph, END

from agents.plan_agent import PlanAgent
from agents.query_agent import QueryAgent
from agents.analysis_agent import AnalysisAgent
from agents.inspection_agent import inspection_agent
from skills import skill_registry
from utils.context_compactor import (
    compact_query_result,
    compact_analysis_context,
    truncate_for_dingtalk,
)
from utils.token_estimator import estimate_tokens_fast
from analysis_planner import analysis_planner, format_plan_for_dingtalk, format_plan_for_analysis
from analysis_task import (
    create_tasks_from_plan, update_task_status, format_task_progress,
    TaskStatus, get_all_tasks,
)
import learning.memory_tools  # ???????ToolRegistry

logger = logging.getLogger(__name__)

# ============================================================
# 启动时依赖验证
# ============================================================
try:
    from tools.database_adapter import DatabaseAdapter
    _db_adapter_available = True
except ImportError:
    _db_adapter_available = False
    logger.warning("[STARTUP WARNING] DatabaseAdapter 导入失败，所有SQL查询将不可用！"
                   "请检查 tools/database_adapter.py 和相关数据库驱动。")

# ============================================================
# State 定义
# ============================================================


class AgentState(TypedDict):
    """编排器全局状态"""
    user_query: str               # 用户原始问题
    user_role: Optional[dict]     # 用户角色信息（权限系统）
    plan_task: Optional[dict]     # Plan Agent 输出
    query_result: Optional[dict]  # Query Agent 输出
    inspection_result: Optional[dict]  # 数据核查探查结果
    activated_skills: Optional[list]   # 匹配到的分析技能名称列表
    analysis: Optional[str]       # Analysis Agent 输出
    final_reply: Optional[str]    # 最终回复
    error: Optional[str]          # 错误信息
    retry_count: int              # 重试计数
    start_time: float             # 开始时间
    last_context: Optional[dict]  # M3: 上一轮查询上下文
    suggested_query: Optional[str]  # M4: 从错误建议中提取的待确认查询


# ============================================================
# Agent 实例（全局单例）
# ============================================================

plan_agent = PlanAgent()
query_agent = QueryAgent()
analysis_agent = AnalysisAgent()


# ============================================================
# 节点函数
# ============================================================

async def plan_node(state: AgentState) -> dict:
    """
    Plan Node: 理解用户需求 → 生成SQL执行计划
    
    输入: state['user_query']
    输出: state['plan_task'] 或 state['error']
    """
    logger.info(f"[Orchestrator] Plan Node 开始处理")

    # --- 自学习缓存快速通道 ---
    try:
        from learning.feedback_loop import feedback_loop
        context = feedback_loop.get_context_for_query(state['user_query'])
        if context and context.get('similar_patterns'):
            pattern = context['similar_patterns']
            # 成熟度 >= 0.3 且出现次数 >= 3 时命中缓存
            if pattern.get('maturity', 0) >= 0.3 and pattern.get('occurrence_count', 0) >= 3:
                logger.info(f"[Orchestrator] 缓存命中 pattern={pattern.get('pattern_id','?')}, "
                           f"maturity={pattern.get('maturity')}, count={pattern.get('occurrence_count')}")
                cached_sql = pattern.get('common_sql', [''])[0]
                if cached_sql:
                    return {'plan_task': {
                        'intent': f"cached_{pattern.get('pattern_id', 'unknown')}",
                        'sql': cached_sql,
                        'table': '',  # Query Agent 会自动处理
                        'from_cache': True,
                        'cache_maturity': pattern.get('maturity', 0),
                    }}
    except Exception as e:
        logger.warning(f"[Orchestrator] 缓存检测失败，走正常流程: {e}")
    # --- 结束缓存快速通道 ---

    try:
        user_role = state.get('user_role')
        last_context = state.get('last_context')  # M3: 获取上下文
        plan_task = await plan_agent.plan(
            state['user_query'],
            user_role=user_role,
            last_context=last_context  # M3: 传递上下文
        )
        
        # 检查 Plan Agent 是否返回了错误（知识解答类 direct_answer 不算错误）
        if 'error' in plan_task and 'sql' not in plan_task and 'direct_answer' not in plan_task:
            error_msg = plan_task.get('error', '计划生成失败')
            suggestion = plan_task.get('suggestion', '')
            full_error = f"{error_msg}\n{suggestion}" if suggestion else error_msg
            
            # M4: 从建议中提取「待确认查询」，供用户回复「是」时自动执行
            suggested_query = None
            if suggestion and '您是否想查询' in suggestion:
                import re
                m = re.search(r'您是否想查询[：:]\s*(.+?)[？?]?$', suggestion)
                if m:
                    suggested_query = m.group(1).strip()
                    logger.info(f"[Orchestrator] 提取到建议查询: {suggested_query[:60]}")
            
            return {'error': full_error, 'suggested_query': suggested_query}
        
        logger.info(f"[Orchestrator] Plan 完成: {plan_task.get('intent', 'N/A')}")
        return {'plan_task': plan_task}
    
    except Exception as e:
        logger.error(f"[Orchestrator] Plan Node 异常: {e}")
        return {'error': f'计划生成失败: {str(e)}'}


async def analysis_plan_node(state: AgentState) -> dict:
    """??????: ????? + ?????? + ????"""
    logger.info("[Orchestrator] Analysis Plan Node ??????")
    try:
        from config.data_dictionary_loader import get_data_dictionary_prompt
        data_dict_text = get_data_dictionary_prompt()
        plan = await analysis_planner.plan(
            user_query=state['user_query'],
            data_dictionary_text=data_dict_text,
        )
        plan_dict = {
            'question_type': plan.question_type,
            'executable_methods': [
                {'id': m.method_id, 'name': m.method_name, 'reason': m.reason}
                for m in plan.executable_methods
            ],
            'blocked_methods': [
                {'id': m.method_id, 'name': m.method_name,
                 'missing_data': m.missing_data}
                for m in plan.blocked_methods
            ],
            'data_gaps': [
                {'field': g.field, 'description': g.description,
                 'needed_for': g.needed_for}
                for g in plan.data_gaps
            ],
            'sql_hints': plan.suggested_sql_hints,
            'summary': plan.summary,
        }
        if not plan.executable_methods:
            gap_summary = '; '.join(
                f"{g.field}({g.description})" for g in plan.data_gaps[:5]
            )
            return {
                'analysis_plan': plan_dict,
                'error': (
                    f"??????: ????????\n"
                    f"????: {plan.summary}\n"
                    f"????: {gap_summary}"
                ),
            }
        logger.info(
            f"[Orchestrator] ??????: {len(plan.executable_methods)}???, "
            f"{len(plan.blocked_methods)}??, {len(plan.data_gaps)}????"
        )

        # ????????
        try:
            tasks = create_tasks_from_plan(plan_dict)
            logger.info(f"[Orchestrator] ?? {len(tasks)} ?????")
        except Exception as e:
            logger.warning(f"[Orchestrator] ???????????: {e}")

        return {'analysis_plan': plan_dict}
    except Exception as e:
        logger.error(f"[Orchestrator] Analysis Plan Node ??: {e}")
        return {'analysis_plan': None}



async def query_node(state: AgentState) -> dict:
    """
    Query Node: 执行SQL → 返回数据
    
    输入: state['plan_task']
    输出: state['query_result'] 或 state['error']
    """
    logger.info(f"[Orchestrator] Query Node 开始执行")
    try:
        user_role = state.get('user_role')
        # L3: SQL硬注入 - 在执行前进行权限校验和条件注入
        if user_role and user_role.get('role_id') != 'admin':
            from permission_manager import permission_manager, PermissionDenied
            plan_task = state['plan_task']
            sql = plan_task.get('sql', '')
            try:
                enforced_sql = permission_manager.enforce_sql(sql, user_role)
                plan_task = {**plan_task, 'sql': enforced_sql}
                # 记录审计
                permission_manager.log_permission_audit(
                    staff_id=user_role.get('staff_id', ''),
                    staff_name=user_role.get('staff_name', ''),
                    role_id=user_role.get('role_id', ''),
                    action='query',
                    original_sql=sql,
                    enforced_sql=enforced_sql,
                    tables_accessed=permission_manager.extract_tables_from_sql(sql)
                )
            except PermissionDenied as e:
                # 记录拒绝审计
                permission_manager.log_permission_audit(
                    staff_id=user_role.get('staff_id', ''),
                    staff_name=user_role.get('staff_name', ''),
                    role_id=user_role.get('role_id', ''),
                    action='denied',
                    original_sql=sql,
                    denied_reason=str(e),
                    tables_accessed=permission_manager.extract_tables_from_sql(sql)
                )
                return {'error': f'🔒 权限不足: {e.message}'}
        else:
            plan_task = state['plan_task']

        # 将 user_query 注入 plan_task，供 QueryAgent 自愈时记录 lesson
        result = await query_agent.execute({**plan_task, 'user_query': state.get('user_query', '')})
        
        if not result.get('success'):
            error_msg = result.get('error', '查询执行失败')
            retries = result.get('retries', 0)
            return {'error': f'查询失败(重试{retries}次): {error_msg}'}
        
        row_count = result.get('row_count', 0)
        logger.info(f"[Orchestrator] Query 完成: {row_count} 行数据")

        # === P0-1: 智能输出截断 ===
        # 对大结果集进行压缩，防止后续Agent（Inspection/Analysis）
        # 因上下文过大导致 Token 溢出或分析质量下降
        if result.get('rows') and len(result.get('rows', [])) > 0:
            raw_token_est = estimate_tokens_fast(str(result.get('rows', [])))
            if raw_token_est > 4000:  # 超过4000 token触发压缩
                compaction = compact_query_result(result)
                logger.info(
                    f"[Orchestrator] 数据压缩: level={compaction.level}, "
                    f"{compaction.rows_total}→{compaction.rows_kept}行, "
                    f"{compaction.original_size}→{compaction.compacted_size} tokens"
                )
                result = compaction.data

        return {'query_result': result}
    
    except Exception as e:
        logger.error(f"[Orchestrator] Query Node 异常: {e}")
        return {'error': f'查询执行失败: {str(e)}'}


async def inspection_node(state: AgentState) -> dict:
    """
    Inspection Node: 数据核查探查

    在 Query Agent 返回数据后、Analysis Agent 分析前执行。
    对数据进行完整性、一致性、准确性、时效性核查，
    生成核查报告和统计画像，传递给 Analysis Agent。

    输入: state['user_query'] + state['query_result']
    输出: state['inspection_result']
    """
    logger.info(f"[Orchestrator] Inspection Node 开始数据核查")
    try:
        query_result = state.get('query_result', {})
        if not query_result or not query_result.get('success'):
            logger.info("[Orchestrator] 无有效查询结果，跳过核查")
            return {'inspection_result': None}

        # 执行数据核查探查
        report = inspection_agent.inspect(
            query_result,
            state['user_query']
        )

        logger.info(
            f"[Orchestrator] 核查完成: passed={report['checks_passed']}, "
            f"warnings={report['warning_count']}, errors={report['error_count']}"
        )
        return {'inspection_result': report}

    except Exception as e:
        logger.error(f"[Orchestrator] Inspection Node 异常: {e}，跳过核查继续分析")
        return {'inspection_result': None}


async def analysis_node(state: AgentState) -> dict:
    """
    Analysis Node: 分析数据 → 生成三段式建议

    输入: state['user_query'] + state['query_result']
          + state['inspection_result']（核查报告）
          + state['activated_skills']（匹配的技能列表）
    输出: state['analysis']
    """
    logger.info(f"[Orchestrator] Analysis Node 开始分析")
    try:
        query_result = state['query_result']

        # M1: 异常检测前置
        anomaly_summary = analysis_agent._detect_anomalies(query_result)
        if anomaly_summary:
            logger.info(f"[Orchestrator] 检测到数据异常:\n{anomaly_summary[:200]}")
        else:
            logger.info("[Orchestrator] 数据无明显异常")

        # === 技能匹配与注入 ===
        activated_skills = state.get('activated_skills', [])
        skills_context = ""
        if not activated_skills:
            # 编排器层面匹配技能
            try:
                matched = skill_registry.match_query(state['user_query'], top_k=3)
                activated_skills = [s.name for s in matched]
                # 构建技能上下文（注入 Analysis Agent）
                if matched:
                    skill_names = ', '.join(s.name for s in matched)
                    logger.info(f"[Orchestrator] 激活技能: {skill_names}")
                    skills_context = "\n\n---\n\n".join(
                        s.prompt_snippet for s in matched
                    )
                    skills_context = (
                        f"## 🧠 以下数据分析方法将指导本次分析\n\n"
                        f"已激活分析技能: {skill_names}\n\n"
                        f"{skills_context}"
                    )
            except Exception as e:
                logger.warning(f"[Orchestrator] 技能匹配失败: {e}，使用默认分析模式")

        # === 核查报告上下文 ===
        inspection_context = ""
        inspection_result = state.get('inspection_result')
        if inspection_result:
            inspection_context = inspection_agent.format_for_analysis(inspection_result)

        # === P0-2: Token估算 + 上下文压缩 ===
        # 分析前检查所有上下文的Token预算，超出阈值自动压缩
        # 借鉴 Claude Code 两层级策略: 70%触发snip, 90%触发auto-compact
        compaction_ctx = compact_analysis_context(
            user_query=state['user_query'],
            query_result=query_result,
            anomaly_summary=anomaly_summary,
            inspection_context=inspection_context,
            skills_context=skills_context,
        )
        compact = compaction_ctx['compact_result']
        if compact.level != 'none':
            logger.info(
                f"[Orchestrator] 上下文压缩: level={compact.level}, "
                f"{compact.original_size}→{compact.compacted_size} tokens"
            )
            # 使用压缩后的查询结果
            query_result = compaction_ctx['query_result_compacted']
        if compaction_ctx['context_truncated']:
            inspection_context = compaction_ctx['inspection_context']
            skills_context = compaction_ctx['skills_context']
            logger.warning("[Orchestrator] 上下文不足，核查报告/技能指令已截断")

        # 将核查报告和技能上下文注入 Analysis Agent
        analysis = await analysis_agent.analyze(
            state['user_query'],
            query_result,
            anomaly_summary=anomaly_summary,
            inspection_context=inspection_context,
            skills_context=skills_context,
        )
        logger.info(f"[Orchestrator] Analysis 完成: {len(analysis)} 字符")
        return {
            'analysis': analysis,
            'activated_skills': activated_skills,
        }

    except Exception as e:
        logger.error(f"[Orchestrator] Analysis Node 异常: {e}")
        return {'error': f'分析生成失败: {str(e)}'}


async def format_output(state: AgentState) -> dict:
    """
    格式化输出节点: 组装最终回复
    
    - 正常流程: 使用 Analysis Agent 的输出，并触发自学习
    - 异常流程: 使用错误信息生成友好提示
    """
    duration = time.time() - state['start_time']
    
    if state.get('error'):
        # 错误兜底回复
        reply = (
            f"⚠️ 处理遇到问题\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{state['error']}\n"
            f"\n"
            f"💡 建议：请尝试简化您的问题，或使用以下格式：\n"
            f"• \"昨天日活多少\"\n"
            f"• \"最近7天新增趋势\"\n"
            f"• \"核心活跃环比变化\""
        )
    elif state.get('analysis'):
        reply = state['analysis']
        # ????????
        analysis_plan = state.get('analysis_plan')
        if analysis_plan and analysis_plan.get('executable_methods'):
            methods_names = [m['name'] for m in analysis_plan['executable_methods']]
            reply = (
                f"?? ????: {' ? '.join(methods_names)}\n"
                f"????????????????\n"
                f"{reply}"
            )
            if analysis_plan.get('data_gaps'):
                gaps = analysis_plan['data_gaps']
                gap_text = '; '.join(g['field'] for g in gaps[:3])
                reply += f"\n\n?? ????: {gap_text}"
        # 触发自学习（失败不阻塞主流程）
        try:
            from learning.feedback_loop import feedback_loop
            from learning.pattern_store import pattern_store
            from learning.memory_consolidator import memory_consolidator
            query_result = state.get('query_result') or {}
            user_query = state.get('user_query', '')
            feedback_loop.on_query_success(
                user_query=user_query,
                sql=query_result.get('sql_executed', ''),
                result=query_result,
                analysis=reply
            )
            # 记录成功查询为最佳实践（写入 lessons 表）
            if query_result.get('sql_executed') and user_query:
                pattern_store.save_lesson(
                    lesson_type='best_practice',
                    original_query=user_query,
                    problem=f"用户问题: {user_query}",
                    solution=f"成功SQL: {query_result.get('sql_executed', '')}"
                )
            # 记录分析反馈（分析文本有实质内容时才写入，避免过度记录）
            if user_query and len(reply) > 100:
                feedback_loop.on_analysis_feedback(
                    user_query=user_query,
                    analysis=reply,
                    feedback='auto_record'
                )
            # === P1-1: 自动记忆合并（MemoryConsolidator） ===
            # 从每次成功会话中自动提取关键经验，合并去重后存入长期记忆
            memory_consolidator.on_session_complete(
                user_query=user_query,
                sql_executed=query_result.get('sql_executed', ''),
                analysis_result=reply,
                query_success=query_result.get('success', False),
                row_count=query_result.get('row_count', 0),
                duration_ms=int(duration * 1000),
            )
        except Exception as e:
            logger.warning(f"[Orchestrator] 自学习回调异常忽略: {e}")
    else:
        reply = "处理完成，但未能生成分析结果。请重试。"
    
    # 追加耗时信息
    reply += f"\n\n⏱️ 用时 {duration:.1f}s"

    # === P0-1: DingTalk消息截断 ===
    # DingTalk 消息限制约20KB，超出会导致发送失败
    reply = truncate_for_dingtalk(reply)

    return {'final_reply': reply}


# ============================================================
# 条件路由
# ============================================================

async def direct_answer_node(state: AgentState) -> dict:
    """
    直接回答节点：处理知识解答类问题，跳过SQL执行。
    将Plan Agent的direct_answer直接作为分析结果输出。
    """
    plan_task = state.get('plan_task', {})
    direct_answer = plan_task.get('direct_answer', '')
    
    logger.info(f"[Orchestrator] Direct Answer Node: {len(direct_answer)} 字符")
    
    # 直接使用Plan Agent生成的回答作为analysis
    formatted_answer = (
        f"💡 知识解答\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{direct_answer}"
    )
    
    return {'analysis': formatted_answer}


def should_continue_after_plan(state: AgentState) -> str:
    """
    Plan节点后的路由判断
    - error → format_output（报错）
    - direct_answer → direct_answer（知识解答，跳过SQL）
    - 其他 → query（执行SQL查询）
    """
    if state.get('error'):
        return 'format_output'
    
    plan_task = state.get('plan_task', {})
    
    # 知识解答类：有direct_answer且不需要SQL
    if plan_task.get('direct_answer') and not plan_task.get('needs_sql', True):
        logger.info(f"[Orchestrator] 知识解答类问题，跳过SQL执行")
        return 'direct_answer'
    
    return 'query'


def should_continue_after_query(state: AgentState) -> str:
    """Query节点后的路由判断
    - error → format_output（跳过探查和分析）
    - success → inspection（数据核查）
    """
    if state.get('error'):
        return 'format_output'
    return 'inspection'


# ============================================================
# 构建 LangGraph StateGraph
# ============================================================

def build_graph() -> StateGraph:
    """
    构建并编译LangGraph工作流。

    流程图 (v2 - 含数据核查+技能注入):
        plan → (错误) → format_output → END
             → (知识解答) → direct_answer → format_output → END
             → (数据查询) → query → (成功) → inspection → analysis → format_output → END
                                   → (失败) → format_output → END

    新增节点:
        inspection - 数据核查探查（完整性/一致性/时效性/统计画像）
        analysis现在接收核查报告+匹配技能的方法论，输出更专业的分析
    """
    workflow = StateGraph(AgentState)

    # 添加节点
    workflow.add_node('analysis_plan', analysis_plan_node)  # ???????
    workflow.add_node('plan', plan_node)
    workflow.add_node('query', query_node)
    workflow.add_node('inspection', inspection_node)  # 新增：数据核查
    workflow.add_node('analysis', analysis_node)
    workflow.add_node('direct_answer', direct_answer_node)  # 知识解答
    workflow.add_node('format_output', format_output)

    # 设置入口
    workflow.set_entry_point('analysis_plan')
    workflow.add_edge('analysis_plan', 'plan')  # ?????????


    # 条件路由: plan → query 或 direct_answer 或 format_output
    workflow.add_conditional_edges(
        'plan',
        should_continue_after_plan,
        {
            'query': 'query',
            'direct_answer': 'direct_answer',
            'format_output': 'format_output'
        }
    )

    # 条件路由: query → inspection 或 format_output
    workflow.add_conditional_edges(
        'query',
        should_continue_after_query,
        {
            'inspection': 'inspection',
            'format_output': 'format_output'
        }
    )

    # 固定路由: inspection → analysis（核查总是进入分析）
    workflow.add_edge('inspection', 'analysis')

    # 固定路由: analysis → format_output → END
    workflow.add_edge('analysis', 'format_output')
    workflow.add_edge('direct_answer', 'format_output')  # 知识解答直达输出
    workflow.add_edge('format_output', END)

    return workflow.compile()


# ============================================================
# 全局编排器实例
# ============================================================

orchestrator = build_graph()


# ============================================================
# 主入口
# ============================================================

async def run_agent(user_query: str, user_role: dict = None, last_context: dict = None) -> tuple:
    """
    主入口：接收用户查询，返回 (reply_text, metadata) 元组。
    
    与 dingtalk_bot.py 的 asyncio 事件循环兼容。
    
    Args:
        user_query: 用户的自然语言查询
        user_role: 用户角色信息字典（权限系统）
        last_context: 上一轮查询上下文（M3追问支持）
        
    Returns:
        (reply_text: str, metadata: dict) 元组
        metadata 包含 plan_task, query_result, analysis, user_query
    """
    logger.info(f"[Orchestrator] 新查询: {user_query[:60]}... (角色: {user_role.get('role_id', 'N/A') if user_role else 'N/A'})")
    
    initial_state: AgentState = {
        'user_query': user_query,
        'user_role': user_role,
        'plan_task': None,
        'query_result': None,
        'inspection_result': None,
        'activated_skills': None,
        'analysis': None,
        'final_reply': None,
        'error': None,
        'retry_count': 0,
        'start_time': time.time(),
        'last_context': last_context,  # M3: 注入上下文
    }
    
    try:
        result = await orchestrator.ainvoke(initial_state)
        reply = result.get('final_reply', '处理失败，请重试。')

        # M3: 嵌入SQL到回复中（dingtalk_bot可提取用于下轮追问上下文）
        query_result = result.get('query_result') or {}
        sql_executed = query_result.get('sql_executed', '')
        if sql_executed:
            reply = reply + f"\n<!--M3_SQL:{sql_executed}-->"

        logger.info(f"[Orchestrator] 查询完成，回复长度: {len(reply)}")
        metadata = {
            'plan_task': result.get('plan_task', {}),
            'analysis_plan': result.get('analysis_plan'),  # v2.2
            'query_result': query_result,
            'inspection_result': result.get('inspection_result'),
            'activated_skills': result.get('activated_skills', []),
            'analysis': result.get('analysis', ''),
            'user_query': user_query,
            'suggested_query': result.get('suggested_query'),  # M4: 传递待确认查询
        }
        return reply, metadata
    
    except Exception as e:
        duration = time.time() - initial_state['start_time']
        logger.error(f"[Orchestrator] 编排器异常: {e}")
        return (
            f"⚠️ 系统异常\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"处理过程中发生未预期错误: {str(e)}\n"
            f"\n"
            f"请稍后重试，或联系管理员。\n"
            f"\n"
            f"⏱️ 用时 {duration:.1f}s"
        ), {}


# ============================================================
# 便捷同步调用（供非异步环境使用）
# ============================================================

def run_agent_sync(user_query: str) -> str:
    """
    同步版本的主入口。
    适用于非异步环境（如测试脚本）。
    """
    reply, _ = asyncio.run(run_agent(user_query))
    return reply


if __name__ == '__main__':
    # 简单测试入口
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    
    query = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else '昨天日活多少'
    print(f"\n查询: {query}\n")
    print(run_agent_sync(query))
