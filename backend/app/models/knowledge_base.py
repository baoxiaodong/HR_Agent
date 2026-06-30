"""
用于组织文档和信息的知识库模型
"""
from sqlalchemy import Column, String, Text, JSON, Integer, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class KnowledgeBase(BaseModel):
    """用于组织文档的知识库模型"""

    __tablename__ = "knowledge_bases"

    # 基本信息
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)

    # 配置
    is_public = Column(Boolean, default=False, nullable=False)
    is_searchable = Column(Boolean, default=True, nullable=False)

    # 元数据
    meta_data = Column(JSON, nullable=True)
    document_count = Column(Integer, default=0, nullable=False)

    # 分类
    category = Column(String(100), nullable=True)  # 例如："HR 政策", "企业手册"
    tags = Column(JSON, nullable=True)  # 标签列表

    # 关系
    documents = relationship("Document", back_populates="knowledge_base")

    def __repr__(self):
        return f"<KnowledgeBase(name='{self.name}', category='{self.category}')>"

class FAQ(BaseModel):
    """常见问题模型"""

    __tablename__ = "faqs"

    # 内容
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)

    # 分类
    category = Column(String(100), nullable=True)
    tags = Column(JSON, nullable=True)

    # 元数据
    view_count = Column(Integer, default=0, nullable=False)
    helpful_count = Column(Integer, default=0, nullable=False)
    not_helpful_count = Column(Integer, default=0, nullable=False)

    # 关系
    knowledge_base_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=True)
    knowledge_base = relationship("KnowledgeBase")

    def __repr__(self):
        return f"<FAQ(question='{self.question[:50]}...', category='{self.category}')>"