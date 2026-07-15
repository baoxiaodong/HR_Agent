"""
完整文档生命周期与向量搜索服务。

上传链路依次读取文件、计算哈希去重、识别类型、提取文本、生成向量、保存原文件及数据库
记录，最后创建检索分块。查询和更新会按用户过滤；删除同时清理磁盘文件、向量分块和
文档记录。轻量查询场景应优先使用 ``LightweightDocumentService``。
"""
import logging
import os
import hashlib
from typing import List, Optional, Dict, Any, BinaryIO
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc, func
from sqlalchemy.orm import selectinload

from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.services.llm_service import LLMService
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.core.config import settings

logger = logging.getLogger(__name__)


class DocumentService:
    """管理文档文件、业务记录和检索数据，并显式暴露跨存储事务边界。"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.llm_service = LLMService()

    async def upload_document(
        self,
        user_id: UUID,
        file: BinaryIO,
        filename: str,
        knowledge_base_id: Optional[UUID] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> Document:
        """完成查重、提取、嵌入、落盘和 Document 持久化的上传链路。

        ``user_id`` 是哈希查重和记录归属边界；知识库 ID 在此不额外验证。文件系统与数据库
        不共享事务：文件先写入，数据库失败时可能留下孤立文件。Document 在分块调用前已经
        ``commit``，后续分块失败不能由 ``rollback`` 撤销已保存记录。
        """
        try:
            # 一次性读取上传流；文件大小、哈希和后续提取都基于同一份内存字节。
            file_content = file.read()
            file_size = len(file_content)

            # 哈希与 user_id 组合查重，因此相同内容允许由不同用户分别保存。
            file_hash = hashlib.sha256(file_content).hexdigest()

            # 检查文档是否已存在
            existing_doc = await self._get_document_by_hash(file_hash, user_id)
            if existing_doc:
                logger.info(f"哈希为{file_hash}的文档已存在")
                return existing_doc

            # MIME 只按文件名后缀映射；文本提取的结果可能是占位符而非真实文件正文。
            mime_type = self._get_mime_type(filename)

            # 提取失败在下层会降级为错误文本，因此该步骤不一定抛异常。
            extracted_content = await self._extract_text_content(file_content, mime_type)

            # 文档级向量只使用文件名和正文前 1000 字，作为 ORM embedding 字段保存。
            embedding = await self.llm_service.generate_embedding(
                f"{filename} {extracted_content[:1000]}"
            )

            # 保存文件到存储
            file_path = await self._save_file(file_content, filename, user_id)

            # 创建文档记录
            document = Document(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                filename=filename,
                file_path=file_path,
                file_size=file_size,
                file_hash=file_hash,
                mime_type=mime_type,
                extracted_content=extracted_content,
                embedding=embedding,
                category=category,
                tags=tags or [],
                meta_data={
                    "upload_method": "api",
                    "processing_status": "completed"
                }
            )

            self.db.add(document)
            # commit 是上传流程的持久化边界；refresh 获取数据库生成的 ID 和时间戳。
            await self.db.commit()
            await self.db.refresh(document)

            # 当前类中没有实现 _create_document_chunks；若运行到这里会在 Document 已提交后失败。
            # 文件底部说明实际分块已迁移到 EnhancedDocumentService，这是理解两条上传链差异的关键。
            await self._create_document_chunks(document)

            logger.info(f"为用户{user_id}上传了文档{document.id}")
            return document

        except Exception as e:
            await self.db.rollback()
            logger.error(f"上传文档时出错: {e}")
            raise

    async def get_document(
        self,
        document_id: UUID,
        user_id: Optional[UUID] = None
    ) -> Optional[Document]:
        """按文档 ID 查询，并可选择把用户归属并入 SQL 条件。

        面向用户的入口应传 ``user_id``；省略时是无所有权限制的内部查询。只读方法不提交事务，
        未找到返回 ``None``。
        """
        try:
            query = select(Document).where(Document.id == document_id)

            # 用户条件使“存在但不属于当前用户”与“不存在”得到相同 None 结果。
            if user_id:
                query = query.where(Document.user_id == user_id)

            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"获取文档{document_id}时出错: {e}")
            raise

    async def get_user_documents(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20,
        category: Optional[str] = None,
        knowledge_base_id: Optional[UUID] = None
    ) -> List[Document]:
        """在当前用户范围内按分类、知识库和分页条件读取文档。

        ``user_id`` 是不可省略的基础过滤器，可选条件只会继续收窄结果；按创建时间倒序返回
        ORM 实体，不加载向量块也不产生写事务。
        """
        try:
            query = select(Document).where(Document.user_id == user_id)

            if category:
                query = query.where(Document.category == category)

            if knowledge_base_id:
                query = query.where(Document.knowledge_base_id == knowledge_base_id)

            query = query.order_by(desc(Document.created_at)).offset(skip).limit(limit)

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取用户{user_id}的文档时出错: {e}")
            raise

    async def update_document(
        self,
        document_id: UUID,
        user_id: UUID,
        document_data: DocumentUpdate
    ) -> Optional[Document]:
        """验证文档归属后，更新请求中显式提供的元数据字段。

        ``exclude_unset`` 防止未提交字段被默认值覆盖；该方法不会重新提取正文、重算 embedding
        或同步 PGVector 块。成功提交后刷新 ORM 实体，失败回滚当前事务。
        """
        try:
            # get_document 同时使用 ID 和用户 ID，越权目标不会进入 update。
            document = await self.get_document(document_id, user_id)
            if not document:
                return None

            update_data = document_data.dict(exclude_unset=True)
            if update_data:
                query = (
                    update(Document)
                    .where(Document.id == document_id)
                    .values(**update_data)
                )
                await self.db.execute(query)
                await self.db.commit()
                await self.db.refresh(document)

            logger.info(f"更新了文档{document_id}")
            return document

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新文档{document_id}时出错: {e}")
            raise

    async def delete_document(
        self,
        document_id: UUID,
        user_id: UUID
    ) -> bool:
        """校验所有者后，依次删除磁盘文件、向量块和业务记录。

        两条数据库删除共用一次 ``commit``，可以一起回滚；磁盘删除发生在事务提交前且不受
        rollback 管理，所以后续 SQL 失败时可能出现“数据库仍有记录但原文件已删除”的部分状态。
        """
        try:
            # 只有当前用户拥有的 ORM 记录才允许进入副作用阶段。
            document = await self.get_document(document_id, user_id)
            if not document:
                return False

            # 文件系统操作不可回滚，且先于数据库事务提交。
            if document.file_path and os.path.exists(document.file_path):
                os.remove(document.file_path)

            # 从langchain_pg_embedding表中删除文档块
            from sqlalchemy import text
            delete_query = text("""
                DELETE FROM langchain_pg_embedding
                WHERE cmetadata->>'document_id' = :document_id
            """)
            await self.db.execute(delete_query, {"document_id": str(document_id)})

            # 删除文档
            await self.db.execute(
                delete(Document).where(Document.id == document_id)
            )

            await self.db.commit()
            logger.info(f"删除了文档{document_id}")
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除文档{document_id}时出错: {e}")
            raise

    async def search_documents(
        self,
        query: str,
        user_id: UUID,
        limit: int = 10,
        knowledge_base_id: Optional[UUID] = None,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """在当前用户文档中执行文本包含查询并返回 JSON 友好的摘要字典。

        当前实现虽然调用嵌入 API 生成 ``query_embedding``，但该变量没有进入 SQL；实际检索是
        ``extracted_content ILIKE``，不是向量相似度。知识库和分类条件会继续收窄用户范围，
        返回正文前 500 字供聊天上下文使用。
        """
        try:
            # 该远程向量目前未被后续查询使用，但其失败仍会使整个搜索抛错。
            query_embedding = await self.llm_service.generate_embedding(query)

            # 用户隔离是基础条件，后续过滤器不能扩大查询范围。
            base_query = select(Document).where(Document.user_id == user_id)

            if knowledge_base_id:
                base_query = base_query.where(Document.knowledge_base_id == knowledge_base_id)

            if category:
                base_query = base_query.where(Document.category == category)

            # 实际 SQL 使用大小写不敏感的正文包含匹配；limit 在数据库侧限制结果数量。
            text_query = base_query.where(
                Document.extracted_content.ilike(f"%{query}%")
            ).limit(limit)

            result = await self.db.execute(text_query)
            documents = result.scalars().all()

            # 格式化结果
            search_results = []
            for doc in documents:
                search_results.append({
                    "id": str(doc.id),
                    "filename": doc.filename,
                    "content": doc.extracted_content[:500],
                    "category": doc.category,
                    "tags": doc.tags,
                    "created_at": doc.created_at.isoformat()
                })

            return search_results

        except Exception as e:
            logger.error(f"搜索文档时出错: {e}")
            raise

    async def _get_document_by_hash(
        self,
        file_hash: str,
        user_id: UUID
    ) -> Optional[Document]:
        """在单个用户范围内按 SHA-256 查重，避免跨用户复用所有权记录。"""
        query = select(Document).where(
            Document.file_hash == file_hash,
            Document.user_id == user_id
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    def _get_mime_type(self, filename: str) -> str:
        """按扩展名映射 MIME，未知后缀退回二进制类型。

        该结果不是文件头检测，不能证明上传内容与扩展名一致。
        """
        extension = filename.lower().split('.')[-1]
        mime_types = {
            'pdf': 'application/pdf',
            'doc': 'application/msword',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'txt': 'text/plain',
            'md': 'text/markdown',
            'html': 'text/html',
            'json': 'application/json',
            'csv': 'text/csv'
        }
        return mime_types.get(extension, 'application/octet-stream')

    async def _extract_text_content(self, file_content: bytes, mime_type: str) -> str:
        """把少数 UTF-8 文本类型解码为正文，其他格式返回占位文本。

        本方法并未真实解析 PDF、DOC 或 DOCX；解码异常也不会抛给上传流程，而会返回固定错误
        字符串，随后该字符串仍可能被嵌入并保存。完整格式解析由增强文档服务承担。
        """
        try:
            if mime_type == 'text/plain':
                return file_content.decode('utf-8')
            elif mime_type == 'application/json':
                return file_content.decode('utf-8')
            elif mime_type == 'text/csv':
                return file_content.decode('utf-8')
            else:
                # 对于其他类型，返回占位符
                # 在生产环境中，您将使用PyPDF2、python-docx等库
                return f"从{mime_type}文件中提取的内容"

        except Exception as e:
            logger.error(f"提取文本内容时出错: {e}")
            return "提取内容时出错"

    async def _save_file(self, file_content: bytes, filename: str, user_id: UUID) -> str:
        """将上传字节写入用户目录，并在原名后加入内容哈希短码。

        返回路径供 ORM 保存；文件系统写入不属于数据库事务。相同内容和原名会得到相同路径，
        上层哈希查重通常会在写入前拦截重复记录。
        """
        try:
            # 用户 UUID 形成第一层目录隔离，文件名仍由上传参数派生。
            user_dir = os.path.join(settings.UPLOAD_DIR, str(user_id))
            os.makedirs(user_dir, exist_ok=True)

            # 生成唯一文件名
            file_hash = hashlib.sha256(file_content).hexdigest()[:8]
            name, ext = os.path.splitext(filename)
            unique_filename = f"{name}_{file_hash}{ext}"

            file_path = os.path.join(user_dir, unique_filename)

            # 保存文件
            with open(file_path, 'wb') as f:
                f.write(file_content)

            return file_path

        except Exception as e:
            logger.error(f"保存文件时出错: {e}")
            raise

    # 注意: _create_document_chunks方法已移除 - 现在直接使用langchain_pg_embedding表
    # 文档块通过enhanced_document_service.py中的PGVector创建