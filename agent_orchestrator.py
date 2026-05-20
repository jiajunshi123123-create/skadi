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

logger = logging.getLogger(__name__)

# ============================================================
# State 定义
# ============================================================


class AgentState(TypedDict):
    """编排器全局状态"""
    user_query: str               # 用户原始问题
    user_role: Optional[dict]     # 用户角色信息（权限系统）
    plan_task: Optional[dict]     # Plan Agent 输出
    query_result: Optional[dict]  # Query Agent 输出
    analysis: Optional[str]       # Analysis Agent 输出
    final_reply: Optional[str]    # 最终回复
    error: Optional[str]          # 错误信息
    retry_count: int              # 重试计数
    start_time: float             # 开始时间
    last_context: Optional[dict]  # M3: 上一轮查询上下文


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
            return {'error': full_error}
        
        logger.info(f"[Orchestrator] Plan 完成: {plan_task.get('intent', 'N/A')}")
        return {'plan_task': plan_task}
    
    except Exception as e:
        logger.error(f"[Orchestrator] Plan Node 异常: {e}")
        return {'error': f'计划生成失败: {str(e)}'}


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

        result = await query_agent.execute(plan_task)
        
        if not result.get('success'):
            error_msg = result.get('error', '查询执行失败')
            retries = result.get('retries', 0)
            return {'error': f'查询失败(重试{retries}次): {error_msg}'}
        
        logger.info(f"[Orchestrator] Query 完成: {result.get('row_count', 0)} 行数据")
        return {'query_result': result}
    
    except Exception as e:
        logger.error(f"[Orchestrator] Query Node 异常: {e}")
        return {'error': f'查询执行失败: {str(e)}'}


async def analysis_node(state: AgentState) -> dict:
    """
    Analysis Node: 分析数据 → 生成三段式建议
    
    输入: state['user_query'] + state['query_result']
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
        
        # 将异常摘要传给Analysis Agent
        analysis = await analysis_agent.analyze(
            state['user_query'],
            query_result,
            anomaly_summary=anomaly_summary
        )
        logger.info(f"[Orchestrator] Analysis 完成: {len(analysis)} 字符")
        return {'analysis': analysis}
    
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
        # 触发自学习（失败不阻塞主流程）
        try:
            from learning.feedback_loop import feedback_loop
            query_result = state.get('query_result') or {}
            feedback_loop.on_query_success(
                user_query=state.get('user_query', ''),
                sql=query_result.get('sql_executed', ''),
                result=query_result,
                analysis=reply
            )
        except Exception as e:
            logger.warning(f"[Orchestrator] 自学习回调异常忽略: {e}")
    else:
        reply = "处理完成，但未能生成分析结果。请重试。"
    
    # 追加耗时信息
    reply += f"\n\n⏱️ 用时 {duration:.1f}s"
    
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
    """Query节点后的路由判断"""
    if state.get('error'):
        return 'format_output'
    return 'analysis'


# ============================================================
# 构建 LangGraph StateGraph
# ============================================================

def build_graph() -> StateGraph:
    """
    构建并编译LangGraph工作流。
    
    流程图:
        plan → (错误) → format_output → END
             → (知识解答) → direct_answer → format_output → END
             → (数据查询) → query → (成功) → analysis → format_output → END
                                   → (失败) → format_output → END
    """
    workflow = StateGraph(AgentState)
    
    # 添加节点
    workflow.add_node('plan', plan_node)
    workflow.add_node('query', query_node)
    workflow.add_node('analysis', analysis_node)
    workflow.add_node('direct_answer', direct_answer_node)  # 知识解答
    workflow.add_node('format_output', format_output)
    
    # 设置入口
    workflow.set_entry_point('plan')
    
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
    
    # 条件路由: query → analysis 或 format_output
    workflow.add_conditional_edges(
        'query',
        should_continue_after_query,
        {
            'analysis': 'analysis',
            'format_output': 'format_output'
        }
    )
    
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
            'query_result': query_result,
            'analysis': result.get('analysis', ''),
            'user_query': user_query,
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
