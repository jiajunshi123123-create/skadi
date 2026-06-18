# 万唯数据平台 · AI Agent 开发指南

> **目标读者**: AI 编程助手（Codex / Qoder / Cursor 等）
> **项目定位**: 万唯公司 StarRocks 数据仓库的知识中枢与应用平台 —— 涵盖 330 张数据表的分析查询、看板原型、业务卡片和 Python 脚本

---

## 一、项目速览

| 维度 | 详情 |
|------|------|
| **核心引擎** | StarRocks 5.1.0 OLAP 数据库 |
| **主数据库** | `app_prod_db`（330 张表：226 基表 + 86 视图 + 13 业务基表 + 5 维度表） |
| **查询语言** | SQL (MySQL 兼容方言) |
| **脚本语言** | Python 3.9+（pymysql / openpyxl） |
| **看板前端** | 纯 HTML + CSS + JS（dashboard_prototype_v3.html） |
| **密码管理** | 环境变量 `STARROCKS_PASSWORD`，严禁硬编码 |

---

## 二、目录结构

```
workspace/
├── AGENTS.md                          ← 本文件（AI 助手入口）
├── README.md                          ← 人类可读的项目入口
├── .qoder/                            ← IDE 配置与规则（不要手动修改 repowiki/）
│   └── rules/                         ← 项目规则文件（编码/安全/查询规范等）
├── docs/                              ← 项目文档（核心参考）
│   ├── PROJECT_WIKI.md                ← P0：项目架构、连接、核心表、陷阱、最佳实践
│   ├── app_prod_db_表用途目录索引.md   ← P1：330 张表按业务领域分组导航
│   ├── app_prod_db_表结构参考手册.md   ← P1：330 张表的完整 DDL + Demo 数据
│   ├── workspace_organization.md      ← P2：目录结构与维护规范
│   ├── 看板体系综合分析报告.md         ← FVS 看板冗余分析与合并方案
│   └── 27季方案对比评估报告.md
├── dashboards/                        ← 看板原型与 SQL
│   ├── dashboard_prototype_v3.html    ← 当前版本看板原型（28 页面）
│   ├── dashboard_sql_queries.md       ← P0：所有看板组件的完整 SQL 查询
│   └── archive/                       ← 历史版本
├── cards/                             ← 业务分析卡片（合约化结构）
│   ├── books_activation.md            ← 图书销售与激活分析
│   ├── answer_funnel.md               ← 答疑业务漏斗
│   ├── user_retention.md              ← 用户留存分析
│   ├── revenue_risk.md                ← 营收与退款风险
│   ├── tifen_member_health.md         ← 提分智教会员健康
│   ├── ad_performance.md              ← 广告位效果分析
│   └── user_profile.md                ← 用户画像基线
├── sql/                               ← 独立业务审计 SQL 脚本
├── scripts/                           ← Python 分析脚本
├── data/                              ← 数据文件（JSON/CSV/FVS 提取数据）
└── screenshots/                       ← 看板截图
```

---

## 三、AI 助手工作流优先级

**每次启动新 Session，按以下优先级加载上下文**：

| 优先级 | 文件 | 何时加载 |
|:--:|------|------|
| **P0** | `AGENTS.md`（本文件） | 始终 |
| **P0** | `docs/PROJECT_WIKI.md` | 需要了解项目架构、数据库连接、核心表 |
| **P0** | `dashboards/dashboard_sql_queries.md` | 需要看板 SQL 查询时 |
| **P1** | `docs/app_prod_db_表用途目录索引.md` | 需要定位数据表时 |
| **P1** | `docs/app_prod_db_表结构参考手册.md` | 需要确认表字段/DDL 时 |
| **P2** | `cards/` 目录（按需选择） | 分析具体业务问题时 |
| **P2** | `.qoder/rules/` 目录 | 编写代码前确认规范 |

---

## 四、数据库连接（全项目唯一标准）

```python
import pymysql, os

conn = pymysql.connect(
    host='fe-c-76ef85649c2dc193.starrocks.aliyuncs.com',
    port=9030,
    user='WanWei_GJshijiajun',          # ⚠️ 严禁使用 root（仅能看 99/330 表）
    password=os.environ.get("STARROCKS_PASSWORD"),
    database='app_prod_db',
    charset='utf8mb4'
)
```

**关键规则**：
- 密码必须从环境变量获取，**禁止硬编码**
- 优先使用 `app_prod_db` 库，跨库查询可直接 `FROM wanwei_ybk_report.table_name`
- DWD/DWS 视图为日更物化视图，**当天数据可能为空**，查 T-1

---

## 五、核心编码规范

### 5.1 SQL 查询规范

1. **优先用 DWS 汇总表 > DWD 宽表 > ODS 基表**（性能考量）
2. **绝不假设列名**：执行前用 `DESC table_name` 确认字段
3. **大表必加日期过滤**：如 `dws_edu_ubb_bhv_user_book_funnel_status`（3071万行）
4. **排除已删除**：`WHERE deleted = 0`
5. **排除测试数据**：`AND name NOT LIKE '%测试%' AND name NOT LIKE '%test%'`
6. **日期字段**：
   - 埋点类：`server_day`
   - 订单类：`d_date`
   - 用户类：`create_time` / `created_time`

