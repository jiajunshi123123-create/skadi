"""异步转发 + 审计日志
改造说明 (2026-05-19):
- forward_to_openclaw() 已废弃，保留注释备份
- 新增 forward_to_langgraph() 使用 LangGraph 三Agent编排器
"""
import asyncio, logging, os, sys, time

from core.audit import write_audit_log

logger = logging.getLogger("agent_forward")

# 确保能找到 agent_orchestrator 模块
WORKSPACE_DIR = os.getenv('WORKSPACE_DIR', '/opt/workspace')
sys.path.insert(0, WORKSPACE_DIR)
from agent_orchestrator import run_agent

def _audit(uid, uname, query, resp, src="langgraph-orchestrator", dur=0, tok=0, status="success"):
    """审计日志。写入失败时由 write_audit_log 记录错误，不再静默忽略。"""
    write_audit_log(
        user_id=uid,
        user_name=uname,
        query=query,
        response=resp,
        source=src,
        duration_ms=dur,
        tokens=tok,
        status=status,
    )


async def forward_to_langgraph(
    prompt: str,
    timeout: int = 120,
    user_role: dict = None,
    last_context: dict = None,
    user_id: str = "",
    user_name: str = "",
) -> tuple:
    """通过 LangGraph 三Agent编排器处理用户查询
    
    替代原 forward_to_openclaw()，直接调用本地 Python 编排器，
    避免旧版 CLI 子进程开销和 sessions_spawn 限制。
    
    Args:
        prompt: 用户的自然语言查询
        timeout: 超时时间（秒），默认120秒
        last_context: 上一轮查询上下文（追问支持）
        
    Returns:
        (reply_text: str, metadata: dict) 元组
    """
    t0 = time.time()
    try:
        reply, metadata = await asyncio.wait_for(
            run_agent(prompt, user_role=user_role, last_context=last_context),
            timeout=timeout
        )
        dur = int((time.time() - t0) * 1000)
        # 审计日志
        _audit(user_id, user_name, prompt[:200], reply[:300] if reply else "", "langgraph-orchestrator", dur, 0, "success")
        return reply if reply else "分析结果为空，请重试。", metadata
    except asyncio.TimeoutError:
        dur = int((time.time() - t0) * 1000)
        _audit(user_id, user_name, prompt[:200], "", "langgraph-orchestrator", dur, 0, "timeout")
        return "⚠️ 分析超时，请简化查询后重试。", {}
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        logger.error(f"LangGraph agent error: {e}")
        _audit(user_id, user_name, prompt[:200], str(e)[:300], "langgraph-orchestrator", dur, 0, "error")
        return f"⚠️ AI服务异常: {str(e)}", {}


# ============================================================
# 旧代码备份 (CLI 调用方式) — 保留用于回滚
# ============================================================
# async def forward_to_openclaw(prompt: str, timeout: int = 120) -> str:
#     t0 = time.time()
#     try:
#         proc = await asyncio.create_subprocess_exec(
#             "openclaw", "agent", "--agent", "plan-agent",
#             "--message", prompt, "--json", "--timeout", str(timeout),
#             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
#         stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout+15)
#         dur = int((time.time()-t0)*1000)
#         if proc.returncode != 0:
#             logger.error(f"Agent failed: {stderr.decode()[:200]}")
#             return "AI服务繁忙，请稍后重试。"
#         data = json.loads(stdout.decode())
#         text = data.get("result",{}).get("payloads",[{}])[0].get("text","")
#         tok = data.get("result",{}).get("meta",{}).get("agentMeta",{}).get("usage",{}).get("total",0)
#         return text if text else "分析结果为空，请重试。"
#     except asyncio.TimeoutError:
#         _audit("","",prompt[:200],"","plan-agent",int((time.time()-t0)*1000),0,"timeout")
#         return "分析超时，请简化查询后重试。"
#     except Exception as e:
#         logger.error(f"Agent error: {e}")
#         return "AI服务异常，已自动回退。"
