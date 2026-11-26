# Skadi — AI 数据分析助手

基于 LangGraph + DeepSeek 的企业级数据 AI 分析系统。

## 架构

```
用户输入 → AnalysisPlanner(意图识别+方法论)
         → PlanAgent(任务规划+SQL生成)
         → QueryAgent(SQL执行+自愈)
         → 全量 Python 计算（均值、环比、同星期几基线、异常）
         → InspectionAgent(数据校验)
         → AnalysisAgent(只引用计算结果)

周报、月报、分析报告、复盘，以及带「基线 / 异常 / 同比」的问题，会先拆成最多 4 条子查询（本期、近 30 天按日、维度拆解、上一周期）。每条都重新做角色校验，并受总行数和时间上限约束，再合成一份「总结 / 数据来源 / 计算方法 / 详细分析 / 建议」。
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

## 入口

- **Web**: FastAPI + SSE 流式, `python web_server.py` 后访问 `localhost:8080`
- **DingTalk Bot**: `python dingtalk_bot.py`
- **飞书 Bot**: `python -m channels.feishu`（长连接，只处理私聊）

## 飞书机器人

飞书通道只回答私聊。群消息直接丢掉，不回复。用户的 `open_id` 必须先写进 `user_identities`（`channel='feishu'`），并对应一条仍有效的 `user_permissions`，否则会被拒绝。

处理一条私聊时，机器人先发一张「正在分析」的交互卡片，分析在后台进行，完成后用 PATCH 更新同一张卡片。卡片底部有 👍 / 👎，点击后记入现有反馈。

### 在飞书开放平台创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app) 创建企业自建应用，启用「机器人」能力。
2. 在「权限管理」开通下面的权限，然后创建并发布版本。权限不发布就不会生效。
   - `im:message.p2p_msg:readonly`：读取用户发给机器人的单聊消息（事件 `im.message.receive_v1`）
   - `im:message:send_as_bot`：以应用身份发消息
   - `im:message`：获取与发送单聊、群组消息。更新已经发出的卡片（`PATCH /open-apis/im/v1/messages/:message_id`）需要它
3. 「事件与回调」里把订阅方式选成 **使用长连接接收事件 / 回调**。不要同时配请求地址。需要订阅：
   - 事件 `im.message.receive_v1`（接收消息）
   - 回调 `card.action.trigger`（卡片按钮回传）
4. 长连接要求保存订阅时客户端已经在线。先在服务器启动 `python -m channels.feishu`（或 `docker-compose up -d feishu`），再回到开放平台保存订阅。
5. 把应用的 App ID、App Secret 写入 `.env` 的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。不要把这两个值提交进仓库。
6. 把允许使用的人写进经验库。`external_id` 是对方的 `open_id`（形如 `ou_...`），可以先让对方给机器人发一句，再从拒绝回复里复制：

```sql
INSERT INTO user_identities (channel, external_id, user_id, display_name)
VALUES ('feishu', 'ou_xxx', 'u-001', '姓名');
```

`u-001` 必须已经在 `user_permissions` 里，并且角色允许他要查的表。

可用 `FEISHU_WORKERS` 调整同时分析的后台线程数，默认 4。

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
| `WEB_API_KEY_ROLES` | Web 密钥到 `role_id` 的映射，形如 `密钥=analyst`。没有映射的密钥不能查询 |
| `QUERY_CACHE_SIMILARITY` | 查询复用的相似度阈值，默认 0.85。只复用 `verified` 的 SQL，并重新查库 |
| `EMBEDDING_MODEL` | 中文向量模型，默认 `BAAI/bge-small-zh-v1.5`。`EMBEDDING_BACKEND=hash` 时不下载 |
| `WEB_BIND_HOST` | `python web_server.py` 的监听地址，默认 `127.0.0.1` |
| `SQL_HOOK_STRICT_MODE` | 默认 `true`。为 true 时 `SELECT *`、未登记表、笛卡尔积等警告也会阻止执行 |
| `PG_PASSWORD` | Compose 启动 Postgres 所必需，至少 12 位，拒绝 `changeme` 等占位符。旧名 `PGPASSWORD` 仍可读，启动时会警告 |
| `PG_DATABASE` | 经验库库名。旧名 `PG_DB`、`PGDATABASE` 仍可读 |
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | 分析库连接。旧名 `STARROCKS_HOST`、`STARROCKS_USER`、`STARROCKS_PASSWORD`、`STARROCKS_DB` 仍可读 |

Web、钉钉、飞书三个入口在启动时检查各自必需的变量。缺一项或仍是 `.env.example` 里的占位符时，进程直接退出，并把缺的名字一次列出来，不会打印变量的值。Web 还要求 `WEB_API_KEY_ROLES`。

`GET /health` 和 `GET /healthz` 不需要密钥，响应里只有状态和已加载技能数量。钉钉和飞书不监听端口，容器健康检查读 `/tmp/skadi-heartbeat`。
| `DB_USER` | 请使用只有 `SELECT` 权限的账号 |

分析库账号不要用 `root`。Postgres 与 Chroma 在 `docker-compose.yml` 里不再映射到宿主机端口；Web 端口默认只绑定 `127.0.0.1:8080`。容器内进程仍监听 `0.0.0.0`，否则 Docker 端口映射无法转发。

## 技术栈

Python 3.9+ / FastAPI / LangGraph / DeepSeek / StarRocks / PostgreSQL / ChromaDB
