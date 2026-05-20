"""数据字典加载器

读取 ``config/data_dictionary.yml`` 并生成 Prompt 注入文本。

设计目标：
1. 用户只需维护一份 YAML 即可描述自己的表结构 / 字段 / 典型查询 / 规则；
2. Plan Agent 启动时自动把 YAML 渲染成 Markdown 注入到 system prompt；
3. 找不到 YAML 时优雅降级（先试 example，再返回空字符串），保证服务不挂。

环境变量：
    DATA_DICTIONARY_PATH  覆盖默认 yml 路径（默认 config/data_dictionary.yml）
"""
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - 友好错误
    yaml = None
    logger.error(
        "PyYAML 未安装，数据字典加载器无法工作。"
        "请执行: pip install PyYAML>=6.0"
    )

_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(_CONFIG_DIR, 'data_dictionary.yml')
EXAMPLE_PATH = os.path.join(_CONFIG_DIR, 'data_dictionary.example.yml')


def load_data_dictionary(path: Optional[str] = None) -> Optional[dict]:
    """加载数据字典 YAML 文件。

    优先级：
        1. 显式传入的 path 参数
        2. 环境变量 DATA_DICTIONARY_PATH
        3. config/data_dictionary.yml
        4. config/data_dictionary.example.yml （fallback）

    Returns:
        解析后的 dict；若全部找不到或解析失败返回 None。
    """
    if yaml is None:
        return None

    target = path or os.getenv('DATA_DICTIONARY_PATH') or DEFAULT_PATH

    if not os.path.exists(target):
        if os.path.exists(EXAMPLE_PATH):
            logger.warning(
                "[data_dictionary] 未找到 %s，使用示例字典 %s",
                target, EXAMPLE_PATH
            )
            target = EXAMPLE_PATH
        else:
            logger.error("[data_dictionary] 未找到任何数据字典文件")
            return None

    try:
        with open(target, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.error("[data_dictionary] 文件内容非 dict: %s", target)
            return None
        logger.info(
            "[data_dictionary] 加载成功: %s (tables=%d)",
            target, len(data.get('tables') or [])
        )
        return data
    except Exception as e:
        logger.exception("[data_dictionary] 解析 YAML 失败: %s (%s)", target, e)
        return None


def generate_prompt_section(dictionary: Optional[dict]) -> str:
    """将数据字典 dict 渲染为 Markdown 文本（用于注入 prompt）。"""
    if not dictionary:
        return ""

    lines = []
    lines.append("## 数据字典\n")

    db_info = dictionary.get('database') or {}
    if db_info:
        lines.append(f"**数据库类型**: {db_info.get('type', 'unknown')}")
        lines.append(f"**数据库名**: {db_info.get('name', 'unknown')}\n")

    tables = dictionary.get('tables') or []
    for i, table in enumerate(tables, 1):
        name = table.get('name', f'table_{i}')
        lines.append(f"### 表{i}: {name}")
        if table.get('description'):
            lines.append(f"**用途**: {table['description']}")

        pk = table.get('partition_key')
        ptype = table.get('partition_type', 'DATE')
        if pk:
            lines.append(f"**分区键**: `{pk}` ({ptype} 类型)")

        if table.get('has_today_data'):
            lines.append(
                "**注意**: 此表包含当天数据，日期上界应使用 `CURRENT_DATE()`（含今天）"
            )

        # 字段表格
        fields = table.get('fields') or []
        if fields:
            lines.append("")
            lines.append("| 字段 | 类型 | 说明 |")
            lines.append("|------|------|------|")
            for fld in fields:
                lines.append(
                    f"| {fld.get('name', '')} "
                    f"| {fld.get('type', '')} "
                    f"| {fld.get('description', '')} |"
                )

        # 基线
        baseline = table.get('baseline')
        if baseline:
            lines.append("")
            lines.append(
                f"**基线参考**: 日均 ~{baseline.get('daily_avg', 'N/A')}"
                f" ({baseline.get('description', '')})"
            )

        # 典型查询
        queries = table.get('typical_queries') or []
        if queries:
            lines.append("")
            lines.append("**典型查询**:")
            for q in queries:
                desc = q.get('description', '')
                sql = (q.get('sql') or '').strip()
                lines.append("```sql")
                if desc:
                    lines.append(f"-- {desc}")
                lines.append(sql)
                lines.append("```")

        lines.append("")  # 表之间空一行

    # 全局规则
    rules = dictionary.get('rules') or {}
    if rules:
        lines.append("## 查询规则")
        lines.append("")
        for category, items in rules.items():
            if not items:
                continue
            lines.append(f"### {category}")
            for item in items:
                lines.append(f"- {item}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def get_data_dictionary_prompt() -> str:
    """一键获取 Markdown 形式的数据字典文本（找不到时返回空串）。"""
    return generate_prompt_section(load_data_dictionary())


def get_table_partition_keys() -> dict:
    """从数据字典抽取 {table_name: partition_key} 映射。

    用于和 ``query_agent.TABLE_PARTITION_KEYS`` 保持兼容；
    Query Agent 可在启动时调用本函数动态填充而非硬编码。
    """
    dictionary = load_data_dictionary() or {}
    mapping = {}
    for tbl in dictionary.get('tables') or []:
        name = tbl.get('name')
        pk = tbl.get('partition_key')
        if name and pk:
            mapping[name] = pk
    return mapping


if __name__ == '__main__':  # pragma: no cover - 手动调试入口
    logging.basicConfig(level=logging.INFO)
    print(get_data_dictionary_prompt())
