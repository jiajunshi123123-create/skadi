#!/usr/bin/env python3
"""DingTalk Stream Bot — WORKING: async process + ChatbotMessage.from_dict.

改造记录 (2026-05-19):
- Agent调用方式替换为 LangGraph 编排器 (forward_to_langgraph)
- 其他逻辑（DingTalk Stream、消息处理、会话管理、审计日志）保持不变
"""
import json, logging, os, re, time, traceback
from datetime import datetime, timezone, timedelta
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage, Credential, DingTalkStreamClient
from openai import OpenAI
import pymysql
import psycopg2
import uuid
import asyncio
from agent_forward import forward_to_langgraph  # 改造: 使用 LangGraph 编排器
from session_store_pg import save_session, get_session, delete_session
from learning.feedback_loop import feedback_loop

# Load .env manually (systemd truncates at # in values)
def _load_env(path=os.environ.get("ENV_FILE", "/opt/workspace/.env")):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or line.startswith(';'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")  # remove optional quotes
                    if k:
                        os.environ[k] = v
    except FileNotFoundError:
        pass

_load_env()

# === 消息去重缓存 ===
# 防止钉钉 Stream 超时重发导致重复处理
_msg_dedup_cache: dict = {}  # {msg_id: timestamp}
_MSG_DEDUP_TTL = 300  # 5分钟TTL，超过后允许重新处理

def _is_duplicate_msg(msg_id: str) -> bool:
    """
    检查消息是否重复。同时清理过期条目防止内存泄漏。
    
    Returns:
        True 表示重复消息，应跳过处理
    """
    if not msg_id:
        return False  # 无法判断，放行
    
    now = time.time()
    
    # 清理过期条目（每次检查时顺便清理）
    expired_keys = [k for k, t in _msg_dedup_cache.items() if now - t > _MSG_DEDUP_TTL]
    for k in expired_keys:
        del _msg_dedup_cache[k]
    
    # 判断是否重复
    if msg_id in _msg_dedup_cache:
        return True
    
    # 记录新消息
    _msg_dedup_cache[msg_id] = now
    return False

# M3: 追问上下文内存缓存
_last_context_cache: dict = {}

def save_last_context(user_id: str, context: dict):
    """保存用户最近一轮查询上下文，context为None时清除缓存"""
    if context is None:
        _last_context_cache.pop(user_id, None)
        return
    _last_context_cache[user_id] = {**context, 'saved_at': time.time()}

def get_last_context(user_id: str, ttl: int = 3600) -> dict:
    """获取上一轮上下文，过期返回None"""
    ctx = _last_context_cache.get(user_id)
    if not ctx:
        return None
    if time.time() - ctx.get('saved_at', 0) > ttl:
        del _last_context_cache[user_id]
        return None
    return ctx

# M4: 待确认建议查询缓存（用户回复「是」时自动执行建议查询）
_pending_suggestion_cache: dict = {}

def save_pending_suggestion(user_id: str, suggested_query: str):
    """保存 Plan Agent 返回的建议查询，用户回复「是」时替换查询"""
    _pending_suggestion_cache[user_id] = {
        'suggested_query': suggested_query,
        'saved_at': time.time(),
    }
    logger.info(f"[M4] 保存待确认建议: user={user_id}, query={suggested_query[:60]}")

def get_pending_suggestion(user_id: str, ttl: int = 600) -> str:
    """获取待确认的建议查询，过期返回None"""
    entry = _pending_suggestion_cache.get(user_id)
    if not entry:
        return None
    if time.time() - entry.get('saved_at', 0) > ttl:
        del _pending_suggestion_cache[user_id]
        return None
    return entry.get('suggested_query')

# Concurrency protection for shared state (P0 fix)
_store_lock = asyncio.Lock()
_history_lock = asyncio.Lock()
_session_lock = asyncio.Lock()

KEY = os.environ.get("DINGTALK_BOT_APP_KEY", "")
SECRET = os.environ.get("DINGTALK_BOT_APP_SECRET", "")
llm = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY",""), base_url="https://api.deepseek.com")

LOG_DIR = os.environ.get("LOG_DIR", "/opt/workspace/logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(os.path.join(LOG_DIR,"dingtalk_bot.log")), logging.StreamHandler()])
logger = logging.getLogger("dingtalk_bot")
TZ = timezone(timedelta(hours=8))
session_store = {}  # user_id -> current plan
chat_history = {}  # user_id -> list of {role, content} for last N rounds

# StarRocks connection
SR = {
    "host": "your-starrocks-host",
    "port": 9030,
    "user": os.environ.get("STARROCKS_USER", "data_user"),
    "password": os.environ.get("STARROCKS_PASSWORD", ""),
    "database": "your_database",
}

MAX_ROWS = 5000  # Safety limit (P2 fix)

def run_sql(sql):
    conn = pymysql.connect(**SR, charset='utf8mb4', connect_timeout=10, read_timeout=30)
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchmany(MAX_ROWS)  # fetchmany instead of fetchall
        cols = [d[0] for d in cur.description]
        return cols, rows
    finally:
        cur.close()
        conn.close()

def verify_step(sql):
    """Quick verification: does this SQL actually return data?"""
    cleaned, err = validate_sql(sql)
    if err:
        return False, err

    # Build a verification query: SELECT COUNT(*) FROM (original) t
    verify_sql = f"SELECT COUNT(*) AS cnt FROM ({cleaned}) t LIMIT 1"
    try:
        conn = pymysql.connect(**SR, charset='utf8mb4', connect_timeout=5, read_timeout=10)
        cur = conn.cursor()
        cur.execute(verify_sql)
        row = cur.fetchone()
        cnt = row[0] if row else 0
        cur.close()
        conn.close()
        if cnt > 0:
            return True, f"验证通过 ({cnt} 条数据)"
        else:
            return False, f"验证失败: 查询返回0条数据，可能是日期范围或条件不对"
    except Exception as e:
        return False, f"验证失败: {str(e)[:100]}"


# SQL security patterns (P2 fix)
FORBIDDEN_SQL = re.compile(
    r'(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|LOAD|IMPORT)',
    re.IGNORECASE
)
MULTI_SEMICOLON = re.compile(r';\s*\w', re.IGNORECASE)

def validate_sql(sql):
    """Validate and extract safe SQL. Returns (cleaned_sql, error_msg)."""
    if FORBIDDEN_SQL.search(sql):
        return None, "SQL包含禁止的关键字 (INSERT/UPDATE/DELETE/DROP/ALTER等)"
    if MULTI_SEMICOLON.search(sql):
        return None, "不支持多语句SQL"
    m = re.search(r'(SELECT|SHOW|DESCRIBE)\s[\s\S]*', sql, re.IGNORECASE)
    if not m:
        return None, "仅支持SELECT/SHOW/DESCRIBE查询语句"
    return m.group(0).rstrip(';').strip(), None

def execute_plan(plan):
    results = []
    for step in plan.get("steps", []):
        if step.get("action", "").strip().upper() == "ANALYSIS":
            continue  # ANALYSIS steps are handled by gen_analysis()
        sql = step["action"]
        cleaned, err = validate_sql(sql)
        if err:
            results.append({"step_id": step["id"], "action": step["action"][:60],
                "cols": [], "rows": [], "total_rows": 0, "error": err})
            continue
        try:
            cols, rows = run_sql(cleaned)
            results.append({"step_id": step["id"], "action": step["action"][:60],
                "cols": cols, "rows": [list(r) for r in rows[:20]], "total_rows": len(rows), "error": None})
        except Exception as e:
            results.append({"step_id": step["id"], "action": step["action"][:60],
                "cols": [], "rows": [], "total_rows": 0, "error": str(e)})
    return results


def gen_analysis(plan_title, original_prompt, results, analysis_instructions=""):
    """Generate Chinese-language analysis from SQL results using LLM."""
    data_summary = []
    for r in results:
        if r["error"]:
            data_summary.append("Step %d: ERROR - %s" % (r["step_id"], r["error"]))
        else:
            cols_str = ", ".join(r["cols"])
            data_summary.append("Step %d (%d rows, cols: %s)" % (r["step_id"], r["total_rows"], cols_str))
            for row in r["rows"][:5]:
                data_summary.append("  " + str(row))
            if r["total_rows"] > 5:
                data_summary.append("  ... (%d more)" % (r["total_rows"]-5))

    prompt = "基于以下查询结果，用中文输出一段简洁的数据分析（50-100字），包含关键数字和趋势判断。\n\n"
    prompt += "查询主题: " + plan_title + "\n"
    prompt += "原始需求: " + original_prompt + "\n\n"
    prompt += "数据结果:\n" + "\n".join(data_summary) + "\n\n"
    prompt += "要求:\n"
    prompt += "1. 用中文，面向业务人员\n"
    prompt += '2. 直接说结论和数据，不要"根据数据"之类的开场白\n'
    prompt += "3. 如果有环比/同比数据，明确说涨跌幅\n"
    prompt += "4. 如果趋势数据可见，描述趋势（上升/下降/平稳）\n"
    prompt += "5. 一句话总结核心发现"
    if analysis_instructions:
        prompt += "\n6. 按照以下分析要求执行: " + analysis_instructions

    try:
        r = llm.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=300,
        )
        analysis = r.choices[0].message.content.strip()
        logger.info("Analysis generated: " + analysis[:100])
        return analysis
    except Exception as e:
        logger.error("Analysis generation failed: " + str(e))
        return None

# PostgreSQL connection for experience DB
PG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "agent_experience",
    "user": "agent_user",
    "password": os.environ.get("PGPASSWORD", ""),
}

