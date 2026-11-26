"""已验证的查询模板。命中后重新填参数、重新执行，不复用旧结果。

状态：
- candidate：刚记下来，不能跳过规划
- verified：得到过 👍，或自动成功达到阈值
- disabled：踩的次数达到阈值，不再使用

只有角色相同、相似度够、并且这个角色仍能读模板里的全部表，才算命中。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional

from learning.embeddings import embed_text
from learning.time_params import normalize_question, render_sql, templatize_sql

logger = logging.getLogger("skadi.query_cache")


@dataclass
class CacheHit:
    id: int
    sql: str
    tables: list
    score: float


def cosine(left: list, right: list) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


class MemoryQueryCacheRepo:
    """测试用。生产用 PgQueryCacheRepo。

    计数和状态在同一次修改里完成，禁用行不会被后来的成功写回。
    (role_scope, normalized_q) 只有一行，和库上的唯一索引一致。
    """

    def __init__(self):
        self.rows = {}
        self._seq = 0

    def insert(self, row: dict) -> int:
        self._seq += 1
        stored = dict(row)
        stored["id"] = self._seq
        self.rows[self._seq] = stored
        return self._seq

    def get(self, row_id: int) -> Optional[dict]:
        found = self.rows.get(row_id)
        return dict(found) if found else None

    def update(self, row: dict) -> None:
        self.rows[row["id"]] = dict(row)

    def list_for_role(self, role_id: str) -> list:
        return [
            dict(row)
            for row in self.rows.values()
            if row.get("role_scope") == role_id and row.get("status") != "disabled"
        ]

    def _find_key(self, role_id: str, normalized_q: str) -> Optional[dict]:
        for row in self.rows.values():
            if row.get("role_scope") == role_id and row.get("normalized_q") == normalized_q:
                return row
        return None

    def increment_success(self, row_id: int, threshold: int, template: Optional[str] = None) -> Optional[str]:
        row = self.rows.get(row_id)
        if not row or row.get("status") == "disabled":
            return None
        if template is not None and row.get("sql_template") != template:
            return None
        row["success_cnt"] = int(row.get("success_cnt") or 0) + 1
        if row.get("status") == "candidate" and row["success_cnt"] >= threshold:
            row["status"] = "verified"
        return row["status"]

    def apply_vote(self, row_id: int, polarity: str, down_threshold: int) -> Optional[str]:
        row = self.rows.get(row_id)
        if not row:
            return None
        if row.get("status") == "disabled":
            return row["status"]
        if polarity == "positive":
            row["up_cnt"] = int(row.get("up_cnt") or 0) + 1
            row["status"] = "verified"
        else:
            row["down_cnt"] = int(row.get("down_cnt") or 0) + 1
            if row.get("status") == "verified":
                row["status"] = "candidate"
            if row["down_cnt"] >= down_threshold:
                row["status"] = "disabled"
        return row["status"]

    def upsert_success(self, row: dict, threshold: int) -> tuple:
        """同一键已有行：模板一致才加一，不一致则换模板并把计数清零。禁用行保持原样。"""
        existing = self._find_key(row.get("role_scope") or "", row.get("normalized_q") or "")
        if existing is None:
            new_id = self.insert(row)
            return new_id, "candidate"
        if existing.get("status") == "disabled":
            return existing["id"], "disabled"
        if existing.get("sql_template") == row.get("sql_template"):
            existing["success_cnt"] = int(existing.get("success_cnt") or 0) + 1
            if existing.get("status") == "candidate" and existing["success_cnt"] >= threshold:
                existing["status"] = "verified"
            return existing["id"], existing["status"]
        existing["sql_template"] = row.get("sql_template")
        existing["params_template"] = dict(row.get("params_template") or {})
        existing["embedding"] = list(row.get("embedding") or [])
        existing["tables"] = list(row.get("tables") or [])
        existing["success_cnt"] = 1
        existing["up_cnt"] = 0
        existing["down_cnt"] = 0
        existing["status"] = "candidate"
        return existing["id"], existing["status"]


class PgQueryCacheRepo:
    def insert(self, row: dict) -> int:
        from psycopg2.extras import Json

        from config.agent_config import PG_DB, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER

        conn = _connect(PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO query_cache
                        (normalized_q, params_template, embedding, sql_template, role_scope,
                         tables, success_cnt, up_cnt, down_cnt, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        row["normalized_q"],
                        Json(row.get("params_template") or {}),
                        Json(row.get("embedding") or []),
                        row["sql_template"],
                        row["role_scope"],
                        row.get("tables") or [],
                        row.get("success_cnt") or 0,
                        row.get("up_cnt") or 0,
                        row.get("down_cnt") or 0,
                        row.get("status") or "candidate",
                    ),
                )
                new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
        finally:
            conn.close()

    def get(self, row_id: int) -> Optional[dict]:
        return self._one(
            "SELECT id, normalized_q, params_template, embedding, sql_template, role_scope, "
            "tables, success_cnt, up_cnt, down_cnt, status FROM query_cache WHERE id = %s",
            (row_id,),
        )

    def update(self, row: dict) -> None:
        from psycopg2.extras import Json

        from config.agent_config import PG_DB, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER

        conn = _connect(PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE query_cache
                    SET success_cnt = %s, up_cnt = %s, down_cnt = %s, status = %s,
                        embedding = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        row.get("success_cnt") or 0,
                        row.get("up_cnt") or 0,
                        row.get("down_cnt") or 0,
                        row.get("status") or "candidate",
                        Json(row.get("embedding") or []),
                        row["id"],
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def increment_success(self, row_id: int, threshold: int, template: Optional[str] = None) -> Optional[str]:
        """成功次数和状态同一条 UPDATE。禁用行不匹配，不会被旧的成功写回复活。"""
        sql = """
            UPDATE query_cache
            SET success_cnt = success_cnt + 1,
                status = CASE
                    WHEN status = 'candidate' AND success_cnt + 1 >= %s THEN 'verified'
                    ELSE status
                END,
                updated_at = NOW()
            WHERE id = %s AND status <> 'disabled'
        """
        params = [threshold, row_id]
        if template is not None:
            sql += " AND sql_template = %s"
            params.append(template)
        sql += " RETURNING status"
        return self._returning_status(sql, tuple(params))

    def apply_vote(self, row_id: int, polarity: str, down_threshold: int) -> Optional[str]:
        """赞踩的计数和状态放进同一条 UPDATE，避免和成功计数互相覆盖。"""
        status = self._returning_status(
            """
            UPDATE query_cache
            SET up_cnt = up_cnt + CASE WHEN %s = 'positive' THEN 1 ELSE 0 END,
                down_cnt = down_cnt + CASE WHEN %s = 'positive' THEN 0 ELSE 1 END,
                status = CASE
                    WHEN %s = 'positive' THEN 'verified'
                    WHEN down_cnt + 1 >= %s THEN 'disabled'
                    WHEN status = 'verified' THEN 'candidate'
                    ELSE status
                END,
                updated_at = NOW()
            WHERE id = %s AND status <> 'disabled'
            RETURNING status
            """,
            (polarity, polarity, polarity, down_threshold, row_id),
        )
        if status is not None:
            return status
        current = self.get(row_id)
        return current.get("status") if current else None

    def upsert_success(self, row: dict, threshold: int) -> tuple:
        """插入或并入已有行。模板一致才原子加一；不一致则换模板并清零。禁用行不动。"""
        from psycopg2.extras import Json

        from config.agent_config import PG_DB, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER

        conn = _connect(PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO query_cache
                        (normalized_q, params_template, embedding, sql_template, role_scope,
                         tables, success_cnt, up_cnt, down_cnt, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 1, 0, 0, 'candidate')
                    ON CONFLICT (role_scope, normalized_q) DO UPDATE SET
                        success_cnt = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.success_cnt
                            WHEN query_cache.sql_template = EXCLUDED.sql_template
                                THEN query_cache.success_cnt + 1
                            ELSE 1
                        END,
                        up_cnt = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.up_cnt
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.up_cnt
                            ELSE 0
                        END,
                        down_cnt = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.down_cnt
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.down_cnt
                            ELSE 0
                        END,
                        status = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.status
                            WHEN query_cache.sql_template = EXCLUDED.sql_template
                                 AND query_cache.status = 'verified' THEN query_cache.status
                            WHEN query_cache.sql_template = EXCLUDED.sql_template
                                 AND query_cache.success_cnt + 1 >= %s THEN 'verified'
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.status
                            ELSE 'candidate'
                        END,
                        sql_template = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.sql_template
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.sql_template
                            ELSE EXCLUDED.sql_template
                        END,
                        params_template = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.params_template
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.params_template
                            ELSE EXCLUDED.params_template
                        END,
                        embedding = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.embedding
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.embedding
                            ELSE EXCLUDED.embedding
                        END,
                        tables = CASE
                            WHEN query_cache.status = 'disabled' THEN query_cache.tables
                            WHEN query_cache.sql_template = EXCLUDED.sql_template THEN query_cache.tables
                            ELSE EXCLUDED.tables
                        END,
                        updated_at = NOW()
                    RETURNING id, status
                    """,
                    (
                        row["normalized_q"],
                        Json(row.get("params_template") or {}),
                        Json(row.get("embedding") or []),
                        row["sql_template"],
                        row["role_scope"],
                        row.get("tables") or [],
                        threshold,
                    ),
                )
                new_id, status = cur.fetchone()
            conn.commit()
            return new_id, status
        finally:
            conn.close()

    def _returning_status(self, sql: str, params: tuple) -> Optional[str]:
        from config.agent_config import PG_DB, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER

        conn = _connect(PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                found = cur.fetchone()
            conn.commit()
            return found[0] if found else None
        finally:
            conn.close()

    def list_for_role(self, role_id: str) -> list:
        rows = self._all(
            """
            SELECT id, normalized_q, params_template, embedding, sql_template, role_scope,
                   tables, success_cnt, up_cnt, down_cnt, status
            FROM query_cache
            WHERE role_scope = %s AND status <> 'disabled'
            """,
            (role_id,),
        )
        return rows

    def _one(self, sql: str, params: tuple) -> Optional[dict]:
        found = self._all(sql, params)
        return found[0] if found else None

    def _all(self, sql: str, params: tuple) -> list:
        from config.agent_config import PG_DB, PG_HOST, PG_PASSWORD, PG_PORT, PG_USER

        conn = _connect(PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [item[0] for item in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            conn.close()


def _connect(host, port, dbname, user, password):
    import psycopg2

    return psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)


class QueryCache:
    def __init__(
        self,
        repo=None,
        embedder: Optional[Callable[[str], list]] = None,
        threshold: Optional[float] = None,
        success_to_verify: Optional[int] = None,
        down_threshold: Optional[int] = None,
    ):
        self.repo = repo if repo is not None else PgQueryCacheRepo()
        self.embedder = embedder or embed_text
        if threshold is None:
            threshold = _float_env("QUERY_CACHE_SIMILARITY", 0.85)
        self.threshold = threshold
        self.success_to_verify = success_to_verify or _int_env("QUERY_CACHE_SUCCESS_TO_VERIFY", 3)
        self.down_threshold = down_threshold or _int_env("QUERY_CACHE_DOWN_THRESHOLD", 2)

    def remember(self, question: str, sql: str, role_id: str, tables: list) -> Optional[int]:
        if not role_id or not sql:
            return None
        normalized, params = normalize_question(question)
        template = templatize_sql(sql, params.get("time"))
        return self.repo.insert({
            "normalized_q": normalized,
            "params_template": params,
            "embedding": self.embedder(normalized),
            "sql_template": template,
            "role_scope": role_id,
            "tables": [str(table).lower() for table in (tables or [])],
            "success_cnt": 1,
            "up_cnt": 0,
            "down_cnt": 0,
            "status": "candidate",
        })

    def lookup(self, question: str, role_id: str, can_read: Callable[[list], bool]) -> Optional[CacheHit]:
        if not role_id:
            return None
        normalized, params = normalize_question(question)
        vector = self.embedder(normalized)
        best = None
        best_score = 0.0
        for row in self.repo.list_for_role(role_id):
            if row.get("status") != "verified" or row.get("role_scope") != role_id:
                continue
            score = cosine(vector, row.get("embedding") or [])
            if score < self.threshold or score <= best_score:
                continue
            tables = [str(table).lower() for table in (row.get("tables") or [])]
            if not can_read(tables):
                continue
            sql = render_sql(row.get("sql_template") or "", row.get("params_template") or {}, params)
            if not sql:
                continue
            best = CacheHit(id=row["id"], sql=sql, tables=tables, score=score)
            best_score = score
        return best

    def record_success(self, row_id: int) -> Optional[str]:
        try:
            return self.repo.increment_success(int(row_id), self.success_to_verify)
        except Exception:
            logger.exception("累加查询缓存失败 id=%s", row_id)
            return None

    def record_vote(self, row_id: int, polarity: str) -> Optional[str]:
        try:
            return self.repo.apply_vote(int(row_id), polarity, self.down_threshold)
        except Exception:
            logger.exception("记录查询缓存反馈失败 id=%s", row_id)
            return None

    def observe_success(self, question: str, sql: str, role_id: str, tables: list, cache_id: int = None):
        """记下一次成功，返回缓存行 id。

        模板化后的 SQL 和已存模板一致才累加。不一致时换掉模板并把计数清零，
        不把另一张表、另一条 SQL 的成功算进原来那一行。
        同一 (角色, 归一化问题) 只有一行，并发首次成功走 ON CONFLICT，不会插出重复行。
        """
        try:
            if not role_id or not sql:
                return None
            normalized, params = normalize_question(question)
            template = templatize_sql(sql, params.get("time"))
            if cache_id:
                status = self.repo.increment_success(
                    int(cache_id), self.success_to_verify, template=template,
                )
                if status:
                    return int(cache_id)
            row_id, status = self.repo.upsert_success({
                "normalized_q": normalized,
                "params_template": params,
                "embedding": self.embedder(normalized),
                "sql_template": template,
                "role_scope": role_id,
                "tables": [str(table).lower() for table in (tables or [])],
                "success_cnt": 1,
                "up_cnt": 0,
                "down_cnt": 0,
                "status": "candidate",
            }, self.success_to_verify)
            if status == "disabled":
                return None
            return int(row_id) if row_id is not None else None
        except Exception:
            logger.exception("写入查询缓存失败 role=%s", role_id)
            return None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0 < value <= 1 else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


query_cache = QueryCache()
