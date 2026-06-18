"""AI数据分析助手 - Web 服务入口

FastAPI 应用，提供：
- POST /api/chat         聊天接口（SSE流式响应）
- GET  /api/skills       列出所有分析技能
- GET  /api/skills/{name} 技能详情
- GET  /                 前端聊天页面（静态文件）
- GET  /health           健康检查

与 dingtalk_bot.py 共享同一个 agent_orchestrator.run_agent() 管道。
两种载体互不冲突，可同时运行。
"""

import asyncio
import json
import logging
import time
import os
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 导入编排器（与 DingTalk Bot 共享）
from agent_orchestrator import run_agent
from skills import skill_registry
from utils.context_compactor import compact_conversation, load_anchors_from_pg
from utils.token_estimator import estimate_tokens_fast

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger("web_server")

app = FastAPI(
    title="AI数据分析助手",
    description="基于 LangGraph 三Agent 的企业数据AI分析系统 Web 服务",
    version="2.0.0",
)

# ============================================================
# 会话管理（内存缓存）
# ============================================================

_session_contexts: dict = {}  # session_id → {last_context, history, created_at}


def _get_or_create_session(session_id: str) -> dict:
    if session_id not in _session_contexts:
        # Load anchors from PG if available
        anchors = load_anchors_from_pg(session_id, limit=3)
        anchor_context = ""
        if anchors:
            anchor_context = "\n\n".join([a["anchor_text"][:300] for a in anchors])

        _session_contexts[session_id] = {
            'last_context': None,
            'history': [],              # [{"role": "user/assistant", "content": "..."}]
            'conversation_messages': [], # Full conversation for compression
            'anchor_context': anchor_context,
            'created_at': time.time(),
            'compaction_count': 0,
        }
    return _session_contexts[session_id]


# 定期清理过期会话（超过1小时未活动）
async def _cleanup_sessions():
    while True:
        await asyncio.sleep(600)  # 每10分钟清理一次
        now = time.time()
        expired = [
            sid for sid, s in _session_contexts.items()
            if now - s['created_at'] > 3600
        ]
        for sid in expired:
            del _session_contexts[sid]
        if expired:
            logger.info(f"[Web] 清理 {len(expired)} 个过期会话")


@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_sessions())
    logger.info(f"[Web] 服务启动，已加载 {len(skill_registry.get_all())} 个分析技能")


# ============================================================
# API 路由
# ============================================================


