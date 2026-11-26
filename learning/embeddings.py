"""查询向量。默认使用中文模型，测试和模型缺失时用确定性哈希，不用 Python 内置 hash()。"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger("skadi.embeddings")

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
_model = None
_warned = False


def hash_embed(text: str, dims: int = 128) -> list[float]:
    """同一段文本永远得到同一个向量。不同文本的余弦相似度接近 0。"""
    digest = hashlib.sha256((text or "").encode("utf-8")).digest()
    raw = (digest * ((dims + len(digest) - 1) // len(digest)))[:dims]
    return [1.0 if byte % 2 == 0 else -1.0 for byte in raw]


def embed_text(text: str) -> list[float]:
    backend = os.getenv("EMBEDDING_BACKEND", "auto").strip().lower() or "auto"
    if backend == "hash":
        return hash_embed(text)
    if backend in {"auto", "sentence-transformers", "bge"}:
        try:
            return _bge_embed(text)
        except Exception:
            if backend != "auto":
                raise
            global _warned
            if not _warned:
                logger.exception(
                    "中文向量模型不可用，暂时使用确定性哈希。安装 sentence-transformers 并允许下载 %s 后会自动改用模型。",
                    os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL),
                )
                _warned = True
            return hash_embed(text)
    return hash_embed(text)


def _bge_embed(text: str) -> list[float]:
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        name = os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        _model = SentenceTransformer(name)
    vector = _model.encode([text or ""], normalize_embeddings=True)[0]
    return [float(value) for value in vector]


def case_id(role_id: str, question: str, sql: str) -> str:
    payload = f"{role_id or ''}\n{question or ''}\n{sql or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
