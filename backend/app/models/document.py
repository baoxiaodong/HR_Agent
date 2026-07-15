"""
上传文档及其检索元数据的数据库模型。

一条记录连接磁盘文件、提取后的纯文本、可选文档级向量、上传用户和知识库。用于 RAG 的
分块向量另存于 LangChain PGVector 表，不由本模型关系管理；删除服务需要同时清理两处
数据和磁盘文件。
"""
from sqlalchemy import Column, String, Text, Integer, ForeignKey, JSON, LargeBinary
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

from app.models.base import BaseModel
from app.core.config import settings


class Document(BaseModel):
    """用于存储文件及其元数据的文档模型"""

    __tablename__ = "documents"

    # 基本信息
    filename = Column(String(255), nullable=False)
    original_filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=False)
    file_hash = Column(String(64), nullable=False)  # SHA256哈希
    mime_type = Column(String(100), nullable=False)

    # 内容和元数据
    extracted_content = Column(Text, nullable=True)  # 提取的文本内容
    meta_data = Column(JSON, nullable=True)  # 附加元数据

    # 用于语义搜索的向量嵌入
    embedding = Column(Vector(settings.VECTOR_DIMENSION), nullable=True)

    # 分类
    category = Column(String(100), nullable=True)  # 例如："policy", "handbook", "form"
    tags = Column(JSON, nullable=True)  # 标签列表

    # 关系
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    uploaded_by = relationship("User", back_populates="documents")

    knowledge_base_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=True)
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")

    def __repr__(self):
        return f"<Document at {hex(id(self))}>"


