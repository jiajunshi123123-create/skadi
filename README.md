# 万唯数据平台 · 项目 Wiki

> 万唯公司 StarRocks 数据仓库的知识中枢 —— 涵盖 330 张数据表的基础设施文档、业务分析卡片和查询最佳实践。

## 快速入口

| 我想... | 去看 |
|--------|------|
| 🤖 让 AI 编程助手理解项目 | [📘 AGENTS.md](AGENTS.md) |
| 了解项目整体架构、技术栈、连接方式 | [📘 PROJECT_WIKI.md](docs/PROJECT_WIKI.md) |
| 按业务领域找到对应数据表 | [📋 表用途目录索引](docs/app_prod_db_表用途目录索引.md) |
| 查某张表的完整字段和 Demo 数据 | [📖 表结构参考手册](docs/app_prod_db_表结构参考手册.md) |
| 分析具体业务问题（留存/营收/漏斗等） | [📊 业务分析卡片](#业务分析卡片) |

## 文档体系

```
AGENTS.md                ← 🤖 AI 编程助手总入口（Codex/Qoder/Cursor 自动读取）
README.md                ← 项目入口（人类可读）
docs/
├── PROJECT_WIKI.md          ← 项目级入口：架构 + 核心表速查 + 陷阱 + 最佳实践
├── app_prod_db_表用途目录索引.md   ← 按业务领域分组的 330 张表导航
├── app_prod_db_表结构参考手册.md   ← 330 张表完整 DDL + Demo 数据
├── workspace_organization.md ← 目录结构与维护规范
└── 看板体系综合分析报告.md
cards/                         ← 业务分析合约卡片（一卡一主题）
├── books_activation.md        — 图书销售与激活分析
├── answer_funnel.md           — 答疑业务漏斗
├── user_retention.md          — 用户留存分析
├── revenue_risk.md            — 营收与退款风险
├── tifen_member_health.md     — 提分智教会员健康
├── ad_performance.md          — 广告位效果分析
└── user_profile.md            — 用户画像基线
dashboards/
├── dashboard_prototype_v3.html — 看板原型（28 页面）
└── dashboard_sql_queries.md   — 全部组件 SQL 查询
.qoder/rules/                   — 项目规范（编码/安全/查询/文档/提交）
```

## 业务分析卡片

> 业务分析卡片采用「合约化」结构：数据源合约 → 计算合约 → 警戒线 → 诊断路径 → 测试问题 → 运营动作。每张卡片独立可读，按需查阅。

| 卡片 | 说明 |
|------|------|
| [图书销售与激活](cards/books_activation.md) | 图书铺量、激活率、答疑权益开通分析 |
| [答疑业务漏斗](cards/answer_funnel.md) | 答疑会话→付费→退款全链路诊断 |
| [用户留存分析](cards/user_retention.md) | 新老用户留存率、流失周期诊断 |
| [营收与退款风险](cards/revenue_risk.md) | 收入趋势、退款率监控、异常预警 |
| [提分智教会员健康](cards/tifen_member_health.md) | 会员开通/续费/流失、学习活跃度 |
| [广告位效果分析](cards/ad_performance.md) | 各广告位曝光/点击/转化效果 |
| [用户画像基线](cards/user_profile.md) | 用户年级/省份/设备分布基线 |

## 技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 数据仓库 | StarRocks | 5.1.0 |
| 查询工具 | MySQL CLI / pymysql | — |
| 脚本语言 | Python 3.9+ | — |
