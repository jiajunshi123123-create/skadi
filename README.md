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
cp .env.example .env  # 填入 API Key 和数据库连接
psql -f scripts/init_db.sql
python -m uvicorn web_server:app --host 0.0.0.0 --port 8080
```

## 技术栈

Python 3.9+ / FastAPI / LangGraph / DeepSeek / StarRocks / PostgreSQL / ChromaDB
