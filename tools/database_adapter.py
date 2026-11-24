"""
统一数据源适配器
支持 MySQL / PostgreSQL / StarRocks，通过 DB_TYPE 环境变量切换。

使用方式:
    from tools.database_adapter import DatabaseAdapter
    db = DatabaseAdapter.create()       # 根据 DB_TYPE 自动选择
    result = db.execute("SELECT 1")
"""
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional

from utils.sql_safety import check_readonly_sql

logger = logging.getLogger(__name__)

_MYSQL_SQL_MODE = "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION"
_MYSQL_READ_ONLY = "SET SESSION TRANSACTION READ ONLY"
_unsupported_session_guards = set()


class DatabaseAdapter(ABC):
    """数据源适配器基类"""

    @staticmethod
    def create(db_type: str = None) -> 'DatabaseAdapter':
        """工厂方法：根据配置创建对应适配器"""
        db_type = (db_type or os.getenv('DB_TYPE', 'mysql')).lower().strip()
        if db_type == 'mysql':
            return MySQLAdapter()
        elif db_type == 'starrocks':
            return StarRocksAdapter()
        elif db_type in ('postgresql', 'postgres', 'pg'):
            return PostgreSQLAdapter()
        else:
            raise ValueError(f"Unsupported DB_TYPE: {db_type}")

    @abstractmethod
    def execute(self, sql: str, database: str = None, *, allow_metadata: bool = False) -> dict:
        """
        执行SQL，返回统一格式：
        {
            'success': bool,
            'cols': list[str],
            'rows': list[list],
            'row_count': int,
            'error': str (仅失败时)
        }
        """
        pass

    @abstractmethod
    def explain(self, sql: str, database: str = None) -> dict:
        """EXPLAIN预校验，返回 {success: bool, error: str | None}"""
        pass

    def get_max_partition(self, table: str, partition_key: str, database: str = None) -> Optional[str]:
        """获取表最大分区值（空结果自愈用）"""
        sql = f"SELECT MAX({partition_key}) FROM {table}"
        result = self.execute(sql, database)
        if result['success'] and result['rows'] and result['rows'][0][0]:
            return str(result['rows'][0][0])
        return None

    def _get_connection_config(self) -> dict:
        """从环境变量读取连接配置"""
        return {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': int(os.getenv('DB_PORT', self._default_port())),
            'user': os.getenv('DB_USER', ''),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', ''),
        }

    def _default_port(self) -> str:
        """子类可覆盖的默认端口"""
        return '3306'

    @staticmethod
    def _check_sql_safety(sql: str, allow_metadata: bool = False) -> Optional[str]:
        """
        SQL安全检查，只允许单条只读语句。
        allow_metadata=True 时额外放行 SHOW / DESCRIBE（扫描器使用）。
        Returns: 错误消息（如果不安全），None表示通过。
        """
        return check_readonly_sql(sql, allow_metadata=allow_metadata)


# ============================================================
# MySQL Adapter（同时兼容 StarRocks）
# ============================================================

class MySQLAdapter(DatabaseAdapter):
    """MySQL 数据源适配器（StarRocks 兼容 MySQL 协议，共用此适配器）"""

    def _default_port(self) -> str:
        return '3306'

    def execute(self, sql: str, database: str = None, *, allow_metadata: bool = False) -> dict:
        """执行SQL查询"""
        # 安全检查
        safety_err = self._check_sql_safety(sql, allow_metadata=allow_metadata)
        if safety_err:
            return {'success': False, 'error': safety_err, 'cols': [], 'rows': [], 'row_count': 0}

        import pymysql

        config = self._get_connection_config()
        if database:
            config['database'] = database

        conn = None
        try:
            conn = pymysql.connect(
                host=config['host'],
                port=config['port'],
                user=config['user'],
                password=config['password'],
                database=config['database'],
                connect_timeout=30,
                read_timeout=120,
                charset='utf8mb4',
            )
            with conn.cursor() as cursor:
                apply_mysql_readonly_session(cursor)
                cursor.execute(sql)
                cols = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                # 将tuple转为list，并处理特殊类型
                rows = [list(self._normalize_row(row)) for row in rows]
                return {
                    'success': True,
                    'cols': cols,
                    'rows': rows,
                    'row_count': len(rows),
                }
        except pymysql.err.OperationalError as e:
            error_code, error_msg = e.args if len(e.args) == 2 else (0, str(e))
            logger.error(f"[MySQLAdapter] 连接/执行异常 (code={error_code}): {error_msg}")
            return {'success': False, 'error': f"数据库操作错误({error_code}): {error_msg}",
                    'cols': [], 'rows': [], 'row_count': 0}
        except pymysql.err.ProgrammingError as e:
            logger.error(f"[MySQLAdapter] SQL语法错误: {e}")
            return {'success': False, 'error': f"SQL语法错误: {e}",
                    'cols': [], 'rows': [], 'row_count': 0}
        except Exception as e:
            logger.error(f"[MySQLAdapter] 未知异常: {e}")
            return {'success': False, 'error': f"查询执行异常: {e}",
                    'cols': [], 'rows': [], 'row_count': 0}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def explain(self, sql: str, database: str = None) -> dict:
        """EXPLAIN预校验"""
        explain_sql = f"EXPLAIN {sql}"
        result = self.execute(explain_sql, database)
        if result['success']:
            return {'success': True, 'error': None}
        else:
            return {'success': False, 'error': result['error']}

    @staticmethod
    def _normalize_row(row) -> list:
        """将行数据中的特殊类型（datetime/Decimal等）转为可序列化形式"""
        import datetime
        from decimal import Decimal

        normalized = []
        for val in row:
            if isinstance(val, datetime.datetime):
                normalized.append(val.isoformat())
            elif isinstance(val, datetime.date):
                normalized.append(val.isoformat())
            elif isinstance(val, Decimal):
                normalized.append(float(val))
            elif isinstance(val, bytes):
                normalized.append(val.decode('utf-8', errors='replace'))
            else:
                normalized.append(val)
        return normalized


