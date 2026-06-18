"""工具注册表 (Tool Registry) — 统一工具发现与管理

借鉴 Claude Code 的 tool_registry.py:
- ToolDef 数据类: name + schema + function + metadata
- 注册/查找/执行 统一接口
- read_only 标记 (安全相关)
- concurrent_safe 标记 (并行执行控制)

与 Skills 系统配合：
- Skills 通过 SkillRegistry 发现分析技能
- Tools 通过 ToolRegistry 发现数据操作工具
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Any

logger = logging.getLogger(__name__)


# ============================================================
# ToolDef 数据类
# ============================================================

@dataclass
class ToolDef:
    """工具定义 — 描述一个可调用的工具

    借鉴 Claude Code 的 ToolDef 设计:
    - name: 工具唯一标识
    - description: 工具功能描述
    - func: 工具函数 (callable)
    - schema: 工具参数定义 (JSON Schema 格式)
    - read_only: 只读标记 (安全审计)
    - concurrent_safe: 是否支持并发调用
    - category: 工具分类 (database / knowledge / utility)
    """
    name: str
    description: str
    func: Callable
    schema: dict = field(default_factory=dict)
    read_only: bool = True
    concurrent_safe: bool = True
    category: str = 'utility'
    tags: list = field(default_factory=list)

    @property
    def is_database_tool(self) -> bool:
        """是否为数据库工具"""
        return self.category == 'database'

    @property
    def is_read_only(self) -> bool:
        """是否只读"""
        return self.read_only

    def get_signature(self) -> str:
        """获取工具签名（用于日志/UI展示）"""
        params = ', '.join(self.schema.get('properties', {}).keys())
        return f"{self.name}({params})"

    def to_summary(self) -> dict:
        """生成工具摘要（用于对外暴露）"""
        return {
            'name': self.name,
            'description': self.description,
            'signature': self.get_signature(),
            'read_only': self.read_only,
            'category': self.category,
            'tags': self.tags,
        }


# ============================================================
# ToolRegistry — 工具注册表
# ============================================================

class ToolRegistry:
    """统一工具注册表

    特性:
    - 按名称注册和查找工具
    - 按分类过滤工具
    - 按标签搜索工具
    - 只读/读写工具分类
    - 工具列表摘要输出

    使用方式:
        registry = ToolRegistry()
        registry.register(ToolDef(name='exec_sql', func=my_func, ...))
        tool = registry.get('exec_sql')
        result = tool.func('SELECT 1')
    """

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        """注册一个工具。

        Args:
            tool: ToolDef 实例

        Raises:
            ValueError: 如果工具名已存在
        """
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册")
        self._tools[tool.name] = tool
        logger.info(f"[ToolRegistry] 注册工具: {tool.name} ({tool.category})")

    def unregister(self, name: str) -> bool:
        """注销工具。

        Returns:
            True 如果工具存在并被移除
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> Optional[ToolDef]:
        """按名称查找工具。

        Returns:
            ToolDef 或 None
        """
        return self._tools.get(name)

    def list_all(self) -> list[ToolDef]:
        """列出所有工具。"""
        return list(self._tools.values())

    def list_by_category(self, category: str) -> list[ToolDef]:
        """按分类列出工具。"""
        return [t for t in self._tools.values() if t.category == category]

    def list_read_only(self) -> list[ToolDef]:
        """列出所有只读工具。"""
        return [t for t in self._tools.values() if t.read_only]

    def search_by_tag(self, tag: str) -> list[ToolDef]:
        """按标签搜索工具。"""
        return [t for t in self._tools.values() if tag in t.tags]

    def execute(self, name: str, *args, **kwargs) -> Any:
        """按名称执行工具。

        Args:
            name: 工具名
            *args, **kwargs: 传递给工具函数的参数

        Returns:
            工具执行结果

        Raises:
            KeyError: 工具不存在
        """
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"工具 '{name}' 不存在")

        logger.info(f"[ToolRegistry] 执行工具: {name}")
        return tool.func(*args, **kwargs)

    def execute_safe(self, name: str, *args, **kwargs) -> tuple:
        """安全执行工具（捕获异常）。

        Returns:
            (result, error) 元组，error为None时成功
        """
        try:
            result = self.execute(name, *args, **kwargs)
            return result, None
        except Exception as e:
            logger.error(f"[ToolRegistry] 工具 '{name}' 执行失败: {e}")
            return None, str(e)

    def get_summary(self) -> list[dict]:
        """获取所有工具的摘要信息。"""
        return [t.to_summary() for t in self._tools.values()]

    def get_stats(self) -> dict:
        """获取注册表统计信息。"""
        tools = self._tools.values()
        return {
            'total_tools': len(self._tools),
            'categories': {
                cat: len(list(tools_cat))
                for cat in set(t.category for t in tools)
                if (tools_cat := [t for t in tools if t.category == cat])
            },
            'read_only': len(self.list_read_only()),
            'writable': len(self._tools) - len(self.list_read_only()),
        }

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())

    def __repr__(self) -> str:
        return f'<ToolRegistry: {len(self._tools)} tools>'


