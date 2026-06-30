"""
用于管理知识库和常见问题的知识库服务
"""
import logging
from typing import List, Optional, Dict, Any, Union
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc, func
from fastapi import HTTPException, status

from app.models.knowledge_base import KnowledgeBase, FAQ
from app.models.document import Document
from app.models.user import Role, UserRoleAssociation
from app.schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseUpdate, FAQCreate, FAQUpdate
from app.schemas.user import User as UserSchema

logger = logging.getLogger(__name__)


class KnowledgeBaseService:
    """用于管理知识库和常见问题的服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_knowledge_base(
        self,
        kb_data: KnowledgeBaseCreate
    ) -> KnowledgeBase:
        """创建新的知识库"""
        try:
            knowledge_base = KnowledgeBase(
                name=kb_data.name,
                description=kb_data.description,
                is_public=kb_data.is_public,
                is_searchable=kb_data.is_searchable,
                category=kb_data.category,
                tags=kb_data.tags or [],
                meta_data=kb_data.meta_data or {}
            )

            self.db.add(knowledge_base)
            await self.db.commit()
            await self.db.refresh(knowledge_base)

            logger.info(f"创建了知识库 {knowledge_base.id}")
            return knowledge_base

        except Exception as e:
            await self.db.rollback()
            logger.error(f"创建知识库时出错: {e}")
            raise

    async def get_knowledge_base(self, kb_id: Union[UUID, str]) -> Optional[KnowledgeBase]:
        """通过ID获取知识库"""
        try:
            # 如果是字符串，转换为UUID
            if isinstance(kb_id, str):
                try:
                    kb_uuid = UUID(kb_id)
                except ValueError:
                    logger.error(f"无效的UUID格式: {kb_id}")
                    return None
            else:
                kb_uuid = kb_id
                
            query = select(KnowledgeBase).where(KnowledgeBase.id == kb_uuid)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"获取知识库 {kb_id} 时出错: {e}")
            raise

    async def get_knowledge_bases(
        self,
        skip: int = 0,
        limit: int = 20,
        is_public: Optional[bool] = None,
        category: Optional[str] = None
    ) -> List[KnowledgeBase]:
        """获取知识库并可选择过滤"""
        try:
            query = select(KnowledgeBase)

            if is_public is not None:
                query = query.where(KnowledgeBase.is_public == is_public)

            if category:
                query = query.where(KnowledgeBase.category == category)

            query = query.order_by(desc(KnowledgeBase.created_at)).offset(skip).limit(limit)

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取知识库时出错: {e}")
            raise

    async def get_accessible_knowledge_bases(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20
    ) -> List[KnowledgeBase]:
        """获取用户可访问的知识库"""
        try:
            # 目前，返回所有公共知识库
            # 未来，这可能包括用户特定的访问控制
            query = select(KnowledgeBase).where(
                KnowledgeBase.is_public == True
            ).order_by(desc(KnowledgeBase.created_at)).offset(skip).limit(limit)

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取用户 {user_id} 可访问的知识库时出错: {e}")
            raise



    async def update_knowledge_base(
        self,
        kb_id: UUID,
        kb_data: KnowledgeBaseUpdate
    ) -> Optional[KnowledgeBase]:
        """更新知识库"""
        try:
            # 检查知识库是否存在
            kb = await self.get_knowledge_base(kb_id)
            if not kb:
                return None

            update_data = kb_data.dict(exclude_unset=True)
            if update_data:
                query = (
                    update(KnowledgeBase)
                    .where(KnowledgeBase.id == kb_id)
                    .values(**update_data)
                )
                await self.db.execute(query)
                await self.db.commit()
                await self.db.refresh(kb)

            logger.info(f"更新了知识库 {kb_id}")
            return kb

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新知识库 {kb_id} 时出错: {e}")
            raise

    async def delete_knowledge_base(self, kb_id: UUID) -> bool:
        """删除知识库并更新相关文档"""
        try:
            # 检查知识库是否存在
            kb = await self.get_knowledge_base(kb_id)
            if not kb:
                return False

            # 更新文档以移除知识库引用
            await self.db.execute(
                update(Document)
                .where(Document.knowledge_base_id == kb_id)
                .values(knowledge_base_id=None)
            )

            # 删除相关常见问题
            await self.db.execute(
                delete(FAQ).where(FAQ.knowledge_base_id == kb_id)
            )

            # 删除知识库
            await self.db.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)
            )

            await self.db.commit()
            logger.info(f"删除了知识库 {kb_id}")
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除知识库 {kb_id} 时出错: {e}")
            raise

    async def search_knowledge_base(
        self,
        kb_id: UUID,
        query: str,
        limit: int = 10
    ) -> Dict[str, Any]:
        """在特定知识库中搜索"""
        try:
            # 获取知识库
            kb = await self.get_knowledge_base(kb_id)
            if not kb:
                return {"documents": [], "faqs": []}

            # 搜索文档
            doc_query = (
                select(Document)
                .where(
                    Document.knowledge_base_id == kb_id,
                    Document.extracted_content.ilike(f"%{query}%")
                )
                .limit(limit)
            )
            doc_result = await self.db.execute(doc_query)
            documents = doc_result.scalars().all()

            # 搜索常见问题
            faq_query = (
                select(FAQ)
                .where(
                    FAQ.knowledge_base_id == kb_id,
                    (FAQ.question.ilike(f"%{query}%") | FAQ.answer.ilike(f"%{query}%"))
                )
                .limit(limit)
            )
            faq_result = await self.db.execute(faq_query)
            faqs = faq_result.scalars().all()

            return {
                "knowledge_base": {
                    "id": str(kb.id),
                    "name": kb.name,
                    "description": kb.description
                },
                "documents": [
                    {
                        "id": str(doc.id),
                        "filename": doc.filename,
                        "content": doc.extracted_content[:300]
                    }
                    for doc in documents
                ],
                "faqs": [
                    {
                        "id": str(faq.id),
                        "question": faq.question,
                        "answer": faq.answer,
                        "category": faq.category
                    }
                    for faq in faqs
                ]
            }

        except Exception as e:
            logger.error(f"搜索知识库 {kb_id} 时出错: {e}")
            raise

    async def get_knowledge_base_stats(self, kb_id: UUID) -> Dict[str, Any]:
        """获取知识库统计信息"""
        try:
            # 获取文档数量
            doc_count_query = select(func.count(Document.id)).where(
                Document.knowledge_base_id == kb_id
            )
            doc_count_result = await self.db.execute(doc_count_query)
            doc_count = doc_count_result.scalar()

            # 获取常见问题数量
            faq_count_query = select(func.count(FAQ.id)).where(
                FAQ.knowledge_base_id == kb_id
            )
            faq_count_result = await self.db.execute(faq_count_query)
            faq_count = faq_count_result.scalar()

            # 更新知识库文档数量
            await self.db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(document_count=doc_count)
            )
            await self.db.commit()

            return {
                "document_count": doc_count,
                "faq_count": faq_count
            }

        except Exception as e:
            logger.error(f"获取知识库统计信息 {kb_id} 时出错: {e}")
            raise

    # 常见问题管理
    async def create_faq(
        self,
        faq_data: FAQCreate,
        knowledge_base_id: Optional[UUID] = None
    ) -> FAQ:
        """创建新的常见问题"""
        try:
            faq = FAQ(
                knowledge_base_id=knowledge_base_id,
                question=faq_data.question,
                answer=faq_data.answer,
                category=faq_data.category,
                tags=faq_data.tags or [],
                meta_data=faq_data.metadata or {}
            )

            self.db.add(faq)
            await self.db.commit()
            await self.db.refresh(faq)

            logger.info(f"创建了常见问题 {faq.id}")
            return faq

        except Exception as e:
            await self.db.rollback()
            logger.error(f"创建常见问题时出错: {e}")
            raise

    async def get_faq(self, faq_id: UUID) -> Optional[FAQ]:
        """通过ID获取常见问题"""
        try:
            query = select(FAQ).where(FAQ.id == faq_id)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"获取常见问题 {faq_id} 时出错: {e}")
            raise

    async def get_faqs(
        self,
        skip: int = 0,
        limit: int = 20,
        knowledge_base_id: Optional[UUID] = None,
        category: Optional[str] = None
    ) -> List[FAQ]:
        """获取常见问题并可选择过滤"""
        try:
            query = select(FAQ)

            if knowledge_base_id:
                query = query.where(FAQ.knowledge_base_id == knowledge_base_id)

            if category:
                query = query.where(FAQ.category == category)

            query = query.order_by(desc(FAQ.view_count), desc(FAQ.created_at)).offset(skip).limit(limit)

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取常见问题时出错: {e}")
            raise

    async def update_faq(
        self,
        faq_id: UUID,
        faq_data: FAQUpdate
    ) -> Optional[FAQ]:
        """更新常见问题"""
        try:
            # 检查常见问题是否存在
            faq = await self.get_faq(faq_id)
            if not faq:
                return None

            update_data = faq_data.dict(exclude_unset=True)
            if update_data:
                query = (
                    update(FAQ)
                    .where(FAQ.id == faq_id)
                    .values(**update_data)
                )
                await self.db.execute(query)
                await self.db.commit()
                await self.db.refresh(faq)

            logger.info(f"更新了常见问题 {faq_id}")
            return faq

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新常见问题 {faq_id} 时出错: {e}")
            raise

    async def delete_faq(self, faq_id: UUID) -> bool:
        """删除常见问题"""
        try:
            # 检查常见问题是否存在
            faq = await self.get_faq(faq_id)
            if not faq:
                return False

            # 删除常见问题
            await self.db.execute(
                delete(FAQ).where(FAQ.id == faq_id)
            )

            await self.db.commit()
            logger.info(f"删除了常见问题 {faq_id}")
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除常见问题 {faq_id} 时出错: {e}")
            raise

    async def increment_faq_view(self, faq_id: UUID) -> None:
        """增加常见问题查看次数"""
        try:
            query = (
                update(FAQ)
                .where(FAQ.id == faq_id)
                .values(view_count=FAQ.view_count + 1)
            )
            await self.db.execute(query)
            await self.db.commit()

        except Exception as e:
            logger.error(f"增加常见问题查看次数 {faq_id} 时出错: {e}")

    async def submit_faq_feedback(
        self,
        faq_id: UUID,
        is_helpful: bool
    ) -> None:
        """提交常见问题反馈"""
        try:
            if is_helpful:
                query = (
                    update(FAQ)
                    .where(FAQ.id == faq_id)
                    .values(helpful_count=FAQ.helpful_count + 1)
                )
            else:
                query = (
                    update(FAQ)
                    .where(FAQ.id == faq_id)
                    .values(not_helpful_count=FAQ.not_helpful_count + 1)
                )

            await self.db.execute(query)
            await self.db.commit()

        except Exception as e:
            logger.error(f"提交常见问题反馈 {faq_id} 时出错: {e}")

    async def search_faqs(
        self,
        query: str,
        limit: int = 10,
        knowledge_base_id: Optional[UUID] = None
    ) -> List[FAQ]:
        """按问题或答案搜索常见问题"""
        try:
            search_query = select(FAQ).where(
                FAQ.question.ilike(f"%{query}%") | FAQ.answer.ilike(f"%{query}%")
            )

            if knowledge_base_id:
                search_query = search_query.where(FAQ.knowledge_base_id == knowledge_base_id)

            search_query = search_query.order_by(desc(FAQ.helpful_count)).limit(limit)

            result = await self.db.execute(search_query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"搜索常见问题时出错: {e}")
            raise


class KnowledgeBaseEndpointService:
    """知识库端点服务类，处理所有知识库相关的业务逻辑"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.kb_service = KnowledgeBaseService(db)

    async def get_accessible_knowledge_bases(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100
    ) -> List[KnowledgeBase]:
        """获取用户可访问的知识库列表"""
        try:
            user_uuid = UUID(user_id)
            knowledge_bases = await self.kb_service.get_accessible_knowledge_bases(
                user_id=user_uuid,
                skip=skip,
                limit=limit
            )
            return knowledge_bases
        except Exception as e:
            error_msg = f"获取知识库列表错误: {e}"
            logger.error(error_msg)
            raise Exception(error_msg)

    async def create_knowledge_base(
        self,
        kb_data: KnowledgeBaseCreate
    ) -> KnowledgeBase:
        """创建知识库"""
        try:
            knowledge_base = await self.kb_service.create_knowledge_base(kb_data)
            return knowledge_base
        except Exception as e:
            error_msg = f"创建知识库错误: {e}"
            logger.error(error_msg)
            raise Exception(error_msg)

    async def _check_admin_permission(self, current_user: UserSchema) -> None:
        """检查管理员权限，无权限则抛出异常"""
        if not current_user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足，只有管理员可以执行此操作"
            )

    # async def get_knowledge_base_with_permission_check(
    #     self,
    #     kb_id: str,
    #     current_user: UserSchema
    # ) -> KnowledgeBase:
    #     """获取知识库并检查权限"""
    #     try:
    #         knowledge_base = await self.kb_service.get_knowledge_base(kb_id)
    #         if not knowledge_base:
    #             raise HTTPException(
    #                 status_code=status.HTTP_404_NOT_FOUND,
    #                 detail="知识库未找到"
    #             )
    #         await self._check_knowledge_base_access(knowledge_base, current_user)
    #         return knowledge_base
    #     except HTTPException:
    #         raise
    #     except Exception as e:
    #         logger.error(f"获取知识库错误: {e}")
    #         raise HTTPException(
    #             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    #             detail="获取知识库失败"
    #         )

    async def update_knowledge_base_with_permission_check(
        self,
        kb_id: str,
        kb_update: KnowledgeBaseUpdate,
        current_user: UserSchema
    ) -> KnowledgeBase:
        """更新知识库并检查权限"""
        try:
            knowledge_base = await self.kb_service.get_knowledge_base(kb_id)
            if not knowledge_base:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="知识库未找到"
                )
            await self._check_admin_permission(current_user)
            
            updated_kb = await self.kb_service.update_knowledge_base(knowledge_base.id, kb_update)
            return updated_kb
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"更新知识库错误: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="更新知识库失败"
            )

    async def delete_knowledge_base_with_permission_check(
        self,
        kb_id: str,
        current_user: UserSchema
    ) -> bool:
        """删除知识库并检查权限"""
        try:
            knowledge_base = await self.kb_service.get_knowledge_base(kb_id)
            if not knowledge_base:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="知识库未找到"
                )
            await self._check_admin_permission(current_user)
            
            success = await self.kb_service.delete_knowledge_base(knowledge_base.id)
            return {"message": "知识库删除成功"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"删除知识库错误: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="删除知识库失败"
            )


    async def _check_knowledge_base_access(
        self,
        knowledge_base: KnowledgeBase,
        current_user: UserSchema
    ) -> bool:
        """检查用户对知识库的访问权限"""
        try:
            # 如果是公开知识库，允许访问
            if knowledge_base.is_public:
                return True

            # 如果是超级管理员，允许访问
            if current_user.is_superuser:
                return True

            # 检查用户是否有访问权限
            query = select(Role).join(UserRoleAssociation).where(
                UserRoleAssociation.user_id == str(current_user.id),
                Role.is_active == True
            )
            result = await self.db.execute(query)
            roles = result.scalars().all()

            # 如果用户有管理员角色，允许访问
            for role in roles:
                if role.name in ["超级管理员", "系统管理员"]:
                    return True

            # 私有知识库，权限不足
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="权限不足，无法访问此知识库"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"检查知识库访问权限错误: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="检查访问权限失败"
            )