"""
知识助手用例的门面服务。

它把 API 层与文档查询、RAG 问答隔开：配置和文档列表走轻量服务，问题则在规范化知识库
UUID 后委托 ``RAGService`` 流式生成。这里统一产出事件字典，SSE 字符串编码和会话消息
持久化由端点层负责。
"""
import json
import logging
from typing import Any, List, Dict, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.services.enhanced_document_service import EnhancedDocumentService
from app.services.lightweight_document_service import LightweightDocumentService
from app.services.rag_service import RAGService
from app.core.config import settings
from app.schemas.user import User as UserSchema

logger = logging.getLogger(__name__)


class KnowledgeAssistantService:
    """在轻量文档查询与 RAG 流式问答之上提供稳定的 API 用例边界。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.enhanced_document_service = EnhancedDocumentService(db)
        self.lightweight_document_service = LightweightDocumentService(db)
        self.rag_service = RAGService(db)

    async def get_config(self) -> Dict[str, Any]:
        """返回前端可见的知识助手运行限制。

        配置直接来自进程设置，不读取数据库，也不会暴露模型密钥、服务地址等内部配置。
        当前仅公开 ``CONTEXT_LIMIT``，由端点作为稳定 JSON 字段返回。
        """
        logger.info("获取知识助手配置")
        return {
            "context_limit": settings.CONTEXT_LIMIT
        }


    async def ask_question_stream(
        self,
        question: str,
        user_id: str,
        knowledge_base_id: Optional[str] = None,
        context_limit: int = 10,
        conversation_history: Optional[List[Dict]] = None
    ):
        """规范化知识库过滤器后，转发 RAG 流式事件。

        ``user_id`` 应来自认证用户。知识库 ID 非法时当前实现降级为 ``None``，查询范围会从
        “指定知识库”扩大为“该用户全部可检索文档”，但仍由 RAG 服务按用户隔离。会话历史
        只作为生成上下文，不作为权限依据。异常转换为单个 error 事件，不向上抛出。
        """
        logger.info(f"用户 {user_id} 提问: {question}")
        
        try:
            # RAG 层接收 UUID；非法字符串被视为未指定过滤器，而不是 400。
            kb_id = None
            if knowledge_base_id:
                try:
                    kb_id = UUID(knowledge_base_id)
                except (ValueError, TypeError):
                    logger.warning(f"无效的知识库ID格式: {knowledge_base_id}")
                    pass  # 如果UUID无效则使用None
            
            # 历史沿用 API 传入的 role/content 字典；空值统一为无历史。
            conv_history = conversation_history or []
            
            # RAG 服务负责检索、重排和模型回答，本门面不保存会话或提交事务。
            async for chunk in self.rag_service.ask_question_stream(
                question=question,
                user_id=user_id,
                knowledge_base_id=kb_id,
                conversation_history=conv_history,
                context_limit=context_limit
            ):
                yield chunk
                
        except Exception as e:
            logger.error(f"生成答案时发生错误: {str(e)}")
            error_data = {
                "type": "error",
                "error": str(e)
            }
            yield error_data

    async def get_documents(
        self,
        user_id: str,
        knowledge_base_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 20
    ) -> Dict[str, Any]:
        """分页读取当前用户文档并把 ORM 字段转换为 JSON 友好字典。

        指定的知识库 ID 非法时返回 400，与问答流的宽松降级不同。轻量服务的 SQL 始终包含
        ``user_id``；UUID 和时间转换为字符串，列表空值补为安全默认值。返回的 ``total`` 是
        本页结果数量，不是满足条件的数据库总数。
        """
        logger.info(f"用户 {user_id} 获取文档列表")
        
        try:
            # 列表接口需要明确过滤语义，因此非法知识库 ID 直接拒绝，不扩大查询范围。
            kb_id = None
            if knowledge_base_id:
                try:
                    kb_id = UUID(knowledge_base_id)
                except (ValueError, TypeError):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="无效的知识库ID格式"
                    )
            
            # 使用轻量级服务获取文档列表（无需LLM初始化）
            documents = await self.lightweight_document_service.get_user_documents(
                user_id=user_id,
                knowledge_base_id=kb_id,
                skip=skip,
                limit=limit
            )
            
            # 将SQLAlchemy模型转换为字典以进行序列化
            documents_data = []
            for doc in documents:
                doc_dict = {
                    "id": str(doc.id),
                    "filename": doc.filename,
                    "original_filename": doc.original_filename,
                    "file_path": doc.file_path,
                    "file_size": doc.file_size,
                    "file_hash": doc.file_hash,
                    "mime_type": doc.mime_type,
                    "extracted_content": doc.extracted_content,
                    "category": doc.category,
                    "tags": doc.tags or [],
                    "knowledge_base_id": str(doc.knowledge_base_id) if doc.knowledge_base_id else None,
                    "user_id": str(doc.user_id),
                    "meta_data": doc.meta_data or {},
                    "created_at": doc.created_at.isoformat() if doc.created_at else None,
                    "updated_at": doc.updated_at.isoformat() if doc.updated_at else None
                }
                documents_data.append(doc_dict)
            
            logger.info(f"获取到 {len(documents_data)} 个文档")
            return {
                "documents": documents_data,
                "total": len(documents_data)
            }
            
        except HTTPException:
            raise  # 重新抛出HTTP异常
        except Exception as e:
            logger.error(f"获取文档列表时发生错误: {str(e)}")
            raise Exception(f"获取文档列表错误: {str(e)}")