# ============================================================
# 全局实例 — 自动注册内置工具
# ============================================================

tool_registry = ToolRegistry()


def register_builtin_tools():
    """注册系统内置工具。

    在应用启动时调用，将已知工具注册到全局注册表中。
    工具函数延迟导入，避免循环依赖。
    """
    # ---- 数据库工具 ----
    try:
        from tools.database_adapter import DatabaseAdapter
        db = DatabaseAdapter.create()

        tool_registry.register(ToolDef(
            name='db_execute',
            description='执行SQL查询并返回结果',
            func=db.execute,
            schema={
                'type': 'object',
                'properties': {
                    'sql': {'type': 'string', 'description': 'SQL查询语句'}
                },
                'required': ['sql']
            },
            read_only=True,
            concurrent_safe=False,
            category='database',
            tags=['sql', 'query', 'starrocks'],
        ))

        tool_registry.register(ToolDef(
            name='db_explain',
            description='执行SQL EXPLAIN预估查询',
            func=db.explain,
            schema={
                'type': 'object',
                'properties': {
                    'sql': {'type': 'string', 'description': 'SQL语句'}
                },
                'required': ['sql']
            },
            read_only=True,
            concurrent_safe=True,
            category='database',
            tags=['sql', 'explain', 'validate'],
        ))

        tool_registry.register(ToolDef(
            name='db_get_max_partition',
            description='获取表的最近分区值',
            func=db.get_max_partition,
            schema={
                'type': 'object',
                'properties': {
                    'table_name': {'type': 'string'},
                    'partition_key': {'type': 'string'}
                },
                'required': ['table_name', 'partition_key']
            },
            read_only=True,
            concurrent_safe=True,
            category='database',
            tags=['sql', 'partition', 'validate'],
        ))
    except Exception as e:
        logger.warning(f"[ToolRegistry] 数据库工具注册失败: {e}")

    # ---- RAG 知识库工具 ----
    try:
        from tools.rag_tool import rag_tool

        tool_registry.register(ToolDef(
            name='rag_search',
            description='从知识库中检索相关文档',
            func=rag_tool.search,
            schema={
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'n_results': {'type': 'integer', 'default': 3}
                },
                'required': ['query']
            },
            read_only=True,
            concurrent_safe=True,
            category='knowledge',
            tags=['rag', 'search', 'chromadb'],
        ))

        tool_registry.register(ToolDef(
            name='rag_add_document',
            description='向知识库中添加文档',
            func=rag_tool.add_document,
            schema={
                'type': 'object',
                'properties': {
                    'doc_id': {'type': 'string'},
                    'content': {'type': 'string'},
                    'metadata': {'type': 'object'}
                },
                'required': ['doc_id', 'content']
            },
            read_only=False,
            concurrent_safe=True,
            category='knowledge',
            tags=['rag', 'write', 'chromadb'],
        ))
    except Exception as e:
        logger.warning(f"[ToolRegistry] RAG工具注册失败: {e}")

    logger.info(f"[ToolRegistry] 内置工具注册完成，共 {len(tool_registry)} 个工具")


# 自动注册（延迟导入，允许部分工具不可用时继续）
# 注释：避免导入时自动执行，由调用方显式调用 register_builtin_tools()
