"""
知识库与 FAQ 的本地持久化服务。

基础服务负责知识库、FAQ、统计和全文搜索，并在每个写操作内提交或回滚事务；删除知识库
前会解除文档关联并清理其 FAQ。文件末尾的端点门面在此基础上增加 UUID、角色和资源
存在性检查，供 API 层直接调用。
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
    """封装知识库与 FAQ 的查询、统计及本地事务，不承担调用方身份认证。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_knowledge_base(
        self,
        kb_data: KnowledgeBaseCreate
    ) -> KnowledgeBase:
        """把已校验的创建请求映射为 ORM 实体并提交。"""
        try:
            # tags/meta_data 是可变集合；请求未提供时显式创建空容器，避免数据库收到 None。
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
            # commit 持久化，refresh 取回数据库生成的 id、时间戳和默认字段。
            await self.db.commit()
            await self.db.refresh(knowledge_base)

            logger.info(f"创建了知识库 {knowledge_base.id}")
            return knowledge_base

        except Exception as e:
            await self.db.rollback()
            logger.error(f"创建知识库时出错: {e}")
            raise

    async def get_knowledge_base(self, kb_id: Union[UUID, str]) -> Optional[KnowledgeBase]:
        """把字符串 ID 规范化为 UUID 后读取知识库，非法格式或未命中均返回 ``None``。

        该基础查询不接收用户信息，也不判断公开性；面向用户的调用必须在上层补充访问控制。
        数据库查询异常仍向上抛出，与“找不到资源”保持区分。
        """
        try:
            # 字符串 ID 在构造 SQL 前转换；非法格式不访问数据库。
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
        """按可选公开性和分类过滤知识库，再以创建时间倒序分页。

        两个过滤条件只会收窄查询；本方法本身不根据当前用户计算权限，调用方不能把
        ``is_public=None`` 的结果直接当作用户可访问列表。
        """
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
        """返回当前权限模型下用户可见的知识库。"""
        try:
            # 当前实现尚未使用 user_id 做用户/角色级授权，只返回公共知识库。
            # 因此该参数用于保留未来扩展接口，不能把本方法理解为已支持私有库授权。
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
        """按局部更新语义修改知识库，未命中时返回 None。"""
        try:
            # 先读取实体用于存在性判断，并在提交后 refresh 同一个 ORM 对象。
            kb = await self.get_knowledge_base(kb_id)
            if not kb:
                return None

            # exclude_unset=True 确保客户端未提交的字段保持数据库原值。
            update_data = kb_data.dict(exclude_unset=True)
            if update_data:
                query = (
                    update(KnowledgeBase)
                    .where(KnowledgeBase.id == kb_id)
                    .values(**update_data)
                )
                await self.db.execute(query)
                await self.db.commit()
                # Core UPDATE 不会自动把所有新值同步到已读取对象，refresh 从数据库重新加载。
                await self.db.refresh(kb)

            logger.info(f"更新了知识库 {kb_id}")
            return kb

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新知识库 {kb_id} 时出错: {e}")
            raise

    async def delete_knowledge_base(self, kb_id: UUID) -> bool:
        """解除文档关联、清理 FAQ 后删除知识库。"""
        try:
            kb = await self.get_knowledge_base(kb_id)
            if not kb:
                return False

            # 文档本身保留，只把外键置空；删除知识库不会删除已上传文件或文档记录。
            await self.db.execute(
                update(Document)
                .where(Document.knowledge_base_id == kb_id)
                .values(knowledge_base_id=None)
            )

            # FAQ 属于知识库内部内容，知识库删除时一并物理删除。
            await self.db.execute(
                delete(FAQ).where(FAQ.knowledge_base_id == kb_id)
            )

            await self.db.execute(
                delete(KnowledgeBase).where(KnowledgeBase.id == kb_id)
            )

            # 三个 SQL 共用同一数据库会话，在一次 commit 中提交；任一步异常都会由下方统一回滚。
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
        """在单个知识库内对文档正文和 FAQ 执行数据库模糊搜索。"""
        try:
            # 先确认知识库存在；本基础方法不做用户权限校验，调用端必须先限制可访问范围。
            kb = await self.get_knowledge_base(kb_id)
            if not kb:
                return {"documents": [], "faqs": []}

            # ilike 生成不区分大小写的 %关键词% 查询。这是数据库全文字段扫描，并非向量检索/RAG。
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
                        # 搜索响应只返回正文前 300 个字符作为预览，不返回整篇文档。
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
        """实时统计关联文档/FAQ 数，并把文档数缓存回知识库。"""
        try:
            # 两个 count 均在数据库执行，不加载文档和 FAQ 实体。
            doc_count_query = select(func.count(Document.id)).where(
                Document.knowledge_base_id == kb_id
            )
            doc_count_result = await self.db.execute(doc_count_query)
            doc_count = doc_count_result.scalar()

            faq_count_query = select(func.count(FAQ.id)).where(
                FAQ.knowledge_base_id == kb_id
            )
            faq_count_result = await self.db.execute(faq_count_query)
            faq_count = faq_count_result.scalar()

            # 该“读取统计”方法同时有写副作用：将实时文档数同步到 knowledge_base.document_count。
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
            # 当前异常分支没有显式 rollback；若失败发生在 UPDATE/commit 阶段，调用方需处理会话事务状态。
            raise

    # 常见问题管理
    async def create_faq(
        self,
        faq_data: FAQCreate,
        knowledge_base_id: Optional[UUID] = None
    ) -> FAQ:
        """把 FAQ 请求映射为 ORM 记录并在独立事务中提交。

        可选知识库 ID 由调用方提供，本方法不检查目标知识库是否存在或是否允许访问；标签和
        元数据缺省时写入空容器。提交失败会回滚，成功后刷新数据库生成字段。
        """
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
        """按 FAQ ID 读取单条记录；不校验关联知识库的可见性。"""
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
        """按可选知识库与分类过滤 FAQ，并以查看次数、创建时间倒序分页。

        查询只处理内容条件，不验证调用者能否访问指定知识库；权限边界必须由上层先建立。
        """
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
        """按局部更新语义修改 FAQ，未命中时返回 ``None``。

        只写入请求中显式提交的字段；Core UPDATE 提交后刷新原 ORM 对象。存在性读取与更新
        共用当前会话，异常会回滚尚未提交的变更。
        """
        try:
            # 先读取目标 FAQ；本层只判断存在性，不检查其知识库权限。
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
        """确认 FAQ 存在后物理删除并提交，未命中返回 ``False``。

        本方法不校验关联知识库权限；调用方必须先建立管理权限。删除失败会回滚当前事务。
        """
        try:
            # 先读取目标 FAQ，避免对未命中 DELETE 仍返回成功。
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
        """使用数据库原子自增记录查看次数。"""
        try:
            # 在 SQL 中执行 view_count + 1，避免先读后写造成并发请求互相覆盖。
            query = (
                update(FAQ)
                .where(FAQ.id == faq_id)
                .values(view_count=FAQ.view_count + 1)
            )
            await self.db.execute(query)
            await self.db.commit()

        except Exception as e:
            # 统计写入失败被日志吸收，不影响 FAQ 主读取流程；当前分支未显式回滚会话。
            logger.error(f"增加常见问题查看次数 {faq_id} 时出错: {e}")

    async def submit_faq_feedback(
        self,
        faq_id: UUID,
        is_helpful: bool
    ) -> None:
        """根据布尔反馈原子增加有用或无用计数。"""
        try:
            # 两个分支只更新其中一个计数器，表达一次互斥反馈。
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
            # 反馈统计失败不会向调用方抛出，但当前分支也未显式 rollback。
            logger.error(f"提交常见问题反馈 {faq_id} 时出错: {e}")

    async def search_faqs(
        self,
        query: str,
        limit: int = 10,
        knowledge_base_id: Optional[UUID] = None
    ) -> List[FAQ]:
        """用数据库 ``ILIKE`` 匹配问题或答案，可选限制到单个知识库。

        结果按有用反馈数倒序并截取 ``limit``；这是关系数据库模糊查询，不执行向量检索，
        也不检查调用者对目标知识库的访问权限。
        """
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
    """为知识库 API 组合 UUID 规范化、管理员校验与基础持久化服务。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.kb_service = KnowledgeBaseService(db)

    async def get_accessible_knowledge_bases(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100
    ) -> List[KnowledgeBase]:
        """把字符串用户 ID 转为 UUID，再查询当前实现允许访问的知识库。

        基础服务目前只返回公共知识库，``user_id`` 尚未参与私有库 ACL 判断；转换失败或查询
        异常会包装为带操作上下文的普通异常，由端点统一映射为 500。
        """
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
        """把已校验创建 Schema 转交基础服务提交，并统一补充失败上下文。

        当前创建路径不把 ``current_user`` 传入服务，因此只保证调用端已认证，不记录创建者，
        也不在本门面追加角色限制。
        """
        try:
            knowledge_base = await self.kb_service.create_knowledge_base(kb_data)
            return knowledge_base
        except Exception as e:
            error_msg = f"创建知识库错误: {e}"
            logger.error(error_msg)
            raise Exception(error_msg)

    async def _check_admin_permission(self, current_user: UserSchema) -> None:
        """只接受 ``is_superuser``，不查询角色表；失败时立即抛出 403。"""
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
        """先确认知识库存在和超级用户权限，再执行局部更新事务。

        ID 非法与资源不存在都由基础读取收敛为 404；权限检查发生在任何写入之前。基础服务
        负责 ``exclude_unset``、提交与回滚，本门面保留 403/404 并把其他异常转换为 500。
        """
        try:
            # 先统一解析/查询知识库；无效 UUID 在基础服务中同样表现为未找到。
            knowledge_base = await self.kb_service.get_knowledge_base(kb_id)
            if not knowledge_base:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="知识库未找到"
                )

            # 修改属于管理操作，只接受 is_superuser；公开可读不代表普通用户可编辑。
            await self._check_admin_permission(current_user)

            # 权限通过后才进入包含 commit/rollback 的基础持久化方法。
            updated_kb = await self.kb_service.update_knowledge_base(knowledge_base.id, kb_update)
            return updated_kb
        except HTTPException:
            # 保留 403/404 的原始语义，不把权限或不存在错误错误映射为 500。
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
        """先确认资源与超级用户权限，再触发知识库关联清理事务。

        基础服务在一次本地提交中解除文档外键、删除 FAQ 和知识库；本门面将成功布尔值转换为
        固定消息字典，同时保留 403/404，其他异常统一映射为 500。
        """
        try:
            # 存在性和管理员权限都在执行关联清理前完成，失败时不会产生数据库写入。
            knowledge_base = await self.kb_service.get_knowledge_base(kb_id)
            if not knowledge_base:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="知识库未找到"
                )
            await self._check_admin_permission(current_user)

            # 基础服务在一个本地事务中解除文档、删除 FAQ 和知识库。
            success = await self.kb_service.delete_knowledge_base(knowledge_base.id)
            # 当前公开返回固定消息，不透传基础服务布尔值。
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
        """按“公开库 → 超级用户 → 管理员角色”顺序判断读取权限。

        私有库没有资源级 ACL；普通用户只有在角色关联表中存在启用的“超级管理员”或
        “系统管理员”角色才能访问。权限不足抛出 403，角色查询异常转换为 500。
        """
        try:
            # 公开库无需查询角色，是最短访问路径。
            if knowledge_base.is_public:
                return True

            # 超级用户标志优先于角色关联表。
            if current_user.is_superuser:
                return True

            # 私有库没有按资源分配的 ACL；当前实现仅查询用户是否拥有两个管理员命名角色。
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