### 5.2 Python 脚本规范

1. 所有脚本通过环境变量获取数据库凭证
2. 导出 UID 时加 `\t` 前缀防止 Excel 科学计数法
3. 使用 `snake_case` 命名文件和函数
4. 放在 `scripts/` 目录，文件名描述功能

### 5.3 文档命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 项目文档 | 中文描述性名称 | `app_prod_db_表结构参考手册.md` |
| SQL 文件 | `snake_case` 英文 | `essay_guidance_unbind_refund.sql` |
| Python 脚本 | `snake_case` 英文 | `fvs_analyzer.py` |
| 看板原型 | `dashboard_prototype_v{N}.html` | `dashboard_prototype_v3.html` |

---

## 六、常见查询陷阱（必读）

| 类别 | ❌ 错误 | ✅ 正确 |
|------|--------|--------|
| 日期列 | `dt` | `server_day`（埋点）/ `d_date`（订单） |
| 事件类型 | `'imp'`/`'click'` | `'public_exposure'`/`'public_click'` |
| 退款表用户 | 直接用 `user_id` | **退款表无 user_id**，JOIN order_date ON order_no |
| 广告ID关联 | 直接 `=` | `CAST(ad_config.id AS STRING) = element_value` |
| UID 导出 | 直接写 CSV | 加 `\t` 前缀防科学计数法 |
| 年级筛选 | 只用 `'六年级'` | `IN ('六年级', '小升初')` |
| 物化视图 | 查当日数据 | DWD/DWS 日更，当日可能为空，查 T-1 |
| 连接用户 | `root` | `WanWei_GJshijiajun` |
| 表名前缀 | `pla` = 平台配置 | ⚠️ `pla` 实为 **plan**（学习计划），不是平台配置 |
| 表名前缀 | `mkt` = 纯营销 | ⚠️ `mkt` 混杂配置/商品/活动/会员变更 |

---

## 七、数据分层架构速查

```
app_prod_db (330张)
├── ODS 层 (226张基表)        ← 业务系统原始数据，ods_ 前缀
│   ├── ods_edu_ubb_bhv_*     — 用户行为埋点 (64张)
│   ├── ods_edu_ubb_usr_*     — 用户体系 (41张)
│   ├── ods_edu_evt_bhv_*     — 听说产品事件 (22张)
│   ├── ods_edu_ubb_smt_*     — 题目/学科/图书 (21张)
│   ├── ods_edu_ubb_ord_*     — 订单交易 (14张)
│   ├── ods_edu_ubb_pla_*     — ⚠️ 学习计划(plan)，非平台配置 (12张)
│   └── ods_edu_ubb_mkt_*     — ⚠️ 混杂：配置/商品/活动 (7张)
├── DIM 层 (5张)              ← 通用维度字典，dim_ 前缀
├── 业务基表 (13张)            ← 非ODS规范的配置/积分/订单表
└── 视图层 (86张)              ← DWD明细/DWS汇总/ADS应用
```

---

## 八、核心业务表速查（最常用 15 张）

| 表名 | 用途 | 行数 |
|------|------|:--:|
| `ods_edu_ubb_usr_app_user_user` | 用户主表（UID/省份/年级/注册时间） | 252万 |
| `ods_edu_ubb_smt_homework_prod_books` | 图书产品主表 | 1594 |
| `ods_edu_ubb_bhv_homework_prod_books_user` | 用户图书激活记录 | 412万 |
| `ods_edu_ubb_bhv_app_answer_question_t_app_session` | 答疑会话记录 | 25.8万 |
| `ods_edu_ubb_bhv_app_answer_question_session_message_record` | 答疑消息明细 | 64.4万 |
| `dwd_edu_mrg_mkt_order_date` | 订单宽表（收入/付费分析核心） | 2.8万 |
| `dwd_edu_mrg_mkt_goods_refund_order` | 退款分析 ⚠️无user_id | 2049 |
| `dws_edu_mrg_revenue_day_wide` | 收入日汇总表 | 1.6万 |
| `dws_edu_ubb_bhv_app_daily_active_user` | 全平台日活（DAU 基准表） | 316万 |
| `dws_edu_ubb_bhv_user_book_funnel_status` | 图书转化漏斗（扫码→激活→付费） | 3071万 |
| `dws_edu_mkt_ad_position_effect_daily` | 广告位效果日表（⭐广告分析首选） | 5688 |
| `dws_edu_tifen_user_active_lifecycle_daily` | 提分智教用户活跃生命周期 | 19万 |
| `dws_edu_ord_daily_rights_expiry_renew_stats` | 权益到期续费统计 | 425 |
| `dws_edu_qna_user_retention_daily` | 答疑用户留存 | 306万 |
| `dws_edu_ubb_bhv_book_right_usernum` | 图书权益用户数 ⚠️仅1行，不可分系列 | 1 |

---

## 九、看板体系概览

