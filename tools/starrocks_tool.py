"""StarRocks 查询工具 - LangChain Tool 封装

提供StarRocks SQL查询的独立工具函数，可被Query Agent直接调用，
也可用于未来LangChain Tool集成扩展。
"""
import os
import json
import subprocess
import logging
from typing import Optional

from config.agent_config import (
    STARROCKS_PASSWORD, STARROCKS_DB,
    VENV_PYTHON, STARROCKS_QUERY_SCRIPT,
    QUERY_TIMEOUT
)

logger = logging.getLogger(__name__)


def execute_starrocks_query(sql: str, database: str = None) -> dict:
    """
    执行 StarRocks SQL 查询。

    通过调用 starrocks_query_safe.py 脚本执行，该脚本内置：
    - EXPLAIN 预校验（防止危险SQL）
    - 只读约束（只允许SELECT）
    - 超时保护

    Args:
        sql: 要执行的SQL语句
        database: 目标数据库，默认使用配置中的 STARROCKS_DB

    Returns:
        成功: {'success': True, 'cols': [...], 'rows': [...], 'row_count': N}
        失败: {'success': False, 'error': '错误描述'}
    """
    db = database or STARROCKS_DB

    cmd = [
        VENV_PYTHON,
        STARROCKS_QUERY_SCRIPT,
        db,
        sql
    ]
    env = os.environ.copy()
    env['STARROCKS_PASSWORD'] = STARROCKS_PASSWORD

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT,
            env=env
        )

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        # 安全校验拒绝
        if 'REJECTED' in stdout:
            return {'success': False, 'error': f'SQL被安全策略拒绝: {stdout}'}
        if 'EXPLAIN FAILED' in stdout:
            return {'success': False, 'error': f'EXPLAIN校验失败: {stdout}'}

        # 进程错误
        if proc.returncode != 0:
            error_msg = stderr or stdout or f'进程退出码: {proc.returncode}'
            return {'success': False, 'error': error_msg}

        # 解析输出
        return parse_query_output(stdout)

    except subprocess.TimeoutExpired:
        return {'success': False, 'error': f'查询超时({QUERY_TIMEOUT}秒)，请缩小查询范围'}
    except FileNotFoundError:
        return {'success': False, 'error': f'查询脚本不存在: {STARROCKS_QUERY_SCRIPT}'}
    except Exception as e:
        logger.error(f"[StarRocksTool] 执行异常: {e}")
        return {'success': False, 'error': f'执行异常: {str(e)}'}


def parse_query_output(output: str) -> dict:
    """
    解析 starrocks_query_safe.py 的标准输出格式。

    输出格式示例:
        COLS: ['count(distinct user_id)']
        ROWS: 1
        [34786]

    Args:
        output: 脚本标准输出文本

    Returns:
        {'success': True, 'cols': [...], 'rows': [...], 'row_count': N}
    """
    if not output:
        return {'success': True, 'cols': [], 'rows': [], 'row_count': 0}

    lines = output.strip().split('\n')
    cols = []
    rows = []
    row_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith('COLS:'):
            cols_str = line[5:].strip()
            try:
                # 尝试JSON解析（将单引号替换为双引号）
                cols = json.loads(cols_str.replace("'", '"'))
            except (json.JSONDecodeError, ValueError):
                try:
                    cols = eval(cols_str)  # noqa: S307 - 受控输入
                except Exception:
                    cols = [cols_str]

        elif line.startswith('ROWS:'):
            try:
                row_count = int(line[5:].strip())
            except ValueError:
                row_count = 0

        elif line.startswith('[') and not line.startswith('COLS'):
            try:
                row = json.loads(line.replace("'", '"'))
                rows.append(row)
            except (json.JSONDecodeError, ValueError):
                try:
                    row = eval(line)  # noqa: S307 - 受控输入
                    rows.append(row)
                except Exception:
                    logger.warning(f"[StarRocksTool] 无法解析行: {line}")

    return {
        'success': True,
        'cols': cols,
        'rows': rows,
        'row_count': row_count or len(rows)
    }
