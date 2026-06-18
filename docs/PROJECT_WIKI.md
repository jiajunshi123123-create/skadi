# 万唯数据平台 · 项目 Wiki

> **最后更新**: 2026-06-06 | **维护者**: AI Agent + 数据团队

---

## 一、项目概述

本项目为万唯公司搭建的数据查询与分析平台，核心为 StarRocks 数据仓库，承载 APP 端用户行为、图书交易、答疑服务、营销活动等全业务线数据。

### 核心技术栈

| 组件 | 技术 | 版本 |
|------|------|------|
| 数据仓库 | StarRocks | 5.1.0 |
| 查询工具 | MySQL CLI / pymysql | — |
| 脚本语言 | Python 3.9+ | — |

### 数据库清单

| 数据库 | 用途 | 表数 |
|--------|------|:--:|
| `app_prod_db` | APP 主库（ODS+DWD+DWS+DIM） | **330** |
| `wanwei_ybk_report` | 报表与埋点 | — |
| `cdp` | 客户数据平台 | — |
| `wanwei_ybk_secret` | 敏感数据 | — |
| `wanwei_marketing_test` | 营销测试 | — |
| `applet_prod_db` | 小程序库 | — |

---

## 二、app_prod_db 数据分层架构

```
app_prod_db (330张)
├── ODS 层 (226张基表) ← 业务系统原始数据，ods_ 前缀
│   ├── ods_edu_ubb_bhv_* (64)  — 用户行为埋点
│   ├── ods_edu_ubb_usr_* (41)  — 用户体系
│   ├── ods_edu_evt_bhv_* (22)  — 听说产品事件行为
│   ├── ods_edu_ubb_smt_* (21)  — 题目/学科/图书
│   ├── ods_edu_ubb_cnt_* (19)  — 内容体系
│   ├── ods_edu_ubb_ord_* (14)  — 订单交易
│   ├── ods_edu_ubb_pla_* (12)  — ⚠️ 学习计划/路径(plan)，非平台配置
│   ├── ods_edu_ubb_mkt_* (7)   — ⚠️ 营销与商品(命名混杂)
│   └── 其他 (26) — 外部系统/消息/ClassIn/应用配置等
├── DIM 层 (5张基表) ← 通用维度字典，dim_ 前缀
│   ├── dim_date (2922行)          — 日期维度
│   ├── dim_device_info_config     — 设备信息配置
│   ├── dim_live_display_config    — 直播展示配置
│   ├── dim_classin_course_mapping — ClassIn课程映射
│   └── ods_edu_dim_activation_code — 激活码维度
├── 业务基表 (13张) ← 非ODS规范的配置/订单/积分表
└── 视图层 (86张) ← 数据加工汇总（DWD明细/DWS汇总/ADS应用）
    ├── 埋点分析 25 | 答疑 8 | 图书 5 | 订单营销 12
    ├── 提分智教 8 | 用户分析 9 | 直播 4 | 知识消息 4
    └── 系统监控 1 | 综合/其他 10
```

> ⚠️ **命名陷阱**: `pla` 实为 **plan**（学习计划/路径），11/12张表为提分智教产品的学习计划、周计划、自主练习数据（高达1.59亿行）；`mkt` 前缀混杂广告配置、首页配置、用户弹窗、商品SPU、大模考、会员变更等不同类型，**无法通过前缀可靠区分业务域**，查询时需逐表确认。

### 表名前缀速查

| 前缀 | 层级 | 含义 |
|------|------|------|
| `ods_edu_ubb_bhv_` | ODS | 用户行为埋点 |
| `ods_edu_ubb_usr_` | ODS | 用户体系 |
| `ods_edu_ubb_smt_` | ODS | 题目/学科/图书 |
| `ods_edu_ubb_ord_` | ODS | 订单交易 |
| `ods_edu_ubb_pla_` | ODS | ⚠️ 学习计划(plan)，非平台配置 |
| `ods_edu_ubb_mkt_` | ODS | ⚠️ 混杂：配置/商品/活动（不可靠） |
| `ods_edu_evt_bhv_` | ODS | 事件行为（听说等产品） |
| `dwd_edu_bhv_maidian_` | DWD | 埋点明细宽表 |
| `dws_edu_bhv_` | DWS | 汇总统计表 |
| `dwd_edu_mrg_mkt_` | DWD | 营销合并宽表 |
| `dim_` | DIM | 维度字典表 |