当前看板原型 `dashboard_prototype_v3.html` 包含 **28 个页面**，覆盖以下模块：

| 模块 | 页面 | 数据状态 |
|------|------|:--:|
| 经营驾驶舱 | 营收与风控、用户概览 | ✅ 为主 |
| 运营分析 | 流量分析、线索管理、社群运营 | 流量✅ 线索/社群❌CRM |
| 转化路径 | A:图书→付费, B:社群→答疑卡, C:广告→试听, D:续费, E:跨产品升级 | A/C/D✅ B❌ E⚠️ |
| 业务看板 | 广告效果、图书激活、提分智教、答疑全景 | ✅ 为主 |
| 销售分析 | 销售漏斗、销售人效 | 试听✅ 其他❌CRM |
| 服务分析 | 服务健康度、服务质量、续费分析 | ✅ 为主 |
| 数据治理 | 数据监控、告警处理、指标字典 | ✅ 为主 |
| 数据报表 | 日报、周报、月报、专题分析 | ✅ |

---

## 十、常见任务执行指南

### 10.1 执行 SQL 查询

```python
# 从已有脚本模板复制执行
# 参见 dashboard_sql_queries.md 中的 Python 执行模板
# 或直接使用 scripts/ 下已有脚本
```

### 10.2 新增分析卡片

1. 在 `cards/` 下创建新 `.md` 文件
2. 遵循「数据源合约 → 计算合约 → 警戒线 → 诊断路径 → 测试问题 → 运营动作」结构
3. 在 `docs/PROJECT_WIKI.md` 附录中添加入口链接

### 10.3 新增 SQL 脚本

1. 看板组件 SQL → 追加到 `dashboards/dashboard_sql_queries.md`
2. 独立业务 SQL → 放入 `sql/` 目录，使用 `snake_case` 命名

### 10.4 修改看板原型

1. 编辑 `dashboards/dashboard_prototype_v3.html`
2. 同步更新 `dashboards/dashboard_sql_queries.md` 中的 SQL
3. 截图验证 → 放入 `screenshots/`

---

## 十一、关键业务规则

- **图书年级**：仅从 `grade_name` 获取，`'小升初'` 等效 `'六年级'` → `IN ('六年级', '小升初')`
- **答疑权益激活**：`is_start_rights = 1`
- **用户唯一标识**：主用 `user_id`（bigint），部分表用 `yid`（varchar）
- **退款表关联**：`dwd_edu_mrg_mkt_goods_refund_order` 无 user_id → JOIN `dwd_edu_mrg_mkt_order_date` ON order_no
- **解题卡学科映射**：`subject_type` 6=地理, 7=生物
- **组合卡权益**：需双表 JOIN 而非 UNION ALL
- **`dws_edu_ubb_bhv_book_right_usernum`**：仅 1 行汇总数据，不可按 series 拆分

---

## 十二、数据安全

- 所有密码通过 `os.environ.get("STARROCKS_PASSWORD")` 获取
- 导出数据时手机号必须脱敏
- UID 导出时加 `\t` 前缀防止科学计数法
- `credentials.json` 严禁提交到公开仓库

---

*本文档为 AI 编程助手提供项目全局上下文。详细技术细节请查阅 `docs/` 目录下的专项文档。*

---

## 十三、AI 数据分析 Agent (v2.2) — 子项目

> 代码位置: 当前目录 (与数据平台共享仓库)
> 依赖: `C:\Users\PC\Documents\ai_data_analysie(skadi)\_vendor`

### 定位
企业级钉钉群 AI 分析平台：`问题 → 科学方法论 → 数据需求 → SQL → 质控 → 深度分析`

### 6节点 LangGraph 编排
```
analysis_plan → plan → [query | direct_answer] → inspection → analysis → format_output
```

### Agent 模型
| Agent | 模型 | 职责 |
|-------|------|------|
| AnalysisPlanner | deepseek-v4-pro | 17种统计方法推理 + 数据缺口声明 |
| PlanAgent | deepseek-v4-pro | 意图理解 + SQL生成 |
| QueryAgent | deepseek-v4-flash | EXPLAIN校验 + 执行 + 自愈 |
| InspectionAgent | 纯Python | 四维质控(完整性/一致性/时效性/统计) |
| AnalysisAgent | deepseek-v4-pro | 三段式输出(数据→分析→建议) |

### 关键技术栈
LangGraph + DeepSeek API + StarRocks + ChromaDB(RAG) + PostgreSQL(经验库) + DingTalk Stream

### 重要文件
- `agent_orchestrator.py` — 核心编排器
- `analysis_planner.py` — 方法论推理引擎
- `knowledge/statistical_methods.py` — 17种统计方法知识库
- `skills/` — 8种分析技能(可插拔)
- `learning/` — 自学习记忆系统(6种记忆类型)
- `_test_flow.py` — 核心测试入口

### 运行测试
```powershell
$env:PYTHONPATH="C:\Users\PC\Documents\ai_data_analysie(skadi)\_vendor"
Set-Location "D:\codex_projects\skadi"
python -X utf8 _test_flow.py
```

