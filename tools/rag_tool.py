"""RAG 检索工具 - ChromaDB 向量知识库

提供语义检索能力，供 Analysis Agent 使用。
基于 ChromaDB PersistentClient，数据存储在 knowledge/chroma_db/。
"""
import os
import logging
from typing import Optional

import chromadb

logger = logging.getLogger(__name__)

# 知识库路径：项目根目录/knowledge/chroma_db/
CHROMA_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'knowledge', 'chroma_db'
)


class RAGTool:
    """ChromaDB 向量知识库检索工具"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or CHROMA_DB_PATH
        # 确保存储目录存在
        os.makedirs(self.db_path, exist_ok=True)
        self.client = chromadb.PersistentClient(path=self.db_path)

    def get_or_create_collection(self, name: str = 'enterprise_knowledge'):
        """获取或创建知识库集合"""
        return self.client.get_or_create_collection(
            name=name,
            metadata={"description": "Enterprise数据AI Agent知识库"}
        )

    def search(self, query: str, n_results: int = 3,
               collection_name: str = 'enterprise_knowledge') -> list:
        """
        语义检索相似文档。

        Args:
            query: 检索查询文本
            n_results: 返回结果数量
            collection_name: 集合名称

        Returns:
            格式化的检索结果列表
        """
        try:
            collection = self.get_or_create_collection(collection_name)
            if collection.count() == 0:
                return []
            results = collection.query(
                query_texts=[query],
                n_results=min(n_results, collection.count())
            )
            return self._format_results(results)
        except Exception as e:
            logger.error(f"[RAG] 检索失败: {e}")
            return []

    def add_document(self, doc_id: str, content: str,
                     metadata: dict = None,
                     collection_name: str = 'enterprise_knowledge'):
        """
        添加单个文档到知识库。

        Args:
            doc_id: 文档唯一标识
            content: 文档内容
            metadata: 元数据字典
            collection_name: 集合名称
        """
        try:
            collection = self.get_or_create_collection(collection_name)
            collection.upsert(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata or {}]
            )
        except Exception as e:
            logger.error(f"[RAG] 添加文档失败: {e}")

    def add_documents(self, ids: list, documents: list,
                      metadatas: list = None,
                      collection_name: str = 'enterprise_knowledge'):
        """
        批量添加文档到知识库。

        Args:
            ids: 文档ID列表
            documents: 文档内容列表
            metadatas: 元数据列表
            collection_name: 集合名称
        """
        try:
            collection = self.get_or_create_collection(collection_name)
            collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas or [{}] * len(ids)
            )
        except Exception as e:
            logger.error(f"[RAG] 批量添加文档失败: {e}")

    def delete_document(self, doc_id: str,
                        collection_name: str = 'enterprise_knowledge'):
        """删除指定文档"""
        try:
            collection = self.get_or_create_collection(collection_name)
            collection.delete(ids=[doc_id])
        except Exception as e:
            logger.error(f"[RAG] 删除文档失败: {e}")

    def get_collection_count(self, collection_name: str = 'enterprise_knowledge') -> int:
        """获取集合中的文档数量"""
        try:
            collection = self.get_or_create_collection(collection_name)
            return collection.count()
        except Exception as e:
            logger.error(f"[RAG] 获取文档数量失败: {e}")
            return 0

    def _format_results(self, results) -> list:
        """格式化检索结果为统一结构"""
        formatted = []
        if results and results.get('documents'):
            for i, doc in enumerate(results['documents'][0]):
                formatted.append({
                    'content': doc,
                    'metadata': results['metadatas'][0][i] if results.get('metadatas') else {},
                    'distance': results['distances'][0][i] if results.get('distances') else None
                })
        return formatted


# 全局实例（懒加载，避免import时即连接）
_rag_tool_instance = None


def get_rag_tool() -> RAGTool:
    """获取RAG工具全局实例"""
    global _rag_tool_instance
    if _rag_tool_instance is None:
        _rag_tool_instance = RAGTool()
    return _rag_tool_instance


# 兼容直接 import rag_tool 的用法
rag_tool = RAGTool()