---

## 三、表结构参考体系（两层查询）

项目维护双层文档体系，实现「业务理解 → 技术细节」的完整查询链路：

| 文档 | 路径 | 规模 | 用途 |
|------|------|:--:|------|
| **表用途目录索引** | `app_prod_db_表用途目录索引.md` | 631行 | 按业务领域分组，快速定位目标表 |
| **表结构参考手册** | `app_prod_db_表结构参考手册.md` | 5448行 | 330张表的完整 DDL + 最新 5 行 demo 数据 |
| **原始 JSON** | `table_reference.json` | 2.7MB | 结构化元数据（可编程读取） |

> **查询流程**: 目录索引（找表→理解业务） → 参考手册（查DDL→确认字段） → 编写SQL

---

## 四、核心业务表速查

### 4.1 图书业务（最常用）

| 表名 | 角色 | 关键字段 |
|------|------|----------|
| `ods_edu_ubb_smt_homework_prod_books` | 图书主表 | id, name, grade_name, study_stage_name |
| `ods_edu_ubb_bhv_homework_prod_books_user` | 用户图书激活 | books_id, yid, is_start_rights, create_time |
| `ods_edu_ubb_usr_app_user_user` | 用户主表 | user_id, province, grade_id, created_time |

**年级筛选规则** ⚠️
- 图书年级**仅从** `grade_name` 获取，不用用户表的 `grade_id`
- `'小升初'` 业务上等效 `'六年级'` → `IN ('六年级', '小升初')`

**答疑权益**: `is_start_rights = 1` 表示已激活

### 4.2 用户体系

| 表名 | 说明 |
|------|------|
| `ods_edu_ubb_usr_app_user_user` | 用户主表（UID/省份/年级ID/注册时间） |
| `ods_edu_ubb_usr_app_user_grade` | 年级变更历史 |
| `ods_edu_ubb_usr_app_user_device` | 设备信息 |
| `ods_edu_ubb_usr_app_user_login_log` | 登录历史 |

### 4.3 订单交易

| 表名 | 行数 | 说明 |
|------|:--:|------|
| `wan_wei_order_t_order` | 5368 | 万唯订单主表 |
| `wan_wei_order_t_order_refund` | — | 退款记录 |
| `wan_wei_order_t_activation_code_record_detail` | 5464 | 激活码明细 |
| `dwd_edu_mrg_mkt_order_date` | MV | 订单宽表 |

> ⚠️ `dwd_edu_mrg_mkt_goods_refund_order` **无 user_id 列**，需 JOIN order_date ON order_no

### 4.4 埋点与广告

| 表名 | 说明 |
|------|------|
| `dws_edu_mkt_ad_position_effect_daily` | 广告位效果日表（⭐推荐优先使用） |
| `dwd_edu_bhv_maidian_user_event_marketing_daily` | 营销事件日表 |
| `ods_edu_ubb_mkt_app_common_ad_info_config` | 广告配置表 |
| `maidian_configuration_index` | 埋点事件标准化配置（50行） |

### 4.5 听说产品埋点

两套命名风格，**数据同源**：
- 历史遗留: `qb_event_log_20260X` / `tingshuo_prod_2407_qb_event_log_20260X`
- 规范命名: `ods_edu_evt_bhv_tingshuo_prod_2407_qb_event_log_20250X`
- `qb` 是源系统前缀，**与千币积分无关**

### 4.6 千币积分

- `account_account_info` — 积分账户表
- `account_record` — 积分流水表

### 4.7 维度表