def save_task_to_db(user_id, user_name, prompt, plan, results):
    """Write completed task to PostgreSQL experience database."""
    try:
        conn = psycopg2.connect(**PG)
        cur = conn.cursor()
        tid = str(uuid.uuid4())[:8]
        tags = [plan.get("type", "other")]
        cur.execute(
            """INSERT INTO tasks (task_id, user_id, user_name, user_question, plan_json, execution, result_summary, tags, status, created_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'completed', now(), now())""",
            (tid, user_id, user_name, prompt, json.dumps(plan, ensure_ascii=False, default=str),
             json.dumps(results, ensure_ascii=False, default=str),
             f"{len(results)} steps, {sum(r['total_rows'] for r in results)} total rows",
             tags)
        )
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"Task saved to PG: {tid} — {prompt[:50]}")
        return tid
    except Exception as e:
        logger.error(f"PG write failed: {e}")
        return None

def log_task(uid, name, prompt, plan=None, status="received"):
    e = {"timestamp": datetime.now(TZ).isoformat(), "user_id": uid, "user_name": name,
         "prompt": prompt, "status": status, "plan": plan}
    with open(os.path.join(LOG_DIR, f"bot_tasks_{datetime.now(TZ).strftime('%Y%m%d')}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")

PLAN_PROMPT = """You are Plan Agent (AI 助手), connected to StarRocks (your_database). Decompose requirements into JSON plans.

## Business Data Dictionary (REAL tables — use these, never guess!)

### User & Active Stats
- `dws_biz_dayi_user_login_daily_stats`: stat_date, dau, new_user_cnt, dau_wow, new_user_wow, dau_yoy_rate, new_user_yoy_rate
  → Daily active/new user stats with WoW/YoY. Use for: 活跃人数, 新增用户, 日活
- `dws_biz_mrg_usr_new_user_count`: date, num
  → Daily new user count. Use for: 新增用户数, 某天新增了多少
- `dws_biz_ubb_bhv_app_daily_active_user`: server_day, user_id, login_cnt, first_login_time, last_login_time
  → Per-user daily activity. Use for: 活跃用户明细, 用户登录
- `dwd_biz_mrg_usr_new_user`: user_id, created_time, date, province, city, region, phone
  → New user detail. Use for: 新用户画像, 分地区新增

### Book/Product Activation (图书激活)
- `dwd_biz_mrg_bhv_books_user_detail`: create_time, uid, activation_code, books_id, name, type, source_category_name, subject, year, grade, province, city, region
  → Book activation records. KEY TABLE for book-related queries!
  → name examples: "2025安徽《中考黑白卷》", "2025《高考黑白卷·新高考》"
  → source_category_name values: "黑白卷", "定心卷", etc.
  → type values: "HBJ"(黑白卷), "DXJ"(定心卷), etc.
  → Use for: 图书激活人数, 某本书激活了多少, 黑白卷激活, 定心卷激活
  → Filter by: source_category_name LIKE '%黑白卷%' OR name LIKE '%黑白卷%'
- `dws_biz_ubb_bhv_user_book_funnel_status`: stat_date, uid, books_id, is_trial_active, active_login, active_ask, active_effective_ask
  → Book user funnel: trial→login→ask→effective_ask. Use for: 图书漏斗, 试用转化
- `dws_biz_ubb_bhv_book_right_usernum`: stat_date, total_free_rights_users, total_valid_qna_users
  → Book rights user count. Use for: 图书权益用户数
- `dws_biz_bhv_books_resource_download_split_daily`: server_day, uid, books_id, books_name, resource_name_single, resource_type, download_count
  → Book resource downloads. Use for: 图书资源下载量
- `ods_biz_ubb_ord_homework_prod_books_activation_code`: activation code inventory
  → Use for: 激活码库存, 激活码使用情况
- `ods_biz_ubb_smt_homework_prod_books`: books_id, name, type, subject, grade, version
  → Book metadata/catalog. Use for: 图书列表, 图书信息查询

### Sales/Orders (销售订单)
- `dwd_biz_mrg_mkt_goods_order`: d_date, order_no, business_type, goods_name, grade, subject, unit_price, sum_num, sum_actual, count_order, province, channel_name
  → Goods orders. Use for: 销售收入, 订单数, 分商品销售
- `dwd_biz_mrg_mkt_order_date`: d_date, order_no, user_id, amount, channel, product_type_en, product_name, pay_method, province
  → Order detail with user. Use for: 用户购买行为, 支付方式分析
- `dwd_biz_mrg_mkt_total_data`: d_date, project_code, project_name, total_income, first_order, renewal, sales_amount, refund_amount
  → Daily revenue summary. Use for: 总收入, 首单/续费分析

### Event Tracking (埋点事件 - use maidian tables, NOT raw qb_event_log!)
- dwd_biz_bhv_maidian_user_core_behavior_daily: server_day, user_id, raw_event_name, std_event_name, event_type, element_name, event_cnt
  → KNOWN std_event_name values: 时文阅读作答, 立即对战, 挑战赛对战, 视频使用, 书本封面扫码, 作业布置点击, 开始练习点击, 继续练习点击, 查看自主练习详情点击, 英语听说点击
  → Use for: 用户行为分析, 功能使用统计, 某功能有多少人使用
  → Core behavior events. Use for: 用户行为分析, 功能使用
- `dwd_biz_bhv_maidian_user_event_resource_daily`: server_day, user_id, raw_event_name, std_event_name, element_value (resource_id), event_cnt
  → Resource-related events. Use for: 资源使用, 视频观看, 文档下载
- `dwd_biz_bhv_maidian_user_event_marketing_daily`: server_day, user_id, raw_event_name, std_event_name, event_cnt
  → Marketing events. Use for: 营销活动分析
- `dwd_biz_bhv_maidian_user_event_value_daily`: server_day, user_id, raw_event_name, std_event_name, event_cnt
  → Value events. Use for: 核心价值行为

### Raw Event Logs (only when maidian tables don't cover it)
- `qb_event_log_202604`, `qb_event_log_202605` (partitioned by month): id, yid, user_info(JSON), event_name, event_info(JSON), region, ip, create_time
  → Raw event tracking. Current months: 202604, 202605. UNION ALL across months.
  → Use only when maidian tables don't have the needed event. Prefer maidian tables!

### Other Key Tables
- `ods_biz_ubb_usr_app_user_user`: user_id, name, nick_name, phone, created_time, province, city, grade_id
  → User profile. Use for: 用户画像, 用户信息
- `dwd_biz_mrg_bhv_user_log`: user behavior log
  → Use for: 用户行为轨迹
- `dwd_biz_app_exam_learning_progress_d`: learning progress
  → Use for: 学习进度, 完成率

## Key SQL Patterns
- Yesterday: WHERE date = CURDATE() - INTERVAL 1 DAY
- Month-to-date: WHERE date >= DATE_FORMAT(CURDATE(),'%Y-%m-01')
- Last 30 days: WHERE date >= CURDATE() - INTERVAL 30 DAY AND date <= CURDATE() - INTERVAL 1 DAY
- Week-over-week: Compare with CURDATE() - INTERVAL 8 DAY
- Year-over-year: Compare with CURDATE() - INTERVAL 1 DAY - INTERVAL 1 YEAR
- Book filter: WHERE source_category_name = '黑白卷' OR name LIKE '%黑白卷%'
- For event queries spanning months: UNION ALL qb_event_log_YYYYMM
- Maidian events: filter by std_event_name for standardized event names

## Output format
{"plan":{"title":"...","type":"data|dashboard|script|other","summary":"...","steps":[{"id":1,"action":"SQL query text","deliverable":"result description","auto":true,"est_minutes":1}],"total_est_minutes":1,"risks":[],"questions":[]}}

## Rules
1. If the question CANNOT be answered with the database (e.g., weather, news, general knowledge): return type="chat", steps=[], and give a conversational reply in the summary.
2. ALWAYS use the real tables above. Never invent table/column names.
3. Prefer ready-made stats tables (dws_*) over raw event logs.
4. For event/behavior queries: use maidian tables (dwd_biz_bhv_maidian_*), NOT qb_event_log.
5. For book/product queries: use dwd_biz_mrg_bhv_books_user_detail as primary source.
6. Simple queries: 1 min. Complex (JOIN + UNION): 3-5 min max.
7. Always respond in Chinese. Self-reference: AI 助手.
8. Output SQL directly in the step action — ready to execute.
9. CRITICAL — CHINESE ALIASES: ALL SQL column aliases MUST use Chinese. Example: SELECT num AS 日新增用户数, date AS 日期.
10. ANALYSIS STEP: Include a final step with action="ANALYSIS" describing what to compute and insights to highlight.
"""

def search_experience(prompt):
    """Search PostgreSQL experience DB for similar past queries and patterns."""
    try:
        conn = psycopg2.connect(**PG)
        cur = conn.cursor()

        # Search tasks with similar keywords
        keywords = [w for w in prompt.replace("，",",").replace("、","").split(",") if len(w.strip()) >= 2]
        if not keywords:
            # Try word-level split for non-comma text
            keywords = [prompt[i:i+2] for i in range(0, len(prompt), 2)][:5]

        results = []
        for kw in keywords[:5]:
            cur.execute(
                "SELECT user_question, result_summary, tags FROM tasks WHERE user_question LIKE %s AND status='completed' LIMIT 3",
                (f"%{kw}%",)
            )
            for row in cur.fetchall():
                if row[0] not in [r[0] for r in results]:
                    results.append((row[0], row[1], row[2]))

        # Also search patterns table (correct column names)
        cur.execute("SELECT pattern_id, trigger_keywords, common_sql FROM patterns WHERE occurrence_count >= 2 ORDER BY occurrence_count DESC LIMIT 5")
        patterns = cur.fetchall()

        cur.close()
        conn.close()

        exp_text = ""
        if patterns:
            exp_text += "已知查询模式:\n"
            for p in patterns:
                exp_text += f"  - {p[0]}: {p[1][:100]}\n"
        if results:
            exp_text += "相似历史问题:\n"
            for r in results[:3]:
                exp_text += f"  - 问: {r[0][:80]}\n    结果: {r[1][:80]}\n"

        if exp_text:
            logger.info(f"Experience found: {len(patterns)} patterns, {len(results)} similar tasks")
        return exp_text
    except Exception as e:
        logger.warning(f"Experience search failed: {e}")
        return ""


def gen_plan(text, history=None):
    msgs = [{"role":"system","content":PLAN_PROMPT}]
    if history:
        msgs.extend(history[-10:])  # last 10 messages (5 rounds)

    # Search experience DB for similar past queries
    experience = search_experience(text)
    if experience:
        msgs.append({"role": "system", "content": "以下是你之前处理过的相似问题和已知模式，可以参考:\n" + experience})

    msgs.append({"role":"user","content":text})
    try:
        r = llm.chat.completions.create(
            model="deepseek-chat",
            messages=msgs,
            temperature=0.3,
            max_tokens=4096,
            response_format={"type": "json_object"}
        )
    except Exception as e:
        logger.error(f"LLM API call failed: {e}")
        return {
            "title": "分析服务暂不可用",
            "type": "other",
            "summary": "DeepSeek API调用失败，请稍后重试。",
            "steps": [],
            "total_est_minutes": 0,
            "risks": [],
            "questions": ["请稍等片刻后重新发送需求"]
        }
    usage = r.usage
    cost_est = (usage.prompt_tokens * 0.28 + usage.completion_tokens * 0.42) / 1_000_000
    logger.info(f"API usage: in={usage.prompt_tokens} out={usage.completion_tokens} cost=¥{cost_est:.4f}")
    raw = r.choices[0].message.content.strip()
    logger.info(f"LLM raw: {raw[:300]}")
    if not raw:
        logger.error("LLM returned empty response")
        return {
            "title": "分析服务异常",
            "type": "other",
            "summary": "模型返回为空，可能是该需求在当前数据字典中无法匹配。请尝试用更具体的关键词描述，或换个说法。",
            "steps": [],
            "total_est_minutes": 0,
            "risks": [],
            "questions": ["请用更具体的业务关键词重新描述需求"]
        }

    # Try direct parse first (JSON mode should give us clean JSON)
    try:
        data = json.loads(raw)
        if "plan" in data:
            return data["plan"]
    except Exception as e:
        logger.warning(f"Direct JSON parse failed: {e}")

    # Try to extract JSON from markdown code blocks
    m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', raw)
    if m:
        try:
            data = json.loads(m.group(1))
            if "plan" in data:
                return data["plan"]
        except Exception as e:
            logger.warning(f"Code block parse failed: {e}")

    # Try balanced brace extraction (handles nested JSON)
    brace_start = raw.find('{')
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(raw)):
            if raw[i] == '{':
                depth += 1
            elif raw[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(raw[brace_start:i+1])
                        if "plan" in data:
                            return data["plan"]
                    except Exception as e:
                        logger.warning(f"Brace extraction parse failed: {e}")
                    break

    # All parsing failed — return a graceful fallback with the raw content
    logger.error(f"All JSON parsing methods failed. Raw response: {raw[:500]}")
    return {
        "title": "解析异常",
        "type": "other",
        "summary": "AI 助手收到了分析结果但格式解析失败，请重试或换个方式提问。",
        "raw_preview": raw[:300],
        "steps": [],
        "total_est_minutes": 0,
        "risks": [],
        "questions": ["请重新描述需求，AI 助手会再次尝试分析"]
    }

def fmt(p):
    if p.get("type") == "chat":
        return p.get("summary", "")
    lines = [f"📋 {p.get('title','?')}", f"   {p.get('summary','?')}", "", "📝 执行步骤:"]
    for s in p.get("steps",[]):
        action = s.get('action','N/A')
        if not isinstance(action, str): action = json.dumps(action, ensure_ascii=False)
        auto = s.get('auto', True)
        est = s.get('est_minutes', 1)
        lines.append(f"  {s.get('id','?')}. {'🤖' if auto else '👤'} {action} (~{est}min)")
    total = p.get('total_est_minutes', sum(s.get('est_minutes',0) for s in p.get('steps',[])))
    lines.append(f"\n⏱ 预计: {total} 分钟")
    risks = p.get('risks',[])
    if risks and isinstance(risks, list): lines.append(f"\n⚠️ {'; '.join(str(r) for r in risks)}")
    questions = p.get('questions',[])
    if questions and isinstance(questions, list): lines.append(f"\n❓ {'; '.join(str(q) for q in questions)}")
    lines.append("\n---\n回复「确认」开始执行") if p.get("steps") else None
    return "\n".join(lines)

class BotHandler(ChatbotHandler):
    def _is_follow_up(self, prompt: str) -> bool:
        """
        M3: 识别用户消息是否为追问（对上一轮查询的扩展）
        
        判断逻辑:
        1. 消息较短（<30字）且包含追问关键词 → 大概率是追问
        2. 以追问动词开头 → 是追问
        """
        # 追问强信号关键词（出现即判定为追问）
        strong_keywords = [
            "继续下钻", "按", "分别看", "换个维度", "改成按",
            "和", "对比", "同比", "环比", "对照", "比一下",
            "为什么", "为何", "原因", "怎么回事",
            "那", "呢", "怎么样", "另外",
        ]
        
        # 追问弱信号（需结合短文本判断）
        follow_up_starters = [
            "继续", "那", "按", "换", "为什么", "原因",
            "和", "对比", "同比", "环比",
        ]
        
        prompt_stripped = prompt.strip()
        
        # 规则1: 短文本 + 强关键词
        if len(prompt_stripped) <= 30:
            for kw in strong_keywords:
                if kw in prompt_stripped:
                    return True
        
        # 规则2: 以特定追问模式开头
        for starter in follow_up_starters:
            if prompt_stripped.startswith(starter):
                return True
        
        return False

    def _is_confirmation(self, prompt: str) -> bool:
        """
        M4: 识别用户消息是否为对建议查询的确认（如「是」/「好的」）
        
        判断逻辑:
        1. 消息很短（≤5字）
        2. 包含确认关键词
        """
        prompt_stripped = prompt.strip()
        if len(prompt_stripped) > 5:
            return False
        confirmation_keywords = ("1", "OK", "ok", "y", "Y", "yes", "Yes", "YES",
                                 "确认", "执行", "开始", "是", "是的", "好的", "好", "可以")
        for kw in confirmation_keywords:
            if kw == prompt_stripped or kw in prompt_stripped:
                return True
        return False

    async def process(self, message):
        try:
            cbm = ChatbotMessage.from_dict(message.data)

            # === 群聊拒绝：仅保留单聊（去掉本段即可重新开启群聊）===
            conv_type = getattr(cbm, 'conversation_type', None)
            if conv_type == '2' or str(conv_type) == '2':
                logger.info(f"[GroupChat] 群聊消息已拒绝: conv_type={conv_type}, sender={getattr(cbm, 'sender_nick', '?')}")
                try:
                    self.reply_text("暂不支持群聊查询，请与我私聊提问 \U0001F60A", cbm)
                except Exception as e:
                    logger.warning(f"[GroupChat] 回复失败: {e}")
                return AckMessage.STATUS_OK, 'ok'

            # === 消息去重：防止钉钉超时重发 ===
            msg_id = cbm.message_id
            if _is_duplicate_msg(msg_id):
                logger.warning(f"[Dedup] 重复消息已忽略: msg_id={msg_id}, sender={cbm.sender_nick}")
                return AckMessage.STATUS_OK, 'ok'

            prompt = cbm.text.content.strip() if cbm.text else ""

            # ===== Phase 1: Smart routing - LangGraph Agent first, old bot fallback =====
            if prompt:
                try:
                    # L1: 权限身份识别
                    sender_staff_id = cbm.sender_staff_id if hasattr(cbm, 'sender_staff_id') else (cbm.sender_id or "unknown")
                    logger.info(f'[Permission] sender_staff_id={sender_staff_id}, sender_id={cbm.sender_id}, sender_nick={cbm.sender_nick}')
                    from permission_manager import permission_manager
                    user_role = permission_manager.get_user_role(sender_staff_id)
                    if user_role is None:
                        self.reply_text(f"⚠️ 您暂无数据查询权限，请联系管理员开通。\n\n您的ID: {sender_staff_id}\n请将此ID发送给管理员进行授权。", cbm)
                        return AckMessage.STATUS_OK, 'ok'

                    sender_id = cbm.sender_id or sender_staff_id
                    sender_nick = cbm.sender_nick or "用户"

                    # === P2: 重置关键字 → 清除会话，快速返回 ===
                    if prompt.strip() in ("重置", "清除记忆", "重新开始", "清空会话"):
                        try:
                            delete_session(sender_id)
                            save_last_context(sender_id, None)
                            _pending_suggestion_cache.pop(sender_id, None)
                            chat_history.pop(sender_id, None)
                            logger.info(f"[Reset] 用户重置会话: user={sender_id}")
                        except Exception as e:
                            logger.error(f"[Reset] 重置会话失败: {e}")
                        self.reply_text("已重置会话记录，您可以重新开始提问~", cbm)
                        return AckMessage.STATUS_OK, 'ok'

                    # === P2: 反馈词识别 → 记录反馈，快速返回 ===
                    _POSITIVE_FEEDBACK = ("👍", "有用", "好的")
                    _NEGATIVE_FEEDBACK = ("👎", "没用", "不准确")
                    if prompt.strip() in _POSITIVE_FEEDBACK + _NEGATIVE_FEEDBACK:
                        try:
                            feedback_type = "positive" if prompt.strip() in _POSITIVE_FEEDBACK else "negative"
                            # 获取上一轮上下文用于反馈关联
                            fb_context = get_last_context(sender_id)
                            fb_query = fb_context.get('last_analysis', '')[:200] if fb_context else ''
                            fb_analysis = fb_context.get('last_result_summary', '') if fb_context else ''
                            feedback_loop.on_analysis_feedback(
                                user_query=fb_query or prompt,
                                analysis=fb_analysis,
                                feedback=feedback_type
                            )
                            logger.info(f"[Feedback] 用户反馈已记录: user={sender_id}, type={feedback_type}")
                        except Exception as e:
                            logger.warning(f"[Feedback] 反馈记录失败(不影响回复): {e}")
                        self.reply_text("感谢您的反馈，我们会持续优化~", cbm)
                        return AckMessage.STATUS_OK, 'ok'

                    # === M4: 确认词识别 → 若有待确认建议查询，替换prompt；否则友好提示 ===
                    if self._is_confirmation(prompt):
                        pending = get_pending_suggestion(sender_id)
                        if pending:
                            logger.info(f"[M4] 用户确认建议查询，替换 prompt: '{prompt}' → '{pending[:60]}'")
                            prompt = pending
                        else:
                            logger.info(f"[M4] 用户确认词但无待确认建议(已过期): '{prompt}'")
                            self.reply_text("之前的建议已过期，请重新描述您的问题，我来帮您查询~", cbm)
                            return AckMessage.STATUS_OK, 'ok'

                    self.reply_text('📊 已收到，AI分析中，请稍候...', cbm)

                    # M3: 追问识别 + 上下文加载（内存优先，PG持久化回退）
                    last_context = None
                    if self._is_follow_up(prompt):
                        logger.info(f"[M3] 识别为追问: {prompt[:50]}")
                        try:
                            last_context = get_last_context(sender_id)
                            if last_context:
                                logger.info(f"[M3] 内存加载上下文成功: SQL={last_context.get('last_sql', '')[:80]}...")
                            else:
                                # 内存无缓存时，回退到PG持久化会话
                                try:
                                    stored = get_session(sender_id)
                                    if stored:
                                        plan_data, question = stored
                                        if isinstance(plan_data, dict) and plan_data.get('last_sql'):
                                            last_context = plan_data
                                            logger.info(f"[M3] PG回退加载上下文成功: SQL={last_context.get('last_sql', '')[:80]}...")
                                        else:
                                            logger.info("[M3] PG会话非M3格式，作为新查询处理")
                                    else:
                                        logger.info("[M3] PG无会话记录，作为新查询处理")
                                except Exception as pg_e:
                                    logger.warning(f"[M3] PG回退加载失败: {pg_e}")
                        except Exception as e:
                            logger.warning(f"[M3] 加载上下文失败: {e}，作为新查询处理")

                    result = await forward_to_langgraph(prompt, user_role=user_role, last_context=last_context)

                    # 适配元组返回（向后兼容旧版字符串返回）
                    if isinstance(result, tuple):
                        reply, metadata = result
                    else:
                        reply, metadata = result, {}

                    if reply and not reply.startswith('Agent service'):
                        # M3: 从reply中提取SQL并清理标记
                        last_sql = ''
                        if '<!--M3_SQL:' in reply:
                            m3_match = re.search(r'<!--M3_SQL:(.+?)-->', reply)
                            if m3_match:
                                last_sql = m3_match.group(1)
                                reply = reply.replace(m3_match.group(0), '').rstrip()

                        # M3: 保存本轮上下文供下次追问使用（内存 + PG持久化双写）
                        m3_context = {
                            'last_sql': last_sql,
                            'last_result_summary': reply[:200] if reply else '',
                            'last_analysis': reply[:500] if reply else '',
                        }
                        try:
                            save_last_context(sender_id, m3_context)
                        except Exception as e:
                            logger.warning(f"[M3] 内存保存上下文失败: {e}")
                        # PG持久化：将M3上下文存入sessions表，进程重启后可恢复
                        try:
                            save_session(sender_id, sender_nick, m3_context, prompt)
                        except Exception as e:
                            logger.warning(f"[M3] PG持久化上下文失败(不影响主流程): {e}")

                        # M4: 保存待确认建议查询（错误回复中包含「您是否想查询」时）
                        suggested = metadata.get('suggested_query')
                        if suggested:
                            try:
                                save_pending_suggestion(sender_id, suggested)
                            except Exception as e:
                                logger.warning(f"[M4] 保存建议查询失败: {e}")
                        # M4-else: 成功查询后，清除旧的待确认建议
                        elif not reply.startswith('⚠️'):
                            _pending_suggestion_cache.pop(sender_id, None)

                        self.reply_text(reply, cbm)

                        # 数据持久化：保存查询任务到 tasks 表
                        try:
                            plan_task = metadata.get('plan_task', {})
                            query_result = metadata.get('query_result', {})
                            analysis = metadata.get('analysis', '')

                            has_result = query_result.get('success') or plan_task.get('direct_answer')
                            if has_result or plan_task:
                                task_results = [{
                                    'total_rows': query_result.get('row_count', 0),
                                    'success': query_result.get('success', False),
                                    'sql_executed': query_result.get('sql_executed', ''),
                                }]
                                save_task_to_db(
                                    user_id=sender_id,
                                    user_name=sender_nick,
                                    prompt=prompt,
                                    plan=plan_task,
                                    results=task_results
                                )
                                logger.info(f"[Bot] 任务已持久化: user={sender_nick}, intent={plan_task.get('intent', '?')}")
                        except Exception as e:
                            logger.warning(f"[Bot] 任务持久化失败(不影响回复): {e}")

                        return AckMessage.STATUS_OK, 'ok'
                except Exception as e:
                    logger.info(f'Agent fallback to old bot: {e}')

            sender_nick = cbm.sender_nick or "用户"
            sender_id = cbm.sender_id or "unknown"
            logger.info(f"[{sender_nick}] {sender_id}: {prompt[:100]}")

            # Maintain chat history
            if sender_id not in chat_history:
                chat_history[sender_id] = []
            chat_history[sender_id].append({"role": "user", "content": prompt})

            if not prompt:
                self.reply_text("请发送文字消息。", cbm)
                return AckMessage.STATUS_OK, "ok"

            if prompt in ("test","测试","hi","hello","111") or any(k in prompt for k in ("在吗","在","你好")):
                log_task(sender_id, sender_nick, prompt, status="greeting")
                self.reply_text("在的！AI 助手已上线 🎉\n试试发个需求？比如「帮我查一下销售数据」", cbm)
                return AckMessage.STATUS_OK, "ok"

            if prompt in ("重置","清除记忆","忘记","clear"):
                chat_history.pop(sender_id, None)
                delete_session(sender_id)
                self.reply_text("🧹 会话记忆已清除。", cbm)
                return AckMessage.STATUS_OK, "ok"

            if prompt in ("1","OK","ok","y","Y","yes","Yes","YES") or any(k in prompt for k in ("确认","执行","开始","是","是的","好的","好","可以")):
                async with _session_lock:
                    stored = get_session(sender_id)
                plan, original_question = stored if stored else (None, prompt)
                if plan:
                    self.reply_text(f"✅ 计划「{plan.get('title','')}」执行中...", cbm)
                    results = execute_plan(plan)
                    # Format results
                    out = [f"📊 **{plan.get('title','')}** 查询结果:\n"]
                    for r in results:
                        if r["error"]:
                            out.append(f"❌ Step {r['step_id']}: {r['error']}")
                        else:
                            out.append(f"✅ Step {r['step_id']}: {r['total_rows']} 条")
                            col_names = ', '.join(r['cols'])
                            out.append(f"   字段: {col_names}")
                            for row in r['rows'][:10]:
                                out.append(f"   {row}")
                            if r['total_rows'] > 10:
                                out.append(f"   ... 还有 {r['total_rows']-10} 条")

                    # Extract ANALYSIS step instructions for better analysis
                    analysis_instructions = ""
                    for step in plan.get("steps", []):
                        if step.get("action", "").strip().upper() == "ANALYSIS":
                            analysis_instructions = step.get("deliverable", "")
                            break
                    analysis = gen_analysis(
                        plan.get('title', ''),
                        original_question,
                        results
                    )
                    if analysis:
                        out.append("")
                        out.append("📈 **数据分析:**")
                        out.append(analysis)

                    self.reply_text("\n".join(out), cbm)
                    log_task(sender_id, sender_nick, original_question, plan=plan, status="executed")
                    task_id = save_task_to_db(sender_id, sender_nick, original_question, plan, results)

                    # Self-learning: record query patterns for future use
                    try:
                        conn = psycopg2.connect(**PG)
                        cur = conn.cursor()
                        for step in plan.get("steps", []):
                            if step.get("action", "").strip().upper() != "ANALYSIS":
                                sql = step.get("action", "")[:500]
                                # Simple keyword extraction from the question
                                kw = [w.strip() for w in original_question.replace("，",",").split(",") if len(w.strip()) >= 2][:5]
                                # Check if similar pattern exists
                                cur.execute(
                                    "SELECT pattern_id, occurrence_count FROM patterns WHERE %s = ANY(common_sql)",
                                    (sql,)
                                )
                                existing = cur.fetchone()
                                if existing:
                                    cur.execute(
                                        "UPDATE patterns SET occurrence_count = occurrence_count + 1, updated_at = now() WHERE pattern_id = %s",
                                        (existing[0],)
                                    )
                                else:
                                    cur.execute(
                                        "INSERT INTO patterns (pattern_id, trigger_keywords, occurrence_count, common_sql, best_plan_template, created_at, updated_at) VALUES (%s, %s, 1, %s, %s, now(), now())",
                                        (str(uuid.uuid4())[:8], kw, [sql], json.dumps(plan, default=str))
                                    )
                        conn.commit()
                        cur.close()
                        conn.close()
                    except Exception as e:
                        logger.warning(f"Pattern recording failed: {e}")
                else:
                    self.reply_text("没有待确认的计划。请先发一个需求。", cbm)
                return AckMessage.STATUS_OK, "ok"


            self.reply_text("🤔 正在分析你的需求...", cbm)
            plan = gen_plan(prompt, chat_history.get(sender_id, []))
            async with _session_lock:
                save_session(sender_id, sender_nick, plan, prompt)  # store plan + original question
            reply = fmt(plan)
            self.reply_text(reply, cbm)
            chat_history[sender_id].append({"role": "assistant", "content": reply})
            # Keep last 10 messages (5 rounds)
            if len(chat_history[sender_id]) > 10:
                chat_history[sender_id] = chat_history[sender_id][-10:]
            log_task(sender_id, sender_nick, prompt, plan=plan, status="planned")
        except Exception as e:
            logger.error(f"Error: {traceback.format_exc()}")
            try:
                self.reply_text(f"❌ 出错: {e}", cbm)
            except:
                pass
        return AckMessage.STATUS_OK, "ok"

def main():
    client = DingTalkStreamClient(Credential(KEY, SECRET))
    client.register_callback_handler("/v1.0/im/bot/messages/get", BotHandler())
    logger.info("Bot starting...")
    client.start_forever()

if __name__ == "__main__":
    main()
