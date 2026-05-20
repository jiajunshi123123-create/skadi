# AI Data Agent — Architecture Document

> Version: v1.0 | Last Updated: 2026-05-19

---

## System Overview

AI Data Agent is a LangGraph-based multi-agent system that translates natural language queries into SQL, executes them against a data warehouse, and returns structured analysis results.

## Data Flow

```
User (DingTalk)
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ dingtalk_bot.py                                      │
│ - DingTalk Stream long-lived connection              │
│ - Message reception & acknowledgment                 │
│ - Async forwarding to orchestrator                   │
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│ agent_orchestrator.py (LangGraph StateGraph)         │
│                                                      │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────┐ │
│  │ Plan Agent   │───▶│ Query Agent  │───▶│ Analysis │ │
│  │ (Intent+SQL) │    │ (Execute+Heal)│    │ Agent    │ │
│  └──────────────┘    └──────────────┘    └──────────┘ │
│                                                      │
│  Each node has error fallback for graceful degradation│
└────────────────────┬────────────────────────────────┘
                     │
                     ▼
              DingTalk Reply
          (3-Part Format Output)
```

## Agent Details

### Plan Agent (`agents/plan_agent.py`)

**Responsibility**: Intent classification + SQL generation

**Input**: User's natural language query
**Output**: JSON with `{intent, sql, confidence, reasoning}`

**Key Capabilities**:
- Intent classification (metric_query, trend_analysis, comparison, etc.)
- SQL generation following data dictionary rules
- Table selection based on intent
- Follow-up query context awareness

**Model**: DeepSeek V4 Pro (high reasoning capability)

### Query Agent (`agents/query_agent.py`)

**Responsibility**: SQL execution with self-healing retry

**Input**: Plan Agent output containing SQL
**Output**: `{success, cols, rows, row_count, sql_executed, retries}`

**Key Capabilities**:
- EXPLAIN validation before execution
- Self-healing on failure (up to 2 retries)
- Safety guard (SELECT-only enforcement)
- Timeout handling

**Model**: DeepSeek V4 Flash (fast, low cost)

### Analysis Agent (`agents/analysis_agent.py`)

**Responsibility**: Data interpretation + actionable recommendations

**Input**: Query results + user's original question
**Output**: Three-part formatted text (Data, Analysis, Recommendations)

**Key Capabilities**:
- Baseline comparison with anomaly detection
- Trend analysis (MoM, YoY, periodicity)
- Root cause analysis framework
- Actionable recommendation generation

**Model**: DeepSeek V4 Pro

## Infrastructure

### Data Warehouse: StarRocks
- Read-only access
- OLAP-optimized for analytical queries
- Partition-based queries to prevent full scans
- Connection via MySQL protocol

### Knowledge Base: ChromaDB + PostgreSQL
- **ChromaDB**: Vector embeddings for business glossary retrieval
- **PostgreSQL**: Session persistence, audit logs, pattern storage

### External Integration: DingTalk Stream API
- Long-lived WebSocket connection
- No webhook callback dependency
- Built-in message acknowledgment

## Permission System (4 Layers)

```
Layer 1: Identity Recognition
    ↓ (staff_id verification)
Layer 2: Prompt Injection Prevention
    ↓ (input sanitization)
Layer 3: SQL Hard Validation
    ↓ (EXPLAIN check)
Layer 4: Full Audit Trail
    ↓ (PostgreSQL audit_logs)
```

## Error Handling

Each pipeline node has a fallback path:
- Plan Agent failure → Friendly error message to user
- Query Agent failure → Self-heal retry (2 attempts) → Friendly error
- Analysis Agent failure → Raw data output without analysis

## Performance Characteristics

| Metric | Target | Notes |
|--------|--------|-------|
| End-to-end latency | < 30s | For simple metric queries |
| Complex query latency | < 60s | With self-healing retry |
| SQL self-heal success rate | > 70% | For common error patterns |
| Three-part format compliance | 100% | All responses use the format |

## Design Decisions

1. **DeepSeek over GPT-4**: Better Chinese language support at 1/10th the cost
2. **LangGraph over custom orchestrator**: Declarative state machine with built-in error handling
3. **ChromaDB over Pinecone**: Self-hosted, zero additional cost
4. **PostgreSQL over SQLite**: Production-grade persistence with connection pooling
5. **systemd timer over custom scheduler**: Eliminates zombie process risk, native Ubuntu integration