| 表名 | 行数 | 说明 |
|------|:--:|------|
| `dim_date` | 2922 | 日期维度（年/季/月/周/日/节假日） |
| `dim_device_info_config` | 3342 | 设备品牌/型号/系统 |
| `dim_live_display_config` | 3 | 直播展示配置 |
| `dim_classin_course_mapping` | 34 | ClassIn→万唯课程映射 |
| `configuration_province_index` | 69 | 省份别名映射 |

### 4.8 数据质量标注

> 以下标注核心表已知的数据质量问题，查询时需注意规避。

| 表名 | 问题 | 严重度 | 影响 | 处理方式 |
|------|------|:--:|------|----------|
| `ods_edu_ubb_usr_app_user_user` | `grade_id` 缺失率 ~65% | 🔴 | 年级分析不可靠 | 用行为数据或图书年级补充 |
| `ods_edu_ubb_usr_app_user_user` | `province` 缺失率 ~59% | 🟡 | 省份分布可能偏差 | 标注"已知缺失"，不作为唯一依据 |
| `ods_edu_ubb_mkt_*` | 前缀混杂，表内字段类型不一 | 🟡 | 无法按前缀过滤查找 | 逐表确认实际内容 |
| `ods_edu_ubb_pla_*` | 前缀语义误导(plan非平台) | 🟡 | 新人会误用为配置表 | 查阅时参考目录索引的告警说明 |
| `ods_edu_ubb_bhv_*` 部分表 | 业务描述不准确，为自动生成 | 🟡 | 自动标注可能有误 | 以实际字段为准，描述仅参考 |
| 听说产品埋点月表 | 部分月份数据量极少(<5000) | 🟢 | 特定月份分析不足 | 聚合多个月份或使用 DWS 汇总 |
| `dwd_*/dws_*` 视图 | 日更物化视图，当日可能为空 | 🟡 | 查当日数据返回空 | 查T-1日数据，或等次日刷新 |
| `dwd_edu_mrg_mkt_goods_refund_order` | **无 user_id 列** | 🔴 | 无法直接按用户查退款 | JOIN `dwd_edu_mrg_mkt_order_date` ON order_no |

### 4.9 核心指标警戒线与诊断路径

> 当指标超出阈值时，按路径下钻排查。

| 业务域 | 指标 | 🟢 正常 | 🟡 关注 | 🔴 告警 | 诊断路径 |
|--------|------|:--:|:--:|:--:|----------|
| 图书激活 | 月环比激活率变化 | ±10%内 | >20%下降 | >40%下降 | ①分图书/年级看分布 → ②查渠道铺量 → ③查激活码发放 |
| 图书激活 | 单书激活用户 < 100 | — | 关注 | 告警 | 检查图书状态(deleted)、年级覆盖 |
| 答疑付费 | 付费转化率 | >8% | 5%-8% | <5% | ①付费路径漏斗 → ②退款率 → ③竞品变化 |
| 答疑退款 | 退款率 | <3% | 3%-5% | >5% | ①分套餐/时段 → ②退款原因归类 → ③叠加激活状态 |
| 用户留存 | 次日留存率 | >40% | 30%-40% | <30% | ①分渠道 → ②分年级 → ③分设备 |
| 用户流失 | 周流失率环比 | ±5%内 | >15% | >25% | ①按最后活跃日分层 → ②关联图书/答疑使用 |
| 营收 | 日收入环比 | ±10%内 | >30%下降 | >50%下降 | ①分产品线 → ②分渠道 → ③查是否有活动结束 |
| 会员续费 | 月续费率 | >60% | 40%-60% | <40% | ①到期用户列表 → ②活跃度分层 → ③触达记录 |
| 广告位 | CTR 环比 | ±10%内 | >30%下降 | >50%下降 | ①分广告位 → ②分页面位置 → ③素材变更记录 |

---

## 五、数据库连接

| 参数 | 值 |
|------|-----|
| 主机 | `fe-c-76ef85649c2dc193.starrocks.aliyuncs.com` |
| 端口 | `9030` |
| 用户 | `WanWei_GJshijiajun`（只读） |
| 数据库 | `app_prod_db` |
| 密码 | 环境变量 `STARROCKS_PASSWORD` |

