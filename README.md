# Skadi — AI 数据分析助手

基于 LangGraph + DeepSeek 的企业级数据 AI 分析系统。

## 架构

```
用户输入 → AnalysisPlanner(意图识别+方法论)
         → PlanAgent(任务规划+SQL生成)
         → QueryAgent(SQL执行+自愈)
         → InspectionAgent(数据校验)
         → AnalysisAgent(深度分析+建议)
```

## 核心组件

| 组件 | 说明 |
|------|------|
| `agent_orchestrator.py` | LangGraph StateGraph 6节点编排器 |
| `analysis_planner.py` | 意图识别 + 统计方法论匹配 |
| `agents/plan_agent.py` | 任务规划 + SQL生成 |
| `agents/query_agent.py` | SQL执行 + EXPLAIN校验 + 自愈重试 |
| `agents/analysis_agent.py` | 深度分析 + 趋势/异常检测 |
| `agents/inspection_agent.py` | 数据质量校验 |
| `skills/` | 8个分析方法技能 |
| `knowledge/statistical_methods.py` | 17种统计方法知识库 |
| `learning/` | 自学习系统(PatternStore + Memory) |

## 双通道

- **Web**: FastAPI + SSE 流式, `python web_server.py` 后访问 `localhost:8080`
- **DingTalk Bot**: `python dingtalk_bot.py`

## 部署

```bash
pip install -r requirements.txt
cp .env.example .env  # 填入 API Key、WEB_API_KEY 和只读数据库账号
psql -f scripts/init_db.sql
python web_server.py  # 默认只监听 127.0.0.1:8080
```

浏览器打开 `http://127.0.0.1:8080` 后，在「密钥」里填入 `WEB_API_KEY`。调用示例：

```bash
curl -N -H "X-API-Key: $WEB_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"昨天日活多少","session_id":"s1"}' \
  http://127.0.0.1:8080/api/chat
```

`POST /api/scan` 需要另一个请求头 `X-Admin-Key: $WEB_ADMIN_API_KEY`。

## 数据库

全新库执行 `scripts/init_db.sql`。已经用旧脚本初始化过的 Postgres 卷不会自动改表，请执行 `scripts/migrate.sql`。步骤、旧表改名规则和授权 SQL 写在 `scripts/MIGRATION.md`。

机器人只接受 `user_identities` 里登记、并且 `user_permissions` 仍有效的用户。`user_id` 与 `staff_id` 相同。未登记的人会收到拒绝说明和自己的渠道 ID，不会进入查询。

## 安全相关环境变量

| 变量 | 作用 |
|------|------|
| `WEB_API_KEY` | 访问 `/api/chat`、`/api/skills` 等接口。至少 16 位，且不能是示例占位符。可用逗号分隔多个密钥，每个密钥的会话互不可见 |
| `WEB_ADMIN_API_KEY` | 仅 `/api/scan`。必须与 `WEB_API_KEY` 不同；不配置则扫描接口关闭 |
| `WEB_RATE_LIMIT_PER_MINUTE` | 每个调用方的用户接口限流，默认 30 |
| `WEB_ADMIN_RATE_LIMIT_PER_MINUTE` | 扫描接口限流，默认 5 |
| `WEB_AUTH_FAILURE_LIMIT_PER_MINUTE` | 同一 IP 认证失败次数，默认 10 |
| `BOT_RATE_LIMIT_PER_MINUTE` | 每个钉钉用户每分钟可发的消息数，默认 30 |
| `WEB_BIND_HOST` | `python web_server.py` 的监听地址，默认 `127.0.0.1` |
| `SQL_HOOK_STRICT_MODE` | 默认 `true`。为 true 时 `SELECT *`、未登记表、笛卡尔积等警告也会阻止执行 |
| `PG_PASSWORD` | Compose 启动 Postgres 所必需，至少 12 位，拒绝 `changeme` 等占位符 |
| `DB_USER` | 请使用只有 `SELECT` 权限的账号 |

分析库账号不要用 `root`。Postgres 与 Chroma 在 `docker-compose.yml` 里不再映射到宿主机端口；Web 端口默认只绑定 `127.0.0.1:8080`。容器内进程仍监听 `0.0.0.0`，否则 Docker 端口映射无法转发。

## 技术栈

Python 3.9+ / FastAPI / LangGraph / DeepSeek / StarRocks / PostgreSQL / ChromaDB
