# -*- coding: utf-8 -*-
"""
数据库自动扫描器 — 连接数据库，自动发现表/字段/分区，生成 data_dictionary.yml

用法:
    python tools/db_scanner.py                    # 扫描并生成到 config/data_dictionary.yml
    python tools/db_scanner.py --output my.yml    # 指定输出路径
    python tools/db_scanner.py --db-type starrocks # 指定数据库类型
"""
import os
import sys
import yaml
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 分区键候选模式（按优先级排列）
PARTITION_KEY_PATTERNS = [
    "server_day", "dt", "dt_utc", "date", "day", "stat_date",
    "created_at", "create_time", "gmt_create",
    "event_date", "report_date", "biz_date", "p_date",
]

# 常见字段名到业务语义的映射
FIELD_SEMANTIC_MAP = {
    "user_id": "用户ID", "uid": "用户ID", "student_id": "学生ID",
    "order_id": "订单ID", "transaction_id": "交易ID",
    "amount": "金额", "price": "价格", "revenue": "收入",
    "city": "城市", "province": "省份", "grade": "年级", "subject": "学科",
    "product_id": "产品ID", "course_id": "课程ID", "book_id": "图书ID",
    "status": "状态", "type": "类型", "source": "来源",
    "event_type": "事件类型", "action": "行为",
    "login_cnt": "登录次数", "active_minutes": "活跃时长",
    "question_cnt": "题目数", "answer_cnt": "答题数",
    "retention_rate": "留存率", "conversion_rate": "转化率",
}


def guess_field_description(field_name: str) -> str:
    """根据字段名猜测业务含义"""
    name_lower = field_name.lower().replace("_", "")
    # 精确匹配
    if field_name.lower() in FIELD_SEMANTIC_MAP:
        return FIELD_SEMANTIC_MAP[field_name.lower()]
    # 模糊匹配
    for key, desc in FIELD_SEMANTIC_MAP.items():
        if key in name_lower:
            return desc
    return ""


def guess_partition_key(fields: list) -> tuple:
    """根据字段名模式推测分区键，返回 (field_name, type)"""
    field_names = [f["name"].lower() for f in fields]
    # 精确匹配优先
    for pattern in PARTITION_KEY_PATTERNS:
        for f in fields:
            if f["name"].lower() == pattern:
                return f["name"], f.get("type", "DATE")
    # 模糊匹配
    for pattern in PARTITION_KEY_PATTERNS:
        for f in fields:
            if pattern in f["name"].lower() and f.get("type", "").upper() in ("DATE", "DATETIME", "TIMESTAMP"):
                return f["name"], f.get("type", "DATE")
    return None, None


def guess_domain(table_name: str) -> str:
    """根据表名推断业务域"""
    name = table_name.lower()
    if any(k in name for k in ("user", "dau", "active", "register", "login")):
        return "用户与活跃"
    if any(k in name for k in ("new_user", "retention", "comeback", "留存")):
        return "新增与留存"
    if any(k in name for k in ("order", "pay", "revenue", "amount", "交易", "付费")):
        return "交易与营收"
    if any(k in name for k in ("book", "图书", "read", "阅读")):
        return "图书业务"
    if any(k in name for k in ("qa", "answer", "question", "答疑", "解题", "essay", "作文")):
        return "答疑与内容"
    if any(k in name for k in ("ad", "advert", "广告", "promotion", "campaign")):
        return "广告与营销"
    if any(k in name for k in ("member", "vip", "会员", "subscribe")):
        return "会员体系"
    if any(k in name for k in ("course", "课程", "class", "tifen", "提分")):
        return "课程与提分"
    return "其他"


def _get_type_category(col_type: str) -> str:
    """判断字段类型的分析类别"""
    t = col_type.upper()
    if any(k in t for k in ("INT", "BIGINT", "TINYINT", "SMALLINT", "MEDIUMINT")):
        return "连续变量（整数）"
    if any(k in t for k in ("DECIMAL", "FLOAT", "DOUBLE", "NUMERIC")):
        return "连续变量（小数）"
    if any(k in t for k in ("VARCHAR", "CHAR", "TEXT", "STRING")):
        return "分类变量"
    if any(k in t for k in ("DATE", "DATETIME", "TIMESTAMP")):
        return "时间维度"
    return "其他"


