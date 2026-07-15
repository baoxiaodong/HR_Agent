"""
增强文档处理与检索服务。

负责语义切分、文件上传与文本提取、PGVector 块写入、语义/文本检索，以及文档查询和删除。
文件系统、业务表和向量表并非始终由同一个事务覆盖，需要显式识别部分成功状态。
"""
import logging
import os
import hashlib
from typing import List, Optional, Dict, Any, BinaryIO
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc, func, text
from sqlalchemy.orm import selectinload

# LangChain imports
from langchain_core.documents import Document as LangChainDocument
from langchain_postgres import PGVector

# Document processing imports
import PyPDF2
from docx import Document as DocxDocument
import tempfile

from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.services.llm_service import LLMService
from app.services.embedding_service import get_embedding_service
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.core.config import settings
from app.utils.text_utils import extract_text_content
from app.services.lightweight_document_service import BaseDocumentService

logger = logging.getLogger(__name__)


class EnhancedDocumentService(BaseDocumentService):
    """编排文档文件、业务记录、文本切分和向量检索。"""

    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.llm_service = LLMService()

        # 使用共享嵌入服务避免重复初始化
        self.embedding_service = get_embedding_service()
        self.embeddings = self.embedding_service.get_embeddings()
        self.text_splitter = self.embedding_service.get_text_splitter()

        # PGVector数据库连接字符串（使用psycopg2进行同步连接）
        self.connection_string = settings.DATABASE_URL

        logger.info("增强文档服务已使用共享嵌入服务初始化")

    # ---------- 语义切分 ----------

    def _split_by_semantic_points(self, text: str, split_points: List[str]) -> List[str]:
        """按模型给出的原文片段位置切分，切分点本身归入后一个文本块。

        分割点必须能在 ``text`` 中找到；不存在或顺序落后的点会被跳过。该函数只负责按位置
        切片，不处理块长度，后续 ``_split_text`` 会统一执行强制拆分和短块合并。
        """
        chunks = []
        current_pos = 0

        # 从上一个切分位置向后搜索，避免相同片段导致切分顺序回退。
        for point in split_points:
            pos = text.find(point, current_pos)
            if pos != -1:
                # 先保存切分点之前的内容，再让下一块从切分点开头开始，保留标题语义。
                if pos > current_pos:
                    chunk = text[current_pos:pos].strip()
                    if chunk:
                        chunks.append(chunk)
                current_pos = pos

        # 尾部没有下一个分割点，需要作为最后一个完整块保留。
        if current_pos < len(text):
            chunk = text[current_pos:].strip()
            if chunk:
                chunks.append(chunk)

        return chunks

    async def get_semantic_split_points(self, content: str) -> List[str]:
        """让 LLM 提议切分点，再用原文校验、消歧并按真实位置排序。

        模型只看到前 10000 个字符，所以超出窗口的正文不会产生语义切分点；任何调用、响应
        或校验异常都降级为空列表，由主切分流程改用确定性的长度切分。
        """
        try:
            # LLM 只负责给出候选原文片段，不直接生成切分后的内容，避免改写原始文档。
            system_prompt = (
                "你是一个文档结构分析助手。只输出用于 split 的分割点字符串，"
                "用`~~`分隔，不要输出任何其他文字。确保每个分割点在原文中唯一，"
                "如果遇到重复标题或目录项，需要在分割点后追加少量后续字符形成唯一片段。"
            )
            user_prompt = (
                "# 任务\n请分析文档，识别适合作为分割点的文本片段。\n\n"
                "# 规则\n"
                "1) 分割点应位于句子或段落的开头；\n"
                "2) 分割后每段尽量<=500字，严禁>800字；\n"
                "3) 若存在重复片段（例如目录与正文相同标题），需在分割点后追加少量后续内容以确保唯一；\n"
                "4) 仅输出分割点字符串，使用`~~`分隔，不要解释或添加其他文本。\n\n"
                f"# 文档（截断）\n{content[:10000]}"
            )

            response = await self.llm_service.client.chat.completions.create(
                model=self.llm_service.llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=1000,
            )
            raw = response.choices[0].message.content or ""
            points = [p.strip() for p in raw.split("~~") if p.strip()]
            if not points:
                return []

            # 去重并保持顺序
            seen = set()
            unique_points = []
            for p in points:
                if p not in seen:
                    seen.add(p)
                    unique_points.append(p)

            # 保证每个分割点在正文中唯一：必要时追加后续字符
            def ensure_unique(point: str) -> str:
                start = content.find(point)
                if start == -1:
                    return ""  # 模型输出不在原文中，丢弃
                
                # 统计出现次数
                count = 0
                search_pos = 0
                while True:
                    idx = content.find(point, search_pos)
                    if idx == -1:
                        break
                    count += 1
                    search_pos = idx + 1
                
                if count <= 1:
                    return point
                
                # 重复：逐步扩展片段直到唯一或达到限制
                # 最多追加 100 个字符，步长 10
                max_extra = 100
                step = 10
                extra = 0
                while extra <= max_extra:
                    candidate = content[start:start + len(point) + extra]
                    if len(candidate) <= len(point):  # 避免空候选
                        extra += step
                        continue
                    
                    # 重新统计候选字符串的出现次数
                    c = 0
                    sp = 0
                    while True:
                        j = content.find(candidate, sp)
                        if j == -1:
                            break
                        c += 1
                        sp = j + 1
                    
                    if c <= 1:
                        return candidate
                    extra += step
                
                # 仍不唯一则返回原始（极少数情况），后续切分时按位置处理
                return point

            adjusted_points_with_index: List[tuple[int, str]] = []
            for p in unique_points:
                adj = ensure_unique(p)
                if not adj:
                    continue
                idx = content.find(adj)
                if idx != -1:
                    adjusted_points_with_index.append((idx, adj))

            # 按在正文中的出现位置排序
            adjusted_points_with_index.sort(key=lambda x: x[0])
            final_points = [pt for _, pt in adjusted_points_with_index]
            return final_points
        except Exception as e:
            logger.error(f"增强文档服务获取语义分割点时出错: {e}")
            return []

    def _force_split_long_chunk(self, chunk: str) -> List[str]:
        """把超过 1000 字符的块拆成有确定上限的子块。

        优先以换行保持段落边界；单行本身超长时递归进入无换行分支，最终按固定字符数切片。
        这里按 Python 字符计数，不等同于嵌入模型或 LLM 的 token 数。
        """
        max_length = 1000
        chunks = []

        # 有自然段落边界时逐行累积，尽量避免在句段中间切断。
        if '\n' in chunk:
            lines = chunk.split('\n')
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 > max_length:
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        # 单行就超过最大长度，需要递归分割
                        line_chunks = self._force_split_long_chunk(line)
                        chunks.extend(line_chunks)
                        current_chunk = ""
                else:
                    if current_chunk:
                        current_chunk += "\n" + line
                    else:
                        current_chunk = line
            if current_chunk:
                chunks.append(current_chunk)
        else:
            # 没有换行符则直接按长度分割
            chunks = [chunk[i:i + max_length] for i in range(0, len(chunk), max_length)]

        return chunks

    def _merge_short_chunks(self, chunks: List[str], min_length: int = 50, max_length: int = 1000) -> List[str]:
        """向后合并过短块，在减少碎片的同时维持最大字符数约束。

        合并只改变块边界，不改写文本内容；当前块会连续吸收后续块，直到达到最小长度、
        触及最大长度或没有后续块。
        """
        if not chunks:
            return []
        merged: List[str] = []
        i = 0
        while i < len(chunks):
            cur = chunks[i]
            
            # 短块优先与后继内容合并，避免向量库中出现语义信息不足的独立记录。
            if len(cur) < min_length and i + 1 < len(chunks):
                # 尝试合并多个连续的短片段
                merged_chunk = cur
                j = i + 1
                
                while j < len(chunks) and len(merged_chunk) < min_length:
                    nxt = chunks[j]
                    # 添加适当的分隔符
                    separator = "\n" if not merged_chunk.endswith("\n") else ""
                    potential_chunk = merged_chunk + separator + nxt
                    
                    if len(potential_chunk) <= max_length:
                        merged_chunk = potential_chunk
                        j += 1
                    else:
                        break
                
                # 如果成功合并了至少一个片段
                if j > i + 1:
                    merged.append(merged_chunk)
                    i = j
                    continue
                else:
                    # 只合并下一个片段
                    nxt = chunks[i + 1]
                    if len(cur) + len(nxt) + 1 <= max_length:
                        separator = "\n" if not cur.endswith("\n") else ""
                        merged.append(cur + separator + nxt)
                        i += 2
                        continue
            
            merged.append(cur)
            i += 1
        return merged

    async def _split_text(self, content: str) -> List[str]:
        """执行“LLM 候选点 -> 原文切片 -> 长块拆分 -> 短块合并”的完整流程。

        LLM 只优化语义边界，不是必需依赖；候选点为空时整篇内容仍会进入确定性的长度约束，
        因而远程模型失败不会阻断后续向量化。
        """
        # 1. 请求语义候选点；异常已在下层转换为空列表。
        points = await self.get_semantic_split_points(content)
        # 2. 只用原文中真实存在的点切片，模型不能向文档注入新内容。
        if points:
            chunks = self._split_by_semantic_points(content, points)
        else:
            chunks = [content]
        # 3. 所有超过 1000 字符的块必须继续拆分，限制单次嵌入输入规模。
        normalized: List[str] = []
        for ch in chunks:
            if len(ch) > 1000:
                normalized.extend(self._force_split_long_chunk(ch))
            else:
                normalized.append(ch)
        # 4. 合并小于 50 字符的碎片，提升每个向量块的上下文完整度。
        normalized = self._merge_short_chunks(normalized, min_length=50, max_length=1000)
        # 5. 最终只返回非空原文块，作为 PGVector 的 page_content。
        normalized = [c.strip() for c in normalized if c and c.strip()]
        return normalized

    # ---------- 上传与处理 ----------

    async def upload_document(
        self,
        file,  # UploadFile object
        user_id: UUID,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        knowledge_base_id: Optional[str] = None
    ) -> Document:
        """把接口层 ``UploadFile`` 适配为内部文件流参数后进入上传主流程。

        ``user_id`` 应来自认证依赖而不是请求体。知识库 ID 在这里只做格式转换；无效字符串会
        降级为“未关联知识库”，本方法本身不验证目标知识库是否存在或属于当前用户。
        """
        # API 层传入字符串，ORM 字段需要 UUID；无法转换时保留上传但不建立知识库关联。
        kb_id = None
        if knowledge_base_id:
            try:
                kb_id = UUID(knowledge_base_id)
            except (ValueError, TypeError):
                logger.warning(f"无效的知识库ID格式: {knowledge_base_id}")
                kb_id = None

        # 只把 Starlette 上传对象拆为底层流和文件名，实际副作用集中在统一主流程中。
        return await self.upload_and_process_document(
            file=file.file,
            filename=file.filename,
            user_id=user_id,
            knowledge_base_id=kb_id,
            category=category,
            tags=tags
        )

    async def upload_and_process_document(
        self,
        file: BinaryIO,
        filename: str,
        user_id: UUID,
        knowledge_base_id: Optional[UUID] = None,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None
    ) -> Document:
        """保存原文件和 Document 记录，不在此阶段提取文本或写入向量。

        文件先写入磁盘，再提交 Document 记录；提交失败时数据库会话回滚，但已经写入的文件
        不会清理。哈希命中已有记录时直接返回，不再写文件。
        """
        try:
            # 读取文件内容
            file_content = file.read()
            file.seek(0)  # 重置文件指针

            # 生成文件哈希
            file_hash = hashlib.sha256(file_content).hexdigest()

            # 检查文档是否已存在
            existing_doc = await self._get_document_by_hash(file_hash, user_id)
            if existing_doc:
                logger.info(f"哈希为 {file_hash} 的文档已存在")
                return existing_doc

            # 确定MIME类型
            mime_type = self._get_mime_type(filename)

            # 先写文件系统；后续数据库失败不会自动删除该文件。
            file_path = await self._save_file(file_content, filename, user_id)

            # 创建文档记录（暂不提取内容和向量化）
            document = Document(
                filename=filename,
                original_filename=filename,
                file_path=file_path,
                file_size=len(file_content),
                file_hash=file_hash,
                mime_type=mime_type,
                extracted_content=None,
                embedding=None,
                category=category,
                tags=tags,
                user_id=user_id,
                knowledge_base_id=knowledge_base_id
            )

            self.db.add(document)
            await self.db.commit()
            await self.db.refresh(document)
            logger.info(f"文档上传成功: {document.id}")
            return document

        except Exception as e:
            logger.error(f"上传文档时出错: {e}")
            await self.db.rollback()
            raise

    async def process_document(self, document_id: UUID) -> Document:
        """读取已上传文件，提取文本、切分并写入 PGVector，最后提交文档内容。

        本方法只按 ``document_id`` 查询，不接收用户身份；资源归属校验必须在调用它的端点或
        上层服务完成。临时文件在 finally 中清理；``extracted_content`` 只在最后一次数据库
        提交后持久化，没有独立的处理状态字段更新。PGVector 使用独立同步连接，若向量写入
        已完成而后续 Document 提交失败，外层 ``rollback`` 不会删除这些向量块。
        """
        # 这里取得的是 ORM 实体，后续赋值先停留在当前 AsyncSession，直到 commit 才持久化。
        result = await self.db.execute(
            select(Document).where(Document.id == document_id)
        )
        document = result.scalar_one_or_none()
        if not document:
            raise ValueError(f"文档不存在: {document_id}")

        temp_file_path = None
        try:
            # 临时保存文件以进行文本提取
            with open(document.file_path, 'rb') as f:
                file_content = f.read()
            temp_file_path = await self._save_temp_file(file_content, document.filename)

            # 1. 从临时文件提取文本，并先更新当前会话中的 Document 对象。
            extracted_content = await extract_text_content(temp_file_path, document.mime_type)
            document.extracted_content = extracted_content

            if extracted_content:
                # 2. 切分和向量写入先于 Document 提交，且不受当前异步事务统一回滚。
                await self._create_document_chunks_with_pgvector(document, extracted_content)

            # 3. 最后提交 extracted_content；空文本也会提交当前 Document 状态。
            await self.db.commit()
            await self.db.refresh(document)
            logger.info(f"文档处理成功: {document.id}")
            return document

        except Exception as e:
            logger.error(f"处理文档时出错: {e}")
            await self.db.rollback()
            raise
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

    async def _create_document_chunks_with_pgvector(self, document: Document, content: str) -> None:
        """语义切分文本，生成嵌入并写入用户对应的 PGVector 集合。

        语义分割点来自远程 LLM，失败时 ``_split_text`` 会退回长度切分；块元数据随向量一起
        通过同步 ``add_documents`` 写入。该写入不使用 ``self.db``，异常向上抛出由调用方
        处理，但调用方的数据库回滚不能撤销已完成的向量写入。
        """
        # text_chunks = self.get_semantic_split_points(content)
        text_chunks = await self._split_text(content)
        if not text_chunks:
            logger.warning(f"未为文档 {document.id} 提供文本块")
            return

        # 创建带有元数据的LangChain文档（内容块）
        chunks_collection = f"document_chunks_{document.user_id}".replace("-", "_")
        
        langchain_docs = []
        for i, chunk_text in enumerate(text_chunks):
            # 直接在创建时包含所有必要的元数据
            doc = LangChainDocument(
                page_content=chunk_text,
                metadata={
                    "document_id": str(document.id),
                    "knowledge_base_id": str(document.knowledge_base_id),
                    "chunk_index": i,
                    "chunk_size": len(chunk_text),
                    "filename": document.filename,
                    "category": document.category or "general",
                    "file_path": document.file_path,
                    "mime_type": document.mime_type,
                    "source_type": "content",
                    "collection_name": chunks_collection
                }
            )
            langchain_docs.append(doc)

        # 获取向量存储
        vector_store = PGVector(
            connection=self.connection_string,
            embeddings=self.embeddings,
            collection_name=chunks_collection,
            use_jsonb=True
        )

        logger.info(
            f"正在向PGVector集合 {chunks_collection} 添加 {len(langchain_docs)} 个文档块"
        )

        # 将文档添加到向量存储（同步操作）
        try:
            vector_store.add_documents(langchain_docs)
            logger.info(
                f"成功向PGVector集合 {chunks_collection} 添加了 {len(langchain_docs)} 个文档块"
            )
            logger.info(
                f"为文档 {document.id} 创建了 {len(text_chunks)} 个块"
            )
        except Exception as e:
            logger.error(f"向PGVector添加文档时出错: {e}")
            # 不在这里处理数据库事务，让调用方处理
            raise

    # ---------- 检索 ----------

    async def semantic_search(
        self,
        query: str,
        user_id: UUID,
        limit: int = 10,
        knowledge_base_id: Optional[UUID] = None,
        category: Optional[str] = None,
        similarity_threshold: float = 0.8
    ) -> List[Dict[str, Any]]:
        """在用户块集合中执行向量检索，失败时回退到业务表文本查询。

        可按知识库和分类过滤，并仅保留距离分数不高于阈值的结果。向量连接、嵌入或查询任一
        环节抛错都会进入 ``_fallback_text_search``；两个分支均为只读，不提交数据库事务。
        """
        try:
            collection_name = f"document_chunks_{user_id}".replace("-", "_")
            
            # 连接到向量存储
            vector_store = PGVector(
                connection=self.connection_string,
                embeddings=self.embeddings,
                collection_name=collection_name,
                use_jsonb=True
            )
            
            # 构建过滤条件
            filter_conditions = {}
            if knowledge_base_id:
                filter_conditions["knowledge_base_id"] = str(knowledge_base_id)
            if category:
                filter_conditions["category"] = category
            
            # 执行相似性搜索
            results = vector_store.similarity_search_with_score(
                query=query,
                k=limit,
                filter=filter_conditions if filter_conditions else None
            )
            
            # 格式化结果
            search_results = []
            for doc, score in results:
                if score <= similarity_threshold:
                    search_results.append({
                        "document_id": doc.metadata.get("document_id"),
                        "filename": doc.metadata.get("filename"),
                        "content": doc.page_content,
                        "chunk_index": doc.metadata.get("chunk_index"),
                        "category": doc.metadata.get("category"),
                        "similarity": float(score),
                        "metadata": doc.metadata
                    })
            
            return search_results
            
        except Exception as e:
            logger.error(f"语义搜索时出错: {e}")
            # 回退到文本搜索
            return await self._fallback_text_search(query, user_id, limit, knowledge_base_id, category)

    async def _fallback_text_search(
        self,
        query: str,
        user_id: UUID,
        limit: int,
        knowledge_base_id: Optional[UUID] = None,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """使用 Document.extracted_content 的 ILIKE 包含查询作为回退。

        返回的是文档级前 500 字预览，并赋固定相似度 0.5，不等同于向量块得分；查询异常
        被记录后返回空列表，不再向上抛出。
        """
        try:
            base_query = select(Document).where(Document.user_id == user_id)
            
            if knowledge_base_id:
                base_query = base_query.where(Document.knowledge_base_id == knowledge_base_id)
            
            if category:
                base_query = base_query.where(Document.category == category)
            
            # 简单文本搜索
            text_query = base_query.where(
                Document.extracted_content.ilike(f"%{query}%")
            ).limit(limit)
            
            result = await self.db.execute(text_query)
            documents = result.scalars().all()
            
            # 格式化结果
            search_results = []
            for doc in documents:
                search_results.append({
                    "document_id": str(doc.id),
                    "filename": doc.filename,
                    "content": doc.extracted_content[:500] if doc.extracted_content else "",
                    "category": doc.category,
                    "similarity": 0.5,  # 文本搜索的默认相似度
                    "created_at": doc.created_at.isoformat()
                })
            
            return search_results
            
        except Exception as e:
            logger.error(f"回退文本搜索时出错: {e}")
            return []

    # ---------- 查询与删除 ----------

    async def get_user_documents(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 100,
        category: Optional[str] = None,
        knowledge_base_id: Optional[UUID] = None
    ) -> List[Document]:
        """读取当前用户可见的文档，并在同一查询中应用分类、知识库和分页条件。

        ``user_id`` 是强制隔离条件，调用方无法通过可选过滤器扩大到其他用户的数据；本方法
        只返回 ORM 实体，不提交事务，也不加载或生成向量块。
        """
        try:
            # 先固定租户边界，再叠加业务过滤，避免遗漏用户隔离条件。
            query = select(Document).where(Document.user_id == user_id)
            
            if category:
                query = query.where(Document.category == category)
            
            if knowledge_base_id:
                query = query.where(Document.knowledge_base_id == knowledge_base_id)
            
            # 分页在数据库执行，并以创建时间倒序保证新上传文档优先返回。
            query = query.offset(skip).limit(limit).order_by(desc(Document.created_at))
            
            result = await self.db.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            logger.error(f"获取用户文档时出错: {e}")
            raise

    async def get_by_id(self, document_id: str, user_id: Optional[UUID] = None) -> Optional[Document]:
        """把字符串 ID 转为 UUID 并读取文档，可选地把用户归属并入 SQL 条件。

        需要权限隔离的调用必须传 ``user_id``；省略它时这是内部管理式查询，不会验证资源
        所有者。无效 ID、数据库异常和未找到都统一返回 ``None``，调用方需据此决定 404 或
        权限错误，不能把本方法的返回值直接解释为某一种失败原因。
        """
        try:
            # UUID 转换在查询前完成，非法外部输入不会进入数据库表达式。
            doc_uuid = UUID(document_id)
            
            # 传入 user_id 时，资源存在性与归属在同一条 SQL 中判断，避免先查后验的窗口。
            query = select(Document).where(Document.id == doc_uuid)
            if user_id:
                query = query.where(Document.user_id == user_id)
            
            result = await self.db.execute(query)
            document = result.scalar_one_or_none()
            
            # 添加日志记录文档状态
            if document:
                content_length = len(document.extracted_content) if document.extracted_content else 0
                logger.info(f"找到文档 {document_id}，内容长度: {content_length}")
            else:
                if user_id:
                    logger.warning(f"为用户 {user_id} 未找到文档 {document_id}")
                else:
                    logger.warning(f"未找到文档 {document_id}")

            return document

        except (ValueError, TypeError) as e:
            logger.error(f"无效的文档ID格式 {document_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"获取文档 {document_id} 时出错: {e}")
            return None

    async def get_document_chunks(self, document_id: str, user_id: UUID) -> List[Dict[str, Any]]:
        """校验文档归属后，从 LangChain 向量表读取用于预览的原始块。

        这里不执行相似度检索，而是按 ``chunk_index`` 返回指定文档的全部块。ORM 文档归属是
        可信权限边界，向量元数据中的 ``document_id`` 仅在通过该校验后用于筛选块。
        """
        try:
            # 先确认业务记录存在；随后用认证用户 ID 显式比较所有者。
            document = await self.get_by_id(document_id)
            if not document:
                raise ValueError("文档未找到")
            
            if document.user_id != user_id:
                raise ValueError("权限被拒绝")
            
            # LangChain 未映射为本项目 ORM 模型，因此使用参数化 SQL 读取 JSON 元数据。
            query = text("""
                SELECT 
                    id,
                    document,
                    cmetadata
                FROM langchain_pg_embedding 
                WHERE cmetadata->>'document_id' = :document_id
                ORDER BY CAST(cmetadata->>'chunk_index' AS INTEGER)
            """)
            
            result = await self.db.execute(query, {"document_id": str(document_id)})
            rows = result.fetchall()
            
            # 格式化块用于响应
            formatted_chunks = []
            for row in rows:
                metadata = row.cmetadata if row.cmetadata else {}
                formatted_chunks.append({
                    "id": str(row.id),
                    "content": row.document,
                    "chunk_index": metadata.get("chunk_index", 0),
                    "chunk_size": metadata.get("chunk_size", len(row.document) if row.document else 0),
                    "metadata": metadata
                })
            return formatted_chunks
            
        except Exception as e:
            logger.error(f"获取文档块时出错: {e}")
            raise

    async def delete(self, document: Document) -> None:
        """先在一个数据库事务中删除向量块和 Document，再清理磁盘文件。

        数据库提交完成后才调用 ``os.unlink``；若文件删除失败，已提交的记录和块删除不会因
        随后的 ``rollback`` 恢复，磁盘上可能保留孤立文件。
        """
        try:
            # 从langchain_pg_embedding表删除文档块
            delete_query = text("""
                DELETE FROM langchain_pg_embedding 
                WHERE cmetadata->>'document_id' = :document_id
            """)
            await self.db.execute(delete_query, {"document_id": str(document.id)})
            
            # 删除文档
            await self.db.delete(document)
            await self.db.commit()
            
            # 如果文件存在则清理
            if document.file_path and os.path.exists(document.file_path):
                os.unlink(document.file_path)
                
            logger.info(f"已删除文档 {document.id} 及其嵌入向量")
            
        except Exception as e:
            logger.error(f"删除文档时出错: {e}")
            await self.db.rollback()
            raise

    async def _get_document_by_hash(
        self,
        file_hash: str,
        user_id: UUID
    ) -> Optional[Document]:
        """在当前用户范围内按内容哈希查重。

        同一份文件允许由不同用户分别保存；因此哈希不能单独作为全局资源标识，必须与
        ``user_id`` 组合查询。
        """
        query = select(Document).where(
            Document.file_hash == file_hash,
            Document.user_id == user_id
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    def _get_mime_type(self, filename: str) -> str:
        """依据文件扩展名生成持久化 MIME，未知格式退回二进制流类型。

        该值用于选择后续文本提取器，不是读取文件头得到的安全校验结果。
        """
        extension = filename.lower().split('.')[-1] if '.' in filename else ''
        mime_types = {
            'pdf': 'application/pdf',
            'doc': 'application/msword',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'txt': 'text/plain',
            'md': 'text/markdown',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'xls': 'application/vnd.ms-excel'
        }
        return mime_types.get(extension, 'application/octet-stream')

    async def _save_temp_file(self, content: bytes, filename: str) -> str:
        """把字节写到系统临时目录，返回供路径型解析器使用的文件名。

        此方法只创建文件，不负责删除；当前调用方 ``process_document`` 在 ``finally`` 中承担
        清理责任。文件名沿用上传名，因此上层应确保传入的是已净化的基础文件名。
        """
        temp_dir = tempfile.gettempdir()
        temp_file_path = os.path.join(temp_dir, f"temp_{filename}")
        
        with open(temp_file_path, 'wb') as f:
            f.write(content)
        
        return temp_file_path

    async def _save_file(self, content: bytes, filename: str, user_id: UUID) -> str:
        """将原始字节持久化到用户目录，并通过数字后缀避免覆盖同名文件。

        文件系统写入不属于 SQLAlchemy 事务；调用方数据库提交失败时，该路径不会自动回滚。
        ``user_id`` 只用于目录隔离，文件名安全性仍依赖上游上传校验。
        """
        # 每个认证用户拥有独立目录，降低不同用户同名文件之间的冲突。
        upload_dir = os.path.join(settings.UPLOAD_DIR, str(user_id))
        os.makedirs(upload_dir, exist_ok=True)
        
        # 不覆盖既有文件；依次尝试 filename、filename_1、filename_2 等可用路径。
        file_path = os.path.join(upload_dir, filename)
        counter = 1
        base_name, extension = os.path.splitext(filename)
        
        while os.path.exists(file_path):
            new_filename = f"{base_name}_{counter}{extension}"
            file_path = os.path.join(upload_dir, new_filename)
            counter += 1
        
        # 保存文件
        with open(file_path, 'wb') as f:
            f.write(content)
        
        return file_path

    
