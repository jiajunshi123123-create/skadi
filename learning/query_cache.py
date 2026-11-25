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
    """测试用。生产用 PgQueryCacheRepo。"""

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
        row = self.repo.get(row_id)
        if not row or row.get("status") == "disabled":
            return None
        row["success_cnt"] = int(row.get("success_cnt") or 0) + 1
        if row.get("status") == "candidate" and row["success_cnt"] >= self.success_to_verify:
            row["status"] = "verified"
        self.repo.update(row)
        return row["status"]

    def record_vote(self, row_id: int, polarity: str) -> Optional[str]:
        row = self.repo.get(row_id)
        if not row or row.get("status") == "disabled":
            return row.get("status") if row else None
        if polarity == "positive":
            row["up_cnt"] = int(row.get("up_cnt") or 0) + 1
            row["status"] = "verified"
        else:
            row["down_cnt"] = int(row.get("down_cnt") or 0) + 1
            if row.get("status") == "verified":
                row["status"] = "candidate"
            if row["down_cnt"] >= self.down_threshold:
                row["status"] = "disabled"
        self.repo.update(row)
        return row["status"]

    def _existing_id(self, question: str, role_id: str) -> Optional[int]:
        """同一角色、同一句式里还在用的那一行。优先已验证、成功次数多的。"""
        if not role_id:
            return None
        normalized, _params = normalize_question(question)
        best_id = None
        best_rank = None
        for row in self.repo.list_for_role(role_id):
            if row.get("normalized_q") != normalized or row.get("status") == "disabled":
                continue
            rank = (
                1 if row.get("status") == "verified" else 0,
                int(row.get("success_cnt") or 0),
                int(row.get("id") or 0),
            )
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_id = row.get("id")
        return best_id

    def observe_success(self, question: str, sql: str, role_id: str, tables: list, cache_id: int = None):
        """记下一次成功，返回缓存行 id。

        命中已有行就累加成功次数，不再另插一条 candidate。
        次数达到阈值后 record_success 会把 candidate 升成 verified。
        """
        try:
            if cache_id:
                status = self.record_success(int(cache_id))
                if status:
                    return int(cache_id)
            existing = self._existing_id(question, role_id)
            if existing:
                self.record_success(existing)
                return int(existing)
            return self.remember(question, sql, role_id, tables)
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