def scan_database(db_adapter) -> dict:
    """扫描数据库，返回数据字典结构"""
    db_name = os.getenv("DB_NAME", "your_database")

    # 1. 获取所有表
    db_type = os.getenv("DB_TYPE", "mysql").lower()
    if db_type in ("mysql", "starrocks"):
        result = db_adapter.execute("SHOW TABLES")
    else:  # postgresql
        result = db_adapter.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )

    if not result["success"]:
        return {"error": result.get("error", "Failed to list tables")}

    table_names = [row[0] for row in result.get("rows", [])]
    logger.info(f"发现 {len(table_names)} 张表")

    # 2. 逐表扫描字段
    tables = []
    for i, table_name in enumerate(table_names):
        if table_name.startswith("_") or table_name.startswith("tmp_") or table_name.startswith("account_"):
            continue  # skip internal/temp/system tables

        if (i+1) % 20 == 0 or i == 0:
            logger.info(f"  [{i+1}/{len(table_names)}] 扫描中...")

        # 获取字段信息
        if db_type in ("mysql", "starrocks"):
            col_result = db_adapter.execute(f"DESCRIBE {table_name}")
        else:
            col_result = db_adapter.execute(
                f"SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name = '{table_name}' ORDER BY ordinal_position"
            )

        if not col_result["success"]:
            logger.warning(f"    跳过（无法获取字段）: {col_result.get('error')}")
            continue

        fields = []
        for row in col_result.get("rows", []):
            field_name = row[0]
            col_type = row[1] if len(row) > 1 else "VARCHAR"
            fields.append({
                "name": field_name,
                "type": col_type.upper(),
                "description": guess_field_description(field_name),
                "type_category": _get_type_category(col_type),
            })

        if not fields:
            continue

        # 推测分区键
        pk, pk_type = guess_partition_key(fields)
        has_today = False  # 默认 false，除非有明确的分区键模式

        # 估算行数（使用近似值，避免大表COUNT超时）
        row_estimate = ""
        try:
            db_type = os.getenv("DB_TYPE", "mysql").lower()
            if db_type == "starrocks":
                # StarRocks: use information_schema for approximate rows
                db_name = os.getenv("DB_NAME", "")
                approx_sql = ("SELECT TABLE_ROWS FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA = '" + db_name + "' "
                    "AND TABLE_NAME = '" + table_name + "'")
            elif db_type == "postgresql":
                approx_sql = (
                    f"SELECT n_live_tup FROM pg_stat_user_tables "
                    f"WHERE relname = '{table_name}'"
                )
            else:
                # MySQL: SHOW TABLE STATUS
                approx_sql = f"SHOW TABLE STATUS LIKE '{table_name}'"

            count_result = db_adapter.execute(approx_sql)
            if count_result.get("success") and count_result.get("rows"):
                raw = count_result["rows"][0][0]
                if raw is not None:
                    n = int(raw)
                    if n >= 1_000_000:
                        row_estimate = f"约{n//10000}万行"
                    elif n >= 1000:
                        row_estimate = f"约{n//1000}K行"
                    elif n > 0:
                        row_estimate = f"约{n}行"
        except Exception:
            pass  # 行数估算失败不影响主流程

        desc = ""
        if row_estimate:
            desc += row_estimate + "。"

        table_entry = {
            "name": table_name,
            "domain": guess_domain(table_name),
            "role": "未审核",  # 人工审核后改为 必用/辅助/禁用
            "description": desc.strip(),
            "fields": fields,
        }

        if pk:
            table_entry["partition_key"] = pk
            table_entry["partition_type"] = pk_type.upper()

        tables.append(table_entry)

    # 3. 生成全局查询规则
    rules = {
        "date_handling": [
            "DATE 类型分区键: 使用 BETWEEN 和 CURRENT_DATE()（含当日）",
            "DATETIME 类型分区键: 使用半开区间 >= start AND < end",
            "不要对 DATE 类型分区键包裹 DATE() 函数",
        ],
        "sql_safety": [
            "只允许 SELECT 语句",
            "WHERE 条件必须包含分区键",
            "用户去重用 COUNT(DISTINCT user_id)，不用 COUNT(*)",
            "查询时间范围不超过 90 天",
        ],
    }

    return {
        "database": {
            "type": db_type,
            "name": db_name,
        },
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "table_count": len(tables),
        "tables": tables,
        "rules": rules,
        "_note": "role 字段默认为'未审核'，请人工审核后改为'必用'/'辅助'/'禁用'",
    }


def main():
    """CLI 入口"""
    import argparse
    # Auto-load .env from project root
    from dotenv import load_dotenv
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    
    parser = argparse.ArgumentParser(description="数据库自动扫描器")
    parser.add_argument("--output", "-o", default="config/data_dictionary.yml",
                        help="输出 YAML 文件路径")
    parser.add_argument("--db-type", default=None,
                        help="数据库类型 (mysql/starrocks/postgresql)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # 延迟导入适配器（避免循环依赖）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.database_adapter import DatabaseAdapter

    if args.db_type:
        os.environ["DB_TYPE"] = args.db_type

    db = DatabaseAdapter.create()
    logger.info(f"数据库类型: {os.getenv('DB_TYPE')}")
    logger.info(f"数据库地址: {os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}")

    data = scan_database(db)

    if "error" in data:
        logger.error(f"扫描失败: {data['error']}")
        sys.exit(1)

    # 写入 YAML
    output_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), args.output)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(f"# ===================================================================\n")
        f.write(f"# Skadi Data Dictionary — 自动生成\n")
        f.write(f"# 扫描时间: {data['scanned_at']}\n")
        f.write(f"# 数据库: {data['database']['name']}\n")
        f.write(f"# 发现: {data['table_count']} 张表\n")
        f.write(f"# ⚠️ role 默认'未审核'，请人工审核调整\n")
        f.write(f"# ===================================================================\n\n")
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    logger.info(f"\n✅ 完成! {data['table_count']} 张表 → {args.output}")


if __name__ == "__main__":
    main()