# ============================================================
# PostgreSQL Adapter
# ============================================================

class PostgreSQLAdapter(DatabaseAdapter):
    """PostgreSQL 数据源适配器"""

    def _default_port(self) -> str:
        return '5432'

    def execute(self, sql: str, database: str = None, *, allow_metadata: bool = False) -> dict:
        """执行SQL查询"""
        # 安全检查
        safety_err = self._check_sql_safety(sql, allow_metadata=allow_metadata)
        if safety_err:
            return {'success': False, 'error': safety_err, 'cols': [], 'rows': [], 'row_count': 0}

        import psycopg2

        config = self._get_connection_config()
        if database:
            config['database'] = database

        conn = None
        try:
            conn = psycopg2.connect(
                host=config['host'],
                port=config['port'],
                user=config['user'],
                password=config['password'],
                dbname=config['database'],
                connect_timeout=30,
                options='-c statement_timeout=120000',  # 120秒查询超时
            )
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor() as cursor:
                cursor.execute(sql)
                cols = [desc[0] for desc in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                rows = [list(self._normalize_row(row)) for row in rows]
                return {
                    'success': True,
                    'cols': cols,
                    'rows': rows,
                    'row_count': len(rows),
                }
        except psycopg2.OperationalError as e:
            logger.error(f"[PostgreSQLAdapter] 连接/执行异常: {e}")
            return {'success': False, 'error': f"数据库操作错误: {e}",
                    'cols': [], 'rows': [], 'row_count': 0}
        except psycopg2.ProgrammingError as e:
            logger.error(f"[PostgreSQLAdapter] SQL语法错误: {e}")
            return {'success': False, 'error': f"SQL语法错误: {e}",
                    'cols': [], 'rows': [], 'row_count': 0}
        except Exception as e:
            logger.error(f"[PostgreSQLAdapter] 未知异常: {e}")
            return {'success': False, 'error': f"查询执行异常: {e}",
                    'cols': [], 'rows': [], 'row_count': 0}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def explain(self, sql: str, database: str = None) -> dict:
        """EXPLAIN预校验"""
        explain_sql = f"EXPLAIN {sql}"
        result = self.execute(explain_sql, database)
        if result['success']:
            return {'success': True, 'error': None}
        else:
            return {'success': False, 'error': result['error']}

    @staticmethod
    def _normalize_row(row) -> list:
        """将行数据中的特殊类型转为可序列化形式"""
        import datetime
        from decimal import Decimal

        normalized = []
        for val in row:
            if isinstance(val, datetime.datetime):
                normalized.append(val.isoformat())
            elif isinstance(val, datetime.date):
                normalized.append(val.isoformat())
            elif isinstance(val, Decimal):
                normalized.append(float(val))
            elif isinstance(val, bytes):
                normalized.append(val.decode('utf-8', errors='replace'))
            else:
                normalized.append(val)
        return normalized


# ============================================================
# StarRocks Adapter（继承 MySQL，仅调整默认端口）
# ============================================================

class StarRocksAdapter(MySQLAdapter):
    """StarRocks 适配器 — 兼容 MySQL 协议，默认端口 9030"""

    def _default_port(self) -> str:
        return '9030'


def apply_mysql_readonly_session(cursor) -> bool:
    """为 MySQL / StarRocks 连接尽量打开会话级只读。

    先固定 sql_mode，使引号解析与 utils.sql_safety 一致，再请求只读事务。
    服务端不支持时只记录一次并继续（StarRocks 部分版本没有该语句）。
    返回是否成功执行了 READ ONLY。
    """
    readonly_ok = False
    statements = (
        f"SET SESSION sql_mode = '{_MYSQL_SQL_MODE}'",
        _MYSQL_READ_ONLY,
    )
    for statement in statements:
        try:
            cursor.execute(statement)
            if statement == _MYSQL_READ_ONLY:
                readonly_ok = True
        except Exception as exc:
            if statement not in _unsupported_session_guards:
                _unsupported_session_guards.add(statement)
                logger.warning(
                    "[MySQLAdapter] 无法执行会话防护 %s（将仅依赖 SQL 解析）: %s",
                    statement,
                    exc,
                )
            connection = getattr(cursor, "connection", None)
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
    return readonly_ok