@app.post("/api/chat")
async def chat(request: Request):
    """
    聊天接口 - SSE 流式响应

    请求体:
    {
        "query": "昨天日活多少",
        "session_id": "optional-session-id"
    }

    响应: SSE 事件流
    - event: status   → {"phase": "planning|querying|inspecting|analyzing"}
    - event: token    → {"content": "增量文本"}
    - event: metadata → {"plan_task": ..., "query_result": ..., "inspection_result": ..., "activated_skills": [...]}
    - event: done     → {"duration": 1.5}
    """
    try:
        body = await request.json()
        query = body.get('query', '').strip()
        session_id = body.get('session_id', 'default')

        if not query:
            return JSONResponse(
                {'error': 'query 不能为空'}, status_code=400
            )

        session = _get_or_create_session(session_id)

        async def generate():
            t0 = time.time()

            try:
                # 阶段1: 计划生成
                yield f"event: status\ndata: {json.dumps({'phase': 'planning', 'msg': '正在理解您的问题...'}, ensure_ascii=False)}\n\n"

                # 执行编排器（复用 DingTalk 同一条管道）
                reply, metadata = await asyncio.wait_for(
                    run_agent(query),
                    timeout=120
                )

                # 清理 M3 SQL 标记
                if '<!--M3_SQL:' in reply:
                    import re
                    reply = re.sub(r'<!--M3_SQL:.+?-->', '', reply).rstrip()

                # 推送完整回复
                yield f"event: token\ndata: {json.dumps({'content': reply}, ensure_ascii=False)}\n\n"

                # 推送元数据（包含核查报告和技能信息）
                # 估算当前会话 token 用量
                conv_text = "".join([m.get("content", "") for m in session.get('conversation_messages', [])])
                current_tokens = estimate_tokens_fast(conv_text)

                metadata_json = json.dumps({
                    'plan_task': metadata.get('plan_task', {}),
                    'analysis_plan': metadata.get('analysis_plan'),
                    'inspection_result': metadata.get('inspection_result'),
                    'activated_skills': metadata.get('activated_skills', []),
                    'user_query': query,
                    'token_usage': current_tokens,
                }, ensure_ascii=False, default=str)
                yield f"event: metadata\ndata: {metadata_json}\n\n"

                # 更新会话上下文
                if metadata.get('query_result'):
                    sql = metadata['query_result'].get('sql_executed', '')
                    session['last_context'] = {
                        'last_sql': sql,
                        'last_result_summary': reply[:200],
                        'last_analysis': reply[:500],
                    }

                # 存储对话历史
                session['conversation_messages'].append({"role": "user", "content": query})
                session['conversation_messages'].append({"role": "assistant", "content": reply})

                # 检查是否需要上下文压缩
                total_text = "".join([m.get("content", "") for m in session['conversation_messages']])
                total_tokens = estimate_tokens_fast(total_text)
                threshold = int(128000 * 0.6)  # 60% of 128k

                if total_tokens > threshold:
                    logger.info(f"[Web] 触发上下文压缩: {total_tokens}/{128000} tokens")
                    try:
                        comp_result = compact_conversation(
                            messages=session['conversation_messages'],
                            session_id=session_id,
                            llm_client=None,  # TODO: wire up LLM client
                        )
                        if comp_result.get('compressed'):
                            session['conversation_messages'] = comp_result['new_context']
                            session['compaction_count'] += 1
                            logger.info(f"[Web] 压缩完成: 锚点已存入PG, compaction #{session['compaction_count']}")
                    except Exception as comp_err:
                        logger.warning(f"[Web] 上下文压缩失败: {comp_err}")

                duration = time.time() - t0
                yield f"event: done\ndata: {json.dumps({'duration': round(duration, 1)})}\n\n"

            except asyncio.TimeoutError:
                yield f"event: status\ndata: {json.dumps({'phase': 'error', 'msg': '分析超时，请简化查询后重试'}, ensure_ascii=False)}\n\n"
                yield f"event: done\ndata: {json.dumps({'duration': round(time.time() - t0, 1)})}\n\n"
            except Exception as e:
                logger.error(f"[Web] 聊天处理异常: {e}")
                yield f"event: status\ndata: {json.dumps({'phase': 'error', 'msg': f'处理异常: {str(e)}'}, ensure_ascii=False)}\n\n"
                yield f"event: done\ndata: {json.dumps({'duration': round(time.time() - t0, 1)})}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )

    except Exception as e:
        logger.error(f"[Web] /api/chat 请求解析失败: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get("/api/skills")
async def list_skills():
    """列出所有分析技能"""
    return JSONResponse({
        'total': len(skill_registry.get_all()),
        'skills': skill_registry.list_skills_info(),
        'categories': {
            cat: [s.name for s in skill_registry.get_by_category(cat)]
            for cat in ['statistical', 'testing', 'ml']
        },
    })


@app.get("/api/skills/{name:path}")
async def get_skill(name: str):
    """获取指定技能的详细信息"""
    skill = skill_registry.get_by_name(name)
    if not skill:
        return JSONResponse({'error': f'技能 "{name}" 不存在'}, status_code=404)

    return JSONResponse({
        'name': skill.name,
        'description': skill.description,
        'category': skill.category,
        'keywords': skill.keywords,
        'priority': skill.priority,
        'has_prompt': bool(skill.prompt_snippet),
    })


@app.post("/api/scan")
async def scan_database():
    """扫描数据库，生成 data_dictionary.yml"""
    import json, asyncio
    from tools.db_scanner import scan_database as do_scan

    async def generate():
        try:
            yield f"event: status\ndata: {json.dumps({'phase': 'scanning', 'msg': '正在连接数据库...'}, ensure_ascii=False)}\n\n"
            
            from tools.database_adapter import DatabaseAdapter
            db = DatabaseAdapter.create()
            
            yield f"event: status\ndata: {json.dumps({'phase': 'scanning', 'msg': '正在扫描表结构...'}, ensure_ascii=False)}\n\n"
            
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, do_scan, db)
            
            if 'error' in data:
                yield f"event: error\ndata: {json.dumps({'error': data['error']}, ensure_ascii=False)}\n\n"
                return
            
            # Save to config/data_dictionary.yml
            import yaml, os
            config_dir = os.path.join(os.path.dirname(__file__), 'config')
            os.makedirs(config_dir, exist_ok=True)
            output_path = os.path.join(config_dir, 'data_dictionary.yml')
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(f"# Skadi Data Dictionary — 自动扫描生成\n")
                f.write(f"# 扫描时间: {data.get('scanned_at', '')}\n")
                f.write(f"# 数据库: {data['database']['name']}\n")
                f.write(f"# 表数量: {data['table_count']}\n")
                f.write(f"# role 默认为'未审核'，请根据业务需要调整\n")
                f.write(f"# ===================================================================\n\n")
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            
            table_count = data["table_count"]
            done_msg = f"扫描完成，发现 {table_count} 张表，已保存到 config/data_dictionary.yml"
            done_data = {"table_count": table_count, "path": "config/data_dictionary.yml", "msg": done_msg}
            yield "event: done\ndata: " + json.dumps(done_data, ensure_ascii=False) + "\n\n"
            
        except Exception as e:
            logger.error(f"[Web] 扫描失败: {e}")
            yield f"event: error\ndata: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


# Check if data dictionary exists on startup
@app.get("/api/dictionary-status")
async def dictionary_status():
    """检查 data_dictionary.yml 是否存在"""
    import os
    config_dir = os.path.join(os.path.dirname(__file__), 'config')
    yml_path = os.path.join(config_dir, 'data_dictionary.yml')
    example_path = os.path.join(config_dir, 'data_dictionary.example.yml')
    
    has_real = os.path.exists(yml_path)
    is_example = False
    if has_real:
        with open(yml_path, 'r', encoding='utf-8') as f:
            first_line = f.readline()
            is_example = 'Example' in first_line or 'your_database' in first_line
    
    return JSONResponse({
        'has_dictionary': has_real,
        'is_example': is_example,
        'path': yml_path,
    })


@app.get("/health")
async def health():
    """健康检查"""
    return {
        'status': 'ok',
        'skills_loaded': len(skill_registry.get_all()),
    }


# ============================================================
# 前端静态文件
# ============================================================

# 尝试挂载 web/ 目录
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
if os.path.isdir(_WEB_DIR):
    app.mount("/", StaticFiles(directory=_WEB_DIR, html=True), name="web")
else:
    @app.get("/")
    async def fallback_index():
        return HTMLResponse("""
        <html><body style="background:#1a1a2e;color:#eee;font-family:monospace;padding:40px;text-align:center">
        <h1>AI数据分析助手</h1>
        <p>Web 前端文件未找到，请确认 web/ 目录存在。</p>
        <p>API 端点:</p>
        <ul style="list-style:none">
            <li>POST /api/chat</li>
            <li>GET /api/skills</li>
            <li>GET /health</li>
        </ul>
        </body></html>
        """)


# ============================================================
# 主入口
# ============================================================

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(
        "web_server:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