> ⚠️ **严禁使用 root 用户**：root 权限受限，仅能看到 99/330 张表

### 连接示例

```python
import pymysql, os
conn = pymysql.connect(
    host='fe-c-76ef85649c2dc193.starrocks.aliyuncs.com',
    port=9030,
    user='WanWei_GJshijiajun',
    password=os.environ.get("STARROCKS_PASSWORD"),
    database='app_prod_db',
    charset='utf8mb4'
)
```

---

## 六、常见查询陷阱

| 类别 | ❌ 错误 | ✅ 正确 |
|------|--------|--------|
| 日期列 | `dt` | `server_day`（埋点）/ `d_date`（订单） |
| 事件类型 | `'imp'`/`'click'` | `'public_exposure'`/`'public_click'` |
| 退款表用户 | 直接用 `user_id` | **退款表无 user_id**，JOIN order_date |
| 广告ID关联 | 直接 `=` | `CAST(ad_config.id AS STRING) = element_value` |
| UID 导出 | 直接写 CSV | 加 `\t` 前缀防科学计数法 |
| 年级筛选 | 只用 `'六年级'` | `IN ('六年级', '小升初')` |
| 物化视图 | 查当日数据 | DWD/DWS 日更，当日可能为空 |
| 连接用户 | `root` | `WanWei_GJshijiajun` |

### 密码安全

- 所有脚本使用 `os.environ.get("STARROCKS_PASSWORD")`，**禁止硬编码**
- `credentials.json` 含多处明文凭证，**严禁提交到公开仓库**

---

## 七、查询最佳实践

1. **先查目录、再查手册**：目录索引定位业务表 → 参考手册确认字段 → 写 SQL
2. **绝不假设列名**：每次用 `DESC table_name` 或查参考手册确认
3. **优先用 DWS 汇总表**：性能远优于从明细表 SUM
4. **DWS 无用户级过滤**：需回退到 marketing_daily + JOIN 用户表
5. **筛选图书加 `deleted = 0`**：过滤已删除记录
6. **排除测试数据**：`name NOT LIKE '%测试%' AND name NOT LIKE '%test%'`

---

## 附录：常见分析场景快速入口

> 按「我想分析什么 → 第一步 → 第二步」组织，帮助快速找到对应表和卡片。

| 我想分析... | 第一步（查表） | 第二步（查卡片） |
|------------|--------------|----------------|
| 图书销售趋势与激活率 | 目录索引→题目/学科/图书 + 图书视图层 | [图书销售与激活](cards/books_activation.md) |
| 某本书的答疑使用情况 | 目录索引→答疑(DWD/DWS) | [答疑业务漏斗](cards/answer_funnel.md) |
| 用户留存与流失 | 目录索引→用户分析(DWD/DWS) | [用户留存分析](cards/user_retention.md) |
| 营收趋势与退款异常 | 目录索引→订单营销(DWD/DWS) | [营收与退款风险](cards/revenue_risk.md) |
| 提分智教会员活跃度 | 目录索引→提分智教(DWD/DWS) + 学习计划 | [提分智教会员健康](cards/tifen_member_health.md) |
| 广告位 ROI 效果 | 目录索引→埋点分析(DWD/DWS) | [广告位效果分析](cards/ad_performance.md) |
| 用户画像（年级/省份/设备） | 目录索引→用户体系 + 用户分析 | [用户画像基线](cards/user_profile.md) |
| 听说产品用户行为 | 目录索引→事件行为(听说等产品) | — |
| 千币积分流水 | 目录索引→千币积分 | — |
| 直播效果分析 | 目录索引→直播(DWD/DWS) | — |
| 有赞商城数据 | 目录索引→外部系统 | — |

---

## 版本历史

| 日期 | 变更 |
|------|------|
| 2026-06-08 | 新增数据质量标注(4.8)、警戒线与诊断路径(4.9)、常见分析场景快速入口(附录) |
| 2026-06-06 | 初版：项目概述、分层架构、双层查询体系、核心表速查、连接配置、常见陷阱、最佳实践 |
