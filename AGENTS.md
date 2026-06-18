# Skadi — AI Agent 开发指南

> 面向 AI 编程助手的项目说明文档

## 项目定位

企业级数据 AI 分析系统，支持自然语言查询数据库并生成统计分析报告。

## 技术架构

- **编排器**: LangGraph StateGraph, 6节点流水线
- **模型**: DeepSeek v4-pro (推理) + v4-flash (执行)
- **数据库**: 支持 StarRocks / MySQL / PostgreSQL (通过 DatabaseAdapter 统一适配)
- **知识库**: ChromaDB (RAG) + PostgreSQL (经验记忆)
- **前端**: FastAPI + SSE 流式 + Vanilla JS

## 目录结构

```
agents/         5个Agent (Plan/Query/Analysis/Inspection/Forward)
skills/         8个分析方法技能
knowledge/      统计方法知识库
learning/       自学习系统 (PatternStore/Memory)
config/         配置和数据字典
tools/          数据库适配器/PG/RAG工具
utils/          上下文压缩/Token估算/工具注册
web/            Web前端 (CSS/JS/HTML)
scripts/        部署脚本
```

## 开发规则

- 数据只读，禁止写操作
- SQL 需经 EXPLAIN 校验
- 分析结论必须基于实际查询数据，禁止凭空推理
- 敏感信息通过 .env 管理，不提交到仓库
