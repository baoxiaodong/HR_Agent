"""
文档上传、更新、查询、检索结果和分块的 Schema。

输入模型只接收用户可设置的分类、标签和知识库字段；数据库/公开模型额外包含文件元数据、
提取文本和向量。字段校验器会把 PostgreSQL/NumPy 返回的向量数组转换为普通列表，使其
能够被 JSON 序列化。
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
import numpy as np
from pydantic import BaseModel, Field, field_validator


class DocumentBase(BaseModel):
    """创建、数据库模型和公开响应共同拥有的文档描述字段。"""

    filename: str = Field(..., min_length=1, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    meta_data: Optional[Dict[str, Any]] = None


class DocumentCreate(DocumentBase):
    """服务层创建文档记录时使用；可选地把文档归入一个知识库。"""

    knowledge_base_id: Optional[UUID] = None


class DocumentUpdate(BaseModel):
    """文档可修改字段；全部可选，服务层只更新请求中实际出现的值。"""

    filename: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    knowledge_base_id: Optional[UUID] = None
    meta_data: Optional[Dict[str, Any]] = None


class DocumentInDB(DocumentBase):
    """数据库读取后的完整文档投影，可由 SQLAlchemy ORM 对象直接转换。"""

    # 资源归属与关联关系
    id: UUID
    user_id: UUID
    knowledge_base_id: Optional[UUID]

    # 文件存储和内容处理结果
    file_path: str
    file_size: int
    file_hash: str
    mime_type: str
    extracted_content: Optional[str]
    embedding: Optional[List[float]] = None

    # 审计时间
    created_at: datetime
    updated_at: datetime

    @field_validator('embedding', mode='before')
    @classmethod
    def convert_embedding(cls, v):
        """把数据库驱动返回的 NumPy 向量转换为 JSON 可序列化的 Python 列表。"""
        if v is None:
            return None
        if isinstance(v, np.ndarray):
            return v.tolist()
        return v

    class Config:
        # 允许 Pydantic 直接读取 SQLAlchemy 对象属性，而不要求端点先转成字典。
        from_attributes = True


class Document(DocumentInDB):
    """文档详情和列表接口对外返回的最终结构。"""
    pass


class DocumentUpload(BaseModel):
    """multipart 文件本体之外的可选上传表单字段。"""
    knowledge_base_id: Optional[UUID] = None
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None


class DocumentSearch(BaseModel):
    """文档检索条件；limit 限制一次最多返回 50 条候选。"""
    query: str = Field(..., min_length=1, max_length=500)
    knowledge_base_id: Optional[UUID] = None
    category: Optional[str] = None
    limit: Optional[int] = Field(10, ge=1, le=50)


class DocumentSearchResult(BaseModel):
    """检索命中的文档摘要；relevance_score 越高表示与查询越相关。"""
    id: str
    filename: str
    content: str
    category: Optional[str]
    tags: List[str]
    created_at: str
    relevance_score: Optional[float] = None


class DocumentChunkBase(BaseModel):
    """切分后文本块的内容、原文顺序和字符大小。"""
    content: str
    chunk_index: int
    chunk_size: int
    meta_data: Optional[Dict[str, Any]] = None


class DocumentChunkInDB(DocumentChunkBase):
    """数据库中的文本块，额外关联原文档并保存检索向量。"""
    id: UUID
    document_id: UUID
    embedding: Optional[List[float]] = None
    created_at: datetime
    updated_at: datetime

    @field_validator('embedding', mode='before')
    @classmethod
    def convert_embedding(cls, v):
        """把数据库驱动返回的 NumPy 向量转换为 JSON 可序列化列表。"""
        if v is None:
            return None
        if isinstance(v, np.ndarray):
            return v.tolist()
        return v

    class Config:
        from_attributes = True


class DocumentChunk(DocumentChunkInDB):
    """文档分块对外响应结构。"""
    pass