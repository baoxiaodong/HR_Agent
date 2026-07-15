"""
不初始化大模型的轻量文档查询服务。

列表、详情和权限检查只依赖 SQLAlchemy，因此适合普通页面加载，避免创建嵌入客户端带来
额外延迟。需要上传、内容提取、分块或向量化时，改由 ``EnhancedDocumentService`` 或
``DocumentService`` 处理。
"""
import logging
from typing import List, Optional
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models.document import Document
from app.schemas.user import User as UserSchema

logger = logging.getLogger(__name__)


class BaseDocumentService:
    """提供文档所有权检查和统一 HTTP 错误映射，不初始化模型组件。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_document_with_permission_check(
        self,
        document_id: str,
        current_user: UserSchema
    ) -> Document:
        """读取文档并以数据库所有者和超级管理员标志执行权限检查。

        ``document_id`` 只用于查找，权限以 ORM 的 ``user_id`` 与认证用户比较；超级管理员可
        越过所有者限制。不存在返回 404，存在但无权返回 403。
        """
        document = await self.get_by_id(document_id)

        if not document:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="文档未找到"
            )

        # 检查权限
        if document.user_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足"
            )

        return document

    async def handle_document_service_error(
        self,
        exception: Exception,
        operation: str = "操作"
    ) -> None:
        """把服务异常按未命中、权限、参数和内部错误映射为 HTTP 状态。

        仅 ``ValueError`` 的英文消息片段 ``not found``、``permission denied`` 能映射为 404/403；
        其他 ValueError 返回 400，其余异常返回 500。该方法总是抛出，不返回正常值。
        """
        error_message = str(exception)

        if isinstance(exception, ValueError):
            if "not found" in error_message.lower():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="文档未找到"
                )
            elif "permission denied" in error_message.lower():
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="权限不足"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_message
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{operation}时出错: {error_message}"
            )


class LightweightDocumentService(BaseDocumentService):
    """只执行用户范围内的文档列表、详情和轻量删除查询。"""
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        logger.info("轻量级文档服务已初始化")

    async def get_user_documents(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 100,
        category: Optional[str] = None,
        knowledge_base_id: Optional[UUID] = None
    ) -> List[Document]:
        """执行不初始化 LLM/向量客户端的用户文档分页查询。

        ``user_id`` 始终进入 SQL，分类和知识库 ID 仅继续收窄范围；结果为按创建时间倒序的
        ORM 实体，适合列表页读取，不进行文本提取或向量检索。
        """
        try:
            query = select(Document).where(Document.user_id == user_id)
            
            if category:
                query = query.where(Document.category == category)
            
            if knowledge_base_id:
                query = query.where(Document.knowledge_base_id == knowledge_base_id)
            
            query = query.offset(skip).limit(limit).order_by(desc(Document.created_at))
            
            result = await self.db.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            logger.error(f"获取用户文档时出错: {e}")
            raise

    async def get_by_id(self, document_id: str) -> Optional[Document]:
        """把字符串转为 UUID 后按主键读取，不执行用户权限判断。

        UUID 非法、数据库异常和未找到都返回 ``None``；需要区分权限的调用应使用基类的
        ``get_document_with_permission_check``，不能把本方法直接作为用户可见授权结果。
        """
        try:
            query = select(Document).where(Document.id == UUID(document_id))
            result = await self.db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"获取文档 {document_id} 时出错: {e}")
            return None

    async def delete_document(self, document_id: str) -> bool:
        """仅删除 Document ORM 记录并提交，不清理文件或向量块。

        本方法不接收用户 ID，也不执行所有权检查；调用方必须先完成权限验证。它与增强文档
        服务的完整删除语义不同，若直接使用可能留下磁盘文件和 PGVector 记录。
        """
        try:
            document = await self.get_by_id(document_id)
            if not document:
                return False
            
            await self.db.delete(document)
            await self.db.commit()
            return True
            
        except Exception as e:
            logger.error(f"删除文档 {document_id} 时出错: {e}")
            raise