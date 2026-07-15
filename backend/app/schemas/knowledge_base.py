"""
知识库、FAQ、检索和反馈的 API Schema。

Create/Update 模型限制客户端可写字段，InDB/公开模型补充主键、计数和时间。搜索响应把
知识库摘要、命中文档和 FAQ 分组返回；反馈请求只携带是否有帮助，由服务层更新计数。
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from pydantic import BaseModel, Field


class KnowledgeBaseBase(BaseModel):
    """知识库创建输入与公开响应共用的描述和检索配置。"""

    # 展示信息
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)

    # 访问和检索开关
    is_public: bool = False
    is_searchable: bool = True

    # 分类与扩展信息
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    meta_data: Optional[Dict[str, Any]] = None


class KnowledgeBaseCreate(KnowledgeBaseBase):
    """创建知识库时允许客户端提交的完整字段集合。"""
    pass


class KnowledgeBaseUpdate(BaseModel):
    """知识库局部更新输入；未出现的字段由服务层保留原值。"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    is_public: Optional[bool] = None
    is_searchable: Optional[bool] = None
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    meta_data: Optional[Dict[str, Any]] = None


class KnowledgeBaseInDB(KnowledgeBaseBase):
    """数据库返回的知识库，增加主键、实时文档计数和审计时间。"""
    id: UUID
    document_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        # 允许直接从 SQLAlchemy KnowledgeBase 对象读取字段。
        from_attributes = True


class KnowledgeBase(KnowledgeBaseInDB):
    """知识库列表和详情接口的最终公开响应。"""
    pass


class KnowledgeBaseSearch(BaseModel):
    """知识库内检索文本及最多返回条数。"""
    query: str = Field(..., min_length=1, max_length=500)
    limit: Optional[int] = Field(10, ge=1, le=50)


class KnowledgeBaseSearchResult(BaseModel):
    """把知识库摘要、文档命中和 FAQ 命中分组返回。"""
    knowledge_base: Dict[str, Any]
    documents: List[Dict[str, Any]]
    faqs: List[Dict[str, Any]]


class FAQBase(BaseModel):
    """FAQ 问题、答案、分类和标签的公共字段。"""
    question: str = Field(..., min_length=1, max_length=500)
    answer: str = Field(..., min_length=1, max_length=2000)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class FAQCreate(FAQBase):
    """FAQ 创建输入，可选择归入某个知识库。"""
    knowledge_base_id: Optional[UUID] = None


class FAQUpdate(BaseModel):
    """FAQ 局部更新输入，未提交字段由服务层保留。"""
    question: Optional[str] = Field(None, min_length=1, max_length=500)
    answer: Optional[str] = Field(None, min_length=1, max_length=2000)
    category: Optional[str] = Field(None, max_length=100)
    tags: Optional[List[str]] = None
    knowledge_base_id: Optional[UUID] = None
    metadata: Optional[Dict[str, Any]] = None


class FAQInDB(FAQBase):
    """已持久化 FAQ 的完整投影，补充主键、反馈计数和审计时间。"""
    id: UUID
    knowledge_base_id: Optional[UUID]
    view_count: int
    helpful_count: int
    not_helpful_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FAQ(FAQInDB):
    """FAQ 详情与列表接口的公开响应。"""
    pass


class FAQFeedback(BaseModel):
    """记录一次“有帮助/无帮助”反馈的请求体。"""
    is_helpful: bool
