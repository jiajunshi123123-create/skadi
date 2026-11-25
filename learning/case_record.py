"""写入向量库的历史案例只保留问题和 SQL，并带上角色。"""

from __future__ import annotations

from learning.embeddings import case_id


def build_case(role_id: str, question: str, sql: str) -> tuple[str, str, dict]:
    role = (role_id or "").strip()
    content = f"问题: {question or ''}\nSQL: {sql or ''}"
    metadata = {
        "type": "historical_case",
        "role_id": role,
    }
    return case_id(role, question or "", sql or ""), content, metadata
